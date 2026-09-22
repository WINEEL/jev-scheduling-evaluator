"""Assignment-count fairness and the configurable target (Task 32).

Hand-written inputs, the real CP-SAT solver, no database.

The whole point of this layer is that it is *subordinate*: it chooses among
schedules that already fill the maximum number of positions. So most tests here
assert a load distribution while also asserting the fill count did not drop --
a fairness win that costs a filled Sunday is not a win.

Where several distributions are equally fair, the tests assert the shape
(sorted loads, costs) rather than which particular person got which slot;
inventing a tie-break to make an assertion convenient would be inventing a
product rule.
"""

from __future__ import annotations

import datetime
from collections import Counter
from pathlib import Path
from types import MappingProxyType

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import (
    DIAGNOSTIC_NO_QUALIFIED_CANDIDATES,
    DIAGNOSTIC_NO_RESPONSE_DISALLOWED,
    SolutionMetrics,
)
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
ASSIST = 13

#: Nothing soft configured at all. Balancing is switched off explicitly
#: because it no longer rides along with the target: the two are separate
#: preferences, and "no target" is not the same statement as "do not spread
#: the work".
NO_TARGET = SchedulingPolicy(
    allow_no_response=False, balance_candidate_loads=False
)
TARGET_3 = SchedulingPolicy(allow_no_response=False, target_assignments_per_candidate=3)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int, *, event_index: int = 0, ministry_role_id: int = LEAD,
    required_count: int = 1, role_is_active: bool = True,
) -> RequirementInput:
    """One requirement on its own Sunday unless told otherwise, so tests about
    load can vary load without tripping the same-event rule.
    """
    return RequirementInput(
        requirement_id=requirement_id, event_id=700 + event_index,
        event_date=_sunday(event_index), ministry_role_id=ministry_role_id,
        ministry_id=3, required_count=required_count, role_is_active=role_is_active,
    )


def _candidate(
    membership_id: int, *, qualified=(LEAD,), available_events=range(0, 12),
    availability=None, blocked=(),
) -> CandidateInput:
    answers = (
        dict(availability)
        if availability is not None
        else {700 + i: AvailabilityState.AVAILABLE for i in available_events}
    )
    return CandidateInput(
        membership_id=membership_id, person_id=membership_id,
        display_name=f"Member {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(answers),
        blocked_dates=frozenset(blocked),
    )


def _input(requirements=(), candidates=(), existing=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
    )


def _proposed_loads(result) -> Counter:
    return Counter(p.membership_id for p in result.proposed_assignments)


def _sorted_total_loads(result) -> list[int]:
    return sorted(result.metrics.load_by_membership.values())


# --------------------------------------------------------------------------
# 1-3 -- Backward compatibility with Task 31
# --------------------------------------------------------------------------


def test_01_no_target_preserves_maximum_fill_semantics():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118), _candidate(119)],
        ),
        policy=NO_TARGET,
    )

    assert result.filled_count == 4
    assert result.is_complete is True


def test_02_no_target_introduces_no_hidden_fairness_preference():
    """Six equivalent Sundays and three people, with balancing switched off.
    The engine must not spread the work anyway: a preference nobody asked for
    is not the engine's to invent.

    Note this is no longer the *default* -- balancing is on unless a caller
    turns it off -- so the policy here says so explicitly.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=NO_TARGET,
    )

    assert result.filled_count == 6
    # No soft optimum was computed, and none is reported.
    assert result.metrics.target_excess_total is None
    assert result.metrics.fairness_cost is None
    # The distribution is simply whatever maximum fill produced.
    assert sum(_proposed_loads(result).values()) == 6


def test_03_the_default_policy_is_still_task_31_behavior():
    """A caller written before Task 32 gets exactly what it got before."""
    policy = SchedulingPolicy(allow_no_response=True)

    assert policy.target_assignments_per_candidate is None

    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability={})],
        ),
        policy=policy,
    )
    assert result.filled_count == 1


# --------------------------------------------------------------------------
# 4-8 -- The primary objective still dominates
# --------------------------------------------------------------------------


def test_04_fairness_never_reduces_maximum_fill():
    """Five Sundays, one generalist and one specialist-by-availability. The
    fairest split is impossible; filling five beats balancing.
    """
    requirements = [_requirement(600 + i, event_index=i) for i in range(5)]
    workhorse = _candidate(118, available_events=range(0, 5))
    occasional = _candidate(119, available_events=[0])

    balanced = solve_schedule(
        _input(requirements=requirements, candidates=[workhorse, occasional]),
        policy=TARGET_3,
    )

    assert balanced.filled_count == 5
    assert balanced.is_complete is True
    assert max(_sorted_total_loads(balanced)) >= 4  # someone had to exceed 3


def test_05_the_target_is_never_a_hard_cap():
    """Six Sundays, one eligible person, target 3. A cap would leave three
    Sundays empty; a preference fills them all.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118)],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 6
    assert result.is_complete is True
    assert result.metrics.load_by_membership[118] == 6
    assert result.metrics.target_excess_total == 3


