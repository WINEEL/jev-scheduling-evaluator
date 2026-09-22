"""Joined multi-ministry scheduling in the pure solver (Task 81).

Hand-written and generated inputs, the real CP-SAT solver, no database.

The rule under test is the church's most important one::

    one canonical Person, at most one ministry, per calendar date

It is **hard and non-overridable**, and in a joined run it lives inside the
CP-SAT model as a constraint added before any objective is expressed. So these
tests assert two different kinds of property and keep them apart:

- that no person ever ends a joined run holding work in two ministries on one
  date -- exact, and always assertable;
- that fill, fairness and the other preferences behave as they should when the
  rule does not bind -- which is about the objective, and is asserted on
  objective-relevant shape rather than on who got which slot.

**Every person here is synthetic.** The ids are invented, the ministries are
named after shapes rather than after the church's, and no qualification,
availability answer or roster in this file describes anybody real.
"""

from __future__ import annotations

import datetime

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.joined import (
    ChurchWideContention,
    JoinedMinistryInput,
    solve_joined_schedule,
)
from app.scheduling.result import DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)
from tests.scheduling_scale_fixture import build_scale_input
from tests.joined_trial_fixture import (
    AV_LIKE,
    KIDS_LIKE,
    SETUP_LIKE,
    SHARED_BOTH_SUNDAYS,
    SHARED_SCARCE,
    SHARED_SPREAD,
    SUNDAYS,
    build_joined_scenario,
    membership_for,
    sunday,
)

LENIENT = SchedulingPolicy(allow_no_response=True)
DATE_A = datetime.date(2030, 3, 3)
DATE_B = datetime.date(2030, 3, 10)


# --------------------------------------------------------------------------
# Small hand-built ministries, for the properties that need an exact shape
# --------------------------------------------------------------------------


def _ministry(
    ministry_id: int,
    *,
    role_id: int,
    demand: dict[datetime.date, int],
    candidates: tuple[CandidateInput, ...],
    policy: SchedulingPolicy = LENIENT,
    existing: tuple[ExistingAssignmentInput, ...] = (),
    same_date_exclusions: tuple[LinkedMembershipPair, ...] = (),
) -> JoinedMinistryInput:
    """One tiny ministry: one role, one event per date, ids derived from the
    ministry id so two ministries in one test cannot collide.
    """
    requirements = []
    for index, (event_date, count) in enumerate(sorted(demand.items())):
        requirements.append(
            RequirementInput(
                requirement_id=ministry_id * 1_000 + index,
                event_id=ministry_id * 100 + index,
                event_date=event_date,
                ministry_role_id=role_id,
                ministry_id=ministry_id,
                required_count=count,
            )
        )
    return JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=ministry_id,
            scheduling_period_id=ministry_id,
            ministry_id=ministry_id,
            requirements=tuple(requirements),
            candidates=candidates,
            existing_assignments=existing,
            same_date_exclusions=same_date_exclusions,
        ),
        policy=policy,
    )


def _person(
    person_id: int,
    ministry_id: int,
    *,
    role_id: int,
    blocked_dates: frozenset[datetime.date] = frozenset(),
    maximum: int | None = None,
) -> CandidateInput:
    return CandidateInput(
        membership_id=ministry_id * 10_000 + person_id,
        person_id=person_id,
        display_name=f"person-{person_id}",
        qualified_role_ids=frozenset({role_id}),
        blocked_dates=blocked_dates,
        max_assignments_in_period=maximum,
    )


def _dates_by_person(
    result, scenario: tuple[JoinedMinistryInput, ...]
) -> dict[int, list[tuple[int, datetime.date]]]:
    """``person_id -> [(ministry id, date), ...]`` over every proposal.

    Built through ``person_id``, never through a membership or a name -- which
    is the whole property these tests exist to check.
    """
    placed: dict[int, list[tuple[int, datetime.date]]] = {}
    for entry in scenario:
        scheduling_input = entry.scheduling_input
        ministry_id = scheduling_input.ministry_id
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in scheduling_input.candidates
        }
        dates = {
            requirement.requirement_id: requirement.event_date
            for requirement in scheduling_input.requirements
        }
        outcome = result.results_by_ministry[ministry_id]
        for proposal in outcome.proposed_assignments:
            placed.setdefault(person_of[proposal.membership_id], []).append(
                (ministry_id, dates[proposal.requirement_id])
            )
        for assignment in scheduling_input.existing_assignments:
            placed.setdefault(person_of[assignment.membership_id], []).append(
                (ministry_id, dates[assignment.requirement_id])
            )
    return placed


