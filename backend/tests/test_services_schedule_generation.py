"""Draft schedule generation (Task 34).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy.** This service is orchestration, so the tests are split to
match what each part actually claims:

- **Gates** (context, authorization, lifecycle) run against real ORM objects
  with only the lookups monkeypatched.
- **Orchestration identity** -- which session, which policy, which actor, and
  ``override_reason=None`` -- is proven by monkeypatching the three
  collaborators and recording exactly what they were handed.
- **Persistence** runs the *real* batch writer (Task 63) against a stubbed
  prefetch, using the same ``_stub_environment`` keywords Task 22's own suite
  established, so a rejection genuinely comes from the shared rules rather
  than from a test double agreeing with itself. The writer's own behaviour --
  its query count, its run-state accumulation, its atomicity -- is
  ``tests/test_services_generated_assignment.py``.
- **Boundaries** (no direct Assignment construction, no audit of its own, no
  lifecycle mutation) are checked with AST/namespace assertions rather than
  by grepping prose.

The end-to-end behavior over real rows -- that proposals really become rows and
that a failed run really leaves nothing behind -- is proven in
``tests/integration/test_pg_schedule_generation.py``.
"""

from __future__ import annotations

import ast
import datetime
import inspect
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

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
    SchedulingPeriod,
)
from app.scheduling.input import SchedulingInput
from app.scheduling.result import (
    ProposedAssignment,
    SchedulingResult,
    UnfilledRequirement,
)
from app.scheduling.solver import SchedulingPolicy
from app.services.audit import ACTION_ASSIGNMENT_ADDED
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.schedule_generation import (
    DraftGenerationResult,
    _newer_version_exists_statement,
    _scheduling_period_statement,
    generate_draft_schedule,
)
from app.services.sunday_conflict import SundayConflictResult

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)

LEAD = 12
ASSIST = 13

POLICY = SchedulingPolicy(allow_no_response=True, target_assignments_per_candidate=3)

EMPTY_INPUT = SchedulingInput(
    schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
)


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