def test_06_a_scarce_specialist_may_exceed_the_target():
    """The approved lead-scarcity behavior, and it needs no special code:
    hard qualification produces it on its own.
    """
    requirements = [
        _requirement(600 + i, event_index=i, ministry_role_id=LEAD) for i in range(5)
    ]
    only_lead = _candidate(118, qualified=(LEAD,))
    assistants = [_candidate(119, qualified=(ASSIST,)), _candidate(120, qualified=(ASSIST,))]

    result = solve_schedule(
        _input(requirements=requirements, candidates=[only_lead, *assistants]),
        policy=TARGET_3,
    )

    assert result.is_complete is True
    assert result.metrics.load_by_membership[118] == 5
    assert 119 not in result.metrics.load_by_membership  # not qualified, not used


def test_07_availability_rules_still_dominate_fairness():
    """A perfectly balanced schedule that used someone who said no is not an
    option: they never get a variable.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[
                _candidate(118, available_events=range(0, 4)),
                _candidate(
                    119,
                    availability={700 + i: AvailabilityState.UNAVAILABLE for i in range(4)},
                ),
            ],
        ),
        policy=TARGET_3,
    )

    assert _proposed_loads(result) == Counter({118: 4})
    assert 119 not in result.metrics.load_by_membership


def test_07b_no_response_policy_still_dominates_fairness():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[
                _candidate(118, available_events=range(0, 4)),
                _candidate(119, availability={}),  # silent
            ],
        ),
        policy=TARGET_3,  # allow_no_response is False
    )

    assert _proposed_loads(result) == Counter({118: 4})


def test_08_the_same_event_rule_still_dominates_fairness():
    """Two roles in one event and one heavily-loaded person: fairness would
    love to spread, but the second role simply cannot go to the same person.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, ministry_role_id=LEAD),
                _requirement(601, event_index=0, ministry_role_id=ASSIST),
            ],
            candidates=[_candidate(118, qualified=(LEAD, ASSIST))],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 1
    assert result.unfilled_count == 1
    assert result.metrics.load_by_membership[118] == 1


# --------------------------------------------------------------------------
# 9-13 -- Basic fairness
# --------------------------------------------------------------------------


def test_09_six_slots_and_three_candidates_are_spread_two_each():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 6
    assert _sorted_total_loads(result) == [2, 2, 2]
    assert result.metrics.fairness_cost == 12  # 2^2 * 3, the convex optimum


def test_10_four_slots_and_two_candidates_are_spread_two_each():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118), _candidate(119)],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 4
    assert _sorted_total_loads(result) == [2, 2]
    assert result.metrics.fairness_cost == 8  # beats 3,1 at 10


