"""Manual Assignment management service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14-21's precedent, adapted for the largest
surface area yet: this service has six private lookup/count helpers plus a
reused Task 21 query (:func:`app.services.sunday_conflict.get_person_sunday_conflicts`,
imported by name into this module's namespace, so orchestration tests
monkeypatch ``app.services.assignment.get_person_sunday_conflicts`` rather
than the original). Every helper is monkeypatched via one shared
``_stub_environment`` fixture-builder that defaults to "everything permitted"
so each test overrides exactly one input to isolate exactly one blocker --
the same "change one thing" discipline every earlier task's test suite used.

- **Each query's SQL** is compiled and inspected directly, with no session or
  database at all (the "Queries" section near the end).
- **Each query's use**, plus the reused conflict check, is exercised via
  ``monkeypatch`` in every orchestration test.
- **The flush and the delete** use a real, unbound ``Session`` subclass whose
  ``flush()`` simulates identity assignment and whose ``delete()`` tracks the
  request explicitly, exactly as Task 16 documented. ``commit()`` and
  ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually returns the rows these queries would select, enforces the
composite foreign keys, or rolls back a real transaction. What it verifies
instead is that every absolute rule is genuinely unconditional, every
overridable rule is genuinely bypassable and nothing else is, and that the
service never touches the session beyond one flush (create) or one delete
(remove).
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SUNDAY_SERVICE,
    Availability,
    Event,
)
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.services import AuthorizationError, InvalidOperationError
from app.services.assignment import (
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    ACTION_ASSIGNMENT_REMOVED,
    _BLOCKER_CAPACITY_FULL,
    _BLOCKER_NOT_QUALIFIED,
    _BLOCKER_ROLE_DEACTIVATED,
    _BLOCKER_SUNDAY_CONFLICT,
    _BLOCKER_UNAVAILABLE,
    _assignment_count_statement,
    _availability_lookup_statement,
    _duplicate_membership_in_event_statement,
    _exact_assignment_lookup_statement,
    _newer_version_exists_statement,
    _qualification_lookup_statement,
    assign_member,
    remove_assignment,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.sunday_conflict import SundayConflictResult

# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


class AssignmentSession(Session):
    """A real, unbound Session for these tests.

    ``flush()`` is not forbidden -- this service legitimately flushes once,
    when it creates a new Assignment, to obtain the identity the audit row
    needs. ``delete()`` is overridden the same way Task 16 documented: the
    genuine ``Session.delete()`` requires a "persistent" object, which these
    hand-built fixtures cannot honestly produce without a bound database, so
    the request is tracked instead of executed. ``commit()`` and
    ``rollback()`` remain forbidden.
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
def session() -> AssignmentSession:
    return AssignmentSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
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


