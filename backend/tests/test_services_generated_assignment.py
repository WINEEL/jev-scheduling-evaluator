"""The batch writer for generated schedules (Task 63).

Offline: no PostgreSQL, no network.

Task 34 persisted a solver result one row at a time through
``assign_member``. A fourteen-Sunday Setup schedule cost **823 SQL statements
and 28.7 seconds**, against 1.5 seconds of actual solving: eleven round trips
per assignment, and against a hosted database query *count* is the response
time. This module writes the same rows in a fixed number of statements.

The tests are split to match what the writer actually claims:

- **Query count** is the headline, and is pinned as a *property* -- the count
  does not grow with the number of proposals -- rather than as a number a
  future refactor might legitimately change. One test does state a number, as
  a ceiling, so an accidental lazy load is visible.
- **Run-state accumulation** is the property batching most easily gets wrong:
  the fiftieth placement must be judged against the forty-nine before it, not
  against the database as it stood before the run.
- **Rule parity** is covered by ``tests/test_services_assignment_rules.py``
  (one definition, two writers) and by Task 22's own suite; here the tests
  check that each rule is actually *reached* with the right facts.
- **Atomicity** -- a refusal writes nothing at all.

Every person, ministry and date here is synthetic.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import EVENT_KIND_SUNDAY_SERVICE, Event
from app.scheduling.result import ProposedAssignment
from app.services import generated_assignment as writer
from app.services.audit import (
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.generated_assignment import persist_generated_assignments

#: Captured at import, before any test stubs it, so a test that wants the real
#: re-check gets the real one rather than whatever ``_install`` last installed.
REAL_REQUIRE_STILL_WRITABLE = writer._require_still_writable

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


class WriterSession(Session):
    """A real, unbound Session that hands out identities on flush.

    ``flush`` is permitted and counted -- the writer legitimately flushes once
    for the whole run, to obtain the identities its audit rows reference --
    while ``commit``, ``rollback`` and ``delete`` are forbidden.
    """

    def __init__(self, *, next_id: int = 9000) -> None:
        super().__init__(autoflush=False)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.delete_calls = 0
        self.flush_calls = 0
        self.flushed_batch_sizes: list[int] = []
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("the writer must never delete")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        self.flushed_batch_sizes.append(len(objects) if objects is not None else -1)
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> WriterSession:
    return WriterSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


def _head_of_the_ministry() -> Person:
    """An active Ministry Head of ministry 3 -- the only kind of person who may
    perform any of the writes in this module, since Task 80.

    A plain function as well as a fixture because one test builds its actor
    inside a loop rather than taking it as an argument.
    """
    person = _person(1, "Demo Admin")
    membership = MinistryMembership(
        person_id=person.id, ministry_id=3, is_ministry_head=True
    )
    membership.id = 9001
    person.ministry_memberships.append(membership)
    return person


@pytest.fixture
def head() -> Person:
    """The actor every operational write in this module is performed by."""
    return _head_of_the_ministry()


class World:
    """A latest DRAFT version with two Sundays, two roles and six volunteers.

    Big enough that "one query per assignment" and "a fixed number of
    queries" are visibly different, small enough to read.
    """

    def __init__(self, *, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
                 required_count: int = 1, sundays: int = 2, roles: int = 2,
                 volunteers: int = 6):
        self.ministry = Ministry(name="Setup", church_id=1)
        self.ministry.id = 3
        self.version = ScheduleVersion(
            schedule_id=400, scheduling_period_id=11, version_number=1, status=status,
        )
        self.version.id = 500

        self.roles = []
        for index in range(roles):
            role = MinistryRole(name=f"Role {index}", ministry_id=3)
            role.id = 12 + index
            role.ministry = self.ministry
            self.roles.append(role)

        self.events = []
        for index in range(sundays):
            event = Event(
                scheduling_period_id=11, ministry_id=3,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
                event_kind=EVENT_KIND_SUNDAY_SERVICE,
            )
            event.id = 700 + index
            self.events.append(event)

        self.requirements: list[ScheduleVersionRequirement] = []
        next_id = 600
        for event in self.events:
            for role in self.roles:
                requirement = ScheduleVersionRequirement(
                    schedule_version_id=500, event_id=event.id,
                    event_date=event.event_date, ministry_role_id=role.id,
                    scheduling_period_id=11, ministry_id=3,
                    required_count=required_count,
                )
                requirement.id = next_id
                requirement.schedule_version = self.version
                requirement.event = event
                requirement.ministry_role = role
                self.requirements.append(requirement)
                next_id += 1

        self.memberships: list[MinistryMembership] = []
        for index in range(volunteers):
            person = _person(40 + index, f"Volunteer {index}")
            membership = MinistryMembership(person_id=person.id, ministry_id=3)
            membership.id = 118 + index
            membership.person = person
            self.memberships.append(membership)

    def proposals(self, count: int | None = None) -> tuple[ProposedAssignment, ...]:
        """One proposal per requirement, each to a different volunteer -- the
        shape a real run produces, with no rule violated.
        """
        pairs = list(zip(self.requirements, self.memberships))
        if count is not None:
            pairs = pairs[:count]
        return tuple(
            ProposedAssignment(
                requirement_id=requirement.id,
                membership_id=membership.id,
                event_id=requirement.event_id,
            )
            for requirement, membership in pairs
        )


@pytest.fixture
def world() -> World:
    return World()


def _facts(
    world: World,
    *,
    existing=None,
    qualified: bool = True,
    unavailable=(),
    conflicted=(),
    serving_maximums=None,
    linked=None,
    filled=None,
    membership_events=(),
    memberships_by_date=None,
    event_sequence=None,
    events_by_membership=None,
    group_caps=(),
    support_requirements=(),
    memberships_by_event=None,
) -> writer._BatchFacts:
    """A prefetch built from plain values, so a test states the world rather
    than the eleven queries that would have read it.
    """
    return writer._BatchFacts(
        existing=dict(existing or {}),
        qualifications=(
            {(m.id, r.ministry_role_id) for m in world.memberships
             for r in world.requirements}
            if qualified else set()
        ),
        availability=set(unavailable),
        conflicts=set(conflicted),
        serving_maximums=dict(serving_maximums or {}),
        linked=dict(linked or {}),
        # Task 71: ``None`` is "this period configures no event-gap rule",
        # which is the world these tests describe unless one supplies a
        # sequence explicitly.
        event_sequence=event_sequence,
        filled_by_requirement=dict(filled or {}),
        held_by_membership={},
        membership_events=set(membership_events),
        # The same rows as ``membership_events``, inverted -- the shape the
        # event-gap rule asks its question in. Derived here so a test states
        # the world once.
        events_by_membership=(
            dict(events_by_membership)
            if events_by_membership is not None
            else {
                membership_id: {
                    event_id
                    for other_id, event_id in membership_events
                    if other_id == membership_id
                }
                for membership_id, _ in membership_events
            }
        ),
        memberships_by_date=dict(memberships_by_date or {}),
        # Task 74: empty is "this period configures neither group-shaped rule",
        # which is the world these tests describe unless one supplies them.
        group_caps_by_membership=writer._group_caps_by_membership(group_caps),
        support_by_membership={
            requirement.subject_membership_id: requirement
            for requirement in support_requirements
        },
        # The same rows as ``membership_events`` again, keyed by event -- the
        # shape the member-group cap and the support rule ask their question
        # in. Derived here so a test still states the world once.
        memberships_by_event=(
            dict(memberships_by_event)
            if memberships_by_event is not None
            else {
                event_id: {
                    other_id
                    for other_id, other_event in membership_events
                    if other_event == event_id
                }
                for _, event_id in membership_events
            }
        ),
    )


def _install(monkeypatch, world: World, facts: writer._BatchFacts, *,
             newer_version_exists: bool = False, requirements=None,
             memberships=None):
    """Replace the writer's reads, leaving every rule and every write real."""
    resolved_requirements = (
        {r.id: r for r in world.requirements} if requirements is None else requirements
    )
    resolved_memberships = (
        {m.id: m for m in world.memberships} if memberships is None else memberships
    )
    monkeypatch.setattr(
        writer, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: newer_version_exists,
    )
    # The final pre-write re-check is its own single query, not a repeat of
    # the entry probe. Stubbed to "nothing changed", which is what these tests
    # are about; its own behaviour has its own tests below.
    monkeypatch.setattr(
        writer, "_require_still_writable", lambda session, *, schedule_version: None,
    )
    monkeypatch.setattr(
        writer, "_load_requirements",
        lambda session, *, schedule_version_id, requirement_ids: {
            k: v for k, v in resolved_requirements.items() if k in requirement_ids
        },
    )
    monkeypatch.setattr(
        writer, "_load_memberships",
        lambda session, *, ministry_id, membership_ids: {
            k: v for k, v in resolved_memberships.items() if k in membership_ids
        },
    )
    monkeypatch.setattr(
        writer._BatchFacts, "prefetch",
        classmethod(lambda cls, session, **kwargs: facts),
    )