def _assert_church_wide_rule_holds(result, scenario) -> None:
    """No canonical person holds work in two ministries on one date.

    The one assertion every test in this file makes, whatever else it is
    about.
    """
    for person_id, placements in _dates_by_person(result, scenario).items():
        by_date: dict[datetime.date, set[int]] = {}
        for ministry_id, event_date in placements:
            by_date.setdefault(event_date, set()).add(ministry_id)
        for event_date, ministries in by_date.items():
            assert len(ministries) == 1, (
                f"person {person_id} serves ministries {sorted(ministries)} on"
                f" {event_date}"
            )


# --------------------------------------------------------------------------
# Canonical identity
# --------------------------------------------------------------------------


def test_one_person_two_memberships_is_one_human_on_one_date():
    """Task 81 §2 and §3, demonstration A.

    The same ``person_id`` reached through two different membership ids, both
    qualified, both available, both wanted on the same Sunday. The solver may
    choose either ministry and must choose exactly one.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(500, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.filled_count == 1
    assert result.unfilled_count == 1


def test_the_two_memberships_are_genuinely_different_rows():
    """The fixture is not accidentally testing one membership twice: the same
    human carries a different membership id in each ministry, and it is
    ``person_id`` alone that ties them together.
    """
    scenario = build_joined_scenario()
    setup_id = membership_for(SHARED_BOTH_SUNDAYS, SETUP_LIKE)
    av_id = membership_for(SHARED_BOTH_SUNDAYS, AV_LIKE)
    assert setup_id != av_id

    by_ministry = {entry.ministry_id: entry.scheduling_input for entry in scenario}
    setup_candidate = by_ministry[SETUP_LIKE].candidate_by_membership_id(setup_id)
    av_candidate = by_ministry[AV_LIKE].candidate_by_membership_id(av_id)
    assert setup_candidate is not None and av_candidate is not None
    assert setup_candidate.person_id == av_candidate.person_id == SHARED_BOTH_SUNDAYS
    # Different ministries, so different role vocabularies -- the memberships
    # carry their own qualifications and neither leaks into the other.
    assert not (
        setup_candidate.qualified_role_ids & av_candidate.qualified_role_ids
    )


def test_a_display_name_collision_does_not_merge_two_people():
    """Two different people who happen to share a display name stay two people.

    Identity is ``person_id``. If the church-wide rule were ever keyed on a
    name, this run would be forced to leave a position unfilled; it must fill
    both.
    """
    same_name = "Sam Taylor"
    first = CandidateInput(
        membership_id=10_600,
        person_id=600,
        display_name=same_name,
        qualified_role_ids=frozenset({11}),
    )
    second = CandidateInput(
        membership_id=20_601,
        person_id=601,
        display_name=same_name,
        qualified_role_ids=frozenset({22}),
    )
    scenario = (
        _ministry(1, role_id=11, demand={DATE_A: 1}, candidates=(first,)),
        _ministry(2, role_id=22, demand={DATE_A: 1}, candidates=(second,)),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.filled_count == 2
    assert result.unfilled_count == 0


# --------------------------------------------------------------------------
# The church-wide rule itself
# --------------------------------------------------------------------------


def test_a_shared_person_may_serve_two_ministries_on_different_dates():
    """Demonstration B. The rule is per *date*, and says nothing about a
    period: one person serving one ministry this Sunday and another next
    Sunday is entirely permitted, and a joined run must not over-apply the
    constraint into a blanket exclusivity.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_B: 1},
            candidates=(_person(500, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.filled_count == 2
    assert result.unfilled_count == 0
    assert result.contentions == ()


def test_one_ministry_per_date_not_one_assignment_per_date():
    """The rule counts *ministries*, never assignment rows.

    Two events of the same ministry on one date, and one person is the only
    candidate for both. Same-ministry behaviour is unchanged by the joined
    path -- rule 2 of the engine still allows at most one position per event,
    so this person takes one of the two, and the church-wide rule neither adds
    nor removes anything.
    """
    ministry = JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=1,
            scheduling_period_id=1,
            ministry_id=1,
            requirements=(
                RequirementInput(
                    requirement_id=1,
                    event_id=101,
                    event_date=DATE_A,
                    ministry_role_id=11,
                    ministry_id=1,
                    required_count=1,
                ),
                RequirementInput(
                    requirement_id=2,
                    event_id=102,
                    event_date=DATE_A,
                    ministry_role_id=11,
                    ministry_id=1,
                    required_count=1,
                ),
            ),
            candidates=(
                _person(500, 1, role_id=11),
                _person(501, 1, role_id=11),
            ),
        ),
        policy=LENIENT,
    )
    result = solve_joined_schedule((ministry,))
    outcome = result.results_by_ministry[1]

    assert outcome.filled_count == 2
    assert outcome.unfilled_count == 0
    # Two different people, one at each event -- and both on the same date,
    # which is the point: the church-wide rule did not forbid the ministry its
    # own second service.
    assert {p.membership_id for p in outcome.proposed_assignments} == {10_500, 10_501}