def test_11_12_existing_load_pushes_new_work_toward_the_less_burdened():
    """A already carries two manual assignments; B and C carry none. The two
    new Sundays should go to B and C.
    """
    existing_requirement = _requirement(600, event_index=0, required_count=2)
    result = solve_schedule(
        _input(
            requirements=[
                existing_requirement,
                _requirement(601, event_index=1),
                _requirement(602, event_index=2),
            ],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
                ),
                ExistingAssignmentInput(
                    assignment_id=901, requirement_id=600, membership_id=119, event_id=700,
                ),
            ],
        ),
        policy=TARGET_3,
    )

    # Two new Sundays for three people, two of whom already carry one each.
    # Several distributions tie at the convex optimum (C could take one or
    # both), so the assertion is on the shape, not on who won a tie.
    assert result.filled_count == 2
    assert _sorted_total_loads(result) == [1, 1, 2]
    assert result.metrics.fairness_cost == 6  # 1+1+4; stacking both on A costs 9
    assert max(_sorted_total_loads(result)) <= 2
    # The person carrying nothing was certainly used.
    assert _proposed_loads(result)[120] >= 1


def test_11b_existing_load_changes_which_schedule_is_optimal():
    """A carries three already; B carries none, and two fresh Sundays are open.

    The optimum is B taking *both*: 3,2 costs 13 where splitting them costs
    17. An engine that ignored existing load would see two idle people and
    split one each -- a different, uniquely-optimal answer in that broken
    model, so this test cannot pass by a lucky tie.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(5)],
            candidates=[_candidate(118), _candidate(119)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900 + i, requirement_id=600 + i, membership_id=118,
                    event_id=700 + i,
                )
                for i in range(3)
            ],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 2
    assert _proposed_loads(result) == Counter({119: 2})
    assert result.metrics.load_by_membership == {118: 3, 119: 2}
    assert result.metrics.fairness_cost == 13


def test_12b_the_least_loaded_candidate_is_favored_for_a_single_slot():
    """One new Sunday, three people, two of whom already carry work. It should
    go to the one carrying none.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, required_count=2),
                _requirement(601, event_index=1),
            ],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
                ),
                ExistingAssignmentInput(
                    assignment_id=901, requirement_id=600, membership_id=119, event_id=700,
                ),
            ],
        ),
        policy=TARGET_3,
    )

    assert _proposed_loads(result) == Counter({120: 1})
    assert _sorted_total_loads(result) == [1, 1, 1]


def test_13_specialized_eligibility_may_legitimately_create_unequal_load():
    """Three lead Sundays and one assist Sunday: the only lead carries three
    while the assistants carry less, and that is correct, not unfair.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, ministry_role_id=LEAD),
                _requirement(601, event_index=1, ministry_role_id=LEAD),
                _requirement(602, event_index=2, ministry_role_id=LEAD),
                _requirement(603, event_index=3, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD,)),
                _candidate(119, qualified=(ASSIST,)),
            ],
        ),
        policy=TARGET_3,
    )

    assert result.is_complete is True
    assert result.metrics.load_by_membership == {118: 3, 119: 1}


# --------------------------------------------------------------------------
# 14-18 -- Target semantics
# --------------------------------------------------------------------------


def test_14_a_candidate_at_the_target_yields_to_one_below_it():
    """A is already at three; B is at zero. The next Sunday should not take A
    to four when B can serve.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, required_count=3),
                _requirement(601, event_index=1),
            ],
            candidates=[_candidate(118), _candidate(119), _candidate(120), _candidate(121)],
            existing=[
                ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                ExistingAssignmentInput(assignment_id=901, requirement_id=600, membership_id=119, event_id=700),
                ExistingAssignmentInput(assignment_id=902, requirement_id=600, membership_id=120, event_id=700),
            ],
        ),
        policy=TARGET_3,
    )

    # The one carrying nothing takes the new Sunday.
    assert _proposed_loads(result) == Counter({121: 1})
    assert result.metrics.target_excess_total == 0


def test_15_exceeding_the_target_is_allowed_when_it_fills_a_position():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118)],
        ),
        policy=TARGET_3,
    )

    assert result.is_complete is True
    assert result.metrics.load_by_membership[118] == 4
    assert result.metrics.target_excess_total == 1