def _target_membership(
    membership_id: int, *, person: Person, ministry: Ministry, deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    if deactivated:
        membership.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return membership


def _role(role_id: int, name: str, *, ministry: Ministry, deactivated: bool = False) -> MinistryRole:
    role = MinistryRole(name=name, ministry_id=ministry.id)
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return role


def _event(event_id: int, *, ministry: Ministry, cancelled: bool = False) -> Event:
    event = Event(
        scheduling_period_id=1, ministry_id=ministry.id,
        event_date=datetime.date(2026, 10, 4), event_kind=EVENT_KIND_SUNDAY_SERVICE,
    )
    event.id = event_id
    if cancelled:
        event.cancelled_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return event


def _version(
    version_id: int, *, schedule_id: int = 500, version_number: int = 1,
    status: str = SCHEDULE_VERSION_STATUS_DRAFT,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=1,
        version_number=version_number, status=status,
    )
    version.id = version_id
    return version


def _requirement(
    requirement_id: int, *, version: ScheduleVersion, event: Event, role: MinistryRole,
    required_count: int = 1, event_date: datetime.date | None = None,
) -> ScheduleVersionRequirement:
    req = ScheduleVersionRequirement(
        schedule_version_id=version.id, event_id=event.id,
        event_date=event_date or event.event_date, ministry_role_id=role.id,
        scheduling_period_id=1, ministry_id=role.ministry_id, required_count=required_count,
    )
    req.id = requirement_id
    req.schedule_version = version
    req.event = event
    req.ministry_role = role
    return req


def _existing_assignment(
    assignment_id: int, *, requirement: ScheduleVersionRequirement, membership: MinistryMembership,
    is_override: bool = False, override_reason: str | None = None,
) -> Assignment:
    a = Assignment(
        schedule_version_requirement_id=requirement.id, ministry_membership_id=membership.id,
        schedule_version_id=requirement.schedule_version_id, event_id=requirement.event_id,
        ministry_id=requirement.ministry_id, is_override=is_override, override_reason=override_reason,
    )
    a.id = assignment_id
    a.schedule_version_requirement = requirement
    a.ministry_membership = membership
    return a


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
    _actor_membership(9001, person=person, ministry=setup_ministry, is_head=True)
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


@pytest.fixture
def draft_version() -> ScheduleVersion:
    return _version(500)


@pytest.fixture
def requirement(draft_version, sunday_event, setup_lead) -> ScheduleVersionRequirement:
    return _requirement(600, version=draft_version, event=sunday_event, role=setup_lead)


@pytest.fixture
def john(setup_ministry: Ministry) -> MinistryMembership:
    return _target_membership(118, person=_person(42, "John"), ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_environment(
    monkeypatch,
    *,
    newer_version_exists: bool = False,
    exact_assignment: Assignment | None = None,
    duplicate_in_event: int | None = None,
    current_count: int = 0,
    qualified: bool | None = True,  # True, False, or None (no row)
    availability_state: str | None = None,  # AVAILABLE, UNAVAILABLE, or None (no row)
    blocked: bool = False,
    serving_limit: int | None = None,  # None = no maximum configured
    linked_membership_ids=frozenset(),  # empty = no pair rule configured
    linked_assignment_on_date: int | None = None,
    assignments_in_version: int = 0,
    event_gap_sequence=None,  # MinistryEventSequence; None = no event-gap rule
    assigned_event_ids=frozenset(),
    member_group_caps=(),  # empty = no member group carries a cap
    support_requirements=(),  # empty = no same-event support requirement
    event_roster=frozenset(),  # who already holds an assignment at this event
) -> None:
    """Configure every lookup for "nothing blocks this assignment" by default;
    each test overrides exactly one keyword to introduce exactly one blocker.
    """
    import app.services.assignment as module

    monkeypatch.setattr(
        module, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: newer_version_exists,
    )
    monkeypatch.setattr(
        module, "_find_exact_assignment",
        lambda session, *, schedule_version_requirement_id, ministry_membership_id: exact_assignment,
    )
    monkeypatch.setattr(
        module, "_find_duplicate_membership_in_event",
        lambda session, *, schedule_version_id, event_id, ministry_membership_id: duplicate_in_event,
    )
    monkeypatch.setattr(
        module, "_current_assignment_count",
        lambda session, *, schedule_version_requirement_id: current_count,
    )

    if qualified is None:
        qualification = None
    else:
        qualification = SimpleNamespace(is_qualified=qualified)
    monkeypatch.setattr(
        module, "_find_qualification",
        lambda session, *, ministry_membership_id, ministry_role_id: qualification,
    )

    if availability_state is None:
        availability = None
    else:
        availability = SimpleNamespace(availability_state=availability_state)
    monkeypatch.setattr(
        module, "_find_availability",
        lambda session, *, ministry_membership_id, event_id: availability,
    )

    conflict_result = SundayConflictResult(
        existing_commitments=(SimpleNamespace(),) if blocked else (),
        authoritative_assignments=(),
    )
    monkeypatch.setattr(
        module, "get_person_sunday_conflicts",
        lambda session, *, person_id, conflict_date, target_ministry_id: conflict_result,
    )

    # The person-period serving maximum (Task 47). ``None`` is "no maximum
    # configured", which is the default world these tests describe.
    monkeypatch.setattr(
        module, "get_serving_limit",
        lambda session, *, ministry_membership_id, scheduling_period_id: serving_limit,
    )
    monkeypatch.setattr(
        module, "_count_assignments_in_version",
        lambda session, *, schedule_version_id, ministry_membership_id: (
            assignments_in_version
        ),
    )

    # The linked-pair same-date exclusion (Task 50). An empty set is "no pair
    # rule configured", which is the default world these tests describe.
    monkeypatch.setattr(
        module, "get_linked_membership_ids",
        lambda session, *, ministry_membership_id, scheduling_period_id: (
            linked_membership_ids
        ),
    )
    monkeypatch.setattr(
        module, "_find_linked_assignment_on_date",
        lambda session, *, schedule_version_id, event_date, linked_membership_ids: (
            linked_assignment_on_date
        ),
    )

    # The ministry event-gap rule (Task 71). ``None`` is "no rule configured
    # for this period", which is the default world these tests describe -- and
    # the one in which the ministry's event sequence is never even loaded.
    monkeypatch.setattr(
        module, "load_event_gap_sequence",
        lambda session, **kwargs: event_gap_sequence,
    )
    monkeypatch.setattr(
        module, "_assigned_event_ids_in_version",
        lambda session, *, schedule_version_id, ministry_membership_id: (
            frozenset(assigned_event_ids)
        ),
    )

    # The member-group per-event cap and the same-event support requirement
    # (Task 74). Empty tuples are "neither rule is configured for this period",
    # which is the default world these tests describe -- and the one in which
    # the event's roster is never even read.
    monkeypatch.setattr(
        module, "load_member_group_caps",
        lambda session, *, scheduling_period_id: tuple(member_group_caps),
    )
    monkeypatch.setattr(
        module, "load_support_requirements",
        lambda session, *, scheduling_period_id: tuple(support_requirements),
    )
    monkeypatch.setattr(
        module, "_memberships_assigned_to_event",
        lambda session, *, schedule_version_id, event_id: frozenset(event_roster),
    )


# --------------------------------------------------------------------------
# 1-9 -- Authorization / version lifecycle
# --------------------------------------------------------------------------


def test_1_admin_can_assign(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_2_own_ministry_head_can_assign(session, setup_ministry, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    head = _person(9, "Head Person")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_3_other_ministry_head_is_rejected(session, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _actor_membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        assign_member(session, actor=av_head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_4_normal_user_is_rejected(session, setup_ministry, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    ordinary = _person(7, "Ada")
    _actor_membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        assign_member(session, actor=ordinary, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_5_finalized_version_is_rejected(session, head, sunday_event, setup_lead, john, monkeypatch):
    _stub_environment(monkeypatch)
    finalized = _version(500, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    req = _requirement(600, version=finalized, event=sunday_event, role=setup_lead)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_6_non_latest_draft_version_is_rejected(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_7_latest_draft_is_allowed(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, newer_version_exists=False)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_8_latest_review_is_allowed(session, head, sunday_event, setup_lead, john, monkeypatch):
    _stub_environment(monkeypatch, newer_version_exists=False)
    review_version = _version(500, status=SCHEDULE_VERSION_STATUS_REVIEW)
    req = _requirement(600, version=review_version, event=sunday_event, role=setup_lead)

    result = assign_member(session, actor=head, requirement=req, membership=john)

    assert isinstance(result, Assignment)


def test_9_idempotent_exact_assignment_still_rejects_if_version_now_immutable(
    session, head, sunday_event, setup_lead, john, monkeypatch
):
    finalized = _version(500, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    req = _requirement(600, version=finalized, event=sunday_event, role=setup_lead)
    existing = _existing_assignment(800, requirement=req, membership=john)
    _stub_environment(monkeypatch, exact_assignment=existing)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john)


# --------------------------------------------------------------------------
# 10-17 -- Structural rules
# --------------------------------------------------------------------------


def test_10_ministry_mismatch_is_rejected(session, head, requirement, monkeypatch):
    _stub_environment(monkeypatch)
    av_ministry = _ministry(4, "AV")
    av_member = _target_membership(118, person=_person(42, "John"), ministry=av_ministry)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=av_member)

    assert _audit_rows(session) == []


def test_11_deactivated_membership_is_rejected(session, head, setup_ministry, requirement, monkeypatch):
    _stub_environment(monkeypatch)
    inactive = _target_membership(118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=inactive)

    assert _audit_rows(session) == []


def test_12_deactivated_person_is_rejected(session, head, setup_ministry, requirement, monkeypatch):
    _stub_environment(monkeypatch)
    departed = _person(42, "John", deactivated=True)
    membership = _target_membership(118, person=departed, ministry=setup_ministry)
    assert membership.deactivated_at is None  # membership itself is active

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=membership)

    assert _audit_rows(session) == []


def test_13_cancelled_event_is_rejected(session, head, setup_ministry, setup_lead, john, monkeypatch):
    _stub_environment(monkeypatch)
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    version = _version(500)
    req = _requirement(600, version=version, event=cancelled_event, role=setup_lead)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john)

    assert _audit_rows(session) == []


def test_14_same_member_already_in_another_role_same_event_version_is_rejected(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(monkeypatch, duplicate_in_event=999)  # a different Assignment id

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_15_exact_same_membership_and_requirement_is_idempotent(session, head, requirement, john, monkeypatch):
    existing = _existing_assignment(800, requirement=requirement, membership=john)
    _stub_environment(monkeypatch, exact_assignment=existing)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_16_requirement_capacity_is_enforced(session, head, sunday_event, setup_lead, john, monkeypatch):
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=setup_lead, required_count=1)
    _stub_environment(monkeypatch, current_count=1)  # already at capacity

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john)

    assert _audit_rows(session) == []


def test_17_required_count_greater_than_1_permits_multiple_different_members(
    session, head, setup_ministry, sunday_event, setup_lead, monkeypatch
):
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=setup_lead, required_count=3)
    _stub_environment(monkeypatch, current_count=1)  # one filled, two more allowed
    member_2 = _target_membership(119, person=_person(43, "Ada"), ministry=setup_ministry)

    result = assign_member(session, actor=head, requirement=req, membership=member_2)

    assert isinstance(result, Assignment)


# --------------------------------------------------------------------------
# 18-21 -- Qualification / role activity
# --------------------------------------------------------------------------


def test_18_qualified_member_is_allowed(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=True)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_19_no_qualification_row_blocks_ordinary_assignment(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_20_explicit_false_qualification_blocks_ordinary_assignment(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=False)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_21_deactivated_role_blocks_ordinary_assignment(
    session, head, setup_ministry, sunday_event, john, monkeypatch
):
    _stub_environment(monkeypatch)
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=inactive_role)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john)

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 22-24 -- Availability
# --------------------------------------------------------------------------


def test_22_available_is_allowed(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, availability_state=AVAILABILITY_AVAILABLE)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_23_unavailable_blocks_ordinary_assignment(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, availability_state=AVAILABILITY_UNAVAILABLE)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_22b_backup_is_allowed_with_no_override_needed(session, head, requirement, john, monkeypatch):
    """Task 52: BACKUP is feasible, not a blocker -- assigning a BACKUP
    candidate manually succeeds with no override_reason, exactly like
    AVAILABLE, and unlike UNAVAILABLE below.
    """
    _stub_environment(monkeypatch, availability_state=AVAILABILITY_BACKUP)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)
    assert result.is_override is False
    assert result.override_reason is None


def test_24_no_availability_row_is_allowed_and_distinguishable_from_available(
    session, head, requirement, john, monkeypatch
):
    """No row is a real third state (no response), never conflated with an
    explicit AVAILABLE row -- both permit assignment, but for different
    reasons the two stub configurations make explicit."""
    _stub_environment(monkeypatch, availability_state=None)
    result_no_response = assign_member(session, actor=head, requirement=requirement, membership=john)
    assert isinstance(result_no_response, Assignment)


# --------------------------------------------------------------------------
# 25-29 -- Sunday conflict
# --------------------------------------------------------------------------


def test_25_no_conflict_is_allowed(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, blocked=False)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_26_explicit_commitment_conflict_blocks(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, blocked=True)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _audit_rows(session) == []


def test_27_authoritative_assignment_conflict_blocks(session, head, requirement, john, monkeypatch):
    """Same is_blocked=True path as test 26 -- the conflict result does not
    distinguish its two sources at this call site, matching Task 21's own
    union semantics (either source alone is sufficient to block)."""
    import app.services.assignment as module

    conflict = SundayConflictResult(
        existing_commitments=(), authoritative_assignments=(SimpleNamespace(),),
    )
    _stub_environment(monkeypatch)
    monkeypatch.setattr(
        module, "get_person_sunday_conflicts",
        lambda session, *, person_id, conflict_date, target_ministry_id: conflict,
    )

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john)


def test_28_same_ministry_situation_is_not_treated_as_cross_ministry_blocker(
    session, head, requirement, john, monkeypatch
):
    """Task 21's own query already excludes same-ministry provenance/assignments
    from is_blocked -- this test proves assignment.py trusts that result
    (not_blocked) rather than re-deriving or second-guessing it."""
    _stub_environment(monkeypatch, blocked=False)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert isinstance(result, Assignment)


def test_29_conflict_call_uses_requirement_event_date_not_current_event_event_date(
    session, head, setup_ministry, setup_lead, john, monkeypatch
):
    """The Event's current event_date differs from the snapshot event_date on
    the requirement -- the conflict lookup must be called with the latter."""
    import app.services.assignment as module

    moved_event = _event(700, ministry=setup_ministry)
    moved_event.event_date = datetime.date(2026, 11, 22)  # "moved" since the snapshot
    version = _version(500)
    req = _requirement(
        600, version=version, event=moved_event, role=setup_lead,
        event_date=datetime.date(2026, 10, 4),  # the original snapshot date
    )
    _stub_environment(monkeypatch)

    captured = {}

    def fake_conflicts(session, *, person_id, conflict_date, target_ministry_id):
        captured["conflict_date"] = conflict_date
        return SundayConflictResult(existing_commitments=(), authoritative_assignments=())

    monkeypatch.setattr(module, "get_person_sunday_conflicts", fake_conflicts)

    assign_member(session, actor=head, requirement=req, membership=john)

    assert captured["conflict_date"] == datetime.date(2026, 10, 4)
    assert captured["conflict_date"] != moved_event.event_date


# --------------------------------------------------------------------------
# 30-46 -- Override
# --------------------------------------------------------------------------


def test_30_valid_override_reason_is_required_for_override_path(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)  # a real blocker

    result = assign_member(
        session, actor=head, requirement=requirement, membership=john,
        override_reason="Only available qualified-adjacent member.",
    )

    assert result.is_override is True


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_31_blank_override_reason_is_rejected(session, head, requirement, john, monkeypatch, blank):
    _stub_environment(monkeypatch)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john, override_reason=blank)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_32_override_can_bypass_missing_qualification(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    result = assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)


def test_33_override_can_bypass_false_qualification(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=False)

    result = assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)