def test_two_ministries_may_both_use_one_date_with_different_people():
    """The constraint is per person, not per date: two ministries meeting on
    the same Sunday is ordinary, and nothing here serializes them.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(501, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.is_complete


def test_an_existing_assignment_in_one_ministry_blocks_the_others_that_date():
    """A fixed row is as binding as a new placement.

    Ministry 1 already holds this person on the date. The joined run may not
    place them in ministry 2 that day, and -- because the engine never removes
    a fixed assignment -- it may not solve the conflict by dropping the
    existing row either.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
            existing=(
                ExistingAssignmentInput(
                    assignment_id=1,
                    requirement_id=1_000,
                    membership_id=10_500,
                    event_id=100,
                ),
            ),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(500, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.results_by_ministry[1].proposed_assignments == ()
    assert result.results_by_ministry[2].proposed_assignments == ()
    assert result.results_by_ministry[2].unfilled_count == 1


def test_the_rule_is_not_traded_away_to_fill_more_positions():
    """Demonstration F, for the church-wide rule specifically.

    Ministry 2 could fill **three** positions by using this person, ministry 1
    only one. If the constraint were a weighted penalty, any sane weighting
    would break it here. It is a constraint, so the answer is one position, not
    two -- and the other two come back unfilled.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
            existing=(
                ExistingAssignmentInput(
                    assignment_id=1,
                    requirement_id=1_000,
                    membership_id=10_500,
                    event_id=100,
                ),
            ),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 3},
            candidates=(_person(500, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.results_by_ministry[2].proposed_assignments == ()
    assert result.results_by_ministry[2].unfilled_count == 3


def test_an_unfillable_joined_demand_is_reported_not_violated():
    """Demonstration D. Three ministries all want the same single person on
    one date; two must go short, and the shortfall is reported with the joined
    diagnostic rather than resolved by breaking the rule.
    """
    scenario = tuple(
        _ministry(
            ministry_id,
            role_id=ministry_id * 11,
            demand={DATE_A: 1},
            candidates=(_person(500, ministry_id, role_id=ministry_id * 11),),
        )
        for ministry_id in (1, 2, 3)
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.filled_count == 1
    assert result.unfilled_count == 2

    short = [
        unfilled
        for outcome in result.results_by_ministry.values()
        for unfilled in outcome.unfilled_requirements
    ]
    assert len(short) == 2
    for unfilled in short:
        assert DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT in unfilled.diagnostic_codes


def test_the_joined_diagnostic_is_not_claimed_when_it_is_not_the_reason():
    """The code says something exact, so it must not appear where a position
    was simply short of people. Nobody is shared here; the position is open
    because one candidate cannot fill two slots.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 2},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(501, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    unfilled = result.results_by_ministry[1].unfilled_requirements
    assert len(unfilled) == 1
    assert DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT not in unfilled[0].diagnostic_codes


# --------------------------------------------------------------------------
# No ministry priority
# --------------------------------------------------------------------------


def test_the_contested_person_follows_total_fill_not_ministry_order():
    """Task 81 §4: there is no approved church-wide ministry priority, and the
    joined objective invents none.

    In both halves of this test the shared person is contested. Which ministry
    gets them is decided by which choice fills more positions overall -- so the
    lower ministry id wins the first half and **loses** the second, on the same
    code path. A hidden priority could not produce both answers.
    """
    contested = 500
    spare = 501

    # Ministry 1 has nobody else; ministry 2 does. Filling both means the
    # contested person serves ministry 1.
    first = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(contested, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(
                _person(contested, 2, role_id=22),
                _person(spare, 2, role_id=22),
            ),
        ),
    )
    result = solve_joined_schedule(first)
    _assert_church_wide_rule_holds(result, first)
    assert result.is_complete
    assert _dates_by_person(result, first)[contested] == [(1, DATE_A)]

    # Mirrored: now ministry 2 has nobody else, so the same rule sends the
    # contested person the other way.
    second = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(
                _person(contested, 1, role_id=11),
                _person(spare, 1, role_id=11),
            ),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(contested, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(second)
    _assert_church_wide_rule_holds(result, second)
    assert result.is_complete
    assert _dates_by_person(result, second)[contested] == [(2, DATE_A)]


def test_an_arbitrated_tie_is_reported_rather_than_silently_settled():
    """When total fill cannot break the tie, the run still has to choose --
    and says so. The contention names the person, the date, who got them and
    who else wanted them, which is the unresolved governance question made
    visible instead of buried.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(500, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    assert len(result.contentions) == 1
    contention = result.contentions[0]
    assert contention.person_id == 500
    assert contention.event_date == DATE_A
    assert contention.scheduled_ministry_id in (1, 2)
    assert contention.contending_ministry_ids == (
        2 if contention.scheduled_ministry_id == 1 else 1,
    )


def test_a_contention_is_reported_even_when_nobody_got_the_person():
    """Two ministries wanting one person and neither getting them is just as
    unarbitrated as one of them getting them.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(
                _person(500, 1, role_id=11),
                _person(501, 1, role_id=11),
            ),
            # The contested person's own limit is already spent elsewhere in
            # this period, so neither ministry can use them that date.
            policy=LENIENT,
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(
                _person(500, 2, role_id=22),
                _person(502, 2, role_id=22),
            ),
        ),
    )
    result = solve_joined_schedule(scenario)

    assert result.is_complete
    assert [c.person_id for c in result.contentions] == [500]
    # Both ministries filled the slot from their own unshared candidate, so
    # nobody holds the contested person and every ministry that wanted them is
    # listed as contending.
    contention = result.contentions[0]
    assert contention.scheduled_ministry_id is None
    assert contention.contending_ministry_ids == (1, 2)