def _persist(session, world, head, proposals):
    return persist_generated_assignments(
        session, actor=head, version=world.version, ministry_id=3,
        proposals=proposals,
    )


def _assignments(session) -> list[Assignment]:
    return [o for o in session.new if isinstance(o, Assignment)]


def _audit(session) -> list[AuditEvent]:
    return [o for o in session.new if isinstance(o, AuditEvent)]


def _compile(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# ==========================================================================
# The query count -- the whole reason this module exists
# ==========================================================================


class _RecordingSession:
    """Counts statements and returns nothing, so only the shape is measured."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(
            scalars=lambda: iter(()), all=lambda: [], scalar_one_or_none=lambda: None,
        )


def _prefetch_statement_count(world: World, *, proposals: int) -> int:
    session = _RecordingSession()
    pairs = list(zip(world.requirements * 20, world.memberships * 20))[:proposals]
    writer._BatchFacts.prefetch(
        session,
        version=world.version,
        ministry_id=world.ministry.id,
        requirements=[requirement for requirement, _ in pairs],
        memberships=[membership for _, membership in pairs],
    )
    return len(session.statements)


def test_the_prefetch_query_count_does_not_grow_with_the_number_of_proposals():
    """The N+1 regression guard, expressed as a property rather than a number.

    This is the property Task 63 bought: Task 34's persistence cost eleven
    statements *per proposal*, so a fourteen-Sunday Setup schedule cost 823.
    Here, one proposal and seventy cost the same.
    """
    world = World(sundays=2, roles=2, volunteers=6)

    counts = [
        _prefetch_statement_count(world, proposals=size) for size in (1, 4, 20, 70)
    ]

    assert counts[0] == counts[1] == counts[2] == counts[3]


def test_the_prefetch_is_a_small_fixed_number_of_set_based_reads():
    """A ceiling, not an exact contract: a future refactor may legitimately
    merge two of these, but an accidental per-row read would blow straight
    past it.
    """
    world = World()

    # Existing assignments, qualifications, availability, serving limits,
    # same-date exclusions, the period's event-gap rule (Task 71), the
    # member-group caps and the same-event support requirements (Task 74), and
    # the two halves of the church-wide conflict rule. The gap rule reads as
    # unconfigured here, so it costs the one lookup and loads no ministry
    # history; the two Task 74 rules likewise read as unconfigured, so each
    # costs the one lookup and loads no members or supporters.
    assert _prefetch_statement_count(world, proposals=4) == 10


def test_the_whole_run_issues_a_fixed_number_of_statements(monkeypatch):
    """End to end, counting every statement the writer causes: the mutability
    probe, the two resolution reads and the real prefetch -- whatever the
    number of proposals. Two multi-row INSERTs follow at the caller's flush.

    Compare Task 34, whose persistence cost ``11 * proposals + 2``.
    """
    counted: list[int] = []
    written: list[int] = []

    for size, shape in ((4, dict(sundays=2, roles=2)), (24, dict(sundays=6, roles=4))):
        world = World(volunteers=24, **shape)
        statements = 0
        real_prefetch = writer._BatchFacts.prefetch.__func__

        class _CountingSession(WriterSession):
            def execute(self, statement, *args, **kwargs):  # noqa: ANN001
                nonlocal statements
                statements += 1
                return SimpleNamespace(
                    scalars=lambda: iter(()), all=lambda: [],
                    scalar_one_or_none=lambda: None,
                )

        def counting_prefetch(cls, session, *, version, ministry_id, requirements,
                              memberships, w=world, real=real_prefetch):
            # The real prefetch runs, so every statement it issues is counted;
            # the empty answers it gets back would refuse every placement, so
            # a permissive set of facts is returned in their place.
            real(cls, session, version=version, ministry_id=ministry_id,
                 requirements=requirements, memberships=memberships)
            return _facts(w)

        monkeypatch.setattr(
            writer._BatchFacts, "prefetch", classmethod(counting_prefetch)
        )
        # The real re-check would reject the fake session's empty answer, so
        # it is replaced by something that issues exactly the one statement it
        # issues -- the count stays honest without the fake result having to
        # be well formed.
        monkeypatch.setattr(
            writer, "_require_still_writable",
            lambda session, *, schedule_version: session.execute(
                writer._writability_statement(
                    schedule_version.schedule_id, schedule_version.id,
                    schedule_version.version_number,
                )
            ),
        )
        monkeypatch.setattr(
            writer, "_load_requirements",
            lambda s, *, schedule_version_id, requirement_ids, w=world: {
                r.id: r for r in w.requirements if r.id in requirement_ids
            },
        )
        monkeypatch.setattr(
            writer, "_load_memberships",
            lambda s, *, ministry_id, membership_ids, w=world: {
                m.id: m for m in w.memberships if m.id in membership_ids
            },
        )

        session = _CountingSession()
        created = _persist(session, world, _head_of_the_ministry(),
                           world.proposals(size))

        # The two loaders were stubbed out, so add them back: each is one
        # query in production, and neither depends on the proposal count.
        counted.append(statements + 2)
        written.append(len(created))

    assert written == [4, 24]
    assert counted[0] == counted[1]
    # Ten prefetch reads, two resolution reads, and the two writability
    # checks -- the fail-fast one on entry and the fresh re-check immediately
    # before the INSERT.
    assert counted[0] == 14


def test_the_whole_run_flushes_once_not_once_per_row(session, head, world, monkeypatch):
    """Task 34 flushed per created row, to obtain the identity its audit row
    referenced. One flush for the whole run obtains all of them.
    """
    _install(monkeypatch, world, _facts(world))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 4
    assert session.flush_calls == 1
    assert session.flushed_batch_sizes == [4]


def test_nothing_is_flushed_when_there_is_nothing_to_write(session, head, world,
                                                           monkeypatch):
    _install(monkeypatch, world, _facts(world))

    assert _persist(session, world, head, ()) == ()
    assert session.flush_calls == 0
    assert _assignments(session) == []
    assert _audit(session) == []


def test_no_proposals_means_no_queries_at_all(head, world):
    session = _RecordingSession()

    assert persist_generated_assignments(
        session, actor=head, version=world.version, ministry_id=3, proposals=(),
    ) == ()
    assert session.statements == []


# ==========================================================================
# Run-state accumulation: the fiftieth row is judged against the forty-nine
# ==========================================================================


def test_capacity_counts_rows_this_run_created(session, head, world, monkeypatch):
    """Two proposals against one ``required_count=1`` requirement: the first
    fills it and the second is refused -- exactly as ``assign_member`` refused
    it by re-counting between rows.
    """
    world = World(required_count=1)
    _install(monkeypatch, world, _facts(world))
    requirement = world.requirements[0]
    proposals = (
        ProposedAssignment(requirement_id=requirement.id,
                           membership_id=world.memberships[0].id,
                           event_id=requirement.event_id),
        ProposedAssignment(requirement_id=requirement.id,
                           membership_id=world.memberships[1].id,
                           event_id=requirement.event_id),
    )

    with pytest.raises(InvalidOperationError, match="already fully staffed"):
        _persist(session, world, head, proposals)


def test_a_required_count_above_one_admits_exactly_that_many(session, head,
                                                             monkeypatch):
    world = World(required_count=2)
    _install(monkeypatch, world, _facts(world))
    requirement = world.requirements[0]
    proposals = tuple(
        ProposedAssignment(requirement_id=requirement.id, membership_id=m.id,
                           event_id=requirement.event_id)
        for m in world.memberships[:2]
    )

    created = _persist(session, world, head, proposals)

    assert len(created) == 2


def test_one_person_cannot_be_placed_twice_in_one_event_by_the_same_run(
    session, head, monkeypatch
):
    """Two roles, one event, one volunteer: the §10 rule must see the row this
    run created a moment ago, not only rows that were there before it started.
    """
    world = World(sundays=1, roles=2)
    _install(monkeypatch, world, _facts(world))
    volunteer = world.memberships[0]
    proposals = tuple(
        ProposedAssignment(requirement_id=r.id, membership_id=volunteer.id,
                           event_id=r.event_id)
        for r in world.requirements
    )

    with pytest.raises(InvalidOperationError, match="already fills a different"):
        _persist(session, world, head, proposals)


def test_the_serving_maximum_counts_rows_this_run_created(session, head,
                                                          monkeypatch):
    """A volunteer with a maximum of one, proposed for two Sundays: the second
    placement is refused, and the refusal reports the run's own first row.
    """
    world = World(sundays=2, roles=1)
    volunteer = world.memberships[0]
    _install(
        monkeypatch, world,
        _facts(world, serving_maximums={volunteer.id: 1}),
    )
    proposals = tuple(
        ProposedAssignment(requirement_id=r.id, membership_id=volunteer.id,
                           event_id=r.event_id)
        for r in world.requirements
    )

    with pytest.raises(InvalidOperationError, match=r"\(1 of 1 already assigned\)"):
        _persist(session, world, head, proposals)


def test_a_serving_maximum_seeded_from_existing_rows_is_respected(session, head,
                                                                  monkeypatch):
    """The count starts from what the version already holds, not from zero."""
    world = World(sundays=2, roles=1)
    volunteer = world.memberships[0]
    facts = _facts(world, serving_maximums={volunteer.id: 1})
    facts._held_by_membership[volunteer.id] = 1
    _install(monkeypatch, world, facts)

    with pytest.raises(InvalidOperationError, match=r"\(1 of 1 already assigned\)"):
        _persist(session, world, head, world.proposals(1))


def test_two_linked_members_cannot_be_seated_on_one_date_by_the_same_run(
    session, head, monkeypatch
):
    """The pair rule must see the run's own first placement. Two roles on one
    Sunday, two linked volunteers.
    """
    world = World(sundays=1, roles=2)
    first, second = world.memberships[0], world.memberships[1]
    _install(
        monkeypatch, world,
        _facts(world, linked={first.id: frozenset({second.id}),
                              second.id: frozenset({first.id})}),
    )
    proposals = (
        ProposedAssignment(requirement_id=world.requirements[0].id,
                           membership_id=first.id,
                           event_id=world.requirements[0].event_id),
        ProposedAssignment(requirement_id=world.requirements[1].id,
                           membership_id=second.id,
                           event_id=world.requirements[1].event_id),
    )

    with pytest.raises(InvalidOperationError, match="same-date exclusion"):
        _persist(session, world, head, proposals)


def test_linked_members_on_different_dates_are_fine(session, head, monkeypatch):
    """The rule is about one calendar date, not about the pair existing."""
    world = World(sundays=2, roles=1)
    first, second = world.memberships[0], world.memberships[1]
    _install(
        monkeypatch, world,
        _facts(world, linked={first.id: frozenset({second.id}),
                              second.id: frozenset({first.id})}),
    )
    proposals = (
        ProposedAssignment(requirement_id=world.requirements[0].id,
                           membership_id=first.id,
                           event_id=world.requirements[0].event_id),
        ProposedAssignment(requirement_id=world.requirements[1].id,
                           membership_id=second.id,
                           event_id=world.requirements[1].event_id),
    )

    assert len(_persist(session, world, head, proposals)) == 2


def test_the_pair_rule_compares_the_snapshot_date_not_the_event(session, head,
                                                                monkeypatch):
    """Two events on one Sunday is one date -- which the one-position-per-event
    rule cannot see, and this one must.
    """
    world = World(sundays=2, roles=1)
    world.requirements[1].event_date = world.requirements[0].event_date
    first, second = world.memberships[0], world.memberships[1]
    _install(
        monkeypatch, world,
        _facts(world, linked={first.id: frozenset({second.id}),
                              second.id: frozenset({first.id})}),
    )
    proposals = (
        ProposedAssignment(requirement_id=world.requirements[0].id,
                           membership_id=first.id,
                           event_id=world.requirements[0].event_id),
        ProposedAssignment(requirement_id=world.requirements[1].id,
                           membership_id=second.id,
                           event_id=world.requirements[1].event_id),
    )

    with pytest.raises(InvalidOperationError, match="same-date exclusion"):
        _persist(session, world, head, proposals)


def test_every_run_varying_fact_is_advanced_when_a_placement_is_accepted():
    """A fact that is seeded but never advanced would make its rule blind to
    the run's own output. Checked against the recorder's actual state.
    """
    world = World()
    facts = _facts(world)
    requirement, membership = world.requirements[0], world.memberships[0]
    assignment = Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id, schedule_version_id=500,
        event_id=requirement.event_id, ministry_id=3,
    )

    facts.record_accepted(
        assignment=assignment, requirement=requirement, membership=membership
    )

    assert facts.exact_assignment(
        requirement_id=requirement.id, membership_id=membership.id
    ) is assignment
    assert facts.fills_other_position_in_event(
        membership_id=membership.id, event_id=requirement.event_id
    )
    assert facts.overridable_facts(
        requirement=requirement, membership=membership
    ).current_filled_count == 1
    facts._serving_maximums[membership.id] = 5
    assert facts.serving_limit(membership_id=membership.id).held == 1
    facts._linked[membership.id] = frozenset({999})
    facts._memberships_by_date[requirement.event_date].add(999)
    assert facts.linked_member_assigned_on_date(
        membership_id=membership.id, event_date=requirement.event_date
    )


# ==========================================================================
# Each rule is reached, with the right facts
# ==========================================================================


def test_a_clean_run_writes_a_row_and_an_audit_row_for_every_proposal(
    session, head, world, monkeypatch
):
    _install(monkeypatch, world, _facts(world))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 4
    assert len(_assignments(session)) == 4
    rows = _audit(session)
    assert len(rows) == 4
    assert {row.action for row in rows} == {ACTION_ASSIGNMENT_ADDED}
    assert {row.target_table for row in rows} == {"assignment"}
    assert [row.target_id for row in rows] == [a.id for a in created]


def test_generated_rows_are_never_overrides(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world))

    created = _persist(session, world, head, world.proposals())

    assert all(a.is_override is False for a in created)
    assert all(a.override_reason is None for a in created)
    assert all(row.reason is None for row in _audit(session))
    assert all(
        "overridden_blockers" not in (row.after_values or {}) for row in _audit(session)
    )
    assert ACTION_ASSIGNMENT_OVERRIDE_APPLIED not in {
        row.action for row in _audit(session)
    }


@pytest.mark.parametrize(
    "facts_kwargs,message",
    [
        ({"qualified": False}, "not currently qualified"),
        ({"unavailable": {(118, 700)}}, "marked unavailable"),
    ],
)
def test_an_overridable_blocker_fails_the_run_rather_than_overriding_it(
    session, head, world, monkeypatch, facts_kwargs, message
):
    """Automatic work never overrides: a blocker that arose after the input was
    built stops the run rather than being bypassed or skipped.
    """
    _install(monkeypatch, world, _facts(world, **facts_kwargs))

    with pytest.raises(InvalidOperationError, match=message):
        _persist(session, world, head, world.proposals())


def test_a_cross_ministry_conflict_fails_the_run_as_an_absolute_rule(
    session, head, world, monkeypatch
):
    """**The church-wide hard rule, in the batch writer.**

    The solver already removes a blocked date from the candidate's domain, so
    a conflicting proposal should not exist. If one arrives anyway -- because
    another ministry finalized between building the input and persisting the
    result -- the run fails rather than writing it. Since Task 79's correction
    this is an *absolute* rule, so it is refused with the other absolutes
    rather than through the overridable catalogue, and nothing the run could
    say would change the answer.
    """
    _install(monkeypatch, world, _facts(world, conflicted={(40, NOV_15)}))

    with pytest.raises(InvalidOperationError) as caught:
        _persist(session, world, head, world.proposals())
    message = str(caught.value)
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in message
    assert "not overridable" in message


def test_a_deactivated_role_fails_the_run(session, head, world, monkeypatch):
    world.roles[0].deactivated_at = DEACTIVATED
    _install(monkeypatch, world, _facts(world))

    with pytest.raises(InvalidOperationError, match="role has been deactivated"):
        _persist(session, world, head, world.proposals())


def test_a_cancelled_event_fails_the_run(session, head, world, monkeypatch):
    world.events[0].cancelled_at = DEACTIVATED
    _install(monkeypatch, world, _facts(world))

    with pytest.raises(InvalidOperationError, match="cancelled event"):
        _persist(session, world, head, world.proposals())


def test_a_deactivated_membership_or_person_fails_the_run(session, head, world,
                                                          monkeypatch):
    world.memberships[0].deactivated_at = DEACTIVATED
    _install(monkeypatch, world, _facts(world))

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        _persist(session, world, head, world.proposals())


def test_the_conflict_fact_is_keyed_by_person_and_snapshot_date(session, head,
                                                                world, monkeypatch):
    """Keyed on the requirement's frozen date, so an Event that has since moved
    cannot silently relocate the question.
    """
    world.requirements[0].event.event_date = NOV_22  # the live row moves
    _install(monkeypatch, world, _facts(world, conflicted={(40, NOV_22)}))

    # The conflict is recorded against the *new* live date, which the rule
    # must not consult -- so the run succeeds.
    assert len(_persist(session, world, head, world.proposals(1))) == 1


def test_no_response_is_not_a_blocker(session, head, world, monkeypatch):
    """Absence of an availability answer is a third state, and generation does
    not turn it into a rejection here -- the solver's own policy decides
    whether to place a no-response candidate at all.
    """
    _install(monkeypatch, world, _facts(world, unavailable=set()))

    assert len(_persist(session, world, head, world.proposals())) == 4


# ==========================================================================
# Idempotency
# ==========================================================================


def test_a_proposal_that_already_exists_yields_the_existing_row(session, head,
                                                                world, monkeypatch):
    requirement, membership = world.requirements[0], world.memberships[0]
    existing = Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id, schedule_version_id=500,
        event_id=requirement.event_id, ministry_id=3,
    )
    existing.id = 77
    _install(
        monkeypatch, world,
        _facts(world, existing={(requirement.id, membership.id): existing}),
    )

    created = _persist(session, world, head, world.proposals(1))

    assert created == (existing,)
    assert _assignments(session) == []   # no second row
    assert _audit(session) == []         # and no audit for a row not created


def test_the_same_pair_proposed_twice_in_one_run_yields_one_row(session, head,
                                                                world, monkeypatch):
    _install(monkeypatch, world, _facts(world))
    proposal = world.proposals(1)[0]

    created = _persist(session, world, head, (proposal, proposal))

    assert len(created) == 2
    assert created[0] is created[1]
    assert len(_assignments(session)) == 1


# ==========================================================================
# Gates: authorization, mutability, resolution
# ==========================================================================


def test_a_normal_member_may_not_write(session, world, monkeypatch):
    ordinary = _person(6, "Ordinary")
    _install(monkeypatch, world, _facts(world))

    with pytest.raises(AuthorizationError):
        _persist(session, world, ordinary, world.proposals())
    assert _assignments(session) == []


def test_a_finalized_version_is_refused_before_anything_is_read(session, head,
                                                                monkeypatch):
    world = World(status=SCHEDULE_VERSION_STATUS_FINALIZED)
    _install(monkeypatch, world, _facts(world))

    with pytest.raises(InvalidOperationError, match="finalized schedule version"):
        _persist(session, world, head, world.proposals())


def test_a_superseded_version_is_refused(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world), newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded by a newer version"):
        _persist(session, world, head, world.proposals())


def test_the_mutability_gate_runs_at_write_time_not_only_before_the_solver(
    session, head, world, monkeypatch
):
    """It is the caller's pre-solve check repeated *after* the solve, because
    "before the solver" is not now.
    """
    probes: list[tuple[int, int]] = []
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: (
            probes.append((schedule_id, version_number)) or False
        ),
    )

    _persist(session, world, head, world.proposals())

    assert probes == [(400, 1)]  # once for the run, not once per row


# ==========================================================================
# The pre-write writability re-check (Task 63 concurrency review)
# ==========================================================================


def _writability_rows(world: World, *, status=SCHEDULE_VERSION_STATUS_DRAFT,
                      successor: bool = False):
    rows = [(world.version.id, status)]
    if successor:
        rows.append((world.version.id + 1, SCHEDULE_VERSION_STATUS_DRAFT))
    return rows


def _install_writability(monkeypatch, rows):
    """Answer the re-check's one query with ``rows``, recording the call."""
    seen: list[object] = []

    class _Result:
        def __init__(self, value):
            self._value = value

        def all(self):
            return self._value

    real_execute = WriterSession.execute

    def patched(self, statement, *args, **kwargs):
        compiled = str(statement)
        if "schedule_version" in compiled and "status" in compiled:
            seen.append(statement)
            return _Result(rows)
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(WriterSession, "execute", patched)
    return seen


def test_the_re_check_runs_after_every_rule_and_before_the_write(
    session, head, world, monkeypatch
):
    """Its whole value is its position: it is the only check evaluated after
    the prefetch, so it must run once the rules have passed and before
    anything is added to the session.
    """
    order: list[str] = []
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable",
        lambda session, *, schedule_version: order.append("re-check"),
    )
    real_build = writer.build_assignment
    monkeypatch.setattr(
        writer, "build_assignment",
        lambda **kwargs: (order.append("rule"), real_build(**kwargs))[1],
    )
    real_add_all = WriterSession.add_all
    monkeypatch.setattr(
        WriterSession, "add_all",
        lambda self, objs: (order.append("write"), real_add_all(self, objs))[1],
    )

    _persist(session, world, head, world.proposals())

    assert order == ["rule"] * 4 + ["re-check", "write"]