def test_34_override_can_bypass_deactivated_role(session, head, setup_ministry, sunday_event, john, monkeypatch):
    _stub_environment(monkeypatch)
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=inactive_role)

    result = assign_member(session, actor=head, requirement=req, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)


def test_35_override_can_bypass_unavailable(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, availability_state=AVAILABILITY_UNAVAILABLE)

    result = assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)


def test_36_override_cannot_bypass_a_cross_ministry_conflict(
    session, head, requirement, john, monkeypatch
):
    """**The rule this test used to prove the opposite of.**

    One Person serves at most one ministry per Sunday is a hard church-wide
    rule, and Task 79's review made it absolute: an ``override_reason`` no
    longer reaches it. The refusal names the rule and says so in as many
    words, because a head who has just been refused needs to know that trying
    again with a better reason is not the remedy.
    """
    _stub_environment(monkeypatch, blocked=True)

    with pytest.raises(InvalidOperationError) as caught:
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="Nobody else could cover it.",
        )
    message = str(caught.value)
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in message
    assert "not overridable" in message
    # Nothing was written on the way to refusing.
    assert not [obj for obj in session.new if isinstance(obj, Assignment)]


def test_36b_a_cross_ministry_conflict_is_refused_with_no_reason_either(
    session, head, requirement, john, monkeypatch
):
    """Symmetry: the answer does not depend on whether a reason was supplied,
    which is what "absolute" means. Before the correction this path produced
    "cannot assign without an override", inviting the caller to supply one."""
    _stub_environment(monkeypatch, blocked=True)

    with pytest.raises(InvalidOperationError) as caught:
        assign_member(session, actor=head, requirement=requirement, membership=john)
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in str(caught.value)


def test_36c_the_conflict_refuses_even_alongside_a_genuine_overridable_blocker(
    session, head, requirement, john, monkeypatch
):
    """A reason that legitimately covers something else still does not cover
    this. The absolute rules run first, so the placement never reaches the
    catalogue that would have accepted the reason."""
    _stub_environment(monkeypatch, blocked=True, qualified=False)

    with pytest.raises(InvalidOperationError) as caught:
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="They are the only one free.",
        )
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in str(caught.value)


def test_36d_the_conflict_is_asked_by_canonical_person_not_by_name(
    session, head, requirement, john, monkeypatch
):
    """The rule is about one human, so the query is keyed by ``person_id``
    reached through the membership -- never a display name or an address, which
    two different humans may share or one human may change."""
    seen: dict = {}

    def _fake_conflicts(session_, *, person_id, conflict_date, target_ministry_id):
        seen.update(
            person_id=person_id,
            conflict_date=conflict_date,
            target_ministry_id=target_ministry_id,
        )
        return SimpleNamespace(is_blocked=False)

    _stub_environment(monkeypatch)
    monkeypatch.setattr(
        _assignment_module(), "get_person_sunday_conflicts", _fake_conflicts
    )
    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen["person_id"] == john.person_id
    # The immutable snapshot date, and this ministry excluded -- two of this
    # ministry's own events on one day are this ministry's business.
    assert seen["conflict_date"] == requirement.event_date
    assert seen["target_ministry_id"] == requirement.ministry_id


def test_37_override_can_bypass_capacity(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, current_count=1)  # required_count defaults to 1

    result = assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)


def test_38_override_cannot_bypass_deactivated_membership(session, head, setup_ministry, requirement, monkeypatch):
    # A genuine overridable blocker (qualified=None) is present too, so the
    # override_reason is legitimately "needed" for something -- otherwise the
    # unrelated "unnecessary override" rule would raise first and mask
    # whether the deactivated-membership check was actually bypassed.
    _stub_environment(monkeypatch, qualified=None)
    inactive = _target_membership(118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=inactive, override_reason="Reason.")


def test_39_override_cannot_bypass_deactivated_person(session, head, setup_ministry, requirement, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)  # see test 38's comment
    departed = _person(42, "John", deactivated=True)
    membership = _target_membership(118, person=departed, ministry=setup_ministry)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=membership, override_reason="Reason.")


def test_40_override_cannot_bypass_cancelled_event(session, head, setup_ministry, setup_lead, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)  # see test 38's comment
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    version = _version(500)
    req = _requirement(600, version=version, event=cancelled_event, role=setup_lead)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john, override_reason="Reason.")


def test_41_override_cannot_bypass_version_immutability(session, head, sunday_event, setup_lead, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)  # see test 38's comment
    finalized = _version(500, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    req = _requirement(600, version=finalized, event=sunday_event, role=setup_lead)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=req, membership=john, override_reason="Reason.")


def test_42_override_cannot_bypass_ministry_mismatch(session, head, requirement, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)  # see test 38's comment
    av_ministry = _ministry(4, "AV")
    av_member = _target_membership(118, person=_person(42, "John"), ministry=av_ministry)

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=av_member, override_reason="Reason.")


def test_43_override_cannot_bypass_duplicate_person_same_event_version(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, duplicate_in_event=999, qualified=None)  # see test 38's comment

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")


