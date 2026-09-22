"""Role Qualification service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy, and how it differs from Task 13's.**

This service is the first that legitimately needs two things Task 13's
Ministry Authority tests deliberately forbid on their shared ``RecordingSession``:
a ``flush()`` (to obtain a new :class:`RoleQualification`'s identity before
building its audit row) and a database lookup (``session.execute(select(...))``,
to find any existing decision for a membership/role pair). Neither can be
exercised against a real, unbound :class:`~sqlalchemy.orm.Session` -- flush
would try to issue real SQL and raise ``UnboundExecutionError``, and so would
execute. Reaching for a bound engine to make them "just work" was considered and
rejected: SQLite cannot honestly stand in for PostgreSQL here either (see the
warnings this module's own docstring inherits from Task 13), and standing up
real PostgreSQL is out of scope for this offline suite.

The chosen strategy keeps both concerns real without a database:

- **The lookup** (:func:`app.services.role_qualification._find_existing_qualification`)
  is exercised through ``monkeypatch`` in the orchestration tests below, so the
  service's *branching* on "found" vs. "not found" is tested against controlled
  values. The lookup's own SQL is tested separately and directly, with **no
  session or database at all** -- ``_qualification_lookup_statement`` returns a
  plain SQLAlchemy ``Select`` that can be compiled and inspected on its own; see
  ``test_the_lookup_statement_filters_on_the_integrity_columns``. Between the
  two, both "does the service decide correctly" and "does the query actually
  filter on the right columns" are proven, without pretending either half alone
  proves both.
- **The flush** uses a real, unbound Session (:class:`RoleQualificationSession`
  below) whose ``flush()`` is overridden to do the one thing this service
  actually depends on a flush for -- assigning an identity to a pending new row
  -- without touching a database. ``commit()`` and ``rollback()`` remain
  forbidden, exactly as in Task 13.

What this **cannot** verify, same honest limitation as Task 13: that PostgreSQL
actually assigns identities and rolls back a real transaction. What it verifies
instead is the same shape of property Task 13 settled for -- the service never
commits or rolls back, flushes only where the design says it must, and the
mutation and its audit row share one session -- plus, now, that the *query* the
service issues is the one the model's own integrity columns call for.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person, RoleQualification
from app.services import (
    AuthorizationError,
    InvalidOperationError,
    MembershipQualification,
    RoleQualifications,
    list_role_qualifications,
    set_role_qualification,
)
from app.services.role_qualification import (
    ACTION_QUALIFICATION_APPROVED,
    ACTION_QUALIFICATION_REVOKED,
    _qualification_lookup_statement,
    _role_qualifications_statement,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class RoleQualificationSession(Session):
    """A real, unbound Session for these tests.

    Unlike Task 13's ``RecordingSession`` -- which forbids flush outright,
    correctly, since Ministry Authority never needs one -- this service
    legitimately flushes once when it creates a brand-new
    :class:`RoleQualification`. ``flush()`` is therefore not forbidden here: it
    is counted, and it simulates identity assignment the way a real flush
    against PostgreSQL would, without a database. ``commit()`` and
    ``rollback()`` remain forbidden -- the transaction still belongs to the
    caller.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        """Assign an id to any pending new row that does not already have one.

        Mirrors what a real flush against PostgreSQL would accomplish for this
        service's purposes -- a usable primary key on the just-added row --
        without issuing any SQL. Idempotent, like a real flush: an object that
        already has an id is left alone.
        """
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> RoleQualificationSession:
    return RoleQualificationSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _membership(
    membership_id: int,
    *,
    person: Person,
    ministry: Ministry,
    is_head: bool = False,
    deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id,
        ministry_id=ministry.id,
        is_ministry_head=is_head,
    )
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    person.ministry_memberships.append(membership)
    return membership


def _role(role_id: int, name: str, *, ministry: Ministry, deactivated: bool = False) -> MinistryRole:
    role = MinistryRole(name=name, ministry_id=ministry.id)
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return role


def _existing_qualification(
    qualification_id: int,
    *,
    membership: MinistryMembership,
    role: MinistryRole,
    is_qualified: bool,
    decided_by: Person,
    decided_at: datetime.datetime | None = None,
) -> RoleQualification:
    qualification = RoleQualification(
        ministry_membership_id=membership.id,
        ministry_role_id=role.id,
        ministry_id=membership.ministry_id,
        is_qualified=is_qualified,
        decided_at=decided_at
        or datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
        decided_by_person_id=decided_by.id,
    )
    qualification.id = qualification_id
    qualification.membership = membership
    qualification.role = role
    return qualification