def test_16_total_excess_above_target_is_minimized():
    """Eight Sundays, three people, target 3. Nine target-slots exist, so a
    distribution with no excess at all is possible and should be chosen.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(8)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 8
    assert result.metrics.target_excess_total == 0
    assert _sorted_total_loads(result) == [2, 3, 3]


def test_16b_when_excess_is_unavoidable_it_is_shared_rather_than_stacked():
    """Ten Sundays, three people, target 3: one over-target assignment is
    unavoidable, and the convex pass spreads the rest.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(10)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=TARGET_3,
    )

    assert result.filled_count == 10
    assert result.metrics.target_excess_total == 1
    assert _sorted_total_loads(result) == [3, 3, 4]


@pytest.mark.parametrize("target", [0, 1])
def test_17_a_small_target_still_does_not_reduce_maximum_fill(target):
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118), _candidate(119)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=False, target_assignments_per_candidate=target
        ),
    )

    assert result.filled_count == 4
    assert result.is_complete is True
    assert _sorted_total_loads(result) == [2, 2]


@pytest.mark.parametrize("target", [-1, -3])
def test_18_a_negative_target_is_rejected(target):
    with pytest.raises(SchedulingInputError, match="target_assignments_per_candidate"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)], candidates=[_candidate(118)],
            ),
            policy=SchedulingPolicy(
                allow_no_response=False, target_assignments_per_candidate=target
            ),
        )


# --------------------------------------------------------------------------
# 19-21 -- The fairness formulation itself
# --------------------------------------------------------------------------


def test_19_convex_balance_separates_distributions_that_target_deviation_ties():
    """Six Sundays, three people, target 3. Every distribution has zero excess
    above the target, so a target-deviation objective alone would call 4,1,1
    and 2,2,2 equally good. The convex cost does not: 18 against 12.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=TARGET_3,
    )

    assert result.metrics.target_excess_total == 0  # the tie the target cannot break
    assert _sorted_total_loads(result) == [2, 2, 2]
    assert result.metrics.fairness_cost == 12
    assert result.metrics.fairness_cost < 4 ** 2 + 1 + 1  # 4,1,1 would cost 18


def test_20_fairness_counts_existing_plus_proposed_load():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, required_count=1),
                _requirement(601, event_index=1),
            ],
            candidates=[_candidate(118), _candidate(119)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
                ),
            ],
        ),
        policy=TARGET_3,
    )

    # The load map counts the existing row as well as the new proposal.
    assert result.metrics.load_by_membership == {118: 1, 119: 1}
    assert result.metrics.fairness_cost == 2
    assert _proposed_loads(result) == Counter({119: 1})


def test_21_existing_assignments_are_never_moved_to_improve_fairness():
    """A is at three and B at zero, with no new slots to hand out. A fairer
    world would move one of A's, and the solver must not: those are decisions
    people made.
    """
    scheduling_input = _input(
        requirements=[_requirement(600, event_index=0, required_count=3)],
        candidates=[_candidate(118), _candidate(119)],
        existing=[
            ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
            ExistingAssignmentInput(assignment_id=901, requirement_id=600, membership_id=118, event_id=700),
            ExistingAssignmentInput(assignment_id=902, requirement_id=600, membership_id=118, event_id=700),
        ],
    )

    with pytest.raises(SchedulingInputError, match="more than once in event"):
        # Three assignments for one person in one event is impossible input --
        # which is itself the guard that this shape cannot be modelled around.
        solve_schedule(scheduling_input, policy=TARGET_3)

    # The legitimate version: three separate Sundays, all already A's.
    legitimate = _input(
        requirements=[_requirement(600 + i, event_index=i) for i in range(3)],
        candidates=[_candidate(118), _candidate(119)],
        existing=[
            ExistingAssignmentInput(assignment_id=900 + i, requirement_id=600 + i, membership_id=118, event_id=700 + i)
            for i in range(3)
        ],
    )

    before = legitimate.existing_assignments
    result = solve_schedule(legitimate, policy=TARGET_3)

    assert result.proposed_assignments == ()  # nothing left to fill
    assert result.metrics.load_by_membership == {118: 3}
    # A's three stayed exactly where they were; none was reassigned to B.
    assert legitimate.existing_assignments == before
    assert all(a.membership_id == 118 for a in legitimate.existing_assignments)


# --------------------------------------------------------------------------
# 22-24 -- Existing assignments
# --------------------------------------------------------------------------


def test_22_existing_assignments_count_toward_target_excess():
    """A already carries four; the target is three. That excess is real and
    reported even though the solver did not create it.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118), _candidate(119)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900 + i, requirement_id=600 + i, membership_id=118,
                    event_id=700 + i,
                )
                for i in range(4)
            ],
        ),
        policy=TARGET_3,
    )

    assert result.proposed_assignments == ()
    assert result.metrics.load_by_membership == {118: 4}
    assert result.metrics.target_excess_total == 1