def test_44_unnecessary_override_reason_is_rejected(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)  # nothing blocks

    with pytest.raises(InvalidOperationError):
        assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Not needed.")

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_45_overridden_assignment_stores_is_override_true_and_reason(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    result = assign_member(
        session, actor=head, requirement=requirement, membership=john, override_reason="Only option available.",
    )

    assert result.is_override is True
    assert result.override_reason == "Only option available."


def test_46_normal_assignment_stores_false_and_none(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert result.is_override is False
    assert result.override_reason is None


# --------------------------------------------------------------------------
# 47-54 -- Audit
# --------------------------------------------------------------------------


def test_47_normal_assignment_uses_assignment_added(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _one_audit_row(session).action == ACTION_ASSIGNMENT_ADDED


def test_48_override_uses_only_override_applied_not_a_second_audit(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == ACTION_ASSIGNMENT_OVERRIDE_APPLIED


def test_49_override_audit_reason_matches_override_reason(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Only option.")

    assert _one_audit_row(session).reason == "Only option."


def test_50_remove_uses_assignment_removed(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing)

    assert _one_audit_row(session).action == ACTION_ASSIGNMENT_REMOVED


def test_51_audit_target_id_is_actual_assignment_id(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _one_audit_row(session).target_id == result.id == 900


def test_52_audit_ministry_is_correct(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _one_audit_row(session).ministry_id == requirement.ministry_id == 3


def test_53_audit_summary_uses_snapshot_event_date(session, head, setup_ministry, setup_lead, john, monkeypatch):
    moved_event = _event(700, ministry=setup_ministry)
    moved_event.event_date = datetime.date(2026, 11, 22)
    version = _version(500)
    req = _requirement(600, version=version, event=moved_event, role=setup_lead, event_date=datetime.date(2026, 10, 4))
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=req, membership=john)

    audit = _one_audit_row(session)
    assert "2026-10-04" in audit.summary
    assert "2026-11-22" not in audit.summary


def test_54_normal_assignment_has_no_audit_reason(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _one_audit_row(session).reason is None


def test_add_audit_after_values_are_correct(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    result = assign_member(session, actor=head, requirement=requirement, membership=john)
    audit = _one_audit_row(session)

    assert audit.before_values is None
    assert audit.after_values == {
        "schedule_version_requirement_id": 600,
        "ministry_membership_id": 118,
        "schedule_version_id": 500,
        "event_id": 700,
        "is_override": False,
        "override_reason": None,
    }
    assert "overridden_blockers" not in audit.after_values


def test_override_audit_records_the_single_blocker_bypassed(session, head, requirement, john, monkeypatch):
    # Exactly one overridable rule is violated (not_qualified); the audit
    # payload must name exactly that one, not the whole blocker vocabulary.
    _stub_environment(monkeypatch, qualified=False)

    assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    audit = _one_audit_row(session)
    assert audit.after_values["overridden_blockers"] == [_BLOCKER_NOT_QUALIFIED]


def test_override_audit_records_every_blocker_bypassed(
    session, head, setup_ministry, sunday_event, john, monkeypatch
):
    # All four overridable rules violated at once: role_deactivated (via a
    # deactivated role on the requirement) plus not_qualified, unavailable and
    # capacity_full (via _stub_environment). The audit payload must name every
    # one of them, not just the first found.
    #
    # ``blocked`` is deliberately left False: the cross-ministry conflict is
    # absolute since Task 79's correction, so a conflicting pair never reaches
    # this catalogue at all -- and could not be recorded as bypassed, because
    # it would not have been.
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=inactive_role)
    _stub_environment(
        monkeypatch,
        qualified=False,
        availability_state=AVAILABILITY_UNAVAILABLE,
        current_count=1,  # required_count defaults to 1
    )

    assign_member(session, actor=head, requirement=req, membership=john, override_reason="Reason.")

    audit = _one_audit_row(session)
    assert audit.after_values["overridden_blockers"] == [
        _BLOCKER_CAPACITY_FULL,
        _BLOCKER_NOT_QUALIFIED,
        _BLOCKER_ROLE_DEACTIVATED,
        _BLOCKER_UNAVAILABLE,
    ]
    assert _BLOCKER_SUNDAY_CONFLICT not in audit.after_values["overridden_blockers"]


def test_override_audit_blocker_ordering_is_deterministic_not_insertion_order(
    session, head, setup_ministry, sunday_event, john, monkeypatch
):
    # role_deactivated is computed (and would be inserted into the internal
    # set) before capacity_full inside _collect_overridable_blockers, but the
    # stored payload must be alphabetically sorted, not insertion-ordered:
    # "capacity_full" < "role_deactivated".
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=inactive_role)
    _stub_environment(monkeypatch, current_count=1)  # required_count defaults to 1

    assign_member(session, actor=head, requirement=req, membership=john, override_reason="Reason.")

    audit = _one_audit_row(session)
    assert audit.after_values["overridden_blockers"] == [_BLOCKER_CAPACITY_FULL, _BLOCKER_ROLE_DEACTIVATED]


def test_normal_assignment_audit_never_claims_overridden_blockers(session, head, requirement, john, monkeypatch):
    # No override_reason supplied and nothing blocks -- ACTION_ASSIGNMENT_ADDED
    # is recorded, and its payload must not carry a misleading
    # "overridden_blockers" key (present-but-empty or otherwise).
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    audit = _one_audit_row(session)
    assert audit.action == ACTION_ASSIGNMENT_ADDED
    assert "overridden_blockers" not in audit.after_values


def test_override_still_writes_exactly_one_audit_row(session, head, requirement, john, monkeypatch):
    # Recording overridden_blockers must not introduce a second AuditEvent --
    # ACTION_ASSIGNMENT_OVERRIDE_APPLIED remains the single audit action.
    _stub_environment(monkeypatch, qualified=False)

    assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == ACTION_ASSIGNMENT_OVERRIDE_APPLIED


def test_override_assignment_row_does_not_store_overridden_blockers(session, head, requirement, john, monkeypatch):
    # overridden_blockers lives only in the audit payload -- never as an
    # attribute or column on the Assignment model itself.
    _stub_environment(monkeypatch, qualified=False)

    result = assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert isinstance(result, Assignment)
    assert not hasattr(result, "overridden_blockers")
    assert result.is_override is True
    assert result.override_reason == "Reason."


def test_assign_summary_matches_the_reviewed_example(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert _one_audit_row(session).summary == "Assigned John to Setup Lead for Setup on 2026-10-04"


def test_assign_summary_with_override_names_it(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch, qualified=None)

    assign_member(session, actor=head, requirement=requirement, membership=john, override_reason="Reason.")

    assert _one_audit_row(session).summary == "Assigned John to Setup Lead for Setup on 2026-10-04 with override"


# --------------------------------------------------------------------------
# 55-62 -- Removal
# --------------------------------------------------------------------------


def test_55_admin_may_remove(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing)

    assert existing in session.deleted_objects


def test_55b_own_head_may_remove(session, setup_ministry, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)
    head = _person(9, "Head Person")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)

    remove_assignment(session, actor=head, assignment=existing)

    assert existing in session.deleted_objects


def test_56_wrong_head_is_rejected(session, requirement, john):
    existing = _existing_assignment(800, requirement=requirement, membership=john)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _actor_membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        remove_assignment(session, actor=av_head, assignment=existing)

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_57_removal_allowed_despite_inactive_member_person_role(
    session, head, setup_ministry, sunday_event, john, monkeypatch
):
    _stub_environment(monkeypatch)
    inactive_role = _role(12, "Setup Lead", ministry=setup_ministry, deactivated=True)
    version = _version(500)
    req = _requirement(600, version=version, event=sunday_event, role=inactive_role)
    departed = _person(42, "John", deactivated=True)
    inactive_membership = _target_membership(118, person=departed, ministry=setup_ministry, deactivated=True)
    existing = _existing_assignment(800, requirement=req, membership=inactive_membership)

    remove_assignment(session, actor=head, assignment=existing)

    assert existing in session.deleted_objects


def test_58_removal_rejected_on_immutable_version(session, head, sunday_event, setup_lead, john):
    finalized = _version(500, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    req = _requirement(600, version=finalized, event=sunday_event, role=setup_lead)
    existing = _existing_assignment(800, requirement=req, membership=john)

    with pytest.raises(InvalidOperationError):
        remove_assignment(session, actor=head, assignment=existing)

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_59_session_delete_is_called(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing)

    assert session.deleted_objects == [existing]


def test_60_removal_uses_no_unnecessary_flush(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing)

    assert session.flush_calls == 0


def test_61_optional_removal_reason_is_recorded(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing, reason="Person withdrew.")

    assert _one_audit_row(session).reason == "Person withdrew."


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_62_whitespace_only_removal_reason_is_rejected(session, head, requirement, john, blank):
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    with pytest.raises(InvalidOperationError):
        remove_assignment(session, actor=head, assignment=existing, reason=blank)

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_removal_audit_before_values_are_correct(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john, is_override=True, override_reason="X.")

    remove_assignment(session, actor=head, assignment=existing)
    audit = _one_audit_row(session)

    assert audit.before_values == {
        "schedule_version_requirement_id": 600,
        "ministry_membership_id": 118,
        "schedule_version_id": 500,
        "event_id": 700,
        "is_override": True,
        "override_reason": "X.",
    }
    assert audit.after_values is None


def test_remove_summary_matches_the_reviewed_example(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    existing = _existing_assignment(800, requirement=requirement, membership=john)

    remove_assignment(session, actor=head, assignment=existing)

    assert _one_audit_row(session).summary == "Removed John from Setup Lead for Setup on 2026-10-04"


# --------------------------------------------------------------------------
# 63-67 -- Transaction / query behavior
# --------------------------------------------------------------------------


def test_63_new_assignment_flushes_once_for_identity(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert session.flush_calls == 1


def test_64_idempotent_path_has_no_flush_or_audit(session, head, requirement, john, monkeypatch):
    existing = _existing_assignment(800, requirement=requirement, membership=john)
    _stub_environment(monkeypatch, exact_assignment=existing)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert session.flush_calls == 0
    assert _audit_rows(session) == []


def test_65_service_never_commits(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    assign_member(session, actor=head, requirement=requirement, membership=john)
    assert session.commit_calls == 0


def test_66_service_never_rolls_back(session, head, requirement, john, monkeypatch):
    _stub_environment(monkeypatch)
    assign_member(session, actor=head, requirement=requirement, membership=john)
    assert session.rollback_calls == 0


def test_67_same_session_used_for_conflict_lookup_and_mutation(session, head, requirement, john, monkeypatch):
    import app.services.assignment as module

    _stub_environment(monkeypatch)
    seen = []

    def fake_conflicts(session_arg, *, person_id, conflict_date, target_ministry_id):
        seen.append(session_arg)
        return SundayConflictResult(existing_commitments=(), authoritative_assignments=())

    monkeypatch.setattr(module, "get_person_sunday_conflicts", fake_conflicts)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen == [session]


# --------------------------------------------------------------------------
# 68-70 -- Queries, tested without any Session or database at all
# --------------------------------------------------------------------------


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_68_latest_version_statement_checks_newer_version_for_same_schedule():
    stmt = _newer_version_exists_statement(500, 1)
    compiled = _compile(stmt)

    assert "schedule_version.schedule_id = 500" in compiled
    assert "schedule_version.version_number > 1" in compiled


def test_69_capacity_query_scoped_to_requirement_id():
    stmt = _assignment_count_statement(600)
    compiled = _compile(stmt)

    assert "assignment.schedule_version_requirement_id = 600" in compiled
    assert "count(assignment.id)" in compiled


def test_70_duplicate_person_query_scoped_to_version_event_membership():
    stmt = _duplicate_membership_in_event_statement(500, 700, 118)
    compiled = _compile(stmt)

    assert "assignment.schedule_version_id = 500" in compiled
    assert "assignment.event_id = 700" in compiled
    assert "assignment.ministry_membership_id = 118" in compiled


def test_exact_assignment_statement_scoped_to_requirement_and_membership():
    stmt = _exact_assignment_lookup_statement(600, 118)
    compiled = _compile(stmt)

    assert "assignment.schedule_version_requirement_id = 600" in compiled
    assert "assignment.ministry_membership_id = 118" in compiled


def test_qualification_statement_scoped_correctly():
    stmt = _qualification_lookup_statement(118, 12)
    compiled = _compile(stmt)

    assert "role_qualification.ministry_membership_id = 118" in compiled
    assert "role_qualification.ministry_role_id = 12" in compiled


def test_availability_statement_scoped_correctly():
    stmt = _availability_lookup_statement(118, 700)
    compiled = _compile(stmt)

    assert "availability.ministry_membership_id = 118" in compiled
    assert "availability.event_id = 700" in compiled


# --------------------------------------------------------------------------
# 20-24 -- the person-period serving maximum (Task 47)
#
# Non-overridable, and enforced here as an *absolute* check: the maximum
# records what a volunteer said they could manage, so raising it is a decision
# made with that person through ``set_serving_limit``, not something a head
# works around from this side.
# --------------------------------------------------------------------------


def test_20_an_assignment_below_the_maximum_succeeds(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(monkeypatch, serving_limit=4, assignments_in_version=2)

    result = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert isinstance(result, Assignment)
    assert result.is_override is False


def test_21_the_assignment_that_exactly_reaches_the_maximum_succeeds(
    session, head, requirement, john, monkeypatch
):
    # Three already held against a maximum of four: this is the fourth, and
    # the rule is "at most four", not "fewer than four".
    _stub_environment(monkeypatch, serving_limit=4, assignments_in_version=3)

    result = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert isinstance(result, Assignment)


def test_22_the_assignment_that_would_exceed_the_maximum_is_rejected(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(monkeypatch, serving_limit=4, assignments_in_version=4)

    with pytest.raises(InvalidOperationError, match="serving maximum"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_23_an_override_reason_cannot_bypass_the_serving_maximum(
    session, head, requirement, john, monkeypatch
):
    """The load-bearing rule of Task 47.

    ``override_reason`` bypasses exactly the bounded overridable blockers and does
    nothing at all to this one. It is refused even when a genuine overridable
    blocker is *also* present, so a head cannot reach the maximum by way of
    something else that was legitimately overridable.
    """
    _stub_environment(monkeypatch, serving_limit=2, assignments_in_version=2)

    with pytest.raises(InvalidOperationError, match="not overridable"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="The head really needs them this Sunday",
        )

    assert _audit_rows(session) == []


def test_23b_the_maximum_is_refused_even_alongside_an_overridable_blocker(
    session, head, requirement, john, monkeypatch
):
    # Unavailable *and* at their maximum. The unavailability is overridable;
    # the maximum is not, and it is checked first, so the call still fails.
    _stub_environment(
        monkeypatch, serving_limit=2, assignments_in_version=2,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    with pytest.raises(InvalidOperationError, match="serving maximum"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="Confirmed with them directly",
        )


def test_23c_the_serving_maximum_is_not_in_the_overridable_catalogue():
    """It must never be mapped into Task 22's bounded blocker vocabulary.

    Those codes are persisted in audit history as the authorization for an
    override; adding this one would make an individual's stated limit the
    weakest rule in the system rather than the firmest.
    """
    from app.services.assignment_policy import (
        BLOCKER_DESCRIPTIONS,
        OVERRIDABLE_BLOCKERS,
    )

    for code in OVERRIDABLE_BLOCKERS:
        assert "serving" not in code
        assert "limit" not in code
        assert "maximum" not in BLOCKER_DESCRIPTIONS[code]


def test_24_the_count_is_scoped_to_this_schedule_version(
    session, head, requirement, john, monkeypatch
):
    """A predecessor version's assignments are history, not commitments.

    A successor that rebuilt the same period would otherwise read one
    quarter's four Sundays as eight and refuse work nobody is doing.
    """
    import app.services.assignment as module

    seen: list[dict] = []

    def recording_count(session, *, schedule_version_id, ministry_membership_id):
        seen.append(
            {
                "schedule_version_id": schedule_version_id,
                "ministry_membership_id": ministry_membership_id,
            }
        )
        return 0

    _stub_environment(monkeypatch, serving_limit=4)
    monkeypatch.setattr(module, "_count_assignments_in_version", recording_count)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen == [
        {
            "schedule_version_id": requirement.schedule_version_id,
            "ministry_membership_id": john.id,
        }
    ]


def test_24b_the_count_statement_filters_on_version_and_membership():
    """The SQL itself, compiled and inspected -- no session, no database."""
    from sqlalchemy.dialects import postgresql

    from app.services.assignment import _count_assignments_in_version_statement

    sql = str(
        _count_assignments_in_version_statement(500, 200).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "assignment.schedule_version_id = 500" in sql
    assert "assignment.ministry_membership_id = 200" in sql


def test_24c_no_configured_maximum_means_no_restriction(
    session, head, requirement, john, monkeypatch
):
    # Absence of a limit is the ordinary case and must not behave like zero.
    _stub_environment(monkeypatch, serving_limit=None, assignments_in_version=99)

    result = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert isinstance(result, Assignment)


# ==========================================================================
# 24-29 (Task 50) -- the linked-pair same-date exclusion
#
# Non-overridable, and enforced on the *calendar date* rather than the event.
# The rule itself lives in :mod:`app.services.same_date_exclusion`; what these
# tests pin is that ``assign_member`` consults it, refuses when it bites, and
# that no ``override_reason`` reaches it.
#
# Every person here is synthetic, and nothing in these tests says -- or could
# say -- why two members are linked.
# ==========================================================================


def _new_assignments(session: Session) -> list[Assignment]:
    """Assignment rows this call added, so a refusal can be shown to have
    written nothing at all."""
    return [obj for obj in session.new if isinstance(obj, Assignment)]


def _assignment_module():
    """The service module itself, for the two tests that patch a name
    ``_stub_environment`` deliberately leaves alone."""
    import app.services.assignment as module

    return module


@pytest.fixture
def linked_partner(setup_ministry: Ministry) -> MinistryMembership:
    """A second Setup member, linked to ``john`` by a same-date exclusion."""
    return _target_membership(
        119, person=_person(43, "Volunteer B"), ministry=setup_ministry
    )


def test_24_assigning_the_second_linked_member_on_the_same_date_is_rejected(
    session, head, requirement, john, monkeypatch
):
    """A is already on this date; B is refused."""
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({119}),
        # The lookup found a linked member's assignment on this date.
        linked_assignment_on_date=901,
    )

    with pytest.raises(
        InvalidOperationError, match="SAME_DATE_LINKED_MEMBER_CONFLICT"
    ):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )
    assert not _new_assignments(session)
    assert not _audit_rows(session)


def test_25_the_rule_is_symmetric_whichever_member_was_assigned_first(
    session, head, requirement, linked_partner, monkeypatch
):
    """The same refusal from the other side.

    ``get_linked_membership_ids`` is asked about the member being assigned and
    answers from either half of the pair, so which of the two was assigned
    first cannot matter.
    """
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({118}),
        linked_assignment_on_date=902,
    )

    with pytest.raises(
        InvalidOperationError, match="SAME_DATE_LINKED_MEMBER_CONFLICT"
    ):
        assign_member(
            session, actor=head, requirement=requirement, membership=linked_partner
        )
    assert not _new_assignments(session)


def test_26_the_linked_pair_may_be_assigned_on_different_dates(
    session, head, requirement, john, monkeypatch
):
    """The rule removes a coincidence, not either person's eligibility.

    The linked member holds an assignment, but not on this requirement's date,
    so the date lookup finds nothing and the assignment proceeds normally.
    """
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({119}),
        linked_assignment_on_date=None,
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118
    assert assignment.is_override is False


def test_27_the_lookup_is_by_snapshot_date_so_two_events_on_one_date_conflict():
    """The query a "same event" implementation would not have written.

    It joins ``schedule_version_requirement`` for its immutable snapshot
    ``event_date`` and filters on that -- never on ``event_id`` -- which is
    exactly what makes a morning and an evening service on one Sunday one
    date.
    """
    from app.services.assignment import _linked_assignment_on_date_statement

    sql = _compile(
        _linked_assignment_on_date_statement(
            500, datetime.date(2026, 10, 18), [119]
        )
    )
    assert "schedule_version_requirement.event_date = '2026-10-18'" in sql
    assert "assignment.schedule_version_id = 500" in sql
    assert "assignment.ministry_membership_id IN (119)" in sql
    # Scoped to the date, never to one event: the whole point of the rule.
    assert "assignment.event_id" not in sql


def test_27b_the_date_lookup_is_scoped_to_this_version_only():
    """A predecessor's assignments are history, not commitments in force."""
    from app.services.assignment import _linked_assignment_on_date_statement

    sql = _compile(
        _linked_assignment_on_date_statement(
            500, datetime.date(2026, 10, 18), [119]
        )
    )
    assert "assignment.schedule_version_id = 500" in sql


def test_28_an_override_reason_cannot_bypass_the_linked_pair_rule(
    session, head, requirement, john, monkeypatch
):
    """Non-overridable, and it must not even *look* overridable.

    The rejection names the pair rule rather than complaining about an
    override that authorized nothing, and no ``Assignment`` is created.
    """
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({119}),
        linked_assignment_on_date=901,
        # A genuine overridable blocker is also present, so this is the
        # sharpest case: the reason is legitimate for that blocker and still
        # does nothing here.
        qualified=False,
    )

    with pytest.raises(
        InvalidOperationError, match="SAME_DATE_LINKED_MEMBER_CONFLICT"
    ):
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="The head accepts the risk.",
        )
    assert not _new_assignments(session)
    assert not _audit_rows(session)


def test_28b_the_rule_is_checked_before_the_overridable_blockers_are_gathered(
    session, head, requirement, john, monkeypatch
):
    """Ordering, asserted through the message.

    The pair rule sits with the absolute checks, so its rejection is what a
    caller sees even when overridable blockers also apply -- rather than a
    message about qualifications that a reason could have resolved.
    """
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({119}),
        linked_assignment_on_date=901,
        qualified=False,
        blocked=True,
    )

    with pytest.raises(InvalidOperationError) as exc_info:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )
    assert "SAME_DATE_LINKED_MEMBER_CONFLICT" in str(exc_info.value)
    assert "cannot assign without an override" not in str(exc_info.value)


def test_28c_the_rejection_says_the_rule_is_not_overridable_and_how_to_change_it(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        linked_membership_ids=frozenset({119}),
        linked_assignment_on_date=901,
    )

    with pytest.raises(InvalidOperationError) as exc_info:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )
    message = str(exc_info.value)
    assert "not overridable" in message
    assert "set_same_date_exclusion" in message
    # And it never says why the two are linked, because nothing knows.
    for word in ("spouse", "husband", "wife", "couple", "household", "family"):
        assert word not in message.lower()


def test_29_a_volunteer_with_no_pair_rule_is_unaffected(
    session, head, requirement, john, monkeypatch
):
    """The ordinary case: no rule configured, so no question is asked.

    ``get_linked_membership_ids`` returns an empty set and the date lookup is
    never reached at all.
    """
    called: list = []

    def never(*args, **kwargs):  # pragma: no cover - must not run
        called.append(kwargs)
        return 901

    _stub_environment(monkeypatch, linked_membership_ids=frozenset())
    monkeypatch.setattr(
        _assignment_module(), "_find_linked_assignment_on_date", never
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118
    assert called == []


def test_29b_the_pair_lookup_is_scoped_to_the_versions_own_period(
    session, head, requirement, john, monkeypatch
):
    """A rule belongs to one period and expires with it."""
    seen: list[dict] = []
    _stub_environment(monkeypatch)
    monkeypatch.setattr(
        _assignment_module(),
        "get_linked_membership_ids",
        lambda session, *, ministry_membership_id, scheduling_period_id: (
            seen.append(
                {
                    "ministry_membership_id": ministry_membership_id,
                    "scheduling_period_id": scheduling_period_id,
                }
            )
            or frozenset()
        ),
    )

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen == [
        {
            "ministry_membership_id": 118,
            # The period the requirement's version belongs to.
            "scheduling_period_id": requirement.schedule_version.scheduling_period_id,
        }
    ]


def test_29c_the_pair_code_is_absent_from_the_overridable_catalogue():
    """Structural, not behavioural: the code is not in the bypassable set."""
    from app.services.assignment_policy import OVERRIDABLE_BLOCKERS
    from app.services.same_date_exclusion import SAME_DATE_LINKED_MEMBER_CONFLICT

    assert SAME_DATE_LINKED_MEMBER_CONFLICT not in OVERRIDABLE_BLOCKERS


# ==========================================================================
# 30-38 -- The ministry event-gap rule (Task 71)
#
# Manual assignment's half of the rule: absolute, non-overridable, and read
# from the same sequence the solver and the finalization gate use. The rule's
# own meaning is pinned in ``tests/test_services_event_gap.py``; what these
# tests pin is that ``assign_member`` reaches it, refuses when it bites, and
# does not otherwise change.
# ==========================================================================


SEP_27 = datetime.date(2026, 9, 27)
OCT_4 = datetime.date(2026, 10, 4)
OCT_11 = datetime.date(2026, 10, 11)


def _gap_sequence(gap: int = 1, *, adjacent=None):
    """A sequence whose middle event, 700, is the one the fixtures assign to.

    699 sits before the period and 701 after it, so both boundaries and both
    directions of the rule are reachable from one shape. ``adjacent`` is the
    authoritative commitments at those two outside events -- the half of
    presence the caller does *not* supply.
    """
    from app.services.event_gap import MinistryEventSequence

    return MinistryEventSequence(
        min_intervening_events=gap,
        events=((699, SEP_27), (700, OCT_4), (701, OCT_11)),
        adjacent_events_by_membership=adjacent or {},
    )


def test_30_no_configured_rule_leaves_assignment_unchanged(
    session, head, requirement, john, monkeypatch
):
    """The legacy path: no rule, so the sequence is never even loaded and the
    assignment proceeds exactly as it did before Task 71.
    """
    asked: list[dict] = []
    _stub_environment(monkeypatch, event_gap_sequence=None)
    monkeypatch.setattr(
        _assignment_module(),
        "_assigned_event_ids_in_version",
        lambda session, **kwargs: asked.append(kwargs) or frozenset(),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118
    assert assignment.is_override is False
    # The membership's own assignments are not read when there is no rule.
    assert asked == []


def test_31_serving_the_previous_event_refuses_the_next_one(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(),
        # John already serves event 699 -- the event immediately before this
        # requirement's own.
        assigned_event_ids=frozenset({699}),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )
    assert not _new_assignments(session)
    assert not _audit_rows(session)


def test_32_serving_the_following_event_refuses_this_one_too(
    session, head, requirement, john, monkeypatch
):
    """The rule is symmetric. Filling a schedule backwards must be refused just
    as squarely as filling it forwards.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(),
        assigned_event_ids=frozenset({701}),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )


def test_33_an_event_outside_the_gap_does_not_refuse(
    session, head, requirement, john, monkeypatch
):
    """Serving two events with the required number between them is exactly what
    the rule permits, and it must not be mistaken for a violation.
    """
    from app.services.event_gap import MinistryEventSequence

    sequence = MinistryEventSequence(
        min_intervening_events=1,
        events=((698, SEP_27), (699, OCT_4), (700, OCT_11)),
    )
    _stub_environment(
        monkeypatch,
        event_gap_sequence=sequence,
        assigned_event_ids=frozenset({698}),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_34_history_before_the_period_refuses_its_first_event(
    session, head, requirement, john, monkeypatch
):
    """The boundary case. John holds no assignment in this version at all --
    the block comes entirely from the authoritative schedule of the event
    before the period, carried on the sequence itself.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(adjacent={118: frozenset({699})}),
        assigned_event_ids=frozenset(),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )


def test_35_history_blocks_only_the_person_who_served(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        # Somebody else served the preceding event.
        event_gap_sequence=_gap_sequence(adjacent={119: frozenset({699})}),
        assigned_event_ids=frozenset(),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_36_an_override_reason_cannot_bypass_the_gap_rule(
    session, head, requirement, john, monkeypatch
):
    """Non-overridable, and it must not even *look* overridable: the rejection
    names the gap rule rather than complaining about an override that
    authorized nothing.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(),
        assigned_event_ids=frozenset({699}),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="The head asked for it",
        )
    assert not _new_assignments(session)


def test_37_the_gap_code_is_absent_from_the_overridable_catalogue():
    """Structural, not behavioural: the code is not in the bypassable set."""
    from app.services.assignment_policy import OVERRIDABLE_BLOCKERS
    from app.services.event_gap import MIN_EVENT_GAP_CONFLICT

    assert MIN_EVENT_GAP_CONFLICT not in OVERRIDABLE_BLOCKERS


def test_37b_the_refusal_names_the_clashing_date_and_the_rule(
    session, head, requirement, john, monkeypatch
):
    """A head reading the message needs to know which assignment is in the way
    and what the rule actually is -- and must not be told anything about days.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(),
        assigned_event_ids=frozenset({699}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    message = str(raised.value)
    assert SEP_27.isoformat() in message
    assert "consecutive events" in message
    assert "set_min_intervening_events" in message


def test_38_the_sequence_is_loaded_for_this_version_period_and_ministry(
    session, head, requirement, john, monkeypatch
):
    """The lookup is scoped three ways, and none of them is optional: another
    ministry's events are a different sequence, and another period's rule is a
    different rule.
    """
    seen: list[dict] = []
    _stub_environment(monkeypatch)
    monkeypatch.setattr(
        _assignment_module(),
        "load_event_gap_sequence",
        lambda session, **kwargs: seen.append(kwargs) or None,
    )

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen == [
        {
            "scheduling_period_id": requirement.schedule_version.scheduling_period_id,
            "ministry_id": requirement.ministry_id,
            "schedule_version_id": requirement.schedule_version.id,
        }
    ]


def test_38b_the_version_presence_lookup_is_scoped_to_this_version_only():
    """A predecessor's assignments are history, not commitments in force -- the
    same scoping the serving count and the pair date lookup use.
    """
    from app.services.assignment import _assigned_event_ids_in_version_statement

    sql = _compile(_assigned_event_ids_in_version_statement(500, 118))
    assert "assignment.schedule_version_id = 500" in sql
    assert "assignment.ministry_membership_id = 118" in sql
    assert "assignment.event_id" in sql


def test_34b_a_commitment_after_the_period_refuses_its_last_event(
    session, head, requirement, john, monkeypatch
):
    """The forward boundary. John holds no assignment in this version at all --
    the block comes entirely from the authoritative schedule of the event
    *after* the period, carried on the sequence itself.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(adjacent={118: frozenset({701})}),
        assigned_event_ids=frozenset(),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )


def test_34c_a_commitment_after_the_period_blocks_only_that_person(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(adjacent={119: frozenset({701})}),
        assigned_event_ids=frozenset(),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_34d_a_commitment_beyond_the_gap_on_either_side_does_not_refuse(
    session, head, requirement, john, monkeypatch
):
    """Five events, with the requirement's own in the middle and the
    commitments two positions away on each side. A gap of one reaches neither.
    """
    from app.services.event_gap import MinistryEventSequence

    sequence = MinistryEventSequence(
        min_intervening_events=1,
        events=(
            (697, SEP_27 - datetime.timedelta(days=14)),
            (699, SEP_27),
            (700, OCT_4),
            (701, OCT_11),
            (703, OCT_11 + datetime.timedelta(days=7)),
        ),
        adjacent_events_by_membership={118: frozenset({697, 703})},
    )
    _stub_environment(
        monkeypatch, event_gap_sequence=sequence, assigned_event_ids=frozenset()
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_34e_both_boundaries_are_consulted_in_the_same_call(
    session, head, requirement, john, monkeypatch
):
    """Committed on both sides at once: the refusal names the nearer clash, and
    which side that is does not change the outcome.
    """
    _stub_environment(
        monkeypatch,
        event_gap_sequence=_gap_sequence(adjacent={118: frozenset({699, 701})}),
        assigned_event_ids=frozenset(),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    message = str(raised.value)
    assert "MIN_EVENT_GAP_CONFLICT" in message
    # Both are one event away; the earlier of the two is reported, and either
    # would be a true statement about the same rule.
    assert SEP_27.isoformat() in message or OCT_11.isoformat() in message


# ==========================================================================
# 39-46 -- The member-group per-event cap (Task 74)
#
# Manual assignment applies the rule; its own meaning is pinned in
# ``tests/test_services_member_group.py``, and the solver's version in
# ``tests/test_scheduling_member_group_cap.py``. What these assert is that
# ``assign_member`` refuses the placements the rule forbids, allows the ones it
# does not, and never lets an ``override_reason`` past it.
# ==========================================================================


def _cap(max_per_event: int, members, *, name: str = "Category A"):
    from app.services.member_group import MemberGroupCapConfig

    return MemberGroupCapConfig(
        member_group_id=400, member_group_name=name,
        max_per_event=max_per_event, member_membership_ids=frozenset(members),
    )


def test_39_no_configured_cap_leaves_assignment_unchanged(
    session, head, requirement, john, monkeypatch
):
    """The legacy-behaviour guarantee: an empty cap tuple asks nothing and
    refuses nothing -- and the event's roster is never even read.
    """
    _stub_environment(monkeypatch, member_group_caps=())

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_40_a_cap_with_room_left_allows_the_placement(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        member_group_caps=(_cap(2, (118, 119)),),
        event_roster=frozenset({119}),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_41_a_cap_already_reached_refuses_the_placement(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        member_group_caps=(_cap(1, (118, 119)),),
        event_roster=frozenset({119}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    message = str(raised.value)
    assert "MEMBER_GROUP_EVENT_LIMIT_CONFLICT" in message
    assert "Category A" in message
    assert "not overridable" in message


def test_42_the_cap_is_not_overridable(
    session, head, requirement, john, monkeypatch
):
    """An ``override_reason`` bypasses exactly the bounded overridable blockers, and this
    is not one of them: the refusal is identical with one supplied.
    """
    _stub_environment(
        monkeypatch,
        member_group_caps=(_cap(1, (118, 119)),),
        event_roster=frozenset({119}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="short-handed this Sunday",
        )

    assert "MEMBER_GROUP_EVENT_LIMIT_CONFLICT" in str(raised.value)
    assert _audit_rows(session) == []


def test_43_a_member_outside_every_capped_group_is_unaffected(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        member_group_caps=(_cap(1, (119, 120)),),
        event_roster=frozenset({119}),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_44_a_member_in_two_capped_groups_must_satisfy_both(
    session, head, requirement, john, monkeypatch
):
    from app.services.member_group import MemberGroupCapConfig

    wide = _cap(3, (118, 119))
    narrow = MemberGroupCapConfig(
        member_group_id=401, member_group_name="Category B",
        max_per_event=1, member_membership_ids=frozenset({118, 120}),
    )
    _stub_environment(
        monkeypatch,
        member_group_caps=(wide, narrow),
        event_roster=frozenset({119, 120}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    assert "Category B" in str(raised.value)


def test_45_the_count_is_of_people_on_this_event_not_the_whole_period(
    session, head, requirement, john, monkeypatch
):
    """The roster read is scoped to this requirement's own event, which is what
    makes this a per-event cap rather than a serving limit.
    """
    import app.services.assignment as module

    seen: list[dict] = []
    _stub_environment(
        monkeypatch,
        member_group_caps=(_cap(2, (118, 119)),),
        event_roster=frozenset({119}),
    )

    def recording(session, *, schedule_version_id, event_id):
        seen.append({"schedule_version_id": schedule_version_id, "event_id": event_id})
        return frozenset({119})

    monkeypatch.setattr(module, "_memberships_assigned_to_event", recording)

    assign_member(session, actor=head, requirement=requirement, membership=john)

    assert seen == [{"schedule_version_id": 500, "event_id": 700}]


def test_46_an_uncapped_member_never_causes_the_roster_to_be_read(
    session, head, requirement, john, monkeypatch
):
    """No capped group contains this member, so the rule has no question to
    ask and the extra round trip is not spent.
    """
    import app.services.assignment as module

    _stub_environment(monkeypatch, member_group_caps=(_cap(1, (119,)),))

    def exploding(session, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the roster must not be read for an uncapped member")

    monkeypatch.setattr(module, "_memberships_assigned_to_event", exploding)

    assign_member(session, actor=head, requirement=requirement, membership=john)


# ==========================================================================
# 47-54 -- The same-event support requirement (Task 74)
#
# Its own meaning is pinned in ``tests/test_services_same_event_support.py``
# and the solver's version in ``tests/test_scheduling_same_event_support.py``.
# What these assert is that ``assign_member`` applies the shared rule, in the
# right direction, against this event's own roster.
# ==========================================================================


def _support(minimum: int, supporters, *, subject: int = 118):
    from app.services.same_event_support import SupportRequirementConfig

    return SupportRequirementConfig(
        support_requirement_id=1, subject_membership_id=subject,
        min_supporters=minimum,
        supporter_membership_ids=frozenset(supporters),
    )


def test_47_no_configured_requirement_leaves_assignment_unchanged(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(monkeypatch, support_requirements=())

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_48_a_subject_without_support_on_this_event_is_refused(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(1, (119,)),),
        event_roster=frozenset({120}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    message = str(raised.value)
    assert "SAME_EVENT_SUPPORT_CONFLICT" in message
    assert "not overridable" in message
    assert "approved supporting member" in message


def test_49_a_subject_with_an_approved_supporter_present_is_allowed(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(1, (119,)),),
        event_roster=frozenset({119}),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_50_an_unrelated_member_on_the_roster_does_not_satisfy_it(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(1, (119,)),),
        event_roster=frozenset({121, 122, 123}),
    )

    with pytest.raises(InvalidOperationError, match="SAME_EVENT_SUPPORT_CONFLICT"):
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )


def test_51_the_requirement_is_not_overridable(
    session, head, requirement, john, monkeypatch
):
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(1, (119,)),),
        event_roster=frozenset(),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john,
            override_reason="they said they would find somebody",
        )

    assert "SAME_EVENT_SUPPORT_CONFLICT" in str(raised.value)
    assert _audit_rows(session) == []


def test_52_a_supporter_is_never_constrained_by_the_rule(
    session, head, requirement, john, monkeypatch
):
    """The rule is directional. John here is an approved supporter of somebody
    else, and assigning him is unaffected by whether that person is on the
    crew.
    """
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(1, (118,), subject=119),),
        event_roster=frozenset(),
    )

    assignment = assign_member(
        session, actor=head, requirement=requirement, membership=john
    )

    assert assignment.ministry_membership_id == 118


def test_53_a_requirement_nobody_can_satisfy_says_so_specifically(
    session, head, requirement, john, monkeypatch
):
    """Too few approved supporters and too few *rostered* supporters send a
    head to two different screens, so the message distinguishes them.
    """
    _stub_environment(
        monkeypatch,
        support_requirements=(_support(2, (119,)),),
        event_roster=frozenset({119}),
    )

    with pytest.raises(InvalidOperationError) as raised:
        assign_member(
            session, actor=head, requirement=requirement, membership=john
        )

    assert "fewer than the requirement asks for" in str(raised.value)


def test_54_an_unconstrained_member_never_causes_the_roster_to_be_read(
    session, head, requirement, john, monkeypatch
):
    import app.services.assignment as module

    _stub_environment(
        monkeypatch, support_requirements=(_support(1, (120,), subject=119),)
    )

    def exploding(session, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the roster must not be read for an unconstrained member")

    monkeypatch.setattr(module, "_memberships_assigned_to_event", exploding)

    assign_member(session, actor=head, requirement=requirement, membership=john)
