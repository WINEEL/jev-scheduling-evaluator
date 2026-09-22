"""Task 30 -> Task 31 bridge: real rows in, a real CP-SAT schedule out.

Deliberately small. The solver is database-independent and its behavior is
proven exhaustively by the pure suite; what needs a database is the *seam* --
that the values Task 30 extracts from real PostgreSQL rows are the values the
engine actually reasons about, and that solving persists nothing.

Nothing is mocked: the real builder, the real Task 21 conflict query and the
real optimizer all run. Rollback-isolated by the shared harness.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.models.schedule_output import SCHEDULE_VERSION_STATUS_DRAFT
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
)
from app.scheduling.result import DIAGNOSTIC_NO_RESPONSE_DISALLOWED
from app.scheduling.solver import SchedulingPolicy, solve_schedule
from app.services.assignment import assign_member
from app.services.availability import set_availability
from app.services.errors import InvalidOperationError
from app.services.scheduling_input_builder import build_scheduling_input
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)

STRICT = SchedulingPolicy(allow_no_response=False)
LENIENT = SchedulingPolicy(allow_no_response=True)
FAIR = SchedulingPolicy(allow_no_response=False, target_assignments_per_candidate=3)


class _Scenario:
    """One Sunday, two roles, and a roster of real people."""

    def __init__(self, session, *, lead_count: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.lead = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        self.assist = f.make_role(session, ministry=self.ministry, name="Setup Assist")
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=NOV_15)
        f.make_staffing_requirement(
            session, event=self.event, role=self.lead, required_count=lead_count,
        )
        f.make_staffing_requirement(session, event=self.event, role=self.assist)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.lead_requirement = f.make_version_requirement(
            session, version=self.version, event=self.event, role=self.lead,
            required_count=lead_count,
        )
        self.assist_requirement = f.make_version_requirement(
            session, version=self.version, event=self.event, role=self.assist,
        )
        session.flush()

    def add_member(self, name: str, *, roles=(), availability: str | None = None):
        person = f.make_person(self.session, church=self.church, name=name)
        membership = f.make_membership(
            self.session, person=person, ministry=self.ministry,
        )
        for role in roles:
            f.make_qualification(
                self.session, membership=membership, role=role,
                decided_by=self.head, is_qualified=True,
            )
        if availability is not None:
            f.make_availability(
                self.session, membership=membership, event=self.event, state=availability,
            )
        self.session.flush()
        return membership

    def solve(self, policy=STRICT):
        scheduling_input = build_scheduling_input(self.session, version=self.version)
        return scheduling_input, solve_schedule(scheduling_input, policy=policy)


def _assignment_count(session) -> int:
    return session.execute(text("SELECT count(*) FROM assignment")).scalar_one()


def test_real_rows_produce_a_real_schedule_and_persist_nothing(db_session):
    """The bridge: build from PostgreSQL, solve with CP-SAT, and prove both the
    proposals and the fact that nothing was written.
    """
    scenario = _Scenario(db_session)
    lead_member = scenario.add_member(
        "Lead", roles=[scenario.lead], availability=AVAILABILITY_AVAILABLE,
    )
    assist_member = scenario.add_member(
        "Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE,
    )
    # Qualified for the lead role but has declined this Sunday.
    scenario.add_member(
        "Declined", roles=[scenario.lead], availability=AVAILABILITY_UNAVAILABLE,
    )
    before_assignments = _assignment_count(db_session)
    before_audits = db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one()

    scheduling_input, result = scenario.solve()

    # The extracted values are the ones the engine reasoned about.
    assert {r.requirement_id for r in scheduling_input.requirements} == {
        scenario.lead_requirement.id, scenario.assist_requirement.id,
    }
    assert result.is_complete is True
    assert {
        (p.requirement_id, p.membership_id) for p in result.proposed_assignments
    } == {
        (scenario.lead_requirement.id, lead_member.id),
        (scenario.assist_requirement.id, assist_member.id),
    }
    # The member who declined was never proposed.
    assert result.filled_count == 2

    # Solving is not persistence: no Assignment row, no audit row.
    assert _assignment_count(db_session) == before_assignments == 0
    assert db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one() == before_audits


def test_an_existing_manual_assignment_is_fixed_and_not_re_proposed(db_session):
    """A real Task 22 assignment consumes its slot and occupies its event."""
    scenario = _Scenario(db_session)
    both_roles = scenario.add_member(
        "Both", roles=[scenario.lead, scenario.assist], availability=AVAILABILITY_AVAILABLE,
    )
    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.lead_requirement,
        membership=both_roles,
    )
    db_session.flush()

    scheduling_input, result = scenario.solve()

    assert len(scheduling_input.existing_assignments) == 1
    assert scheduling_input.existing_assignments[0].assignment_id == assignment.id
    # Not re-emitted, and not moved to the other role in the same event.
    assert result.proposed_assignments == ()
    assert [u.requirement_id for u in result.unfilled_requirements] == [
        scenario.assist_requirement.id
    ]
    assert _assignment_count(db_session) == 1


def test_the_no_response_policy_changes_the_answer_for_the_same_real_rows(db_session):
    """The same database state, two ministries' rules -- which is exactly why
    the tri-state is preserved all the way through Task 30 rather than being
    collapsed at the door.
    """
    scenario = _Scenario(db_session)
    silent = scenario.add_member("Silent", roles=[scenario.lead])  # no availability row
    scenario.add_member(
        "Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE,
    )

    strict_input, strict_result = scenario.solve(policy=STRICT)
    lenient_input, lenient_result = scenario.solve(policy=LENIENT)

    # Task 30 reported silence as silence, both times.
    from app.scheduling.input import AvailabilityState

    silent_candidate = strict_input.candidate_by_membership_id(silent.id)
    assert silent_candidate.availability_for(scenario.event.id) is AvailabilityState.NO_RESPONSE

    strict_lead = [
        u for u in strict_result.unfilled_requirements
        if u.requirement_id == scenario.lead_requirement.id
    ]
    assert len(strict_lead) == 1
    assert DIAGNOSTIC_NO_RESPONSE_DISALLOWED in strict_lead[0].diagnostic_codes

    assert lenient_result.is_complete is True
    assert (scenario.lead_requirement.id, silent.id) in {
        (p.requirement_id, p.membership_id) for p in lenient_result.proposed_assignments
    }
    assert _assignment_count(db_session) == 0


# --------------------------------------------------------------------------
# Task 32 -- fairness over real rows
# --------------------------------------------------------------------------


class _SeasonScenario:
    """Six Sundays of one role, and three equally-qualified volunteers."""

    def __init__(self, session, *, sundays: int = 6, volunteers: int = 3):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        self.period = f.make_period(session, ministry=self.ministry)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.events = []
        self.requirements = []
        for index in range(sundays):
            event = f.make_event(
                session, period=self.period,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
            )
            f.make_staffing_requirement(session, event=event, role=self.role)
            self.requirements.append(
                f.make_version_requirement(
                    session, version=self.version, event=event, role=self.role,
                )
            )
            self.events.append(event)
        self.memberships = []
        for index in range(volunteers):
            person = f.make_person(session, church=self.church, name=f"Volunteer{index}")
            membership = f.make_membership(session, person=person, ministry=self.ministry)
            f.make_qualification(
                session, membership=membership, role=self.role,
                decided_by=self.head, is_qualified=True,
            )
            for event in self.events:
                f.make_availability(
                    session, membership=membership, event=event,
                    state=AVAILABILITY_AVAILABLE,
                )
            self.memberships.append(membership)
        session.flush()

    def solve(self, policy):
        scheduling_input = build_scheduling_input(self.session, version=self.version)
        return solve_schedule(scheduling_input, policy=policy)


def test_a_target_balances_real_rows_without_costing_any_filled_position(db_session):
    """Six real Sundays, three real volunteers: both policies fill all six,
    and only the configured target spreads them evenly.
    """
    scenario = _SeasonScenario(db_session, sundays=6, volunteers=3)
    before_assignments = _assignment_count(db_session)
    before_audits = db_session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()

    unbalanced = scenario.solve(STRICT)
    balanced = scenario.solve(FAIR)

    # Identical maximum fill either way -- fairness costs nothing.
    assert unbalanced.filled_count == balanced.filled_count == 6
    assert balanced.is_complete is True

    loads = sorted(balanced.metrics.load_by_membership.values())
    assert loads == [2, 2, 2]
    assert balanced.metrics.target_excess_total == 0
    assert balanced.metrics.fairness_cost == 12
    # Task 31's policy still computes no soft optimum.
    assert unbalanced.metrics.target_excess_total is None

    # Solving persists nothing, under either policy.
    assert _assignment_count(db_session) == before_assignments == 0
    assert db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one() == before_audits


def test_a_scarce_real_volunteer_exceeds_the_target_rather_than_leaving_sundays_empty(
    db_session,
):
    """Five Sundays of a role only one real person is qualified for. The
    target is three; leaving two Sundays empty to honour it would be exactly
    the behavior the approved design rejects.
    """
    scenario = _SeasonScenario(db_session, sundays=5, volunteers=1)

    result = scenario.solve(FAIR)

    assert result.is_complete is True
    assert result.filled_count == 5
    only_volunteer = scenario.memberships[0]
    assert result.metrics.load_by_membership == {only_volunteer.id: 5}
    assert result.metrics.target_excess_total == 2
    # Exceeding a soft target is not an unresolved staffing slot.
    assert result.unfilled_requirements == ()
    assert _assignment_count(db_session) == 0


def test_a_real_existing_assignment_shifts_the_fair_distribution(db_session):
    """One volunteer already has a real Task 22 assignment, so the remaining
    Sundays lean toward the others.
    """
    scenario = _SeasonScenario(db_session, sundays=3, volunteers=3)
    already = scenario.memberships[0]
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirements[0],
        membership=already,
    )
    db_session.flush()

    result = scenario.solve(FAIR)

    assert result.is_complete is True
    assert result.filled_count == 2  # two Sundays left to fill
    assert sorted(result.metrics.load_by_membership.values()) == [1, 1, 1]
    assert already.id not in {p.membership_id for p in result.proposed_assignments}
    assert _assignment_count(db_session) == 1  # only the real one, none added


# --------------------------------------------------------------------------
# Task 33 -- role variety over real rows
# --------------------------------------------------------------------------


class _TwoRoleScenario:
    """Two interchangeable roles across several Sundays, with volunteers
    qualified for both -- so the only thing separating schedules is which
    role each person ends up serving.
    """

    def __init__(self, session, *, sundays: int = 2, volunteers: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role_a = f.make_role(session, ministry=self.ministry, name="Position Two")
        self.role_b = f.make_role(session, ministry=self.ministry, name="Position Three")
        self.period = f.make_period(session, ministry=self.ministry)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.events = []
        self.requirements = {}
        for index in range(sundays):
            event = f.make_event(
                session, period=self.period,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
            )
            self.events.append(event)
            for role in (self.role_a, self.role_b):
                f.make_staffing_requirement(session, event=event, role=role)
                self.requirements[(index, role.id)] = f.make_version_requirement(
                    session, version=self.version, event=event, role=role,
                )
        self.memberships = []
        for index in range(volunteers):
            person = f.make_person(session, church=self.church, name=f"Volunteer{index}")
            membership = f.make_membership(session, person=person, ministry=self.ministry)
            for role in (self.role_a, self.role_b):
                f.make_qualification(
                    session, membership=membership, role=role,
                    decided_by=self.head, is_qualified=True,
                )
            for event in self.events:
                f.make_availability(
                    session, membership=membership, event=event,
                    state=AVAILABILITY_AVAILABLE,
                )
            self.memberships.append(membership)
        session.flush()

    def solve(self, policy):
        scheduling_input = build_scheduling_input(self.session, version=self.version)
        return scheduling_input, solve_schedule(scheduling_input, policy=policy)

    def roles_served(self, scheduling_input, result, membership_id):
        by_requirement = {r.requirement_id: r for r in scheduling_input.requirements}
        return sorted(
            by_requirement[p.requirement_id].ministry_role_id
            for p in result.proposed_assignments
            if p.membership_id == membership_id
        )


def test_role_variety_spreads_real_roles_without_costing_fill(db_session):
    """Two Sundays, two roles, one volunteer. Both policies fill two positions
    (the same-event rule allows one per Sunday); only the variety policy makes
    them one of each role.
    """
    scenario = _TwoRoleScenario(db_session, sundays=2, volunteers=1)
    volunteer = scenario.memberships[0]
    variety = SchedulingPolicy(
        allow_no_response=False,
        role_variety_role_ids=frozenset({scenario.role_a.id, scenario.role_b.id}),
    )
    before_assignments = _assignment_count(db_session)
    before_audits = db_session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()

    plain_input, plain = scenario.solve(STRICT)
    varied_input, varied = scenario.solve(variety)

    assert plain.filled_count == varied.filled_count == 2
    assert scenario.roles_served(varied_input, varied, volunteer.id) == sorted(
        [scenario.role_a.id, scenario.role_b.id]
    )
    assert varied.metrics.role_variety_cost == 2  # one of each, not 4
    assert plain.metrics.role_variety_cost is None

    assert _assignment_count(db_session) == before_assignments == 0
    assert db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one() == before_audits


def test_role_variety_leaves_the_target_and_fairness_optima_untouched(db_session):
    """Four Sundays x two roles and two volunteers, with the target also
    configured: adding variety must not move fill, excess or fairness.
    """
    scenario = _TwoRoleScenario(db_session, sundays=4, volunteers=2)
    roles = frozenset({scenario.role_a.id, scenario.role_b.id})
    target_only = SchedulingPolicy(
        allow_no_response=False, target_assignments_per_candidate=3
    )
    target_and_variety = SchedulingPolicy(
        allow_no_response=False, target_assignments_per_candidate=3,
        role_variety_role_ids=roles,
    )

    _, plain = scenario.solve(target_only)
    varied_input, varied = scenario.solve(target_and_variety)

    assert plain.filled_count == varied.filled_count
    assert plain.metrics.target_excess_total == varied.metrics.target_excess_total
    assert plain.metrics.fairness_cost == varied.metrics.fairness_cost
    # And variety did something useful within that: nobody is stacked on one
    # role more than necessary.
    for membership in scenario.memberships:
        served = scenario.roles_served(varied_input, varied, membership.id)
        assert len(set(served)) == 2 or len(served) < 2
    assert _assignment_count(db_session) == 0


def test_a_real_existing_assignment_steers_the_new_role_choice(db_session):
    """The volunteer has a real Task 22 assignment in role A. On the next
    Sunday, variety should send them to role B.
    """
    scenario = _TwoRoleScenario(db_session, sundays=2, volunteers=1)
    volunteer = scenario.memberships[0]
    first_role_a = scenario.requirements[(0, scenario.role_a.id)]
    assign_member(
        db_session, actor=scenario.head, requirement=first_role_a,
        membership=volunteer,
    )
    db_session.flush()

    variety = SchedulingPolicy(
        allow_no_response=False,
        role_variety_role_ids=frozenset({scenario.role_a.id, scenario.role_b.id}),
    )
    scheduling_input, result = scenario.solve(variety)

    assert len(scheduling_input.existing_assignments) == 1
    assert result.filled_count == 1  # the second Sunday
    assert scenario.roles_served(scheduling_input, result, volunteer.id) == [
        scenario.role_b.id
    ]
    # One existing A plus one new B: 1 + 1.
    assert result.metrics.role_variety_cost == 2
    # The existing assignment was neither moved nor duplicated.
    assert _assignment_count(db_session) == 1


# --------------------------------------------------------------------------
# Task 56 -- availability recorded through the real service, not the
# factory, flows through the real builder and the real solver exactly as
# Task 52's pure-model suite already proved it must.
# --------------------------------------------------------------------------


def test_available_recorded_through_the_real_service_fills_the_position(db_session):
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    lead_member = scenario.add_member("Lead", roles=[scenario.lead])
    scenario.add_member("Assist", roles=[scenario.assist])

    set_availability(
        db_session, actor=scenario.head, membership=lead_member, event=scenario.event,
        availability_state=AVAILABILITY_AVAILABLE,
    )
    db_session.flush()

    _, result = scenario.solve()

    assert (scenario.lead_requirement.id, lead_member.id) in {
        (p.requirement_id, p.membership_id) for p in result.proposed_assignments
    }
    assert result.metrics.backup_placement_total == 0


def test_backup_recorded_through_the_real_service_loses_to_a_real_available_candidate(
    db_session,
):
    """Two real candidates, one BACKUP and one AVAILABLE, both recorded
    through :func:`set_availability` -- the same function the new
    ``PUT /events/{id}/availability/{id}`` endpoint calls. The AVAILABLE
    candidate is chosen, exactly as Task 52's pure-model suite proved with
    hand-built input.
    """
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    backup_member = scenario.add_member("Backup", roles=[scenario.lead])
    available_member = scenario.add_member("Available", roles=[scenario.lead])
    scenario.add_member("Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE)

    set_availability(
        db_session, actor=scenario.head, membership=backup_member, event=scenario.event,
        availability_state=AVAILABILITY_BACKUP,
    )
    set_availability(
        db_session, actor=scenario.head, membership=available_member, event=scenario.event,
        availability_state=AVAILABILITY_AVAILABLE,
    )
    db_session.flush()

    _, result = scenario.solve()

    assert (scenario.lead_requirement.id, available_member.id) in {
        (p.requirement_id, p.membership_id) for p in result.proposed_assignments
    }
    assert (scenario.lead_requirement.id, backup_member.id) not in {
        (p.requirement_id, p.membership_id) for p in result.proposed_assignments
    }
    assert result.metrics.backup_placement_total == 0


def test_backup_recorded_through_the_real_service_still_fills_when_it_is_the_only_option(
    db_session,
):
    """Feasible, not a fallback: with nobody else available, the BACKUP
    candidate is used rather than leaving the position open.
    """
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    backup_member = scenario.add_member("Backup", roles=[scenario.lead])
    scenario.add_member("Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE)

    set_availability(
        db_session, actor=scenario.head, membership=backup_member, event=scenario.event,
        availability_state=AVAILABILITY_BACKUP,
    )
    db_session.flush()

    _, result = scenario.solve()

    assert (scenario.lead_requirement.id, backup_member.id) in {
        (p.requirement_id, p.membership_id) for p in result.proposed_assignments
    }
    assert result.metrics.backup_placement_total == 1


def test_unavailable_recorded_through_the_real_service_is_never_proposed(db_session):
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    declined_member = scenario.add_member("Declined", roles=[scenario.lead])
    scenario.add_member("Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE)

    set_availability(
        db_session, actor=scenario.head, membership=declined_member, event=scenario.event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()

    _, result = scenario.solve()

    assert result.filled_count == 1  # the assist position only
    assert declined_member.id not in {p.membership_id for p in result.proposed_assignments}


def test_clearing_through_the_real_service_returns_to_no_response_behavior(db_session):
    """Recording AVAILABLE, then clearing it back to no response
    (``availability_state=None``), makes the same candidate behave exactly
    as someone who never answered: excluded under the strict policy, usable
    under the lenient one -- proving the clear path, not merely the record
    path, reaches the builder correctly.
    """
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    member = scenario.add_member("Maybe", roles=[scenario.lead])
    scenario.add_member("Assist", roles=[scenario.assist], availability=AVAILABILITY_AVAILABLE)

    set_availability(
        db_session, actor=scenario.head, membership=member, event=scenario.event,
        availability_state=AVAILABILITY_AVAILABLE,
    )
    set_availability(
        db_session, actor=scenario.head, membership=member, event=scenario.event,
        availability_state=None,
    )
    db_session.flush()

    _, strict_result = scenario.solve(STRICT)
    assert member.id not in {p.membership_id for p in strict_result.proposed_assignments}
    assert DIAGNOSTIC_NO_RESPONSE_DISALLOWED in strict_result.unfilled_requirements[0].diagnostic_codes

    _, lenient_result = scenario.solve(LENIENT)
    assert (scenario.lead_requirement.id, member.id) in {
        (p.requirement_id, p.membership_id) for p in lenient_result.proposed_assignments
    }


def test_a_locked_period_refuses_a_new_answer_through_the_real_service(db_session):
    scenario = _Scenario(db_session)
    member = scenario.add_member("Lead", roles=[scenario.lead])
    # Explicit, though the factory default is already locked: this test's
    # whole point is the lock, and that should not depend on a default that
    # could change.
    scenario.period.availability_locked_at = datetime.datetime(2026, 10, 1, tzinfo=UTC)
    db_session.flush()

    with pytest.raises(InvalidOperationError):
        set_availability(
            db_session, actor=scenario.head, membership=member, event=scenario.event,
            availability_state=AVAILABILITY_AVAILABLE,
        )
