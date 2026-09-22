"""Staffing Requirement service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14/15's precedent exactly, for the same
reasons: this service needs both a ``session.flush()`` (to obtain a new row's
identity before the audit row referencing it can be built) and a database
lookup (``session.execute(select(...))``, to find an existing requirement for
an event/role pair). Neither can be exercised against a real, unbound
:class:`~sqlalchemy.orm.Session`.

- **The lookup's SQL** is tested directly and mechanically, with no session or
  database at all: ``_requirement_lookup_statement`` returns a plain
  SQLAlchemy ``Select`` that is compiled with literal binds and inspected.
- **The lookup's use** (found vs. not-found) is exercised via ``monkeypatch``
  in every orchestration test, exactly as Tasks 14 and 15 did.
- **The flush** uses a real, unbound ``Session`` subclass whose ``flush()``
  simulates identity assignment without touching a database. ``commit()`` and
  ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually assigns identities and rolls back a real transaction. What
it verifies instead is the same shape of property those tasks settled for --
the service never commits or rolls back, flushes only where the design says it
must, and every mutation and its audit row share one session.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.scheduling_input import EVENT_KIND_SUNDAY_SERVICE, Event, StaffingRequirement
from app.services import AuthorizationError, InvalidOperationError
from app.services.staffing_requirement import (
    ACTION_STAFFING_REQUIREMENT_CHANGED,
    ACTION_STAFFING_REQUIREMENT_CREATED,
    ACTION_STAFFING_REQUIREMENT_REMOVED,
    EventStaffing,
    RoleStaffing,
    _event_staffing_statement,
    _requirement_lookup_statement,
    list_event_staffing_requirements,
    set_staffing_requirement,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class StaffingRequirementSession(Session):
    """A real, unbound Session for these tests.

    Like Tasks 14/15's Session subclasses, ``flush()`` is not forbidden here --
    this service legitimately flushes once, when it creates a new row, to
    obtain the identity the audit row needs. ``commit()`` and ``rollback()``
    remain forbidden.
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
        """Track a delete request without SQLAlchemy's real persistence check.

        The genuine ``Session.delete()`` requires the object to be
        "persistent" -- loaded via an actual query, or added and flushed
        against a real engine -- which this offline fixture cannot honestly
        produce without a bound database (the fixtures below build
        ``StaffingRequirement`` objects directly and hand-assign ``.id``, so
        SQLAlchemy sees them as transient). What these tests verify is that
        the *service* calls delete on the right object at the right point in
        the same session as everything else, which this tracking override
        proves without pretending to simulate SQLAlchemy's identity-map
        machinery.
        """
        self.deleted_objects.append(instance)


@pytest.fixture
def session() -> StaffingRequirementSession:
    return StaffingRequirementSession()


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
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _role(role_id: int, name: str, *, ministry: Ministry, deactivated: bool = False) -> MinistryRole:
    role = MinistryRole(name=name, ministry_id=ministry.id)
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return role


def _event(
    event_id: int,
    *,
    ministry: Ministry,
    event_date: datetime.date = datetime.date(2026, 10, 4),
    cancelled: bool = False,
) -> Event:
    event = Event(
        scheduling_period_id=1,
        ministry_id=ministry.id,
        event_date=event_date,
        event_kind=EVENT_KIND_SUNDAY_SERVICE,
    )
    event.id = event_id
    if cancelled:
        event.cancelled_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return event