def test_the_re_check_refuses_a_version_finalized_since_the_prefetch(
    session, head, world, monkeypatch
):
    """The dangerous one: rows added to a finalized version are rows added to
    published history, and finalization readiness would report the version
    ready, because every individual row really is valid.
    """
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable", REAL_REQUIRE_STILL_WRITABLE
    )
    _install_writability(
        monkeypatch, _writability_rows(world, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    )

    with pytest.raises(InvalidOperationError, match="finalized schedule version"):
        _persist(session, world, head, world.proposals())

    assert _assignments(session) == []
    assert session.flush_calls == 0


def test_the_re_check_refuses_a_version_superseded_since_the_prefetch(
    session, head, world, monkeypatch
):
    """Task 34 re-probed this before *every* row. Batching replaced N probes
    with one on entry; this is the one that restores the parity.
    """
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable", REAL_REQUIRE_STILL_WRITABLE
    )
    _install_writability(monkeypatch, _writability_rows(world, successor=True))

    with pytest.raises(InvalidOperationError, match="superseded by a newer version"):
        _persist(session, world, head, world.proposals())

    assert _assignments(session) == []
    assert session.flush_calls == 0


def test_the_re_check_refuses_a_version_whose_row_has_gone(
    session, head, world, monkeypatch
):
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable", REAL_REQUIRE_STILL_WRITABLE
    )
    _install_writability(monkeypatch, [])

    with pytest.raises(InvalidOperationError, match="no longer exists"):
        _persist(session, world, head, world.proposals())