def test_23_an_existing_assignment_is_not_re_emitted_by_the_fairness_layer():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, event_index=0, required_count=2)],
            candidates=[_candidate(118), _candidate(119)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
                ),
            ],
        ),
        policy=TARGET_3,
    )

    assert _proposed_loads(result) == Counter({119: 1})
    assert 118 not in {p.membership_id for p in result.proposed_assignments}
    assert result.metrics.load_by_membership == {118: 1, 119: 1}


def test_24_an_overfilled_requirement_takes_nothing_but_its_load_still_counts():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, required_count=1),
                _requirement(601, event_index=1),
            ],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
            existing=[
                ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                ExistingAssignmentInput(assignment_id=901, requirement_id=600, membership_id=119, event_id=700),
            ],
        ),
        policy=TARGET_3,
    )

    # The overfilled requirement gets nothing new and is not a failure...
    assert not any(p.requirement_id == 600 for p in result.proposed_assignments)
    assert result.is_complete is True
    # ...and both existing volunteers' load counted, so the fresh Sunday went
    # to the person carrying nothing.
    assert _proposed_loads(result) == Counter({120: 1})


# --------------------------------------------------------------------------
# 25-26 -- Determinism
# --------------------------------------------------------------------------


def test_25_repeated_solves_of_the_same_input_are_stable():
    scheduling_input = _input(
        requirements=[_requirement(600 + i, event_index=i) for i in range(7)],
        candidates=[_candidate(118), _candidate(119), _candidate(120)],
    )

    results = [solve_schedule(scheduling_input, policy=TARGET_3) for _ in range(5)]

    assert all(r == results[0] for r in results)
    assert all(
        r.metrics.load_by_membership == results[0].metrics.load_by_membership
        for r in results
    )


def test_26_output_ordering_remains_deterministic():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(602, event_index=2),
                _requirement(600, event_index=0),
                _requirement(601, event_index=1),
            ],
            candidates=[_candidate(120), _candidate(118), _candidate(119)],
        ),
        policy=TARGET_3,
    )

    keys = [(p.requirement_id, p.membership_id) for p in result.proposed_assignments]
    assert keys == sorted(keys)
    assert list(result.metrics.load_by_membership) == sorted(
        result.metrics.load_by_membership
    )


# --------------------------------------------------------------------------
# 27-28 -- Diagnostics stay about hard feasibility
# --------------------------------------------------------------------------


def test_27_hard_diagnostics_are_unchanged_by_the_fairness_layer():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, ministry_role_id=LEAD)],
            candidates=[_candidate(118, qualified=(ASSIST,))],
        ),
        policy=TARGET_3,
    )

    assert result.unfilled_requirements[0].diagnostic_codes == (
        DIAGNOSTIC_NO_QUALIFIED_CANDIDATES,
    )


