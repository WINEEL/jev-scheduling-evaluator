"""Assignment carry-forward tests (schedule-output §7, §8, §14; Task 22/28).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy.** This module is orchestration: it resolves lineage and the
corresponding requirement, then hands the real decision to
:func:`~app.services.assignment.assign_member`. The tests are split to match:

- **Lineage, target lifecycle and requirement mapping** run against real ORM
  objects with only the four lookup helpers monkeypatched, so the rules this
  module owns are exercised directly.
- **The hand-off** is proven by monkeypatching ``assign_member`` and recording
  exactly what it was called with -- the session, the resolved requirement,
  the membership, and (load-bearing) ``override_reason=None``.
- **Current validation** is proven by letting the *real* ``assign_member`` run
  against a stubbed environment, using the same ``_stub_environment`` shape
  Task 22's own suite established. Those tests confirm a rejection propagates
  rather than being swallowed; Task 22's suite remains the source of truth for
  the detail of each rule.

What only a real database can show -- that a genuine Task 22 override on
version 1 does not travel to version 2, and that repeated calls leave exactly
one row and one audit -- is proven in
``tests/integration/test_pg_assignment_carry_forward.py``.
"""

from __future__ import annotations

import datetime
import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SUNDAY_SERVICE,
    Event,
)
from app.services.assignment_carry_forward import (
    _membership_lookup_statement,
    _newer_version_exists_statement,
    _requirement_by_scheduling_identity_statement,
    _requirement_lookup_statement,
    _version_lookup_statement,
    carry_forward_assignment,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.audit import ACTION_ASSIGNMENT_ADDED
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.sunday_conflict import SundayConflictResult

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


class CarrySession(Session):
    """A real, unbound Session. ``flush`` is permitted -- Task 22 legitimately
    flushes once for the new assignment's identity -- and counted, so "no
    extra carry-forward flush" is checkable. ``commit``, ``rollback`` and
    ``delete`` are forbidden.
    """

    def __init__(self, *, next_id: int = 9000) -> None:
        super().__init__(autoflush=False)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.delete_calls = 0
        self.flush_calls = 0
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("carry-forward must never delete")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> CarrySession:
    return CarrySession()


def _person(person_id: int, name: str, *, is_admin: bool = False, deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


def _ministry(ministry_id: int = 3, name: str = "Setup") -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _membership(
    membership_id: int = 118, *, person: Person, ministry: Ministry, deactivated: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    if deactivated:
        membership.deactivated_at = DEACTIVATED
    return membership


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _role(
    role_id: int = 12, name: str = "Setup Lead", *, ministry: Ministry, deactivated: bool = False
) -> MinistryRole:
    role = MinistryRole(name=name, ministry_id=ministry.id)
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = DEACTIVATED
    return role


def _event(event_id: int = 700, *, ministry: Ministry, cancelled: bool = False) -> Event:
    event = Event(
        scheduling_period_id=11, ministry_id=ministry.id, event_date=NOV_15,
        event_kind=EVENT_KIND_SUNDAY_SERVICE,
    )
    event.id = event_id
    if cancelled:
        event.cancelled_at = DEACTIVATED
    return event


def _version(
    version_id: int, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
    amends_version_id: int | None = None,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status,
        finalized_at=FINALIZED_AT if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
        amends_version_id=amends_version_id,
        amendment_reason="Amending" if amends_version_id else None,
    )
    version.id = version_id
    return version


def _requirement(
    requirement_id: int, *, version: ScheduleVersion, event: Event, role: MinistryRole,
    event_date: datetime.date = NOV_15, required_count: int = 1,
) -> ScheduleVersionRequirement:
    requirement = ScheduleVersionRequirement(
        schedule_version_id=version.id, event_id=event.id, event_date=event_date,
        ministry_role_id=role.id, scheduling_period_id=11, ministry_id=role.ministry_id,
        required_count=required_count,
    )
    requirement.id = requirement_id
    requirement.schedule_version = version
    requirement.event = event
    requirement.ministry_role = role
    return requirement


def _assignment(
    assignment_id: int, *, requirement: ScheduleVersionRequirement,
    membership: MinistryMembership, is_override: bool = False,
    override_reason: str | None = None,
) -> Assignment:
    assignment = Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id,
        schedule_version_id=requirement.schedule_version_id,
        event_id=requirement.event_id, ministry_id=requirement.ministry_id,
        is_override=is_override, override_reason=override_reason,
    )
    assignment.id = assignment_id
    assignment.schedule_version_requirement = requirement
    assignment.ministry_membership = membership
    return assignment


class World:
    """V1 (finalized, one assignment) and V2 (its direct DRAFT successor)."""

    def __init__(
        self, *, v1_status: str = SCHEDULE_VERSION_STATUS_FINALIZED,
        target_event_date: datetime.date = NOV_15, target_required_count: int = 1,
        source_is_override: bool = False, target_role: MinistryRole | None = None,
        target_event: Event | None = None,
    ):
        self.ministry = _ministry()
        self.role = _role(ministry=self.ministry)
        self.event = _event(ministry=self.ministry)
        self.person = _person(42, "John")
        self.membership = _membership(person=self.person, ministry=self.ministry)

        self.v1 = _version(500, version_number=1, status=v1_status)
        self.source_requirement = _requirement(
            600, version=self.v1, event=self.event, role=self.role,
        )
        self.source_assignment = _assignment(
            900, requirement=self.source_requirement, membership=self.membership,
            is_override=source_is_override,
            override_reason="Head approved the conflict." if source_is_override else None,
        )

        self.v2 = _version(
            501, version_number=2, status=SCHEDULE_VERSION_STATUS_DRAFT,
            amends_version_id=self.v1.id,
        )
        self.target_requirement = _requirement(
            601, version=self.v2,
            event=target_event or self.event, role=target_role or self.role,
            event_date=target_event_date, required_count=target_required_count,
        )


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def head() -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _actor_membership(9001, person=person, ministry=_ministry(), is_head=True)
    return person


def _stub_lookups(
    monkeypatch, world: World, *, newer_version_exists: bool = False,
    target_requirement: ScheduleVersionRequirement | None = ...,
    source_version: ScheduleVersion | None = ...,
) -> list[tuple[str, object, dict]]:
    """Patch this module's four resolvers, recording every call so call order,
    scoping and "same session" claims can be asserted.
    """
    import app.services.assignment_carry_forward as module

    calls: list[tuple[str, object, dict]] = []
    resolved_source = world.v1 if source_version is ... else source_version
    resolved_target = (
        world.target_requirement if target_requirement is ... else target_requirement
    )

    def fake_version(session, version_id):
        calls.append(("version", session, {"version_id": version_id}))
        if resolved_source is None:
            raise InvalidOperationError("version could not be resolved")
        return resolved_source

    def fake_requirement(session, requirement_id):
        calls.append(("requirement", session, {"requirement_id": requirement_id}))
        return world.source_requirement

    def fake_match(session, *, schedule_version_id, event_id, ministry_role_id):
        calls.append(("match", session, {
            "schedule_version_id": schedule_version_id, "event_id": event_id,
            "ministry_role_id": ministry_role_id,
        }))
        if resolved_target is None:
            return None
        # Honour the scheduling identity the real query filters on, so a test
        # that passes a mismatched role/event still sees "no match".
        if (
            resolved_target.event_id != event_id
            or resolved_target.ministry_role_id != ministry_role_id
            or resolved_target.schedule_version_id != schedule_version_id
        ):
            return None
        return resolved_target

    def fake_membership(session, membership_id):
        calls.append(("membership", session, {"membership_id": membership_id}))
        return world.membership

    def fake_newer(session, *, schedule_id, version_number):
        calls.append(("newer", session, {
            "schedule_id": schedule_id, "version_number": version_number,
        }))
        return newer_version_exists

    monkeypatch.setattr(module, "_resolve_version", fake_version)
    monkeypatch.setattr(module, "_resolve_requirement", fake_requirement)
    monkeypatch.setattr(module, "_find_requirement_by_scheduling_identity", fake_match)
    monkeypatch.setattr(module, "_resolve_membership", fake_membership)
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    return calls


def _stub_assign_member(monkeypatch, *, result=None, raises: Exception | None = None):
    """Replace Task 22 with a recorder, to prove the hand-off arguments."""
    import app.services.assignment_carry_forward as module

    seen: list[tuple[object, dict]] = []

    def fake_assign(session, **kwargs):
        seen.append((session, kwargs))
        if raises is not None:
            raise raises
        return result if result is not None else SimpleNamespace(id=999)

    monkeypatch.setattr(module, "assign_member", fake_assign)
    return seen


def _stub_task22_environment(
    monkeypatch, *, qualified: bool | None = True, availability_state: str | None = None,
    blocked: bool = False, current_count: int = 0, exact_assignment=None,
    duplicate_in_event=None, newer_version_exists: bool = False,
    serving_limit: int | None = None, assignments_in_version: int = 0,
    linked_membership_ids=frozenset(), linked_assignment_on_date=None,
    event_gap_sequence=None, member_group_caps=(), support_requirements=(),
    event_roster=frozenset(),
):
    """Configure the *real* ``assign_member``'s world, in the same shape Task
    22's own suite uses -- so these tests exercise genuine Task 22 rules
    rather than a re-implementation of them.
    """
    import app.services.assignment as assignment_module

    monkeypatch.setattr(
        assignment_module, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: newer_version_exists,
    )
    monkeypatch.setattr(
        assignment_module, "_find_exact_assignment",
        lambda session, *, schedule_version_requirement_id, ministry_membership_id: exact_assignment,
    )
    monkeypatch.setattr(
        assignment_module, "_find_duplicate_membership_in_event",
        lambda session, *, schedule_version_id, event_id, ministry_membership_id: duplicate_in_event,
    )
    monkeypatch.setattr(
        assignment_module, "_current_assignment_count",
        lambda session, *, schedule_version_requirement_id: current_count,
    )
    qualification = None if qualified is None else SimpleNamespace(is_qualified=qualified)
    monkeypatch.setattr(
        assignment_module, "_find_qualification",
        lambda session, *, ministry_membership_id, ministry_role_id: qualification,
    )
    availability = (
        None if availability_state is None
        else SimpleNamespace(availability_state=availability_state)
    )
    monkeypatch.setattr(
        assignment_module, "_find_availability",
        lambda session, *, ministry_membership_id, event_id: availability,
    )
    conflict = SundayConflictResult(
        existing_commitments=(SimpleNamespace(),) if blocked else (),
        authoritative_assignments=(),
    )
    monkeypatch.setattr(
        assignment_module, "get_person_sunday_conflicts",
        lambda session, *, person_id, conflict_date, target_ministry_id: conflict,
    )
    # Task 47's serving maximum, which carry-forward inherits for free by
    # going through the real ``assign_member``. ``None`` is "no maximum".
    monkeypatch.setattr(
        assignment_module, "get_serving_limit",
        lambda session, *, ministry_membership_id, scheduling_period_id: serving_limit,
    )
    monkeypatch.setattr(
        assignment_module, "_count_assignments_in_version",
        lambda session, *, schedule_version_id, ministry_membership_id: (
            assignments_in_version
        ),
    )
    # Task 50's linked-pair same-date exclusion, which carry-forward inherits
    # for free by going through the real ``assign_member``. An empty set is
    # "no pair rule configured".
    monkeypatch.setattr(
        assignment_module, "get_linked_membership_ids",
        lambda session, *, ministry_membership_id, scheduling_period_id: (
            linked_membership_ids
        ),
    )
    monkeypatch.setattr(
        assignment_module, "_find_linked_assignment_on_date",
        lambda session, *, schedule_version_id, event_date, linked_membership_ids: (
            linked_assignment_on_date
        ),
    )
    # Task 71's event-gap rule, which carry-forward likewise inherits for free
    # by going through the real ``assign_member``. ``None`` is "this period
    # configures no gap", the world these tests describe -- and the one in
    # which the ministry's event sequence is never loaded.
    monkeypatch.setattr(
        assignment_module, "load_event_gap_sequence",
        lambda session, **kwargs: event_gap_sequence,
    )
    monkeypatch.setattr(
        assignment_module, "_assigned_event_ids_in_version",
        lambda session, *, schedule_version_id, ministry_membership_id: frozenset(),
    )
    # Task 74's member-group cap and same-event support requirement, which
    # carry-forward likewise inherits for free by going through the real
    # ``assign_member``. Empty tuples are "neither rule is configured", the
    # world these tests describe -- and the one in which the event's roster is
    # never read.
    monkeypatch.setattr(
        assignment_module, "load_member_group_caps",
        lambda session, *, scheduling_period_id: tuple(member_group_caps),
    )
    monkeypatch.setattr(
        assignment_module, "load_support_requirements",
        lambda session, *, scheduling_period_id: tuple(support_requirements),
    )
    monkeypatch.setattr(
        assignment_module, "_memberships_assigned_to_event",
        lambda session, *, schedule_version_id, event_id: frozenset(event_roster),
    )


def _new_assignments(session: Session) -> list[Assignment]:
    return [o for o in session.new if isinstance(o, Assignment)]


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [o for o in session.new if isinstance(o, AuditEvent)]


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _snapshot_of(assignment: Assignment) -> dict:
    return {
        "id": assignment.id,
        "schedule_version_id": assignment.schedule_version_id,
        "schedule_version_requirement_id": assignment.schedule_version_requirement_id,
        "ministry_membership_id": assignment.ministry_membership_id,
        "event_id": assignment.event_id,
        "ministry_id": assignment.ministry_id,
        "is_override": assignment.is_override,
        "override_reason": assignment.override_reason,
    }


# --------------------------------------------------------------------------
# 1-10 -- Lineage and persisted context
# --------------------------------------------------------------------------


def test_01_admin_can_carry_a_valid_assignment(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert isinstance(result, Assignment)
    assert result.schedule_version_requirement_id == world.target_requirement.id
    assert result.ministry_membership_id == world.membership.id
    assert result.schedule_version_id == world.v2.id


def test_02_own_ministry_head_can_carry(session, world, monkeypatch):
    head = _person(2, "Head")
    _actor_membership(200, person=head, ministry=world.ministry, is_head=True)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert isinstance(result, Assignment)


def test_03_head_of_another_ministry_is_rejected_by_task_22_authorization(
    session, world, monkeypatch
):
    """Carry-forward invents no authorization rule of its own; the rejection
    comes from ``assign_member``.
    """
    other_head = _person(5, "AV Head")
    _actor_membership(201, person=other_head, ministry=_ministry(4, "AV"), is_head=True)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(AuthorizationError):
        carry_forward_assignment(
            session, actor=other_head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert _new_assignments(session) == []


def test_04_a_normal_member_is_rejected(session, world, monkeypatch):
    ordinary = _person(6, "Ordinary")
    _actor_membership(202, person=ordinary, ministry=world.ministry, is_head=False)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(AuthorizationError):
        carry_forward_assignment(
            session, actor=ordinary, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


@pytest.mark.parametrize(
    "field",
    ["id", "schedule_version_id", "schedule_version_requirement_id", "ministry_membership_id"],
)
def test_05_missing_source_context_is_rejected(session, head, world, field):
    setattr(world.source_assignment, field, None)

    with pytest.raises(InvalidOperationError, match="source assignment must be persisted"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


@pytest.mark.parametrize(
    "field", ["id", "schedule_id", "scheduling_period_id", "version_number"]
)
def test_05b_missing_target_context_is_rejected(session, head, world, field):
    setattr(world.v2, field, None)

    with pytest.raises(InvalidOperationError, match="target version must be persisted"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_06_a_different_schedule_is_rejected(session, head, world, monkeypatch):
    world.v2.schedule_id = 999
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="different schedules"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_07_a_different_scheduling_period_is_rejected(session, head, world, monkeypatch):
    world.v2.scheduling_period_id = 99
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="different scheduling periods"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_08_a_target_that_does_not_amend_the_source_version_is_rejected(
    session, head, world, monkeypatch
):
    world.v2.amends_version_id = None
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="does not amend"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_08b_a_target_amending_some_other_version_is_rejected(
    session, head, world, monkeypatch
):
    world.v2.amends_version_id = 777
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="does not amend"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_09_v1_to_v3_is_rejected(session, head, world, monkeypatch):
    """Reaching across an intermediate version would skip whatever decisions
    version 2 made; carry forward one version at a time.
    """
    v3 = _version(
        502, version_number=3, status=SCHEDULE_VERSION_STATUS_DRAFT, amends_version_id=501,
    )
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="does not amend"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=v3,
        )


def test_10_a_non_adjacent_version_number_is_rejected(session, head, world, monkeypatch):
    """Even with the lineage pointer set, the number must be exactly +1."""
    world.v2.version_number = 3
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="next version"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_10b_an_unresolvable_source_version_is_rejected(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world, source_version=None)

    with pytest.raises(InvalidOperationError):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


# --------------------------------------------------------------------------
# 11-14 -- Target lifecycle
# --------------------------------------------------------------------------


def test_11_a_latest_draft_target_is_allowed(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world, newer_version_exists=False)
    _stub_task22_environment(monkeypatch)

    assert isinstance(
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        ),
        Assignment,
    )


@pytest.mark.parametrize(
    "status", [SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED]
)
def test_12_13_a_review_or_finalized_target_is_rejected(
    session, head, world, monkeypatch, status
):
    """Stricter than Task 22, which accepts REVIEW: carrying rows into a
    version under review would change it underneath the reviewers.
    """
    world.v2.status = status
    if status == SCHEDULE_VERSION_STATUS_FINALIZED:
        world.v2.finalized_at = FINALIZED_AT
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="only be carried into a DRAFT"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert _new_assignments(session) == []


def test_14_a_superseded_draft_target_is_rejected(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_14b_the_latest_check_is_scoped_to_the_targets_own_schedule_and_number(
    session, head, world, monkeypatch
):
    calls = _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    newer = next(c for c in calls if c[0] == "newer")
    assert newer[2] == {"schedule_id": 400, "version_number": 2}


# --------------------------------------------------------------------------
# 15-20 -- Requirement mapping
# --------------------------------------------------------------------------


def test_15_the_same_event_role_and_snapshot_date_resolve(session, head, world, monkeypatch):
    calls = _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    match = next(c for c in calls if c[0] == "match")
    assert match[2] == {
        "schedule_version_id": world.v2.id,
        "event_id": world.event.id,
        "ministry_role_id": world.role.id,
    }
    assert seen[0][1]["requirement"] is world.target_requirement


def test_16_a_missing_target_requirement_is_rejected(session, head, world, monkeypatch):
    """Covers every way a position stops existing: requirement removed, role
    requirement dropped, or the event cancelled and absent from the snapshot.
    """
    _stub_lookups(monkeypatch, world, target_requirement=None)

    with pytest.raises(InvalidOperationError, match="no requirement for this event and role"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_17_a_different_role_is_not_treated_as_a_match(session, head, monkeypatch):
    other_role = _role(13, "Setup Assist", ministry=_ministry())
    world = World(target_role=other_role)
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="no requirement for this event and role"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_18_a_different_event_is_not_treated_as_a_match(session, head, monkeypatch):
    other_event = _event(701, ministry=_ministry())
    world = World(target_event=other_event)
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="no requirement for this event and role"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_19_a_changed_snapshot_event_date_is_rejected(session, head, monkeypatch):
    """The conservative rule: the event moved, so carrying the old decision
    would silently commit this person to a different Sunday.
    """
    world = World(target_event_date=NOV_22)
    _stub_lookups(monkeypatch, world)

    with pytest.raises(InvalidOperationError, match="has moved from 2026-11-15 to 2026-11-22"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert _new_assignments(session) == []


def test_19b_the_dates_compared_are_the_two_snapshots_not_the_current_event(
    session, head, monkeypatch
):
    """Both snapshots say 15 November while the live Event row has moved to
    the 22nd. The carry is allowed, because the two versions describe the same
    commitment -- the current row is not consulted for this question.
    """
    world = World(target_event_date=NOV_15)
    world.event.event_date = NOV_22  # the live row moved; snapshots did not
    _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert len(seen) == 1


@pytest.mark.parametrize("target_count", [2, 5])
def test_20_a_changed_required_count_alone_still_permits_the_carry(
    session, head, monkeypatch, target_count
):
    """A count that rose leaves this person's place intact; the requirement is
    simply underfilled until someone else is assigned.
    """
    world = World(target_required_count=target_count)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert isinstance(result, Assignment)


# --------------------------------------------------------------------------
# 21-29 -- Current validation, through the real Task 22
# --------------------------------------------------------------------------


def test_21_a_currently_valid_member_carries_successfully(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch, qualified=True, availability_state=None, blocked=False, current_count=0,
    )

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert result.is_override is False
    assert result.override_reason is None
    assert [a.action for a in _audit_rows(session)] == [ACTION_ASSIGNMENT_ADDED]


def test_22_a_now_deactivated_membership_is_rejected(session, head, monkeypatch):
    world = World()
    world.membership.deactivated_at = DEACTIVATED
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_23_a_now_deactivated_person_is_rejected(session, head, monkeypatch):
    world = World()
    world.person.deactivated_at = DEACTIVATED
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(InvalidOperationError, match="deactivated person"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_24_a_now_deactivated_role_is_rejected(session, head, monkeypatch):
    world = World()
    world.target_requirement.ministry_role.deactivated_at = DEACTIVATED
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(InvalidOperationError, match="deactivated"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


@pytest.mark.parametrize("qualified", [None, False])
def test_25_a_missing_or_revoked_qualification_is_rejected(
    session, head, world, monkeypatch, qualified
):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, qualified=qualified)

    with pytest.raises(InvalidOperationError, match="not currently qualified"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_26_an_explicit_unavailable_is_rejected(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, availability_state=AVAILABILITY_UNAVAILABLE)

    with pytest.raises(InvalidOperationError, match="unavailable"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_27_a_new_cross_ministry_conflict_is_rejected(session, head, world, monkeypatch):
    """Since Task 79's correction this is an *absolute* rule rather than an
    overridable blocker, and carry-forward -- which has no override argument
    at all -- is refused by it just the same."""
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, blocked=True)

    with pytest.raises(InvalidOperationError, match=CROSS_MINISTRY_SUNDAY_CONFLICT):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_28_a_full_target_requirement_is_rejected(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, current_count=1)  # required_count is 1

    with pytest.raises(InvalidOperationError, match="fully staffed"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_29_a_cancelled_current_event_is_rejected(session, head, monkeypatch):
    world = World()
    world.target_requirement.event.cancelled_at = DEACTIVATED
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    with pytest.raises(InvalidOperationError, match="cancelled event"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_29b_a_task_22_rejection_is_never_swallowed(session, head, world, monkeypatch):
    """One assignment per call exists so the caller sees each failure and
    decides -- nothing here catches and skips.
    """
    _stub_lookups(monkeypatch, world)
    _stub_assign_member(monkeypatch, raises=InvalidOperationError("task 22 said no"))

    with pytest.raises(InvalidOperationError, match="task 22 said no"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


# --------------------------------------------------------------------------
# 30-36 -- The override boundary
# --------------------------------------------------------------------------


def test_30_a_normal_source_carries_as_a_normal_target_assignment(
    session, head, world, monkeypatch
):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert result.is_override is False
    assert result.override_reason is None


def test_31_the_old_override_reason_is_never_passed_to_assign_member(
    session, head, monkeypatch
):
    world = World(source_is_override=True)
    assert world.source_assignment.override_reason == "Head approved the conflict."
    _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    kwargs = seen[0][1]
    assert kwargs["override_reason"] is None
    assert "Head approved the conflict." not in repr(kwargs)


def test_32_an_overridden_source_does_not_make_the_target_an_override(
    session, head, monkeypatch
):
    """The source was an override, current conditions are clean, so the new
    row is an ordinary assignment. This is correct: nothing needed overriding.
    """
    world = World(source_is_override=True)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert result.is_override is False
    assert result.override_reason is None
    audit = _audit_rows(session)[0]
    assert audit.action == ACTION_ASSIGNMENT_ADDED
    assert "overridden_blockers" not in audit.after_values


def test_33_no_override_audit_history_is_read_for_authorization(session, head, monkeypatch):
    """This module never looks up an override AuditEvent -- it cannot reach
    the action constant or the audit model at all -- so no stored
    ``overridden_blockers`` could authorize the new row.
    """
    import app.services.assignment_carry_forward as module

    assert not hasattr(module, "AuditEvent")
    assert not hasattr(module, "ACTION_ASSIGNMENT_OVERRIDE_APPLIED")
    assert not hasattr(module, "record_audit_event")
    assert not [n for n in vars(module) if "audit" in n.lower()]
    assert not [n for n in vars(module) if "blocker" in n.lower()]

    world = World(source_is_override=True)
    _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)
    carry_forward_assignment(
        CarrySession(), actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )
    assert seen[0][1]["override_reason"] is None


def test_34_a_previously_overridden_assignment_carries_when_the_blocker_has_cleared(
    session, head, monkeypatch
):
    world = World(source_is_override=True)
    _stub_lookups(monkeypatch, world)
    # The Sunday conflict that justified the original override is gone.
    _stub_task22_environment(monkeypatch, blocked=False)

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert result.is_override is False


def test_35_a_previously_overridden_assignment_is_rejected_while_the_blocker_remains(
    session, head, monkeypatch
):
    """No automatic override: a head who still wants this must make a fresh
    Task 22 assignment with a new, currently-justified reason.
    """
    world = World(source_is_override=True)
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, blocked=True)

    with pytest.raises(InvalidOperationError, match=CROSS_MINISTRY_SUNDAY_CONFLICT):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert _new_assignments(session) == []


def test_36_the_public_api_exposes_no_override_reason_argument():
    params = inspect.signature(carry_forward_assignment).parameters

    assert list(params) == ["session", "actor", "source_assignment", "target_version"]
    assert "override_reason" not in params


# --------------------------------------------------------------------------
# 37-39 -- The source is preserved
# --------------------------------------------------------------------------


def test_37_38_39_the_source_assignment_version_and_history_are_untouched(
    session, head, monkeypatch
):
    world = World(source_is_override=True)
    before_assignment = _snapshot_of(world.source_assignment)
    before_version = {
        "status": world.v1.status, "finalized_at": world.v1.finalized_at,
        "version_number": world.v1.version_number,
        "amends_version_id": world.v1.amends_version_id,
    }
    before_requirement = {
        "event_date": world.source_requirement.event_date,
        "required_count": world.source_requirement.required_count,
    }
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert _snapshot_of(world.source_assignment) == before_assignment
    assert {
        "status": world.v1.status, "finalized_at": world.v1.finalized_at,
        "version_number": world.v1.version_number,
        "amends_version_id": world.v1.amends_version_id,
    } == before_version
    assert {
        "event_date": world.source_requirement.event_date,
        "required_count": world.source_requirement.required_count,
    } == before_requirement
    # The only audit row written is the new assignment's own.
    assert len(_audit_rows(session)) == 1
    assert _audit_rows(session)[0].target_id != world.source_assignment.id


# --------------------------------------------------------------------------
# 40-44 -- Idempotency and audit
# --------------------------------------------------------------------------


def test_40_41_42_a_repeated_carry_returns_the_existing_target_assignment(
    session, head, world, monkeypatch
):
    """Idempotency is Task 22's, reused -- this module adds no duplicate
    detection of its own.
    """
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    first = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )
    # Now the exact pair already exists, exactly as Task 22 would find it.
    _stub_task22_environment(monkeypatch, exact_assignment=first)
    second = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert second is first
    assert len(_new_assignments(session)) == 1
    assert len(_audit_rows(session)) == 1


def test_43_a_successful_carry_relies_on_exactly_one_task_22_audit(
    session, head, world, monkeypatch
):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == ACTION_ASSIGNMENT_ADDED
    assert rows[0].target_table == "assignment"


def test_44_no_carry_forward_audit_action_constant_is_introduced():
    import app.services.assignment_carry_forward as module
    from app.services import audit as audit_module

    assert not [n for n in vars(module) if n.startswith("ACTION_")]
    assert not [n for n in vars(audit_module) if "CARRY" in n or "CARRIED" in n]


# --------------------------------------------------------------------------
# 45-48 -- Transaction and session
# --------------------------------------------------------------------------


def test_45_every_read_and_the_assignment_use_the_one_supplied_session(
    session, head, world, monkeypatch
):
    calls = _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert [c[0] for c in calls] == ["version", "newer", "requirement", "match", "membership"]
    assert all(call[1] is session for call in calls)
    assert seen[0][0] is session


def test_46_47_never_commits_or_rolls_back(session, head, world, monkeypatch):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert session.delete_calls == 0


def test_48_carry_forward_performs_no_flush_of_its_own(session, head, world, monkeypatch):
    """With Task 22 replaced by a recorder, nothing flushes at all -- so every
    flush in the real path belongs to ``assign_member``.
    """
    _stub_lookups(monkeypatch, world)
    _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert session.flush_calls == 0


def test_48b_the_real_path_flushes_exactly_once_for_the_new_assignment(
    session, head, world, monkeypatch
):
    """That one flush is Task 22's own, for the identity its audit row needs."""
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert session.flush_calls == 1


def test_the_hand_off_passes_exactly_the_resolved_requirement_and_membership(
    session, head, world, monkeypatch
):
    _stub_lookups(monkeypatch, world)
    seen = _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert len(seen) == 1
    kwargs = seen[0][1]
    assert kwargs == {
        "actor": head,
        "requirement": world.target_requirement,
        "membership": world.membership,
        "override_reason": None,
    }


def test_the_membership_carried_is_exactly_the_sources_never_a_substitute(
    session, head, world, monkeypatch
):
    calls = _stub_lookups(monkeypatch, world)
    _stub_assign_member(monkeypatch)

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    lookup = next(c for c in calls if c[0] == "membership")
    assert lookup[2] == {"membership_id": world.source_assignment.ministry_membership_id}


# --------------------------------------------------------------------------
# Query shape (compiled SQL)
# --------------------------------------------------------------------------


def test_the_requirement_match_query_uses_the_stable_scheduling_identity():
    compiled = _compile(_requirement_by_scheduling_identity_statement(501, 700, 12))

    assert "schedule_version_requirement.schedule_version_id = 501" in compiled
    assert "schedule_version_requirement.event_id = 700" in compiled
    assert "schedule_version_requirement.ministry_role_id = 12" in compiled
    # The date is compared after the lookup, so a moved event gets its own
    # error rather than a generic "no such requirement".
    assert "event_date" not in compiled.split("WHERE")[1]


def test_the_newer_version_probe_is_scoped_and_strictly_greater():
    compiled = _compile(_newer_version_exists_statement(400, 2))

    assert "schedule_version.schedule_id = 400" in compiled
    assert "schedule_version.version_number > 2" in compiled
    assert ">= 2" not in compiled
    assert "LIMIT 1" in compiled


def test_the_row_lookups_are_scoped_to_their_ids():
    assert "schedule_version.id = 500" in _compile(_version_lookup_statement(500))
    assert "schedule_version_requirement.id = 600" in _compile(_requirement_lookup_statement(600))
    assert "ministry_membership.id = 118" in _compile(_membership_lookup_statement(118))


def test_the_module_reaches_no_mutation_machinery_of_its_own():
    """Its only write is through assign_member: no model is constructed here,
    and no audit is recorded.
    """
    import app.services.assignment_carry_forward as module

    assert hasattr(module, "assign_member")
    assert not hasattr(module, "remove_assignment")
    assert not hasattr(module, "record_audit_event")


# --------------------------------------------------------------------------
# 31-32 -- the person-period serving maximum (Task 47)
#
# Carry-forward inherits this rule for free: it hands the real decision to
# ``assign_member`` with ``override_reason=None``, so a constraint added to
# that service's absolute checks applies here without carry-forward knowing
# the rule exists. These tests prove the wiring rather than re-testing the
# rule.
# --------------------------------------------------------------------------


def test_31_carry_forward_is_refused_when_it_would_exceed_the_maximum(
    session, head, world, monkeypatch
):
    """Revalidated against the *current* maximum, like every other rule.

    A limit agreed after the predecessor was built applies to the successor,
    because carry-forward re-decides rather than copies.
    """
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch, serving_limit=2, assignments_in_version=2
    )

    with pytest.raises(InvalidOperationError, match="serving maximum"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )

    assert _new_assignments(session) == []


def test_31b_carry_forward_succeeds_while_within_the_maximum(
    session, head, world, monkeypatch
):
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch, serving_limit=4, assignments_in_version=1
    )

    result = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert isinstance(result, Assignment)


def test_31c_a_source_override_does_not_carry_authority_over_the_maximum(
    session, head, world, monkeypatch
):
    """An override on the predecessor authorizes nothing here.

    Carry-forward already passes ``override_reason=None``, and the maximum is
    not overridable in any case -- so a head who overrode something in v1
    cannot reach a fifth assignment in v2 through the carried row.
    """
    world.source_assignment.is_override = True
    world.source_assignment.override_reason = "Bypassed a conflict in v1."
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch, serving_limit=2, assignments_in_version=2
    )

    with pytest.raises(InvalidOperationError, match="not overridable"):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )


def test_32_the_maximum_counts_the_target_version_only(
    session, head, world, monkeypatch
):
    """A predecessor's assignments are never added to the successor's count.

    v1 and v2 are two records of one quarter, not eight serving commitments,
    so the count that matters is scoped to the version being written.
    """
    import app.services.assignment as assignment_module

    seen: list[int] = []

    def recording_count(session, *, schedule_version_id, ministry_membership_id):
        seen.append(schedule_version_id)
        return 0

    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, serving_limit=4)
    monkeypatch.setattr(
        assignment_module, "_count_assignments_in_version", recording_count
    )

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    # The successor's id, and never the predecessor's.
    assert seen == [world.v2.id]
    assert world.v1.id not in seen


# ==========================================================================
# 34-35 (Task 50) -- the linked-pair same-date exclusion travels for free
#
# A pair rule belongs to (membership pair x SchedulingPeriod), not to any one
# ScheduleVersion, so a successor rebuilt for the same period is governed by
# the same rules. Carry-forward inherits the enforcement by going through the
# real ``assign_member`` and adding nothing of its own -- which is the property
# these tests pin.
# ==========================================================================


def test_34_carry_forward_is_revalidated_against_the_current_pair_rule(
    session, head, world, monkeypatch
):
    """A rule recorded after v1 was built blocks the carry into v2.

    Nothing about the source assignment changed; the constraint did. That is
    exactly what "re-made now, against current facts" means, and it is Task
    22's answer rather than a second rule written here.
    """
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch,
        linked_membership_ids=frozenset({999}),
        linked_assignment_on_date=901,
    )

    with pytest.raises(
        InvalidOperationError, match="SAME_DATE_LINKED_MEMBER_CONFLICT"
    ):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert not _new_assignments(session)


def test_34b_an_old_override_does_not_bypass_the_pair_rule(
    session, head, world, monkeypatch
):
    """Historical authorization never transfers, and could not reach this rule
    even if it did: the exclusion is not one of the bounded overridable blockers.

    ``carry_forward_assignment`` has no ``override_reason`` parameter at all,
    so the source's own override flag and reason are simply not consulted.
    """
    world.source_assignment.is_override = True
    world.source_assignment.override_reason = "The head accepted the risk in v1."
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch,
        linked_membership_ids=frozenset({999}),
        linked_assignment_on_date=901,
    )

    with pytest.raises(
        InvalidOperationError, match="SAME_DATE_LINKED_MEMBER_CONFLICT"
    ):
        carry_forward_assignment(
            session, actor=head, source_assignment=world.source_assignment,
            target_version=world.v2,
        )
    assert not _new_assignments(session)


def test_34c_a_carry_succeeds_when_no_pair_rule_bites(
    session, head, world, monkeypatch
):
    """The ordinary case, so the test above is about the rule and not the setup."""
    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(
        monkeypatch,
        linked_membership_ids=frozenset({999}),
        # The linked member holds nothing on this date in the successor.
        linked_assignment_on_date=None,
    )

    assignment = carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )
    assert assignment.is_override is False


def test_35_predecessor_assignments_are_not_counted_in_the_successor(
    session, head, world, monkeypatch
):
    """Only assignments actually present in the successor matter.

    v1 and v2 are two records of one quarter. A linked member's v1 assignment
    is history; if the successor does not contain it, it constrains nothing --
    so the date lookup is scoped to the target version's id and never the
    predecessor's.
    """
    import app.services.assignment as assignment_module

    seen: list[int] = []

    def recording_lookup(
        session, *, schedule_version_id, event_date, linked_membership_ids
    ):
        seen.append(schedule_version_id)
        return None

    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch, linked_membership_ids=frozenset({999}))
    monkeypatch.setattr(
        assignment_module, "_find_linked_assignment_on_date", recording_lookup
    )

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    # The successor's id, and never the predecessor's.
    assert seen == [world.v2.id]
    assert world.v1.id not in seen


def test_35b_the_pair_rule_is_read_for_the_period_not_the_version(
    session, head, world, monkeypatch
):
    """A pair rule belongs to the period, which v1 and v2 share.

    So the successor is governed by the same rules without anything being
    copied forward -- there is nothing version-scoped to copy.
    """
    import app.services.assignment as assignment_module

    seen: list[dict] = []

    def recording_pairs(session, *, ministry_membership_id, scheduling_period_id):
        seen.append(
            {
                "ministry_membership_id": ministry_membership_id,
                "scheduling_period_id": scheduling_period_id,
            }
        )
        return frozenset()

    _stub_lookups(monkeypatch, world)
    _stub_task22_environment(monkeypatch)
    monkeypatch.setattr(
        assignment_module, "get_linked_membership_ids", recording_pairs
    )

    carry_forward_assignment(
        session, actor=head, source_assignment=world.source_assignment,
        target_version=world.v2,
    )

    assert len(seen) == 1
    assert seen[0]["scheduling_period_id"] == world.v2.scheduling_period_id
    # v1 and v2 are the same period, which is why nothing needs copying.
    assert world.v1.scheduling_period_id == world.v2.scheduling_period_id


def test_35c_carry_forward_adds_no_pair_rule_of_its_own():
    """One definition of "may serve", and it is Task 22's.

    The module must not import the exclusion service or re-implement the
    check: a second, quietly divergent definition is exactly what routing
    every carry through ``assign_member`` exists to prevent.
    """
    from pathlib import Path

    source = Path("app/services/assignment_carry_forward.py").read_text()
    assert "same_date_exclusion" not in source
    assert "get_linked_membership_ids" not in source
