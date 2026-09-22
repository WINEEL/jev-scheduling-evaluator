"""Existing Commitment service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14-17's precedent, for the same reasons: this
service needs both a ``session.flush()`` (to obtain a new row's identity
before the audit row referencing it) and a database lookup
(``session.execute(select(...))``, to find an existing commitment for a
person/date/source-ministry key) and, for the removal path,
``session.delete()`` on an object this offline fixture cannot make genuinely
"persistent" without a bound engine.

- **The lookup's SQL** is tested directly and mechanically, with no session or
  database at all: ``_commitment_lookup_statement`` returns a plain SQLAlchemy
  ``Select`` compiled with literal binds and inspected -- including the
  ``IS NULL`` case for a source-less commitment.
- **The lookup's use** (found vs. not-found) is exercised via ``monkeypatch``
  in every orchestration test.
- **The flush** and **the delete** use a real, unbound ``Session`` subclass
  whose ``flush()`` simulates identity assignment and whose ``delete()``
  tracks the request explicitly, exactly as Task 16 documented -- neither
  touches a database. ``commit()`` and ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually assigns identities, enforces the ``NULLS NOT DISTINCT``
unique index, or rolls back a real transaction. What it verifies instead is
the same shape of property those tasks settled for -- the service never
commits or rolls back, flushes only where the design says it must, and every
mutation and its audit row share one session.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import ExistingCommitment
from app.services import AuthorizationError, InvalidOperationError
from app.services.existing_commitment import (
    ACTION_EXISTING_COMMITMENT_CHANGED,
    ACTION_EXISTING_COMMITMENT_RECORDED,
    ACTION_EXISTING_COMMITMENT_REMOVED,
    _commitment_lookup_statement,
    remove_existing_commitment,
    set_existing_commitment,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class ExistingCommitmentSession(Session):
    """A real, unbound Session for these tests.

    Like Tasks 14-17's Session subclasses, ``flush()`` is not forbidden --
    this service legitimately flushes once, when it records a new commitment,
    to obtain the identity the audit row needs. ``delete()`` is overridden the
    same way Task 16 documented: the genuine ``Session.delete()`` requires a
    "persistent" object (loaded via query, or added and flushed against a real
    engine), which these hand-built fixtures cannot honestly produce without a
    bound database, so the request is tracked instead of executed. ``commit()``
    and ``rollback()`` remain forbidden -- the transaction still belongs to the
    caller.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self.deleted_objects: list = []
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def delete(self, instance) -> None:
        self.deleted_objects.append(instance)


@pytest.fixture
def session() -> ExistingCommitmentSession:
    return ExistingCommitmentSession()


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


def _ministry(ministry_id: int, name: str, *, deactivated: bool = False) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    if deactivated:
        ministry.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return ministry


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _commitment(
    commitment_id: int,
    *,
    person: Person,
    commitment_date: datetime.date = datetime.date(2026, 10, 4),
    source_ministry: Ministry | None = None,
    reason: str | None = None,
) -> ExistingCommitment:
    commitment = ExistingCommitment(
        person_id=person.id,
        commitment_date=commitment_date,
        source_ministry_id=source_ministry.id if source_ministry is not None else None,
        reason=reason,
    )
    commitment.id = commitment_id
    commitment.person = person
    commitment.source_ministry = source_ministry
    return commitment


@pytest.fixture
def head(av_ministry: Ministry) -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _actor_membership(9001, person=person, ministry=av_ministry, is_head=True)
    return person


@pytest.fixture
def admin() -> Person:
    """A church-wide Admin who heads nothing.

    Unlike everywhere else, this module still needs one: a commitment with
    **no source ministry** has no ministry to derive authority from, so it is
    Admin-only and always was (:func:`require_active_admin`). Task 80 narrowed
    the *other* half of this service -- a commitment that names a source
    ministry -- to that ministry's head, which is what ``head`` is for.
    """
    return _person(2, "Demo Admin", is_admin=True)


@pytest.fixture
def av_ministry() -> Ministry:
    return _ministry(4, "AV")