def test_the_re_check_passes_when_nothing_changed(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable", REAL_REQUIRE_STILL_WRITABLE
    )
    seen = _install_writability(monkeypatch, _writability_rows(world))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 4
    # One query for the whole run, not one per row.
    assert len(seen) == 1


def test_the_re_check_asks_for_this_version_and_any_newer_one_in_one_query():
    sql = _compile(writer._writability_statement(400, 500, 1))

    assert "schedule_version.schedule_id = 400" in sql
    assert "schedule_version.id = 500" in sql
    assert "schedule_version.version_number > 1" in sql
    assert " OR " in sql                      # both halves, one round trip
    assert "schedule_version.status" in sql   # read fresh, not off the ORM row


def test_the_live_status_is_read_from_the_database_not_the_orm_row(
    session, head, world, monkeypatch
):
    """The ORM object's ``status`` was loaded when the request began and
    ``flush()`` never expires it, so re-reading the attribute would return the
    same answer however long ago it was read. The re-check must not do that.
    """
    _install(monkeypatch, world, _facts(world))
    monkeypatch.setattr(
        writer, "_require_still_writable", REAL_REQUIRE_STILL_WRITABLE
    )
    _install_writability(
        monkeypatch, _writability_rows(world, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    )

    # The in-memory object still says DRAFT; only the database knows better.
    assert world.version.status == SCHEDULE_VERSION_STATUS_DRAFT
    with pytest.raises(InvalidOperationError, match="finalized schedule version"):
        _persist(session, world, head, world.proposals())


def test_an_unresolvable_requirement_is_rejected(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world), requirements={})

    with pytest.raises(InvalidOperationError,
                       match="does not belong to schedule version"):
        _persist(session, world, head, world.proposals())