def test_only_real_contentions_are_reported():
    """A person eligible in one ministry only is never a contention, however
    many ministries the run covers.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(501, 2, role_id=22),),
        ),
    )
    assert solve_joined_schedule(scenario).contentions == ()


# --------------------------------------------------------------------------
# Every other rule keeps working, scoped to its own ministry
# --------------------------------------------------------------------------


def test_a_person_period_maximum_still_binds_inside_a_joined_run():
    """Demonstration F for a ministry-scoped hard rule. A serving maximum is
    that ministry's agreement with that person; a joined run neither widens it
    nor lets another ministry's demand spend it.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1, DATE_B: 1},
            candidates=(_person(500, 1, role_id=11, maximum=1),),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(501, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.results_by_ministry[1].filled_count == 1
    assert result.results_by_ministry[1].unfilled_count == 1
    assert result.results_by_ministry[2].is_complete


def test_a_linked_pair_exclusion_stays_scoped_to_its_own_ministry():
    """A configured same-date exclusion is a rule about two memberships of one
    ministry. A joined run applies it there and does not spread it across the
    church.
    """
    pair = LinkedMembershipPair(membership_a_id=10_500, membership_b_id=10_501)
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 2},
            candidates=(
                _person(500, 1, role_id=11),
                _person(501, 1, role_id=11),
            ),
            same_date_exclusions=(pair,),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 2},
            candidates=(
                _person(502, 2, role_id=22),
                _person(503, 2, role_id=22),
            ),
        ),
    )
    result = solve_joined_schedule(scenario)

    # The linked pair costs ministry 1 a position, exactly as it would on its
    # own -- and costs ministry 2 nothing.
    assert result.results_by_ministry[1].filled_count == 1
    assert result.results_by_ministry[1].unfilled_count == 1
    assert result.results_by_ministry[2].is_complete