def _existing_requirement(
    requirement_id: int, *, event: Event, role: MinistryRole, required_count: int,
) -> StaffingRequirement:
    requirement = StaffingRequirement(
        event_id=event.id, ministry_role_id=role.id, ministry_id=event.ministry_id,
        required_count=required_count,
    )
    requirement.id = requirement_id
    return requirement


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
def sunday_event(setup_ministry: Ministry) -> Event:
    return _event(700, ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_lookup(monkeypatch, existing: StaffingRequirement | None) -> None:
    import app.services.staffing_requirement as module

    monkeypatch.setattr(
        module, "_find_existing_requirement",
        lambda session, *, event_id, ministry_role_id: existing,
    )


# --------------------------------------------------------------------------
# 1, 2, 3, 4 -- Authorization
# --------------------------------------------------------------------------


def test_admin_creates_requirement(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=1,
    )

    assert result.required_count == 1
    assert result.event_id == 700
    assert result.ministry_role_id == 12
    assert result.ministry_id == 3


def test_own_ministry_head_creates_requirement(session, setup_ministry, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=1,
    )

    assert result.required_count == 1


def test_other_ministry_head_is_rejected(session, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_staffing_requirement(
            session, actor=av_head, event=sunday_event, role=setup_lead, required_count=1,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_is_rejected(session, setup_ministry, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    ordinary = _person(7, "Ada")
    _membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        set_staffing_requirement(
            session, actor=ordinary, event=sunday_event, role=setup_lead, required_count=1,
        )

    assert _audit_rows(session) == []


def test_deactivated_actor_is_rejected(session, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    departed = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        set_staffing_requirement(
            session, actor=departed, event=sunday_event, role=setup_lead, required_count=1,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 5 -- Event/Role ministry mismatch
# --------------------------------------------------------------------------


def test_event_role_ministry_mismatch_is_rejected(session, head, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_role = _role(20, "Sound", ministry=av_ministry)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=sunday_event, role=av_role, required_count=1,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 6, 7 -- New positive requirement requires an active target
# --------------------------------------------------------------------------


def test_new_positive_requirement_rejected_for_cancelled_event(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=cancelled_event, role=setup_lead, required_count=1,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_new_positive_requirement_rejected_for_deactivated_role(
    session, head, setup_ministry, sunday_event, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=sunday_event, role=inactive_role, required_count=1,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_changing_an_existing_positive_requirement_also_requires_an_active_target(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    """Not just brand-new rows: changing an existing positive count on a
    cancelled event must also be rejected."""
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    existing = _existing_requirement(500, event=cancelled_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=cancelled_event, role=setup_lead, required_count=2,
        )

    assert existing.required_count == 1  # untouched
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 8, 9 -- Removal is state repair: permitted against an inactive target
# --------------------------------------------------------------------------


def test_existing_requirement_may_be_removed_from_a_cancelled_event(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    existing = _existing_requirement(500, event=cancelled_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)

    result = set_staffing_requirement(
        session, actor=head, event=cancelled_event, role=setup_lead, required_count=0,
    )

    assert result is None
    assert existing in session.deleted_objects


def test_existing_requirement_may_be_removed_for_a_deactivated_role(
    session, head, setup_ministry, sunday_event, monkeypatch
):
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    existing = _existing_requirement(500, event=sunday_event, role=inactive_role, required_count=1)
    _stub_lookup(monkeypatch, existing)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=inactive_role, required_count=0,
    )

    assert result is None
    assert existing in session.deleted_objects


# --------------------------------------------------------------------------
# 10 -- Negative required_count rejected
# --------------------------------------------------------------------------


def test_negative_required_count_is_rejected(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=sunday_event, role=setup_lead, required_count=-1,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 11, 12 -- Idempotency
# --------------------------------------------------------------------------


def test_no_row_plus_zero_is_idempotent_no_op(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=0,
    )

    assert result is None
    assert _audit_rows(session) == []
    assert session.flush_calls == 0
    assert session.deleted_objects == []


def test_existing_same_count_is_idempotent_no_op(session, head, sunday_event, setup_lead, monkeypatch):
    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=2)
    _stub_lookup(monkeypatch, existing)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=2,
    )

    assert result is existing
    assert result.required_count == 2
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_idempotent_same_count_no_op_even_against_a_cancelled_event(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    """The idempotency rule is stated unconditionally: an unchanged request is
    never re-validated against activity, since nothing about the row changes."""
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    existing = _existing_requirement(500, event=cancelled_event, role=setup_lead, required_count=2)
    _stub_lookup(monkeypatch, existing)

    result = set_staffing_requirement(
        session, actor=head, event=cancelled_event, role=setup_lead, required_count=2,
    )

    assert result is existing
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 13, 14, 15 -- Flush discipline
# --------------------------------------------------------------------------


def test_create_uses_exactly_one_flush(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=1,
    )

    assert session.flush_calls == 1


def test_update_uses_no_flush(session, head, sunday_event, setup_lead, monkeypatch):
    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)

    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=2,
    )

    assert session.flush_calls == 0


def test_removal_uses_no_flush(session, head, sunday_event, setup_lead, monkeypatch):
    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)

    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=0,
    )

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 16, 17, 18, 19, 20 -- Audit correctness
# --------------------------------------------------------------------------


def test_create_audit_is_correct(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=1,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_STAFFING_REQUIREMENT_CREATED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "staffing_requirement"
    assert audit.target_id == result.id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Required 1 Setup Lead for Setup on 2026-10-04"
    assert audit.before_values is None
    assert audit.after_values == {
        "event_id": 700, "ministry_role_id": 12, "required_count": 1,
    }
    assert audit.reason is None


def test_update_audit_is_correct(session, head, sunday_event, setup_lead, monkeypatch):
    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)

    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=2,
        reason="More coverage needed for the holiday service.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_STAFFING_REQUIREMENT_CHANGED
    assert audit.target_id == 500
    assert audit.ministry_id == 3
    assert audit.summary == "Changed Setup Lead staffing for Setup on 2026-10-04 from 1 to 2"
    assert audit.before_values == {"required_count": 1}
    assert audit.after_values == {"required_count": 2}
    assert audit.reason == "More coverage needed for the holiday service."


def test_removal_audit_is_correct(session, head, sunday_event, setup_lead, monkeypatch):
    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=3)
    _stub_lookup(monkeypatch, existing)

    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=0,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_STAFFING_REQUIREMENT_REMOVED
    assert audit.target_id == 500
    assert audit.ministry_id == 3
    assert audit.summary == "Removed Setup Lead staffing requirement for Setup on 2026-10-04"
    assert audit.before_values == {
        "event_id": 700, "ministry_role_id": 12, "required_count": 3,
    }
    assert audit.after_values is None


# --------------------------------------------------------------------------
# 21 -- Whitespace-only reason
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_reason_is_rejected(session, head, sunday_event, setup_lead, monkeypatch, blank):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_staffing_requirement(
            session, actor=head, event=sunday_event, role=setup_lead,
            required_count=1, reason=blank,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 22 -- Transaction discipline
# --------------------------------------------------------------------------


def test_service_never_commits_or_rolls_back(session, head, sunday_event, setup_lead, monkeypatch):
    _stub_lookup(monkeypatch, None)
    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=1,
    )

    existing = _existing_requirement(500, event=sunday_event, role=setup_lead, required_count=1)
    _stub_lookup(monkeypatch, existing)
    set_staffing_requirement(
        session, actor=head, event=sunday_event, role=setup_lead, required_count=0,
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# --------------------------------------------------------------------------
# 23, 24 -- Multiple roles/events remain structurally allowed
# --------------------------------------------------------------------------


def test_multiple_roles_for_one_event_remain_allowed(
    session, head, setup_ministry, sunday_event, monkeypatch
):
    lead = _role(12, "Setup Lead", ministry=setup_ministry)
    helper = _role(13, "Setup Helper", ministry=setup_ministry)
    _stub_lookup(monkeypatch, None)

    r1 = set_staffing_requirement(session, actor=head, event=sunday_event, role=lead, required_count=1)
    r2 = set_staffing_requirement(session, actor=head, event=sunday_event, role=helper, required_count=3)

    assert r1.ministry_role_id == 12
    assert r2.ministry_role_id == 13
    assert r1.event_id == r2.event_id == 700
    assert len(_audit_rows(session)) == 2


def test_same_role_for_different_events_remains_allowed(
    session, head, setup_ministry, setup_lead, monkeypatch
):
    event_a = _event(700, ministry=setup_ministry, event_date=datetime.date(2026, 10, 4))
    event_b = _event(701, ministry=setup_ministry, event_date=datetime.date(2026, 10, 11))
    _stub_lookup(monkeypatch, None)

    r1 = set_staffing_requirement(session, actor=head, event=event_a, role=setup_lead, required_count=1)
    r2 = set_staffing_requirement(session, actor=head, event=event_b, role=setup_lead, required_count=1)

    assert r1.event_id == 700
    assert r2.event_id == 701
    assert r1.ministry_role_id == r2.ministry_role_id == 12
    assert len(_audit_rows(session)) == 2


# --------------------------------------------------------------------------
# Lookup statement, tested without any Session or database at all
# --------------------------------------------------------------------------


def test_the_lookup_statement_filters_on_the_integrity_columns():
    stmt = _requirement_lookup_statement(700, 12)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM staffing_requirement" in compiled
    assert "staffing_requirement.event_id = 700" in compiled
    assert "staffing_requirement.ministry_role_id = 12" in compiled


def test_the_lookup_statement_selects_the_staffing_requirement_model():
    stmt = _requirement_lookup_statement(1, 1)

    assert stmt.column_descriptions[0]["type"] is StaffingRequirement


# ==========================================================================
# list_event_staffing_requirements (Task 54)
# ==========================================================================


class _Row:
    """A stand-in for one row of ``_event_staffing_statement``'s result."""

    def __init__(self, ministry_role_id, name, description, display_order, required_count):
        self.ministry_role_id = ministry_role_id
        self.name = name
        self.description = description
        self.display_order = display_order
        self.required_count = required_count


def _stub_event_staffing_rows(session, monkeypatch, rows: list[_Row]) -> None:
    """Bypass the query entirely: its SQL shape is pinned separately below
    (``test_the_event_staffing_statement_...``), and here only orchestration
    (authorization, and the rows-to-dataclass mapping) is under test.
    """

    class _Result:
        def all(self):
            return rows

    monkeypatch.setattr(session, "execute", lambda stmt: _Result())


def test_admin_can_list_event_staffing(session, head, setup_ministry, sunday_event, monkeypatch):
    _stub_event_staffing_rows(
        session, monkeypatch,
        [_Row(12, "Setup Lead", "Leads the team.", 0, 2), _Row(13, "Setup 2", None, 1, None)],
    )

    result = list_event_staffing_requirements(session, actor=head, event=sunday_event)

    assert isinstance(result, EventStaffing)
    assert result.event_id == 700
    assert result.ministry_id == 3
    assert result.roles == (
        RoleStaffing(ministry_role_id=12, name="Setup Lead", description="Leads the team.", display_order=0, required_count=2),
        RoleStaffing(ministry_role_id=13, name="Setup 2", description=None, display_order=1, required_count=None),
    )


def test_own_ministry_head_can_list_event_staffing(session, setup_ministry, sunday_event, monkeypatch):
    _stub_event_staffing_rows(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = list_event_staffing_requirements(session, actor=head, event=sunday_event)

    assert result.roles == ()


def test_other_ministry_head_cannot_list_event_staffing(session, sunday_event, monkeypatch):
    _stub_event_staffing_rows(session, monkeypatch, [])
    other_ministry = _ministry(4, "AV")
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=other_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        list_event_staffing_requirements(session, actor=head, event=sunday_event)


def test_normal_member_cannot_list_event_staffing(session, setup_ministry, sunday_event, monkeypatch):
    _stub_event_staffing_rows(session, monkeypatch, [])
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        list_event_staffing_requirements(session, actor=member, event=sunday_event)


def test_a_role_with_no_requirement_reports_required_count_none_not_zero(
    session, head, sunday_event, monkeypatch
):
    """Absence of a row is "not needed here", never a stored zero -- the read
    side must preserve the same distinction the write side does.
    """
    _stub_event_staffing_rows(session, monkeypatch, [_Row(12, "Setup Lead", None, 0, None)])

    result = list_event_staffing_requirements(session, actor=head, event=sunday_event)

    assert result.roles[0].required_count is None


def test_the_event_staffing_statement_only_admits_active_roles_of_this_ministry():
    stmt = _event_staffing_statement(700, 3)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "ministry_role.ministry_id = 3" in compiled
    assert "ministry_role.deactivated_at IS NULL" in compiled


def test_the_event_staffing_statement_left_joins_so_an_unrequired_role_still_appears():
    stmt = _event_staffing_statement(700, 3)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "LEFT OUTER JOIN staffing_requirement" in compiled
    # The event filter lives in the join condition, not a WHERE clause --
    # an INNER join with the same predicate in WHERE would silently become an
    # inner join in effect, dropping every role with no requirement row.
    assert "staffing_requirement.event_id = 700" in compiled