def test_an_unresolvable_membership_is_rejected(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world), memberships={})

    with pytest.raises(InvalidOperationError, match="does not belong to ministry"):
        _persist(session, world, head, world.proposals())


def test_resolution_is_scoped_to_this_version_and_ministry():
    """Both scopes live in SQL, so a row from another version or ministry
    simply does not come back and the proposal reports as unresolvable.
    """
    requirements = _compile(writer._requirements_statement(500, [600, 601]))
    memberships = _compile(writer._memberships_statement(3, [118, 119]))

    assert "schedule_version_requirement.schedule_version_id = 500" in requirements
    assert "schedule_version_requirement.id IN (600, 601)" in requirements
    assert "ministry_membership.ministry_id = 3" in memberships
    assert "ministry_membership.id IN (118, 119)" in memberships
    # Activity is deliberately not *filtered*: a deactivated membership must
    # reach the rules and be refused there, with its own clear error. (The
    # column appears in the SELECT list of a whole-entity select, which is why
    # only the WHERE clause is inspected.)
    assert "deactivated_at" not in memberships.split("WHERE")[1]


def test_the_rows_the_rules_read_on_every_proposal_are_eager_loaded():
    """``event.cancelled_at``, ``ministry_role.deactivated_at`` and the role's
    and ministry's names are read for every proposal. Left lazy they would be
    a round trip per distinct event and role -- a per-row query reintroduced
    inside the batch.
    """
    requirements = _compile(writer._requirements_statement(500, [600]))
    memberships = _compile(writer._memberships_statement(3, [118]))

    assert "LEFT OUTER JOIN event" in requirements
    assert "LEFT OUTER JOIN ministry_role" in requirements
    assert "LEFT OUTER JOIN ministry " in requirements
    assert "LEFT OUTER JOIN person" in memberships