@pytest.fixture
def head(setup_ministry: Ministry) -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _membership(9001, person=person, ministry=setup_ministry, is_head=True)
    return person


@pytest.fixture
def setup_ministry() -> Ministry:
    return _ministry(3, "Setup")


@pytest.fixture
def setup_lead(setup_ministry: Ministry) -> MinistryRole:
    return _role(12, "Setup Lead", ministry=setup_ministry)


@pytest.fixture
def john_membership(setup_ministry: Ministry) -> MinistryMembership:
    return _membership(118, person=_person(42, "John"), ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_lookup(monkeypatch, existing: RoleQualification | None) -> None:
    """Replace the DB lookup with a controlled value (see module docstring)."""
    import app.services.role_qualification as module

    monkeypatch.setattr(
        module, "_find_existing_qualification", lambda session, membership, role: existing
    )


# --------------------------------------------------------------------------
# 1 — Admin approves
# --------------------------------------------------------------------------


def test_admin_can_approve_qualification(session, head, john_membership, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert result.is_qualified is True
    assert result.ministry_membership_id == 118
    assert result.ministry_role_id == 12
    assert result.ministry_id == 3
    assert result.decided_by_person_id == 1
    assert result.id == 900  # assigned by the flush


def test_approve_records_a_correct_audit_row(session, head, john_membership, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_QUALIFICATION_APPROVED
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "role_qualification"
    assert audit.target_id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Approved John for Setup Lead in Setup"
    assert audit.before_values == {"is_qualified": None}
    assert audit.after_values == {"is_qualified": True}
    assert audit.reason is None


# --------------------------------------------------------------------------
# 2 — Admin explicitly marks not qualified (first assessment)
# --------------------------------------------------------------------------


def test_admin_can_explicitly_mark_not_qualified(
    session, head, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    assert result.is_qualified is False
    audit = _one_audit_row(session)
    assert audit.action == ACTION_QUALIFICATION_REVOKED
    assert audit.summary == "Marked John as not qualified for Setup Lead in Setup"
    assert audit.before_values == {"is_qualified": None}
    assert audit.after_values == {"is_qualified": False}


# --------------------------------------------------------------------------
# 3 & 4 — Ministry Head authorization, scoped to their own ministry
# --------------------------------------------------------------------------


def test_ministry_head_can_manage_qualification_in_their_own_ministry(
    session, setup_ministry, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    head_person = _person(9, "Head Person")
    _membership(200, person=head_person, ministry=setup_ministry, is_head=True)

    result = set_role_qualification(
        session, actor=head_person, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert result.is_qualified is True
    assert _one_audit_row(session).actor_person_id == 9


def test_ministry_head_cannot_manage_qualification_in_another_ministry(
    session, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_head_person = _person(9, "AV Head")
    _membership(201, person=av_head_person, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_role_qualification(
            session, actor=av_head_person, membership=john_membership,
            role=setup_lead, is_qualified=True,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 5 — Normal user cannot decide
# --------------------------------------------------------------------------


def test_normal_user_cannot_decide_qualification(
    session, setup_ministry, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    ordinary_person = _person(7, "Ada")
    _membership(202, person=ordinary_person, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        set_role_qualification(
            session, actor=ordinary_person, membership=john_membership,
            role=setup_lead, is_qualified=True,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 6 — Deactivated actor
# --------------------------------------------------------------------------


def test_deactivated_admin_cannot_decide(session, john_membership, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        set_role_qualification(
            session, actor=departed_admin, membership=john_membership,
            role=setup_lead, is_qualified=True,
        )

    assert _audit_rows(session) == []


def test_deactivated_ministry_head_cannot_decide(
    session, setup_ministry, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    departed_head = _person(9, "Departed Head", deactivated=True)
    _membership(200, person=departed_head, ministry=setup_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_role_qualification(
            session, actor=departed_head, membership=john_membership,
            role=setup_lead, is_qualified=True,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 7 — Cross-ministry membership/role rejected before mutation
# --------------------------------------------------------------------------


def test_membership_and_role_from_different_ministries_rejected(
    session, head, john_membership, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_role = _role(20, "Sound", ministry=av_ministry)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=john_membership, role=av_role,
            is_qualified=True,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 8, 9, 10 — Cannot newly approve against a deactivated target
# --------------------------------------------------------------------------


def test_cannot_newly_approve_a_deactivated_membership(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    inactive_membership = _membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=inactive_membership, role=setup_lead,
            is_qualified=True,
        )

    assert _audit_rows(session) == []


def test_cannot_newly_approve_a_globally_deactivated_person(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    """Active membership, but the person behind it has left the church."""
    _stub_lookup(monkeypatch, None)
    departed = _person(42, "John", deactivated=True)
    membership = _membership(118, person=departed, ministry=setup_ministry)
    assert membership.deactivated_at is None

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=membership, role=setup_lead,
            is_qualified=True,
        )

    assert _audit_rows(session) == []


def test_cannot_newly_approve_a_deactivated_role(
    session, head, setup_ministry, john_membership, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=john_membership, role=inactive_role,
            is_qualified=True,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 11 — Existing True may become False for state repair, even if deactivated
# --------------------------------------------------------------------------


def test_existing_true_may_be_revoked_despite_deactivated_membership(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    inactive_membership = _membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )
    existing = _existing_qualification(
        501, membership=inactive_membership, role=setup_lead,
        is_qualified=True, decided_by=head,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=inactive_membership, role=setup_lead,
        is_qualified=False,
    )

    assert result is existing
    assert result.is_qualified is False
    audit = _one_audit_row(session)
    assert audit.action == ACTION_QUALIFICATION_REVOKED
    assert audit.target_id == 501
    assert audit.before_values == {"is_qualified": True}
    assert audit.after_values == {"is_qualified": False}


def test_existing_true_may_be_revoked_despite_deactivated_person(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    departed = _person(42, "John", deactivated=True)
    membership = _membership(118, person=departed, ministry=setup_ministry)
    existing = _existing_qualification(
        501, membership=membership, role=setup_lead, is_qualified=True, decided_by=head
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=membership, role=setup_lead,
        is_qualified=False,
    )

    assert result.is_qualified is False


def test_existing_true_may_be_revoked_despite_deactivated_role(
    session, head, setup_ministry, monkeypatch
):
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    membership = _membership(118, person=_person(42, "John"), ministry=setup_ministry)
    existing = _existing_qualification(
        501, membership=membership, role=inactive_role, is_qualified=True, decided_by=head
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=membership, role=inactive_role,
        is_qualified=False,
    )

    assert result.is_qualified is False


# --------------------------------------------------------------------------
# The documented asymmetry: a brand-new False against an inactive target
# is NOT given the same exemption as revoking an existing True.
# --------------------------------------------------------------------------


def test_a_brand_new_false_assessment_is_rejected_for_a_deactivated_membership(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    """The preferred rule from the Task 14 brief, implemented as written: a
    first-ever decision requires an active target, whichever way it decides."""
    _stub_lookup(monkeypatch, None)
    inactive_membership = _membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=inactive_membership, role=setup_lead,
            is_qualified=False,
        )

    assert _audit_rows(session) == []


def test_turning_an_existing_false_into_true_requires_an_active_target(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    """False -> True is a fresh grant, not state repair, so it is not exempt."""
    inactive_membership = _membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )
    existing = _existing_qualification(
        501, membership=inactive_membership, role=setup_lead,
        is_qualified=False, decided_by=head,
    )
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=inactive_membership, role=setup_lead,
            is_qualified=True,
        )

    assert _audit_rows(session) == []
    assert existing.is_qualified is False  # untouched


# --------------------------------------------------------------------------
# 12 — No row + False creates real state, not a no-op
# --------------------------------------------------------------------------


def test_no_row_plus_false_creates_an_explicit_row_not_a_no_op(
    session, head, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    assert isinstance(result, RoleQualification)
    assert result.is_qualified is False
    assert result.decided_by_person_id == 1
    assert len(_audit_rows(session)) == 1
    assert session.flush_calls == 1


# --------------------------------------------------------------------------
# Task 14.1 correction: a repeated True request must not silently confirm
# success once the target it names has since been deactivated -- mirrors the
# Task 13.1 correction already applied to grant_ministry_head. Each of these
# checks that the existing row, its decision metadata, and the audit trail are
# all completely untouched by the rejected call.
# --------------------------------------------------------------------------


def test_existing_true_plus_true_is_rejected_for_a_deactivated_person(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    departed = _person(42, "John", deactivated=True)
    membership = _membership(118, person=departed, ministry=setup_ministry)
    assert membership.deactivated_at is None, "membership itself must be active"
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=membership, role=setup_lead, is_qualified=True,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=membership, role=setup_lead,
            is_qualified=True,
        )

    assert existing.is_qualified is True
    assert existing.decided_at == decided_at
    assert existing.decided_by_person_id == 2
    assert _audit_rows(session) == []


def test_existing_true_plus_true_is_rejected_for_a_deactivated_membership(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    inactive_membership = _membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=inactive_membership, role=setup_lead, is_qualified=True,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=inactive_membership, role=setup_lead,
            is_qualified=True,
        )

    assert existing.is_qualified is True
    assert existing.decided_at == decided_at
    assert existing.decided_by_person_id == 2
    assert _audit_rows(session) == []


def test_existing_true_plus_true_is_rejected_for_a_deactivated_role(
    session, head, setup_ministry, john_membership, monkeypatch
):
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=john_membership, role=inactive_role, is_qualified=True,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=john_membership, role=inactive_role,
            is_qualified=True,
        )

    assert existing.is_qualified is True
    assert existing.decided_at == decided_at
    assert existing.decided_by_person_id == 2
    assert _audit_rows(session) == []


def test_existing_false_plus_false_remains_idempotent_despite_deactivation(
    session, head, setup_ministry, monkeypatch
):
    """The negative/state-repair side is unaffected by this correction: an
    existing False may always be re-confirmed, active target or not (§9 of the
    Task 14.1 correction report)."""
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    inactive_membership = _membership(
        118,
        person=_person(42, "John", deactivated=True),
        ministry=setup_ministry,
        deactivated=True,
    )
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=inactive_membership, role=inactive_role, is_qualified=False,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=inactive_membership, role=inactive_role,
        is_qualified=False,
    )

    assert result is existing
    assert result.decided_at == decided_at
    assert result.decided_by_person_id == 2
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_true_to_false_state_repair_still_works_after_the_correction(
    session, head, setup_ministry, monkeypatch
):
    """Regression check: state repair (existing True -> requested False) must
    remain permissive even with membership, person and role all deactivated at
    once -- the correction changed only the True-request path.
    """
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    inactive_membership = _membership(
        118,
        person=_person(42, "John", deactivated=True),
        ministry=setup_ministry,
        deactivated=True,
    )
    existing = _existing_qualification(
        501, membership=inactive_membership, role=inactive_role, is_qualified=True,
        decided_by=head,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=inactive_membership, role=inactive_role,
        is_qualified=False,
    )

    assert result.is_qualified is False
    audit = _one_audit_row(session)
    assert audit.action == ACTION_QUALIFICATION_REVOKED
    assert audit.before_values == {"is_qualified": True}
    assert audit.after_values == {"is_qualified": False}


# --------------------------------------------------------------------------
# 13 & 14 — Idempotency
# --------------------------------------------------------------------------


def test_existing_true_plus_true_is_idempotent(
    session, head, john_membership, setup_lead, monkeypatch
):
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=True,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert result is existing
    assert result.decided_at == decided_at
    assert result.decided_by_person_id == 2  # unchanged -- not overwritten by head (1)
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_existing_false_plus_false_is_idempotent(
    session, head, john_membership, setup_lead, monkeypatch
):
    decided_at = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=False,
        decided_by=other_person, decided_at=decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    assert result is existing
    assert result.decided_at == decided_at
    assert result.decided_by_person_id == 2
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 15 & 16 — Real transitions update decision metadata and audit
# --------------------------------------------------------------------------


def test_false_to_true_updates_decision_metadata_and_audits(
    session, head, john_membership, setup_lead, monkeypatch
):
    old_decided_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=False,
        decided_by=other_person, decided_at=old_decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert result is existing
    assert result.is_qualified is True
    assert result.decided_by_person_id == 1  # now the acting head
    assert result.decided_at > old_decided_at

    audit = _one_audit_row(session)
    assert audit.action == ACTION_QUALIFICATION_APPROVED
    assert audit.target_id == 501
    assert audit.before_values == {"is_qualified": False}
    assert audit.after_values == {"is_qualified": True}
    assert session.flush_calls == 0  # the row already had an id


def test_true_to_false_updates_decision_metadata_and_audits(
    session, head, john_membership, setup_lead, monkeypatch
):
    old_decided_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    other_person = _person(2, "Other Admin", is_admin=True)
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=True,
        decided_by=other_person, decided_at=old_decided_at,
    )
    _stub_lookup(monkeypatch, existing)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False, reason="Stepped back from lead duties.",
    )

    assert result.is_qualified is False
    assert result.decided_by_person_id == 1
    assert result.decided_at > old_decided_at

    audit = _one_audit_row(session)
    assert audit.action == ACTION_QUALIFICATION_REVOKED
    assert audit.before_values == {"is_qualified": True}
    assert audit.after_values == {"is_qualified": False}
    assert audit.reason == "Stepped back from lead duties."
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 17 & 18 — Flush and transaction discipline
# --------------------------------------------------------------------------


def test_new_qualification_flushes_exactly_once(
    session, head, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert session.flush_calls == 1
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_service_never_commits_or_rolls_back_across_every_branch(
    session, head, setup_ministry, john_membership, setup_lead, monkeypatch
):
    # New row.
    _stub_lookup(monkeypatch, None)
    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    # Update.
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=True,
        decided_by=head,
    )
    _stub_lookup(monkeypatch, existing)
    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    # Idempotent no-op.
    existing.is_qualified = False
    _stub_lookup(monkeypatch, existing)
    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_idempotent_path_does_not_flush(
    session, head, john_membership, setup_lead, monkeypatch
):
    existing = _existing_qualification(
        501, membership=john_membership, role=setup_lead, is_qualified=True,
        decided_by=head,
    )
    _stub_lookup(monkeypatch, existing)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert session.flush_calls == 0


def test_rejected_calls_never_flush(session, head, john_membership, monkeypatch):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_role = _role(20, "Sound", ministry=av_ministry)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=john_membership, role=av_role,
            is_qualified=True,
        )

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 19 & 20 — Audit target and ministry come from the real target
# --------------------------------------------------------------------------


def test_audit_target_is_the_actual_qualification_id(
    session, head, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    result = set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    audit = _one_audit_row(session)
    assert audit.target_id == result.id
    assert audit.target_id == 900


def test_audit_ministry_id_comes_from_the_actual_target_ministry(
    session, head, john_membership, setup_lead, monkeypatch
):
    """membership.ministry_id, not a caller-supplied value -- there is no
    ministry_id parameter on the public function at all."""
    _stub_lookup(monkeypatch, None)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert _one_audit_row(session).ministry_id == john_membership.ministry_id == 3


# --------------------------------------------------------------------------
# 21 — Never-assessed audit before value is null, not false
# --------------------------------------------------------------------------


def test_audit_before_value_is_null_for_never_assessed(
    session, head, john_membership, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=False,
    )

    audit = _one_audit_row(session)
    assert audit.before_values == {"is_qualified": None}
    assert audit.before_values["is_qualified"] is not False  # explicit, not collapsed


# --------------------------------------------------------------------------
# 22 — Whitespace-only reason rejected before mutation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_reason_rejected_before_mutation(
    session, head, john_membership, setup_lead, monkeypatch, blank
):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_role_qualification(
            session, actor=head, membership=john_membership, role=setup_lead,
            is_qualified=True, reason=blank,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_a_reason_is_optional(session, head, john_membership, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_role_qualification(
        session, actor=head, membership=john_membership, role=setup_lead,
        is_qualified=True,
    )

    assert _one_audit_row(session).reason is None


# --------------------------------------------------------------------------
# The lookup statement, tested without any session or database at all
# --------------------------------------------------------------------------


def test_the_lookup_statement_filters_on_the_integrity_columns():
    """No Session, no database -- a compiled Select is inspectable on its own.

    This is deliberately separate from the orchestration tests above, which
    monkeypatch the function that executes this statement (see the module
    docstring): together they prove the service both builds the right query
    and behaves correctly given what it returns, without needing to prove both
    facts through the same mechanism.
    """
    stmt = _qualification_lookup_statement(118, 12)
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "FROM role_qualification" in compiled
    assert "role_qualification.ministry_membership_id = 118" in compiled
    assert "role_qualification.ministry_role_id = 12" in compiled


def test_the_lookup_statement_selects_the_role_qualification_model():
    stmt = _qualification_lookup_statement(1, 1)

    assert stmt.column_descriptions[0]["type"] is RoleQualification


# ==========================================================================
# list_role_qualifications (Task 55)
# ==========================================================================


class _Row:
    """A stand-in for one row of ``_role_qualifications_statement``'s result."""

    def __init__(
        self, ministry_membership_id, person_id, person_display_name,
        membership_deactivated_at, person_deactivated_at, is_qualified, decided_at,
    ):
        self.ministry_membership_id = ministry_membership_id
        self.person_id = person_id
        self.person_display_name = person_display_name
        self.membership_deactivated_at = membership_deactivated_at
        self.person_deactivated_at = person_deactivated_at
        self.is_qualified = is_qualified
        self.decided_at = decided_at


def _stub_role_qualification_rows(session, monkeypatch, rows: list[_Row]) -> None:
    """Bypass the query entirely: its SQL shape is pinned separately below,
    and here only orchestration (authorization, and the rows-to-dataclass
    mapping) is under test.
    """

    class _Result:
        def all(self):
            return rows

    monkeypatch.setattr(session, "execute", lambda stmt: _Result())


def test_admin_can_list_role_qualifications(session, head, setup_ministry, setup_lead, monkeypatch):
    decided = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)
    _stub_role_qualification_rows(
        session, monkeypatch,
        [
            _Row(118, 42, "Ann", None, None, True, decided),
            _Row(119, 43, "Ben", None, None, False, decided),
            _Row(120, 44, "Cam", None, None, None, None),
        ],
    )

    result = list_role_qualifications(session, actor=head, role=setup_lead)

    assert isinstance(result, RoleQualifications)
    assert result.ministry_role_id == 12
    assert result.ministry_id == 3
    assert result.memberships == (
        MembershipQualification(118, 42, "Ann", None, None, True, decided),
        MembershipQualification(119, 43, "Ben", None, None, False, decided),
        MembershipQualification(120, 44, "Cam", None, None, None, None),
    )


def test_own_ministry_head_can_list_role_qualifications(session, setup_ministry, setup_lead, monkeypatch):
    _stub_role_qualification_rows(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = list_role_qualifications(session, actor=head, role=setup_lead)

    assert result.memberships == ()


def test_other_ministry_head_cannot_list_role_qualifications(session, setup_lead, monkeypatch):
    _stub_role_qualification_rows(session, monkeypatch, [])
    other_ministry = _ministry(4, "AV")
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=other_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        list_role_qualifications(session, actor=head, role=setup_lead)


def test_normal_member_cannot_list_role_qualifications(session, setup_ministry, setup_lead, monkeypatch):
    _stub_role_qualification_rows(session, monkeypatch, [])
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        list_role_qualifications(session, actor=member, role=setup_lead)


def test_never_assessed_reports_is_qualified_and_decided_at_as_none(
    session, head, setup_lead, monkeypatch
):
    _stub_role_qualification_rows(
        session, monkeypatch, [_Row(118, 42, "Ann", None, None, None, None)],
    )

    result = list_role_qualifications(session, actor=head, role=setup_lead)

    assert result.memberships[0].is_qualified is None
    assert result.memberships[0].decided_at is None


def test_the_role_qualifications_statement_filters_on_ministry_and_role():
    stmt = _role_qualifications_statement(12, 3, include_inactive=False)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "ministry_membership.ministry_id = 3" in compiled
    assert "role_qualification.ministry_role_id = 12" in compiled
    assert "LEFT OUTER JOIN role_qualification" in compiled


def test_the_role_qualifications_statement_hides_inactive_by_default():
    stmt = _role_qualifications_statement(12, 3, include_inactive=False)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "ministry_membership.deactivated_at IS NULL" in compiled
    assert "person.deactivated_at IS NULL" in compiled


def test_the_role_qualifications_statement_includes_inactive_when_asked():
    stmt = _role_qualifications_statement(12, 3, include_inactive=True)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "deactivated_at IS NULL" not in compiled