class GenerationSession(Session):
    """A real, unbound Session. ``flush`` is permitted -- Task 22 legitimately
    flushes once per created assignment -- and counted, so "no flush of its
    own" is checkable. ``commit``, ``rollback`` and ``delete`` are forbidden.
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
        raise AssertionError("generation must never delete")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> GenerationSession:
    return GenerationSession()


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


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _membership(
    membership_id: int, *, person: Person, ministry: Ministry, deactivated: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    if deactivated:
        membership.deactivated_at = DEACTIVATED
    return membership


def _period(period_id: int = 11, *, ministry: Ministry) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name="Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _role(role_id: int = LEAD, *, ministry: Ministry, deactivated: bool = False) -> MinistryRole:
    role = MinistryRole(name=f"Role {role_id}", ministry_id=ministry.id)
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
    version_id: int = 500, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status,
        finalized_at=datetime.datetime(2026, 10, 1, tzinfo=UTC)
        if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
    )
    version.id = version_id
    return version


def _requirement(
    requirement_id: int, *, version: ScheduleVersion, event: Event, role: MinistryRole,
    required_count: int = 1,
) -> ScheduleVersionRequirement:
    requirement = ScheduleVersionRequirement(
        schedule_version_id=version.id, event_id=event.id, event_date=NOV_15,
        ministry_role_id=role.id, scheduling_period_id=11,
        ministry_id=role.ministry_id, required_count=required_count,
    )
    requirement.id = requirement_id
    requirement.schedule_version = version
    requirement.event = event
    requirement.ministry_role = role
    return requirement


class World:
    """A latest DRAFT version, one requirement, one candidate."""

    def __init__(self, *, status: str = SCHEDULE_VERSION_STATUS_DRAFT):
        self.ministry = _ministry()
        self.period = _period(ministry=self.ministry)
        self.role = _role(ministry=self.ministry)
        self.event = _event(ministry=self.ministry)
        self.version = _version(status=status)
        self.requirement = _requirement(
            600, version=self.version, event=self.event, role=self.role,
        )
        self.person = _person(42, "John")
        self.membership = _membership(118, person=self.person, ministry=self.ministry)


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


def _result(*pairs, unfilled=()) -> SchedulingResult:
    return SchedulingResult(
        proposed_assignments=tuple(
            ProposedAssignment(requirement_id=r, membership_id=m, event_id=e)
            for r, m, e in pairs
        ),
        unfilled_requirements=tuple(unfilled),
    )


def _stub(
    monkeypatch, world: World, *, newer_version_exists: bool = False,
    scheduling_result: SchedulingResult | None = None,
    requirements=None, memberships=None,
    builder_error: Exception | None = None, solver_error: Exception | None = None,
) -> dict:
    """Patch the gates and the two engine collaborators, recording every call
    so session/policy/actor identity and call order can be asserted.
    """
    import app.services.schedule_generation as module

    seen: dict[str, list] = {"builder": [], "solver": [], "calls": []}
    resolved_requirements = (
        {world.requirement.id: world.requirement} if requirements is None
        else requirements
    )
    resolved_memberships = (
        {world.membership.id: world.membership} if memberships is None else memberships
    )

    def fake_period(session, version):
        seen["calls"].append(("period", session))
        return world.period

    def fake_newer(session, *, schedule_id, version_number):
        seen["calls"].append(("newer", session))
        return newer_version_exists

    def fake_builder(session, *, version):
        seen["builder"].append((session, version))
        if builder_error is not None:
            raise builder_error
        return EMPTY_INPUT

    def fake_solver(scheduling_input, *, policy):
        seen["solver"].append((scheduling_input, policy))
        if solver_error is not None:
            raise solver_error
        return scheduling_result if scheduling_result is not None else _result()

    def fake_load_requirements(session, *, schedule_version_id, requirement_ids):
        seen["calls"].append(("requirements", session))
        return {k: v for k, v in resolved_requirements.items() if k in requirement_ids}

    def fake_load_memberships(session, *, ministry_id, membership_ids):
        seen["calls"].append(("memberships", session))
        return {k: v for k, v in resolved_memberships.items() if k in membership_ids}

    import app.services.generated_assignment as writer

    monkeypatch.setattr(module, "_resolve_scheduling_period", fake_period)
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(module, "build_scheduling_input", fake_builder)
    monkeypatch.setattr(module, "solve_schedule", fake_solver)
    # Resolution moved to the batch writer (Task 63) along with the writes;
    # the recording shape is unchanged, so every orchestration assertion below
    # still reads the same.
    monkeypatch.setattr(writer, "_load_requirements", fake_load_requirements)
    monkeypatch.setattr(writer, "_load_memberships", fake_load_memberships)
    monkeypatch.setattr(writer, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(
        writer, "_require_still_writable",
        lambda session, *, schedule_version: writer._require_mutable_working_version(
            session, schedule_version=schedule_version
        ),
    )
    return seen


def _stub_persistence(monkeypatch, *, raises_on: int | None = None):
    """Replace the batch writer with a recorder, to prove the hand-off
    arguments and that a failure is not caught.

    Task 34 recorded one call per proposal, because there was one call per
    proposal. Task 63 hands the whole result over once, so what is recorded is
    the proposals *within* that one call -- the same facts (which session,
    which actor, which rows, in which order, with no override), read off a
    different shape.
    """
    import app.services.schedule_generation as module

    calls: list[tuple] = []

    def fake_persist(session, *, actor, version, ministry_id, proposals):
        calls.append((session, actor, version, ministry_id, tuple(proposals)))
        if raises_on is not None:
            raise InvalidOperationError("the writer refused this placement")
        created = []
        for index, proposal in enumerate(proposals):
            assignment = Assignment(
                schedule_version_requirement_id=proposal.requirement_id,
                ministry_membership_id=proposal.membership_id,
                schedule_version_id=version.id,
                event_id=proposal.event_id, ministry_id=ministry_id,
                is_override=False, override_reason=None,
            )
            assignment.id = 900 + index
            created.append(assignment)
        return tuple(created)

    monkeypatch.setattr(module, "persist_generated_assignments", fake_persist)
    return calls


def _proposal_pairs(calls) -> list[tuple[int, int]]:
    """(requirement_id, membership_id) for every proposal handed over, in
    order -- what Task 34's per-call recording used to spell out.
    """
    return [
        (proposal.requirement_id, proposal.membership_id)
        for call in calls
        for proposal in call[4]
    ]


def _stub_write_environment(
    monkeypatch, *, qualified: bool = True, availability_state: str | None = None,
    blocked: bool = False, current_count: int = 0, exact_assignment=None,
    duplicate_in_event: bool = False, newer_version_exists: bool = False,
    serving_limit: int | None = None, assignments_in_version: int = 0,
    linked_membership_ids=frozenset(), linked_assignment_on_date: bool = False,
):
    """Configure the *real* batch writer's world, in the same shape Task 22's
    own suite established -- so a rejection here is genuinely a rule's.

    The keywords are deliberately unchanged from the ones Task 34's tests used
    against ``assign_member``: the rules are the same rules, and the point of
    Task 63 is that only the *reading* moved. What changes is that these facts
    are now installed once, as the prefetch, instead of once per lookup.
    """
    import app.services.generated_assignment as writer

    monkeypatch.setattr(
        writer, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: newer_version_exists,
    )
    monkeypatch.setattr(
        writer, "_require_still_writable",
        lambda session, *, schedule_version: writer._require_mutable_working_version(
            session, schedule_version=schedule_version
        ),
    )

    def fake_prefetch(cls, session, *, version, ministry_id, requirements, memberships):
        membership_ids = [m.id for m in memberships]
        event_ids = [r.event_id for r in requirements]
        role_ids = [r.ministry_role_id for r in requirements]
        dates = [r.event_date for r in requirements]
        person_ids = [m.person_id for m in memberships]
        return writer._BatchFacts(
            existing=(
                {}
                if exact_assignment is None
                else {
                    (r.id, m.id): exact_assignment
                    for r in requirements
                    for m in memberships
                }
            ),
            qualifications=(
                {(m, r) for m in membership_ids for r in role_ids}
                if qualified else set()
            ),
            availability=(
                {(m, e) for m in membership_ids for e in event_ids}
                if availability_state == AVAILABILITY_UNAVAILABLE else set()
            ),
            conflicts=(
                {(p, d) for p in person_ids for d in dates} if blocked else set()
            ),
            serving_maximums=(
                {} if serving_limit is None
                else {m: serving_limit for m in membership_ids}
            ),
            linked={m: frozenset(linked_membership_ids) for m in membership_ids},
            filled_by_requirement={r.id: current_count for r in requirements},
            held_by_membership={m: assignments_in_version for m in membership_ids},
            membership_events=(
                {(m, e) for m in membership_ids for e in event_ids}
                if duplicate_in_event else set()
            ),
            memberships_by_date=(
                {d: set(linked_membership_ids) for d in dates}
                if linked_assignment_on_date else {}
            ),
            # Task 71: ``None`` is "this period configures no event-gap rule",
            # which is the world these tests describe.
            event_sequence=None,
            events_by_membership={},
            # Task 74: no member-group cap and no same-event support
            # requirement, which is the world these tests describe.
            group_caps_by_membership={},
            support_by_membership={},
            memberships_by_event={},
        )

    monkeypatch.setattr(
        writer._BatchFacts, "prefetch", classmethod(fake_prefetch)
    )


def _new_assignments(session: Session) -> list[Assignment]:
    return [o for o in session.new if isinstance(o, Assignment)]


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [o for o in session.new if isinstance(o, AuditEvent)]


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 1-7 -- Authorization and context
# --------------------------------------------------------------------------


def test_01_an_admin_can_generate(session, head, world, monkeypatch):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert isinstance(result, DraftGenerationResult)
    assert result.created_count == 1
    assert result.created_assignments[0].ministry_membership_id == 118


def test_02_an_own_ministry_head_can_generate(session, world, monkeypatch):
    head = _person(2, "Head")
    _actor_membership(200, person=head, ministry=world.ministry, is_head=True)
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert result.created_count == 1


def test_03_a_head_of_another_ministry_is_rejected(session, world, monkeypatch):
    outsider = _person(5, "AV Head")
    _actor_membership(201, person=outsider, ministry=_ministry(4, "AV"), is_head=True)
    seen = _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))

    with pytest.raises(AuthorizationError):
        generate_draft_schedule(
            session, actor=outsider, version=world.version, policy=POLICY,
        )
    assert _new_assignments(session) == []


def test_04_a_normal_member_is_rejected(session, world, monkeypatch):
    ordinary = _person(6, "Ordinary")
    _actor_membership(202, person=ordinary, ministry=world.ministry, is_head=False)
    _stub(monkeypatch, world)

    with pytest.raises(AuthorizationError):
        generate_draft_schedule(
            session, actor=ordinary, version=world.version, policy=POLICY,
        )


def test_05_a_deactivated_actor_is_rejected(session, world, monkeypatch):
    former = _person(7, "Former Admin", is_admin=True, deactivated=True)
    _stub(monkeypatch, world)

    with pytest.raises(AuthorizationError):
        generate_draft_schedule(
            session, actor=former, version=world.version, policy=POLICY,
        )


def test_05b_authorization_happens_before_any_solver_work(session, world, monkeypatch):
    """Solving a schedule for someone who may not ask is wasted effort at
    best; the gate runs first.
    """
    outsider = _person(5, "Outsider")
    seen = _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))

    with pytest.raises(AuthorizationError):
        generate_draft_schedule(
            session, actor=outsider, version=world.version, policy=POLICY,
        )

    assert seen["builder"] == []
    assert seen["solver"] == []


def test_06_the_ministry_is_derived_from_the_versions_period(session, monkeypatch):
    """No ministry_id parameter exists; authorization is scoped to whatever
    the resolved period says.
    """
    world = World()
    kids = _ministry(9, "Kids")
    world.period = _period(12, ministry=kids)
    head = _person(8, "Kids Head")
    _actor_membership(300, person=head, ministry=kids, is_head=True)
    _stub(monkeypatch, world)

    result = generate_draft_schedule(
        session := GenerationSession(), actor=head, version=world.version, policy=POLICY,
    )

    assert result.created_count == 0  # authorized, nothing proposed


@pytest.mark.parametrize(
    "missing", ["id", "schedule_id", "scheduling_period_id", "version_number"]
)
def test_07_missing_persisted_version_context_is_rejected(session, head, world, missing):
    setattr(world.version, missing, None)

    with pytest.raises(InvalidOperationError, match="must be persisted"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )


# --------------------------------------------------------------------------
# 8-12 -- Version lifecycle
# --------------------------------------------------------------------------


def test_08_a_latest_draft_is_allowed(session, head, world, monkeypatch):
    _stub(monkeypatch, world, newer_version_exists=False, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)

    assert generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    ).created_count == 1


@pytest.mark.parametrize(
    "status", [SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED]
)
def test_09_10_review_and_finalized_versions_are_rejected(session, head, monkeypatch, status):
    """Stricter than Task 22's DRAFT-or-REVIEW manual rule, deliberately:
    generation rewrites many rows and must not happen underneath reviewers.
    """
    world = World(status=status)
    seen = _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))

    with pytest.raises(InvalidOperationError, match="DRAFT"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )
    assert seen["builder"] == []  # refused before any expensive work
    assert _new_assignments(session) == []


def test_11_a_superseded_draft_is_rejected(session, head, world, monkeypatch):
    seen = _stub(monkeypatch, world, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )
    assert seen["builder"] == []


def test_12_the_version_status_is_unchanged_after_generation(session, head, world, monkeypatch):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)
    before = (
        world.version.status, world.version.finalized_at, world.version.notes,
        world.version.version_number,
    )

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    assert (
        world.version.status, world.version.finalized_at, world.version.notes,
        world.version.version_number,
    ) == before
    assert world.version.status == SCHEDULE_VERSION_STATUS_DRAFT


# --------------------------------------------------------------------------
# 13-16 -- Builder and solver orchestration
# --------------------------------------------------------------------------


def test_13_the_builder_receives_the_supplied_session_and_version(
    session, head, world, monkeypatch
):
    seen = _stub(monkeypatch, world)

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    assert len(seen["builder"]) == 1
    used_session, used_version = seen["builder"][0]
    assert used_session is session
    assert used_version is world.version


def test_14_the_exact_policy_is_passed_to_the_solver_unchanged(
    session, head, world, monkeypatch
):
    """The service adds nothing to the policy and reads nothing from it: no
    hard-coded target, no ministry role ids, no fairness switched on quietly.
    """
    policy = SchedulingPolicy(
        allow_no_response=False, target_assignments_per_candidate=5,
        role_variety_role_ids=frozenset({LEAD, ASSIST}),
    )
    seen = _stub(monkeypatch, world)

    generate_draft_schedule(session, actor=head, version=world.version, policy=policy)

    assert len(seen["solver"]) == 1
    scheduling_input, used_policy = seen["solver"][0]
    assert used_policy is policy
    assert used_policy.allow_no_response is False
    assert used_policy.target_assignments_per_candidate == 5
    assert used_policy.role_variety_role_ids == frozenset({LEAD, ASSIST})
    # And the solver was given exactly what the builder produced.
    assert scheduling_input is EMPTY_INPUT


def test_15_a_stale_builder_rejection_propagates(session, head, world, monkeypatch):
    """The builder owns the staleness gate; generation neither repeats it nor
    creates a successor version to work around it.
    """
    _stub(
        monkeypatch, world,
        builder_error=InvalidOperationError("snapshot no longer matches current"),
    )

    with pytest.raises(InvalidOperationError, match="no longer matches current"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )
    assert _new_assignments(session) == []


def test_16_a_solver_error_propagates(session, head, world, monkeypatch):
    from app.scheduling.solver import SchedulingEngineError

    _stub(monkeypatch, world, solver_error=SchedulingEngineError("model invalid"))

    with pytest.raises(SchedulingEngineError, match="model invalid"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )
    assert _new_assignments(session) == []


# --------------------------------------------------------------------------
# 17-26 -- Persistence through Task 22
# --------------------------------------------------------------------------


def test_17_one_proposal_is_handed_over_exactly_once(
    session, head, world, monkeypatch
):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    calls = _stub_persistence(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert len(calls) == 1
    assert _proposal_pairs(calls) == [(600, 118)]
    assert result.created_count == 1


def test_18_every_proposal_is_handed_over_in_solver_order(session, head, monkeypatch):
    world = World()
    second_role = _role(ASSIST, ministry=world.ministry)
    second_requirement = _requirement(
        601, version=world.version, event=world.event, role=second_role,
    )
    second_membership = _membership(
        119, person=_person(43, "Mary"), ministry=world.ministry,
    )
    _stub(
        monkeypatch, world,
        scheduling_result=_result((600, 118, 700), (601, 119, 700)),
        requirements={600: world.requirement, 601: second_requirement},
        memberships={118: world.membership, 119: second_membership},
    )
    calls = _stub_persistence(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    # One hand-off, both proposals, in the order the solver emitted them --
    # which is what Task 34 expressed as two calls in that order.
    assert len(calls) == 1
    assert result.created_count == 2
    assert _proposal_pairs(calls) == [(600, 118), (601, 119)]


def test_19_override_reason_is_always_none(session, head, world, monkeypatch):
    """Automatic scheduling never creates an override -- there is not even a
    parameter for one on the public operation.
    """
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    calls = _stub_persistence(monkeypatch)

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    # There is nothing on a proposal an override could even be carried on, and
    # nothing on the writer's signature for one to arrive through.
    assert not hasattr(calls[0][4][0], "override_reason")
    import app.services.generated_assignment as writer

    assert "override_reason" not in inspect.signature(
        writer.persist_generated_assignments
    ).parameters
    assert "override_reason" not in inspect.signature(generate_draft_schedule).parameters


def test_20_21_the_same_actor_and_session_are_supplied(session, head, world, monkeypatch):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    calls = _stub_persistence(monkeypatch)

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    used_session, used_actor = calls[0][0], calls[0][1]
    assert used_session is session
    assert used_actor is head
    assert calls[0][2] is world.version
    assert calls[0][3] == world.period.ministry_id


def test_22_proposals_resolve_to_the_right_requirement_and_membership(
    session, head, world, monkeypatch
):
    """Resolution moved into the batch writer with the writes, so this asks
    the writer directly: the ids the solver emitted must become the very rows
    the version and ministry hold, not copies and not near-misses.
    """
    import app.services.generated_assignment as writer

    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)
    seen: list[tuple] = []
    real_build = writer.build_assignment

    def recording_build(*, requirement, membership, is_override, override_reason):
        seen.append((requirement, membership, is_override, override_reason))
        return real_build(
            requirement=requirement, membership=membership,
            is_override=is_override, override_reason=override_reason,
        )

    monkeypatch.setattr(writer, "build_assignment", recording_build)

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    assert len(seen) == 1
    assert seen[0][0] is world.requirement
    assert seen[0][1] is world.membership
    assert seen[0][2] is False       # automatic work never overrides
    assert seen[0][3] is None


def test_25_an_unresolvable_requirement_is_rejected(session, head, world, monkeypatch):
    _stub(
        monkeypatch, world, scheduling_result=_result((999, 118, 700)), requirements={},
    )

    with pytest.raises(InvalidOperationError, match="does not belong to schedule version"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )


def test_26_an_unresolvable_membership_is_rejected(session, head, world, monkeypatch):
    _stub(
        monkeypatch, world, scheduling_result=_result((600, 999, 700)), memberships={},
    )

    with pytest.raises(InvalidOperationError, match="does not belong to ministry"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )


def test_a_real_task_22_rejection_propagates(session, head, world, monkeypatch):
    """The backstop: a blocker that arose after the input was built stops the
    run rather than being overridden or skipped.
    """
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch, availability_state=AVAILABILITY_UNAVAILABLE)

    with pytest.raises(InvalidOperationError, match="unavailable"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )


# --------------------------------------------------------------------------
# 27-30 -- Existing assignments
# --------------------------------------------------------------------------


def test_27_28_29_30_only_proposals_are_written_and_nothing_else_is_touched(
    session, head, world, monkeypatch
):
    """Existing rows reach the solver as fixed inputs and never appear in
    ``proposed_assignments``, so this service never sees them at all -- it
    writes exactly what the run decided and deletes nothing.
    """
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    calls = _stub_persistence(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert _proposal_pairs(calls) == [(600, 118)]  # one proposal, one write
    assert result.created_count == 1
    assert session.delete_calls == 0
    # Nothing about existing state is reachable from here.
    import app.services.schedule_generation as module

    assert not hasattr(module, "remove_assignment")
    assert not [n for n in vars(module) if "existing" in n.lower()]


# --------------------------------------------------------------------------
# 31-34 -- Incomplete generation is a success
# --------------------------------------------------------------------------


def test_31_32_33_34_an_incomplete_result_still_succeeds(session, head, world, monkeypatch):
    """Four of five staffed: the four are written, the fifth comes back with
    its diagnostics, and nothing raises.
    """
    unfilled = UnfilledRequirement(
        requirement_id=601, missing_count=1,
        diagnostic_codes=("NO_QUALIFIED_CANDIDATES",),
    )
    _stub(
        monkeypatch, world,
        scheduling_result=_result((600, 118, 700), unfilled=[unfilled]),
    )
    _stub_write_environment(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert result.created_count == 1
    assert result.is_complete is False
    assert result.scheduling_result.unfilled_requirements == (unfilled,)
    assert result.scheduling_result.unfilled_requirements[0].diagnostic_codes == (
        "NO_QUALIFIED_CANDIDATES",
    )


# --------------------------------------------------------------------------
# 35-40 -- Atomic failure
# --------------------------------------------------------------------------


def test_35_36_37_a_later_failure_propagates_without_catching_or_cleanup(
    session, head, monkeypatch
):
    """The second write fails. The first is deliberately left in the session
    for the caller's ROLLBACK -- deleting it here would be a second, untested
    code path doing what ROLLBACK already does correctly.
    """
    world = World()
    second_requirement = _requirement(
        601, version=world.version, event=world.event,
        role=_role(ASSIST, ministry=world.ministry),
    )
    second_membership = _membership(
        119, person=_person(43, "Mary"), ministry=world.ministry,
    )
    _stub(
        monkeypatch, world,
        scheduling_result=_result((600, 118, 700), (601, 119, 700)),
        requirements={600: world.requirement, 601: second_requirement},
        memberships={118: world.membership, 119: second_membership},
    )
    calls = _stub_persistence(monkeypatch, raises_on=True)

    with pytest.raises(InvalidOperationError, match="refused this placement"):
        generate_draft_schedule(
            session, actor=head, version=world.version, policy=POLICY,
        )

    assert _proposal_pairs(calls) == [(600, 118), (601, 119)]  # both were offered
    assert session.delete_calls == 0  # no compensating delete
    assert session.rollback_calls == 0  # and no rollback of its own


def test_38_39_40_the_service_never_commits_or_rolls_back(
    session, head, world, monkeypatch
):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    # The transaction boundary belongs to the caller; nothing here reaches for it.
    import app.services.schedule_generation as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "commit" not in calls
    assert "rollback" not in calls
    assert "add" not in calls


# --------------------------------------------------------------------------
# 41-43 -- Repeated generation
# --------------------------------------------------------------------------


def test_41_42_zero_proposals_creates_no_assignments_and_no_audit(
    session, head, world, monkeypatch
):
    """A second run on an already-complete schedule: the builder reports the
    first run's rows as existing, the solver proposes nothing, and nothing is
    written.
    """
    _stub(monkeypatch, world, scheduling_result=_result())
    calls = _stub_persistence(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    assert _proposal_pairs(calls) == []
    assert result.created_assignments == ()
    assert result.created_count == 0
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_43_no_generation_run_provenance_is_stored(session):
    """Repeatability comes from rebuilding the input, not from remembering
    that a solver ran. No provenance column, no run entity.
    """
    columns = set(Assignment.__table__.c.keys())

    assert "assignment_origin" not in columns
    assert "generated_by" not in columns
    assert "solver_run_id" not in columns
    assert "is_generated" not in columns


# --------------------------------------------------------------------------
# 44-46 -- Audit
# --------------------------------------------------------------------------


def test_44_no_new_generation_audit_action_exists():
    from app.services import audit as audit_module
    import app.services.schedule_generation as module

    assert not [n for n in vars(module) if n.startswith("ACTION_")]
    assert not [
        n for n in vars(audit_module)
        if n.startswith("ACTION_") and ("GENERAT" in n or "SOLVER" in n)
    ]


def test_45_created_assignments_rely_on_task_22s_own_audit_rows(
    session, head, world, monkeypatch
):
    """One created assignment, one Task 22 audit row -- and no extra row
    saying "the solver ran".
    """
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)

    result = generate_draft_schedule(
        session, actor=head, version=world.version, policy=POLICY,
    )

    rows = _audit_rows(session)
    assert result.created_count == 1
    assert len(rows) == 1
    assert rows[0].action == ACTION_ASSIGNMENT_ADDED
    assert rows[0].target_table == "assignment"


def test_46_the_service_records_no_audit_event_of_its_own():
    import app.services.schedule_generation as module

    assert not hasattr(module, "record_audit_event")
    assert not hasattr(module, "AuditEvent")
    assert not [n for n in vars(module) if "audit" in n.lower()]


# --------------------------------------------------------------------------
# 47-51 -- The mutation boundary
# --------------------------------------------------------------------------


def test_47_48_49_the_batch_writer_is_the_only_write_path():
    """No Assignment is constructed here and none is added to the session --
    checked against the module's actual calls, not by grepping prose.
    """
    import app.services.schedule_generation as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)

    constructed = [
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert "Assignment" not in constructed

    attribute_calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "add" not in attribute_calls
    assert "delete" not in attribute_calls
    assert "flush" not in attribute_calls
    # The one write path is present and used.
    assert "persist_generated_assignments" in constructed
    # And the one it replaced is not reachable from here at all: a second
    # write path would be exactly the drift Task 63 removed.
    assert "assign_member" not in constructed
    assert not hasattr(module, "assign_member")


def test_50_51_no_version_or_snapshot_state_is_mutated(session, head, world, monkeypatch):
    _stub(monkeypatch, world, scheduling_result=_result((600, 118, 700)))
    _stub_write_environment(monkeypatch)
    requirement_before = (
        world.requirement.event_date, world.requirement.required_count,
        world.requirement.ministry_role_id,
    )

    generate_draft_schedule(session, actor=head, version=world.version, policy=POLICY)

    assert (
        world.requirement.event_date, world.requirement.required_count,
        world.requirement.ministry_role_id,
    ) == requirement_before
    # Nothing here can reach the lifecycle operations either.
    import app.services.schedule_generation as module

    assert not hasattr(module, "submit_schedule_version_for_review")
    assert not hasattr(module, "finalize_schedule_version")
    assert not hasattr(module, "create_successor_schedule_version")


# --------------------------------------------------------------------------
# 52-53 -- The purity boundary
# --------------------------------------------------------------------------


def test_52_the_pure_scheduling_package_is_still_database_free():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; import app.scheduling;"
            " print(sorted(m for m in sys.modules if m.startswith("
            "('sqlalchemy','app.models','app.services'))))",
        ],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout


def test_53_the_orchestration_lives_in_services_not_in_the_scheduling_package():
    import app.scheduling as scheduling_package

    assert not hasattr(scheduling_package, "generate_draft_schedule")
    assert not hasattr(scheduling_package, "DraftGenerationResult")

    from app.services import generate_draft_schedule as exported

    assert exported.__module__ == "app.services.schedule_generation"


def test_the_result_type_is_frozen_and_reports_both_halves():
    result = DraftGenerationResult(scheduling_result=_result((600, 118, 700)))

    assert result.created_assignments == ()
    assert result.created_count == 0
    with pytest.raises(Exception):
        result.created_assignments = ()
    # The service-layer result may hold ORM rows; the pure result may not.
    assert set(DraftGenerationResult.__dataclass_fields__) == {
        "scheduling_result", "created_assignments",
    }


def test_the_period_and_latest_queries_are_scoped():
    period = _compile(_scheduling_period_statement(11))
    newer = _compile(_newer_version_exists_statement(400, 1))

    assert "scheduling_period.id = 11" in period
    assert "schedule_version.schedule_id = 400" in newer
    assert "schedule_version.version_number > 1" in newer
    assert "LIMIT 1" in newer