def test_the_existing_assignment_read_uses_the_snapshot_date_and_this_version():
    sql = _compile(writer._existing_assignments_statement(500))

    assert "assignment.schedule_version_id = 500" in sql
    assert "schedule_version_requirement.event_date" in sql
    # Reached through the requirement, never through the live event row.
    assert "JOIN schedule_version_requirement" in sql
    assert "event.event_date" not in sql


def test_the_prefetch_reads_are_set_based():
    qualifications = _compile(writer._qualifications_statement([118, 119], [12, 13]))
    availability = _compile(writer._availability_statement([118, 119], [700, 701]))

    assert "role_qualification.ministry_membership_id IN (118, 119)" in qualifications
    assert "role_qualification.ministry_role_id IN (12, 13)" in qualifications
    assert "availability.ministry_membership_id IN (118, 119)" in availability
    assert "availability.event_id IN (700, 701)" in availability


def test_the_linked_pair_inversion_reads_both_sides():
    """A membership may be stored as either half, because the pair is
    unordered.
    """
    linked = writer._linked_membership_ids([(118, 119), (119, 120)])

    assert linked[118] == frozenset({119})
    assert linked[119] == frozenset({118, 120})
    assert linked[120] == frozenset({119})


# ==========================================================================
# Atomicity and the transaction boundary
# ==========================================================================


def test_a_refusal_part_way_through_writes_nothing_at_all(session, head,
                                                          monkeypatch):
    """Task 34 left earlier rows in the session for the caller's ROLLBACK.
    Nothing is flushed until every placement has been accepted, so a refused
    run has written nothing -- a strictly stronger property, reached by
    batching rather than by a compensating delete.
    """
    world = World(sundays=1, roles=2)
    _install(monkeypatch, world, _facts(world))
    volunteer = world.memberships[0]
    proposals = tuple(
        ProposedAssignment(requirement_id=r.id, membership_id=volunteer.id,
                           event_id=r.event_id)
        for r in world.requirements
    )

    with pytest.raises(InvalidOperationError):
        _persist(session, world, head, proposals)

    assert _assignments(session) == []
    assert _audit(session) == []
    assert session.flush_calls == 0
    assert session.delete_calls == 0
    assert session.rollback_calls == 0