@pytest.fixture
def john() -> Person:
    return _person(42, "John")


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_lookup(monkeypatch, existing: ExistingCommitment | None) -> None:
    import app.services.existing_commitment as module

    monkeypatch.setattr(
        module, "_find_existing_commitment",
        lambda session, *, person_id, commitment_date, source_ministry_id: existing,
    )


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_active_admin_can_record_source_ministry_commitment(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert result.source_ministry_id == 4


def test_own_ministry_head_can_record_a_commitment_sourced_from_their_ministry(
    session, john, av_ministry, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    head = _person(9, "AV Head")
    _actor_membership(200, person=head, ministry=av_ministry, is_head=True)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert result.source_ministry_id == 4


def test_head_of_another_ministry_is_rejected(session, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)
    setup_ministry = _ministry(3, "Setup")
    setup_head = _person(9, "Setup Head")
    _actor_membership(201, person=setup_head, ministry=setup_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_existing_commitment(
            session, actor=setup_head, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_is_rejected(session, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)
    ordinary = _person(7, "Ada")
    _actor_membership(202, person=ordinary, ministry=av_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        set_existing_commitment(
            session, actor=ordinary, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry,
        )

    assert _audit_rows(session) == []


def test_source_less_commitment_requires_admin(session, john, monkeypatch):
    _stub_lookup(monkeypatch, None)
    ordinary = _person(7, "Ada")

    with pytest.raises(AuthorizationError):
        set_existing_commitment(
            session, actor=ordinary, person=john, commitment_date=datetime.date(2026, 10, 4),
            reason="Preaching.",
        )

    assert _audit_rows(session) == []


def test_ministry_head_cannot_create_a_source_less_church_wide_commitment(
    session, john, av_ministry, monkeypatch
):
    """Even a head of a real ministry cannot manage a church-wide (source-less)
    commitment -- there is no ministry to derive that authority from."""
    _stub_lookup(monkeypatch, None)
    head = _person(9, "AV Head")
    _actor_membership(200, person=head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_existing_commitment(
            session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
            reason="Preaching.",
        )

    assert _audit_rows(session) == []


def test_deactivated_actor_is_rejected(session, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)
    departed = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        set_existing_commitment(
            session, actor=departed, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# Domain / integrity / provenance
# --------------------------------------------------------------------------


def test_new_source_ministry_commitment_created(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert isinstance(result, ExistingCommitment)
    assert result.person_id == 42
    assert result.commitment_date == datetime.date(2026, 10, 4)
    assert result.source_ministry_id == 4
    assert result.reason is None


def test_new_source_less_commitment_created_with_reason(session, admin, john, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=admin, person=john, commitment_date=datetime.date(2026, 10, 4),
        reason="Preaching at the 10am service.",
    )

    assert result.source_ministry_id is None
    assert result.reason == "Preaching at the 10am service."


def test_source_less_and_no_reason_is_rejected(session, admin, john, monkeypatch):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=admin, person=john, commitment_date=datetime.date(2026, 10, 4),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_source_less_and_blank_reason_is_rejected(session, admin, john, monkeypatch, blank):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=admin, person=john, commitment_date=datetime.date(2026, 10, 4),
            reason=blank,
        )

    assert _audit_rows(session) == []


def test_source_ministry_and_no_reason_is_allowed(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert result.reason is None


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_source_ministry_and_blank_supplied_reason_is_rejected(session, head, john, av_ministry, monkeypatch, blank):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry, reason=blank,
        )

    assert _audit_rows(session) == []


def test_target_deactivated_person_is_rejected_on_create(session, head, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)
    departed = _person(42, "John", deactivated=True)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=head, person=departed, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_deactivated_source_ministry_is_rejected_on_create(session, head, john, monkeypatch):
    _stub_lookup(monkeypatch, None)
    inactive_ministry = _ministry(4, "AV", deactivated=True)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=inactive_ministry,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_no_target_ministry_membership_requirement_is_invented(session, head, john, av_ministry, monkeypatch):
    """John need not have any MinistryMembership in AV at all -- the block is
    church-wide, and source_ministry is provenance, not scope."""
    _stub_lookup(monkeypatch, None)
    assert john.ministry_memberships == []

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert result.source_ministry_id == 4


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------


def test_source_ministry_lookup_uses_equality():
    stmt = _commitment_lookup_statement(42, datetime.date(2026, 10, 4), 4)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "existing_commitment.source_ministry_id = 4" in compiled
    assert "IS NULL" not in compiled


def test_source_less_lookup_uses_is_null():
    stmt = _commitment_lookup_statement(42, datetime.date(2026, 10, 4), None)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "existing_commitment.source_ministry_id IS NULL" in compiled


def test_logical_duplicate_is_found_not_recreated(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving AV.",
    )

    assert result is existing
    assert session.flush_calls == 0  # no new row created


# --------------------------------------------------------------------------
# Idempotency / update
# --------------------------------------------------------------------------


def test_existing_same_reason_is_no_op(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving AV.",
    )

    assert result is existing


def test_no_audit_or_flush_on_same_reason(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving AV.",
    )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_reason_normalization_treats_whitespace_variants_as_the_same_reason(
    session, head, john, av_ministry, monkeypatch
):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="  Serving AV.  ",
    )

    assert result is existing
    assert _audit_rows(session) == []  # normalized to the same value -- no-op


def test_existing_reason_may_change(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving as AV lead.",
    )

    assert result is existing
    assert result.reason == "Serving as AV lead."


def test_reason_change_audits_old_to_new(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving as AV lead.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_EXISTING_COMMITMENT_CHANGED
    assert audit.before_values == {"reason": "Serving AV."}
    assert audit.after_values == {"reason": "Serving as AV lead."}


def test_reason_change_rejected_if_target_person_is_deactivated(session, head, av_ministry, monkeypatch):
    departed = _person(42, "John", deactivated=True)
    existing = _commitment(500, person=departed, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=head, person=departed, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=av_ministry, reason="Serving as AV lead.",
        )

    assert existing.reason == "Serving AV."  # untouched
    assert _audit_rows(session) == []


def test_reason_change_rejected_if_source_ministry_is_deactivated(session, head, john, monkeypatch):
    inactive_ministry = _ministry(4, "AV", deactivated=True)
    existing = _commitment(500, person=john, source_ministry=inactive_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_existing_commitment(
            session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
            source_ministry=inactive_ministry, reason="Serving as AV lead.",
        )

    assert existing.reason == "Serving AV."  # untouched
    assert _audit_rows(session) == []


def test_true_same_state_no_op_is_harmless_if_target_later_deactivated(session, head, av_ministry, monkeypatch):
    departed = _person(42, "John", deactivated=True)
    existing = _commitment(500, person=departed, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=departed, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving AV.",
    )

    assert result is existing
    assert _audit_rows(session) == []


def test_true_same_state_no_op_is_harmless_if_source_ministry_later_deactivated(session, head, john, monkeypatch):
    inactive_ministry = _ministry(4, "AV", deactivated=True)
    existing = _commitment(500, person=john, source_ministry=inactive_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=inactive_ministry, reason="Serving AV.",
    )

    assert result is existing
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


def test_admin_can_remove(session, head, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert existing in session.deleted_objects


def test_own_source_ministry_head_can_remove(session, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    head = _person(9, "AV Head")
    _actor_membership(200, person=head, ministry=av_ministry, is_head=True)

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert existing in session.deleted_objects


def test_wrong_ministry_head_is_rejected_on_removal(session, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    setup_ministry = _ministry(3, "Setup")
    setup_head = _person(9, "Setup Head")
    _actor_membership(201, person=setup_head, ministry=setup_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        remove_existing_commitment(session, actor=setup_head, commitment=existing)

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_source_less_removal_requires_admin(session, john):
    existing = _commitment(500, person=john, reason="Preaching.")
    ordinary = _person(7, "Ada")

    with pytest.raises(AuthorizationError):
        remove_existing_commitment(session, actor=ordinary, commitment=existing)

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_removal_allowed_for_deactivated_target_person(session, head, av_ministry):
    departed = _person(42, "John", deactivated=True)
    existing = _commitment(500, person=departed, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert existing in session.deleted_objects


def test_removal_allowed_for_deactivated_source_ministry(session, head, john):
    inactive_ministry = _ministry(4, "AV", deactivated=True)
    existing = _commitment(500, person=john, source_ministry=inactive_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert existing in session.deleted_objects


def test_correct_delete_request_occurs(session, head, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert session.deleted_objects == [existing]


def test_removal_uses_no_unnecessary_flush(session, head, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def test_recorded_audit_is_correct_with_source_ministry(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_EXISTING_COMMITMENT_RECORDED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "existing_commitment"
    assert audit.target_id == result.id == 900
    assert audit.ministry_id == 4
    assert audit.summary == "Recorded AV commitment for John on 2026-10-04"
    assert audit.before_values is None
    assert audit.after_values == {
        "person_id": 42, "commitment_date": "2026-10-04",
        "source_ministry_id": 4, "reason": None,
    }
    assert audit.reason is None


def test_source_less_audit_ministry_id_is_none(session, admin, john, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_existing_commitment(
        session, actor=admin, person=john, commitment_date=datetime.date(2026, 10, 4),
        reason="Preaching.",
    )
    audit = _one_audit_row(session)

    assert audit.ministry_id is None
    assert audit.summary == "Recorded church-wide commitment for John on 2026-10-04"
    assert audit.after_values == {
        "person_id": 42, "commitment_date": "2026-10-04",
        "source_ministry_id": None, "reason": "Preaching.",
    }


def test_changed_audit_is_correct(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving as AV lead.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_EXISTING_COMMITMENT_CHANGED
    assert audit.target_id == 500
    assert audit.ministry_id == 4
    assert audit.summary == "Changed John's AV commitment details for 2026-10-04"
    assert audit.before_values == {"reason": "Serving AV."}
    assert audit.after_values == {"reason": "Serving as AV lead."}


def test_removed_audit_is_correct(session, head, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)
    audit = _one_audit_row(session)

    assert audit.action == ACTION_EXISTING_COMMITMENT_REMOVED
    assert audit.target_id == 500
    assert audit.ministry_id == 4
    assert audit.summary == "Removed AV commitment for John on 2026-10-04"
    assert audit.before_values == {
        "person_id": 42, "commitment_date": "2026-10-04",
        "source_ministry_id": 4, "reason": "Serving AV.",
    }
    assert audit.after_values is None


def test_date_payload_is_json_compatible(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )
    audit = _one_audit_row(session)

    assert isinstance(audit.after_values["commitment_date"], str)
    import json
    json.dumps(audit.after_values)  # must not raise


def test_audit_target_id_is_the_actual_commitment_id(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert _one_audit_row(session).target_id == result.id


# --------------------------------------------------------------------------
# Transaction
# --------------------------------------------------------------------------


def test_create_flushes_exactly_once_for_identity(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    assert session.flush_calls == 1


def test_update_does_not_flush_unnecessarily(session, head, john, av_ministry, monkeypatch):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")
    _stub_lookup(monkeypatch, existing)

    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry, reason="Serving as AV lead.",
    )

    assert session.flush_calls == 0


def test_remove_does_not_flush_unnecessarily(session, head, john, av_ministry):
    existing = _commitment(500, person=john, source_ministry=av_ministry, reason="Serving AV.")

    remove_existing_commitment(session, actor=head, commitment=existing)

    assert session.flush_calls == 0


def test_service_never_commits_or_rolls_back(session, head, john, av_ministry, monkeypatch):
    _stub_lookup(monkeypatch, None)
    set_existing_commitment(
        session, actor=head, person=john, commitment_date=datetime.date(2026, 10, 4),
        source_ministry=av_ministry,
    )

    existing = _commitment(500, person=john, source_ministry=av_ministry, reason=None)
    remove_existing_commitment(session, actor=head, commitment=existing)

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# --------------------------------------------------------------------------
# Lookup statement mechanics, tested without any Session or database at all
# --------------------------------------------------------------------------


def test_the_lookup_statement_selects_the_existing_commitment_model():
    stmt = _commitment_lookup_statement(1, datetime.date(2026, 10, 4), 1)

    assert stmt.column_descriptions[0]["type"] is ExistingCommitment