def test_28_no_soft_outcome_is_reported_as_an_unfilled_hard_diagnostic():
    """A fully staffed schedule that exceeds the target is not "unresolved" --
    it has no unfilled rows at all, and no soft code exists to put in one.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(5)],
            candidates=[_candidate(118)],
        ),
        policy=TARGET_3,
    )

    assert result.is_complete is True
    assert result.unfilled_requirements == ()
    assert result.metrics.target_excess_total == 2

    from app.scheduling import result as result_module

    codes = {v for k, v in vars(result_module).items() if k.startswith("DIAGNOSTIC_")}
    assert "UNFAIR" not in codes
    assert "OVER_TARGET" not in codes


def test_28b_a_soft_shortfall_still_reports_only_its_hard_cause():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(2)],
            candidates=[_candidate(118, availability={})],
        ),
        policy=TARGET_3,  # strict about silence
    )

    for unfilled in result.unfilled_requirements:
        assert DIAGNOSTIC_NO_RESPONSE_DISALLOWED in unfilled.diagnostic_codes


# --------------------------------------------------------------------------
# 29-32 -- Purity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ["app.scheduling.solver", "app.scheduling.result", "app.scheduling.input"],
)
def test_29_30_the_fairness_layer_adds_no_database_imports(module_name):
    import ast
    import importlib

    module = importlib.import_module(module_name)
    tree = ast.parse(Path(module.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    for name in imported:
        assert not name.startswith("sqlalchemy"), name
        assert not name.startswith("app.models"), name
        assert not name.startswith("app.services"), name
        assert not name.startswith("app.db"), name


def test_31_solving_with_a_target_does_not_mutate_the_input():
    scheduling_input = _input(
        requirements=[_requirement(600 + i, event_index=i) for i in range(3)],
        candidates=[_candidate(118), _candidate(119)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
        ],
    )
    before = (
        scheduling_input.requirements,
        scheduling_input.candidates,
        scheduling_input.existing_assignments,
    )

    solve_schedule(scheduling_input, policy=NO_TARGET)
    solve_schedule(scheduling_input, policy=TARGET_3)

    assert (
        scheduling_input.requirements,
        scheduling_input.candidates,
        scheduling_input.existing_assignments,
    ) == before


def test_32_no_persistence_or_audit_logic_appears_in_the_solver():
    import app.scheduling.solver as module

    for forbidden in ("record_audit_event", "assign_member", "Assignment", "Session"):
        assert not hasattr(module, forbidden)


def test_the_metrics_object_is_frozen_and_read_only():
    result = solve_schedule(
        _input(requirements=[_requirement(600)], candidates=[_candidate(118)]),
        policy=TARGET_3,
    )

    with pytest.raises(Exception):
        result.metrics.fairness_cost = 0
    with pytest.raises(TypeError):
        result.metrics.load_by_membership[999] = 1
    assert isinstance(result.metrics, SolutionMetrics)


# --------------------------------------------------------------------------
# 33-36 -- How the objective is actually implemented
# --------------------------------------------------------------------------


def test_33_34_35_the_passes_are_lexicographic_not_weighted():
    """Each pass fixes its optimum as a constraint before the next sets its
    objective, so no weight can let fairness outbid a filled position.
    """
    import ast

    import app.scheduling.solver as module

    tree = ast.parse(Path(module.__file__).read_text())
    objectives = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Maximize", "Minimize", "AddWeightedSum"}
    ]
    # Task 33 appended a fourth objective; Task 52 appended a fifth (backup
    # avoidance, directly after fill). The ordering property is unchanged.
    assert objectives == ["Maximize", "Minimize", "Minimize", "Minimize", "Minimize"]

    # Each pass is labelled, and every optimum is constrained before the next
    # objective is set.
    source = Path(module.__file__).read_text()
    assert source.count("_solve_pass(") == 6  # one definition, five calls
    assert "model.Add(fill == optimal_fill)" in source
    assert "model.Add(total_backup == optimal_backup)" in source
    assert "model.Add(total_excess == optimal_excess)" in source
    assert "model.Add(total_cost == optimal_cost)" in source


def test_36_no_alphabetical_or_id_based_tie_break_is_introduced():
    """Nothing in the objective mentions a candidate id or name; where several
    distributions are equally fair, CP-SAT's deterministic search picks one and
    no business preference is implied.
    """
    import ast

    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    for smell in ("display_name", "sorted(candidates", "person_id"):
        assert smell not in source

    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Minimize", "Maximize"}
        ):
            # The objectives are plain sums of fill, excess or square vars.
            rendered = ast.unparse(node)
            assert "membership_id" not in rendered
            assert "name" not in rendered