def test_the_writer_never_commits_or_rolls_back(session, head, world, monkeypatch):
    _install(monkeypatch, world, _facts(world))

    _persist(session, world, head, world.proposals())

    assert session.commit_calls == 0
    assert session.rollback_calls == 0

    tree = ast.parse(Path(writer.__file__).read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "commit" not in called
    assert "rollback" not in called
    assert "delete" not in called


def test_the_order_the_solver_chose_is_the_order_that_is_written(session, head,
                                                                 world, monkeypatch):
    """Nothing is reordered to make batching easier: a run that would have
    been refused at the third placement is still refused at the third.
    """
    _install(monkeypatch, world, _facts(world))
    proposals = world.proposals()

    created = _persist(session, world, head, proposals)

    assert [
        (a.schedule_version_requirement_id, a.ministry_membership_id) for a in created
    ] == [(p.requirement_id, p.membership_id) for p in proposals]


def test_the_writer_records_no_audit_action_of_its_own():
    """One created assignment, one creation audit row -- and no extra row
    saying "the solver ran".
    """
    assert not [name for name in vars(writer) if name.startswith("ACTION_")]
    assert not hasattr(writer, "record_audit_event")


# ==========================================================================
# The ministry event-gap rule (Task 71)
#
# The batch writer's half: the sequence and the events either side of it are
# read once for the run, while which events each member occupies *in this
# version* is advanced per accepted row. The rule's own meaning is pinned in
# ``tests/test_services_event_gap.py``; what these check is that the writer
# reaches it, and that a run cannot create a violation out of its own output.
# ==========================================================================


def _gap_sequence(world: World, *, gap: int = 1, adjacent=None):
    """The ministry's sequence around this world's own events.

    ``world`` builds two events, 700 and 701. 699 sits before them and 702
    after, so a test can commit somebody on either side without that event
    belonging to the version.
    """
    from app.services.event_gap import MinistryEventSequence

    return MinistryEventSequence(
        min_intervening_events=gap,
        events=(
            (699, NOV_15 - datetime.timedelta(days=7)),
            (700, NOV_15),
            (701, NOV_22),
            (702, NOV_22 + datetime.timedelta(days=7)),
        ),
        adjacent_events_by_membership=adjacent or {},
    )


def test_a_run_cannot_place_one_person_at_two_consecutive_events(
    session, head, world, monkeypatch
):
    """The run-varying half. Both proposals are legitimate on the pre-run
    state; the second is refused only because the first was accepted.
    """
    _install(monkeypatch, world, _facts(world, event_sequence=_gap_sequence(world)))
    membership = world.memberships[0]
    proposals = tuple(
        ProposedAssignment(
            requirement_id=requirement.id,
            membership_id=membership.id,
            event_id=requirement.event_id,
        )
        # The first requirement at each of the two events.
        for requirement in (world.requirements[0], world.requirements[2])
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        _persist(session, world, head, proposals)

    # Atomic: the accepted first row is discarded with the run.
    assert _assignments(session) == []


def test_a_commitment_before_the_window_refuses_a_proposal(
    session, head, world, monkeypatch
):
    membership = world.memberships[0]
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            event_sequence=_gap_sequence(
                world, adjacent={membership.id: frozenset({699})}
            ),
        ),
    )

    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        _persist(
            session, world, head,
            (
                ProposedAssignment(
                    requirement_id=world.requirements[0].id,
                    membership_id=membership.id,
                    event_id=world.requirements[0].event_id,
                ),
            ),
        )


def test_a_commitment_after_the_window_refuses_a_proposal(
    session, head, world, monkeypatch
):
    """The forward boundary in the batch writer. Nothing in this version
    involves the member; the block is entirely the next quarter's published
    schedule.
    """
    membership = world.memberships[0]
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            event_sequence=_gap_sequence(
                world, adjacent={membership.id: frozenset({702})}
            ),
        ),
    )

    # world.requirements[2] is the first requirement at the *last* event.
    with pytest.raises(InvalidOperationError, match="MIN_EVENT_GAP_CONFLICT"):
        _persist(
            session, world, head,
            (
                ProposedAssignment(
                    requirement_id=world.requirements[2].id,
                    membership_id=membership.id,
                    event_id=world.requirements[2].event_id,
                ),
            ),
        )


def test_a_commitment_outside_the_window_leaves_the_far_end_alone(
    session, head, world, monkeypatch
):
    """Committed after the window, proposed at the event furthest from it: two
    positions away, so a gap of one does not reach.
    """
    membership = world.memberships[0]
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            event_sequence=_gap_sequence(
                world, adjacent={membership.id: frozenset({702})}
            ),
        ),
    )

    created = _persist(
        session, world, head,
        (
            ProposedAssignment(
                requirement_id=world.requirements[0].id,
                membership_id=membership.id,
                event_id=world.requirements[0].event_id,
            ),
        ),
    )

    assert len(created) == 1


def test_no_configured_rule_leaves_the_batch_writer_unchanged(
    session, head, world, monkeypatch
):
    """The legacy guarantee for this writer: with no sequence, consecutive
    events for one person are written without complaint.
    """
    _install(monkeypatch, world, _facts(world, event_sequence=None))
    membership = world.memberships[0]
    proposals = tuple(
        ProposedAssignment(
            requirement_id=requirement.id,
            membership_id=membership.id,
            event_id=requirement.event_id,
        )
        for requirement in (world.requirements[0], world.requirements[2])
    )

    created = _persist(session, world, head, proposals)

    assert len(created) == 2


# ==========================================================================
# Task 74: the member-group cap and the same-event support requirement
#
# Both rules' own meaning is pinned in their service tests and their solver
# behaviour in ``tests/test_scheduling_member_group_cap.py`` and
# ``tests/test_scheduling_same_event_support.py``. What these assert is that the
# *batch* writer applies them exactly as ``assign_member`` does -- including
# against rows this very run created, which is the property batching risks
# losing.
# ==========================================================================


def _cap(max_per_event: int, members, *, group_id: int = 400,
         name: str = "Category A"):
    from app.services.member_group import MemberGroupCapConfig

    return MemberGroupCapConfig(
        member_group_id=group_id, member_group_name=name,
        max_per_event=max_per_event, member_membership_ids=frozenset(members),
    )


def _support(minimum: int, supporters, *, subject: int):
    from app.services.same_event_support import SupportRequirementConfig

    return SupportRequirementConfig(
        support_requirement_id=1, subject_membership_id=subject,
        min_supporters=minimum, supporter_membership_ids=frozenset(supporters),
    )


def test_no_configured_task74_rule_leaves_the_batch_writer_unchanged(
    session, head, world, monkeypatch
):
    """The legacy-behaviour guarantee: neither rule configured, every proposal
    written exactly as before.
    """
    _install(monkeypatch, world, _facts(world))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 4
    assert len(_assignments(session)) == 4


def test_a_run_cannot_put_more_of_a_group_on_one_event_than_the_cap_allows(
    session, head, world, monkeypatch
):
    """The placements are all this run's own: nothing existed before it.

    A per-row writer re-counting from the database would catch this; a batch
    writer that did not advance its own state would not, which is exactly the
    bug this test exists for.
    """
    world = World(sundays=1, roles=3, volunteers=3)
    cap = _cap(2, (118, 119, 120))
    _install(monkeypatch, world, _facts(world, group_caps=(cap,)))

    with pytest.raises(InvalidOperationError) as raised:
        _persist(session, world, head, world.proposals())

    assert "MEMBER_GROUP_EVENT_LIMIT_CONFLICT" in str(raised.value)
    assert "Category A" in str(raised.value)


def test_a_run_may_put_exactly_the_cap_on_one_event(
    session, head, world, monkeypatch
):
    world = World(sundays=1, roles=2, volunteers=2)
    cap = _cap(2, (118, 119))
    _install(monkeypatch, world, _facts(world, group_caps=(cap,)))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 2