def test_a_commitment_outside_the_run_is_still_a_blocked_date():
    """Task 79 behaviour is unchanged. A ministry not in this joined run
    reaches the model the way it always has -- as ``blocked_dates`` on the
    candidate -- and the joined path neither reinterprets it nor overrides it.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(
                _person(500, 1, role_id=11, blocked_dates=frozenset({DATE_A})),
            ),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(501, 2, role_id=22),),
        ),
    )
    result = solve_joined_schedule(scenario)

    assert result.results_by_ministry[1].proposed_assignments == ()
    assert result.results_by_ministry[1].unfilled_count == 1
    assert result.results_by_ministry[2].is_complete
    # A block from outside the run is not a contention: no second ministry in
    # this run ever had a claim on them.
    assert result.contentions == ()


# --------------------------------------------------------------------------
# The single-ministry engine is unchanged
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ministry_id", [SETUP_LIKE, AV_LIKE, KIDS_LIKE])
def test_a_joined_run_of_one_ministry_equals_the_single_ministry_run(ministry_id):
    """The strongest regression guard available: with one block the joined
    path *is* the single-ministry path -- same variables, same constraints,
    same lexicographic passes -- so it must return exactly the same schedule,
    proposal for proposal and metric for metric.
    """
    entry = {
        candidate.ministry_id: candidate for candidate in build_joined_scenario()
    }[ministry_id]

    alone = solve_schedule(entry.scheduling_input, policy=entry.policy)
    joined = solve_joined_schedule((entry,)).results_by_ministry[ministry_id]

    assert joined.proposed_assignments == alone.proposed_assignments
    assert joined.unfilled_requirements == alone.unfilled_requirements
    assert joined.metrics == alone.metrics


def test_a_joined_run_of_one_ministry_equals_the_single_ministry_run_at_scale():
    """The same equivalence on a full-quarter shape.

    The three scenario ministries above are small enough that every pass has
    few choices to make. This is the fifty-volunteer, thirteen-Sunday,
    ten-position input the search strategy was pinned against (Task 68) -- the
    one where fairness genuinely has an enormous space of equally good
    rearrangements to walk. If the split changed the model or the order the
    passes fix their optima in, this is where it would show.
    """
    scheduling_input = build_scale_input(seed=7)
    policy = SchedulingPolicy(
        allow_no_response=False,
        target_assignments_per_candidate=3,
        balance_candidate_loads=True,
        role_variety_role_ids=frozenset({1, 2, 3}),
    )
    entry = JoinedMinistryInput(scheduling_input=scheduling_input, policy=policy)

    alone = solve_schedule(scheduling_input, policy=policy)
    joined = solve_joined_schedule((entry,)).results_by_ministry[
        scheduling_input.ministry_id
    ]

    assert joined.proposed_assignments == alone.proposed_assignments
    assert joined.unfilled_requirements == alone.unfilled_requirements
    assert joined.metrics == alone.metrics


# --------------------------------------------------------------------------
# The three-ministry development scenario
# --------------------------------------------------------------------------


def test_the_development_scenario_fills_everything_it_can():
    """Demonstration C. Three ministries, five Sundays, overlapping people,
    and every required position filled -- under the church-wide rule, not
    around it.
    """
    scenario = build_joined_scenario()
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.ministry_ids == (SETUP_LIKE, AV_LIKE, KIDS_LIKE)
    assert result.is_complete
    assert result.unfilled_count == 0


def test_the_development_scenario_lets_a_shared_person_serve_both_ministries():
    """Demonstration B at scenario scale: one human, two ministries, different
    Sundays, and never both on one Sunday.
    """
    scenario = build_joined_scenario()
    result = solve_joined_schedule(scenario)

    placements = _dates_by_person(result, scenario)[SHARED_SPREAD]
    assert {ministry_id for ministry_id, _ in placements} == {SETUP_LIKE, KIDS_LIKE}
    assert len({event_date for _, event_date in placements}) == len(placements)


def test_the_development_scenario_reports_its_contended_person():
    """The scarce shared volunteer is wanted by two ministries on every Sunday
    of the period, and every one of those is reported.
    """
    result = solve_joined_schedule(build_joined_scenario())

    contended = [c for c in result.contentions if c.person_id == SHARED_SCARCE]
    assert [c.event_date for c in contended] == list(SUNDAYS)
    for contention in contended:
        assert set(contention.contending_ministry_ids) | {
            contention.scheduled_ministry_id
        } == {SETUP_LIKE, AV_LIKE}


def test_moving_one_ministrys_requirement_can_cost_another_ministry():
    """Demonstration E. Nothing about ministry 2 changes between these two
    runs -- same role, same date, same single candidate. All that moves is
    **which Sunday ministry 1 needs somebody on**, and because the two
    ministries share that one person, ministry 2's schedule changes with it.

    A sequence of separate solves cannot express this. Whichever ministry ran
    second would see the first ministry's decision as an already-finalized
    commitment and simply work around it; neither would ever learn that the
    church as a whole was about to be short.
    """
    def scenario(setup_date: datetime.date):
        return (
            _ministry(
                1,
                role_id=11,
                demand={setup_date: 1},
                candidates=(_person(500, 1, role_id=11),),
            ),
            _ministry(
                2,
                role_id=22,
                demand={DATE_A: 1},
                candidates=(_person(500, 2, role_id=22),),
            ),
        )

    # Different Sundays: the shared person serves both ministries, and the
    # church-wide rule has nothing to say.
    apart = scenario(DATE_B)
    result = solve_joined_schedule(apart)
    _assert_church_wide_rule_holds(result, apart)
    assert result.is_complete
    assert result.contentions == ()

    # The same Sunday: the same people, the same total demand, and now one
    # position cannot be filled by anyone.
    together = scenario(DATE_A)
    result = solve_joined_schedule(together)
    _assert_church_wide_rule_holds(result, together)
    assert result.filled_count == 1
    assert result.unfilled_count == 1
    assert [c.person_id for c in result.contentions] == [500]


def test_the_development_scenario_reports_demand_it_cannot_meet():
    """Demonstration D at scenario scale: more positions than the joined
    roster can staff, reported as unfilled -- never as a broken rule, and
    never at the cost of the other two ministries.
    """
    scenario = build_joined_scenario(kids_final_sunday_helpers=12)
    result = solve_joined_schedule(scenario)

    _assert_church_wide_rule_holds(result, scenario)
    assert result.results_by_ministry[KIDS_LIKE].unfilled_count > 0
    assert result.results_by_ministry[SETUP_LIKE].is_complete
    assert result.results_by_ministry[AV_LIKE].is_complete


def test_the_joined_run_is_reproducible():
    """Same input, same schedule -- twice, and whatever order the ministries
    were passed in. One worker, one named strategy, a fixed seed, and blocks
    built in ascending ministry id.
    """
    scenario = build_joined_scenario()
    first = solve_joined_schedule(scenario)
    second = solve_joined_schedule(scenario)
    reversed_order = solve_joined_schedule(tuple(reversed(scenario)))

    for ministry_id in first.ministry_ids:
        expected = first.results_by_ministry[ministry_id]
        assert second.results_by_ministry[ministry_id] == expected
        assert reversed_order.results_by_ministry[ministry_id] == expected
    assert second.contentions == first.contentions
    assert reversed_order.contentions == first.contentions


def test_each_ministry_keeps_its_own_policy():
    """Fairness and variety are read per ministry, never pooled: a ministry
    that configures no role-variety preference reports no variety cost even
    when another ministry in the same run does.
    """
    result = solve_joined_schedule(build_joined_scenario())

    assert result.results_by_ministry[SETUP_LIKE].metrics.role_variety_cost is not None
    assert result.results_by_ministry[AV_LIKE].metrics.role_variety_cost is None
    assert result.results_by_ministry[SETUP_LIKE].metrics.target_excess_total is not None
    assert result.results_by_ministry[KIDS_LIKE].metrics.target_excess_total is None
    # Loads are per membership and therefore per ministry: a shared person
    # appears in both maps, under different ids, carrying different work.
    setup_loads = result.results_by_ministry[SETUP_LIKE].metrics.load_by_membership
    kids_loads = result.results_by_ministry[KIDS_LIKE].metrics.load_by_membership
    assert membership_for(SHARED_SPREAD, SETUP_LIKE) in setup_loads
    assert membership_for(SHARED_SPREAD, KIDS_LIKE) in kids_loads


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_a_joined_run_needs_at_least_one_ministry():
    with pytest.raises(SchedulingInputError, match="at least one ministry"):
        solve_joined_schedule(())


def test_a_ministry_may_not_appear_twice():
    entry = _ministry(
        1, role_id=11, demand={DATE_A: 1}, candidates=(_person(500, 1, role_id=11),)
    )
    with pytest.raises(SchedulingInputError, match="each ministry once"):
        solve_joined_schedule((entry, entry))


def test_one_membership_may_not_belong_to_two_ministries():
    shared = CandidateInput(
        membership_id=7_777,
        person_id=500,
        display_name="person-500",
        qualified_role_ids=frozenset({11, 22}),
    )
    scenario = (
        _ministry(1, role_id=11, demand={DATE_A: 1}, candidates=(shared,)),
        _ministry(2, role_id=22, demand={DATE_A: 1}, candidates=(shared,)),
    )
    with pytest.raises(SchedulingInputError, match="one membership belongs"):
        solve_joined_schedule(scenario)


def test_a_requirement_may_not_name_a_different_ministry():
    entry = JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=1,
            scheduling_period_id=1,
            ministry_id=1,
            requirements=(
                RequirementInput(
                    requirement_id=1,
                    event_id=101,
                    event_date=DATE_A,
                    ministry_role_id=11,
                    ministry_id=2,
                    required_count=1,
                ),
            ),
            candidates=(_person(500, 1, role_id=11),),
        ),
        policy=LENIENT,
    )
    with pytest.raises(SchedulingInputError, match="names ministry"):
        solve_joined_schedule((entry,))


def test_input_that_already_breaks_the_rule_is_named_not_solved_around():
    """Two existing assignments already placing one person in two ministries
    on one date cannot be fixed by scheduling: the engine never removes a
    fixed row. Saying so beats letting CP-SAT report an infeasible model.
    """
    scenario = (
        _ministry(
            1,
            role_id=11,
            demand={DATE_A: 1},
            candidates=(_person(500, 1, role_id=11),),
            existing=(
                ExistingAssignmentInput(
                    assignment_id=1,
                    requirement_id=1_000,
                    membership_id=10_500,
                    event_id=100,
                ),
            ),
        ),
        _ministry(
            2,
            role_id=22,
            demand={DATE_A: 1},
            candidates=(_person(500, 2, role_id=22),),
            existing=(
                ExistingAssignmentInput(
                    assignment_id=2,
                    requirement_id=2_000,
                    membership_id=20_500,
                    event_id=200,
                ),
            ),
        ),
    )
    with pytest.raises(SchedulingInputError, match="already holds assignments"):
        solve_joined_schedule(scenario)


def test_each_ministrys_own_input_is_validated_too():
    """The single-ministry validation is not skipped on the joined path."""
    entry = _ministry(
        1,
        role_id=11,
        demand={DATE_A: 1},
        candidates=(
            _person(500, 1, role_id=11),
            _person(500, 1, role_id=11),
        ),
    )
    with pytest.raises(SchedulingInputError, match="membership ids must be unique"):
        solve_joined_schedule((entry,))