def test_the_cap_binds_each_event_separately_in_a_run(
    session, head, world, monkeypatch
):
    """Two events, one position each, a cap of one: both fill, because the cap
    is per event and the run's own state is tracked per event.
    """
    world = World(sundays=2, roles=1, volunteers=2)
    cap = _cap(1, (118, 119))
    _install(monkeypatch, world, _facts(world, group_caps=(cap,)))

    created = _persist(session, world, head, world.proposals())

    assert len(created) == 2


def test_an_existing_group_assignment_consumes_the_run_s_allowance(
    session, head, world, monkeypatch
):
    world = World(sundays=1, roles=2, volunteers=2)
    cap = _cap(1, (118, 119))
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            group_caps=(cap,),
            membership_events=((118, 700),),
        ),
    )

    # The one allowed place is taken by the existing row, so the proposal for
    # the *other* group member is refused.
    with pytest.raises(InvalidOperationError, match="MEMBER_GROUP_EVENT_LIMIT"):
        _persist(
            session, world, head,
            (
                ProposedAssignment(
                    requirement_id=world.requirements[1].id,
                    membership_id=119, event_id=700,
                ),
            ),
        )


def test_a_refused_cap_writes_nothing_at_all(session, head, world, monkeypatch):
    world = World(sundays=1, roles=3, volunteers=3)
    _install(monkeypatch, world, _facts(world, group_caps=(_cap(1, (118, 119, 120)),)))

    with pytest.raises(InvalidOperationError):
        _persist(session, world, head, world.proposals())

    assert session.flush_calls == 0
    assert _assignments(session) == []
    assert _audit(session) == []


def test_the_support_rule_is_satisfied_by_a_placement_later_in_the_same_run(
    session, head, world, monkeypatch
):
    """**The order-independence property.**

    The subject's placement is evaluated *before* their supporter's, which is
    exactly the case a per-row application of this rule would refuse -- for a
    schedule that satisfies it perfectly once complete. The rule is applied to
    the run's finished state, so both rows are written.
    """
    world = World(sundays=1, roles=2, volunteers=2)
    support = _support(1, (119,), subject=118)
    _install(monkeypatch, world, _facts(world, support_requirements=(support,)))

    created = _persist(
        session, world, head,
        (
            # Subject first, deliberately.
            ProposedAssignment(requirement_id=world.requirements[0].id,
                               membership_id=118, event_id=700),
            ProposedAssignment(requirement_id=world.requirements[1].id,
                               membership_id=119, event_id=700),
        ),
    )

    assert len(created) == 2
    assert session.flush_calls == 1


def test_a_run_that_seats_a_subject_without_support_is_refused(
    session, head, world, monkeypatch
):
    world = World(sundays=1, roles=2, volunteers=3)
    support = _support(1, (119,), subject=118)
    _install(monkeypatch, world, _facts(world, support_requirements=(support,)))

    with pytest.raises(InvalidOperationError) as raised:
        _persist(
            session, world, head,
            (
                ProposedAssignment(requirement_id=world.requirements[0].id,
                                   membership_id=118, event_id=700),
                # An unrelated member, not an approved supporter.
                ProposedAssignment(requirement_id=world.requirements[1].id,
                                   membership_id=120, event_id=700),
            ),
        )

    assert "SAME_EVENT_SUPPORT_CONFLICT" in str(raised.value)


def test_a_refused_support_rule_writes_nothing_at_all(
    session, head, world, monkeypatch
):
    """The deferred check still runs before the flush, so a rejected run has
    written nothing rather than part of a schedule nobody decided on.
    """
    world = World(sundays=1, roles=2, volunteers=3)
    support = _support(1, (119,), subject=118)
    _install(monkeypatch, world, _facts(world, support_requirements=(support,)))

    with pytest.raises(InvalidOperationError):
        _persist(
            session, world, head,
            (
                ProposedAssignment(requirement_id=world.requirements[0].id,
                                   membership_id=118, event_id=700),
                ProposedAssignment(requirement_id=world.requirements[1].id,
                                   membership_id=120, event_id=700),
            ),
        )

    assert session.flush_calls == 0
    assert _assignments(session) == []
    assert _audit(session) == []


def test_an_existing_supporter_row_satisfies_a_run_s_subject_placement(
    session, head, world, monkeypatch
):
    world = World(sundays=1, roles=2, volunteers=2)
    support = _support(1, (119,), subject=118)
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            support_requirements=(support,),
            membership_events=((119, 700),),
        ),
    )

    created = _persist(
        session, world, head,
        (
            ProposedAssignment(requirement_id=world.requirements[0].id,
                               membership_id=118, event_id=700),
        ),
    )

    assert len(created) == 1


def test_the_support_rule_is_scoped_to_the_event_not_the_date(
    session, head, world, monkeypatch
):
    """The supporter is on a *different* event, so the subject's placement is
    refused -- a date-level reading would have allowed it.
    """
    world = World(sundays=2, roles=1, volunteers=2)
    support = _support(1, (119,), subject=118)
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            support_requirements=(support,),
            membership_events=((119, 701),),
        ),
    )

    with pytest.raises(InvalidOperationError, match="SAME_EVENT_SUPPORT_CONFLICT"):
        _persist(
            session, world, head,
            (
                ProposedAssignment(requirement_id=world.requirements[0].id,
                                   membership_id=118, event_id=700),
            ),
        )


def test_a_subject_this_run_did_not_place_is_not_judged_by_it(
    session, head, world, monkeypatch
):
    """A pre-existing violation is reported by the finalization gate, never
    repaired here -- and never allowed to fail a run that did not cause it.
    """
    world = World(sundays=2, roles=1, volunteers=3)
    support = _support(1, (119,), subject=118)
    _install(
        monkeypatch,
        world,
        _facts(
            world,
            support_requirements=(support,),
            # The subject already holds an unsupported row at the first event.
            membership_events=((118, 700),),
        ),
    )

    created = _persist(
        session, world, head,
        (
            ProposedAssignment(requirement_id=world.requirements[1].id,
                               membership_id=120, event_id=701),
        ),
    )

    assert len(created) == 1


def test_automatic_work_still_never_overrides_either_new_rule(
    session, head, world, monkeypatch
):
    """``persist_generated_assignments`` has no ``override_reason`` parameter
    at all, so neither rule can acquire a quiet exemption through it.
    """
    import inspect

    parameters = inspect.signature(persist_generated_assignments).parameters

    assert "override_reason" not in parameters
