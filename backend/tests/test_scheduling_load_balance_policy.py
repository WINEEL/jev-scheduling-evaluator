"""Task 45 -- candidate load balancing is independent of the numeric target.

Hand-written inputs, the real CP-SAT solver, no database.

The bug this file pins down was found by the real AV validation (Task 44): the
load-balancing pass was gated behind ``target_assignments_per_candidate is not
None``, so a ministry with no documented serving target got **no balancing at
all** -- one volunteer on nearly every service while eligible people sat
unused -- and the only way to obtain a sensible spread was to invent a number
the ministry had never agreed to. The target's *value* barely mattered; its
mere presence switched fairness on.

The two are separate preferences and are now separate fields. These tests
cover all four combinations, plus the ordering rules that keep every soft
preference subordinate to filling positions.

Where several distributions are equally good the assertions are on
objective-relevant shape -- sorted loads, costs, how many people served --
never on which particular person got which slot. Multiple optima exist here by
construction, and asserting one of them would be asserting a CP-SAT tie-break
rather than a product rule.
"""

from __future__ import annotations

import datetime
from collections import Counter
from types import MappingProxyType

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.solver import SchedulingPolicy, solve_schedule

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
SUPPORT_A = 13
SUPPORT_B = 14

# The four combinations under test. Balancing defaults to True, so the two
# "balance on" policies deliberately leave it unstated -- that is what an
# existing caller sends.
NO_TARGET_BALANCED = SchedulingPolicy(allow_no_response=False)
NO_TARGET_UNBALANCED = SchedulingPolicy(
    allow_no_response=False, balance_candidate_loads=False
)
TARGET_3_BALANCED = SchedulingPolicy(
    allow_no_response=False, target_assignments_per_candidate=3
)
TARGET_3_UNBALANCED = SchedulingPolicy(
    allow_no_response=False, target_assignments_per_candidate=3,
    balance_candidate_loads=False,
)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int, *, event_index: int, ministry_role_id: int = LEAD
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=700 + event_index,
        event_date=_sunday(event_index), ministry_role_id=ministry_role_id,
        ministry_id=3, required_count=1, role_is_active=True,
    )


def _candidate(
    membership_id: int, *, qualified=(LEAD,), available_events=range(0, 12)
) -> CandidateInput:
    return CandidateInput(
        membership_id=membership_id, person_id=membership_id,
        display_name=f"Member {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(
            {700 + i: AvailabilityState.AVAILABLE for i in available_events}
        ),
        blocked_dates=frozenset(),
    )


def _input(requirements=(), candidates=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=(),
    )


def _sorted_loads(result) -> list[int]:
    return sorted(result.metrics.load_by_membership.values())


def _nine_sundays_three_people(policy):
    """Nine interchangeable Sundays, three interchangeable people.

    Every schedule that fills all nine is legal, so the *only* thing that can
    distinguish 3/3/3 from 9/0/0 is the balancing preference.
    """
    return solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(9)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=policy,
    )


# --------------------------------------------------------------------------
# A / B / C / D -- the four combinations
# --------------------------------------------------------------------------


def test_a_no_target_with_balancing_still_spreads_the_work():
    """The AV case, and the bug. No numeric target, balancing on."""
    result = _nine_sundays_three_people(NO_TARGET_BALANCED)

    assert result.filled_count == 9
    # Perfectly even is reachable here, so anything else means the pass did
    # not run: sum(load^2) is 27 for 3/3/3 and 81 for 9/0/0.
    assert _sorted_loads(result) == [3, 3, 3]
    assert result.metrics.fairness_cost == 27
    # No target was set, so nothing measured excess against one.
    assert result.metrics.target_excess_total is None


def test_b_no_target_and_no_balancing_optimizes_neither():
    """Nothing soft configured: the engine must invent no preference."""
    result = _nine_sundays_three_people(NO_TARGET_UNBALANCED)

    assert result.filled_count == 9
    assert result.metrics.target_excess_total is None
    assert result.metrics.fairness_cost is None


def test_c_target_with_balancing_preserves_setups_behavior():
    """Setup's configuration, unchanged by this task."""
    result = _nine_sundays_three_people(TARGET_3_BALANCED)

    assert result.filled_count == 9
    assert _sorted_loads(result) == [3, 3, 3]
    assert result.metrics.target_excess_total == 0
    assert result.metrics.fairness_cost == 27


def test_d_target_without_balancing_still_honours_the_target():
    """The target preference applies on its own; only the squared-load pass
    is withheld.
    """
    result = _nine_sundays_three_people(TARGET_3_UNBALANCED)

    assert result.filled_count == 9
    # Nine positions, three people, target 3: zero excess is reachable, and
    # the target pass must reach it without the balancing pass.
    assert result.metrics.target_excess_total == 0
    assert result.metrics.fairness_cost is None
    # Zero excess against a target of 3 forces no load above 3, which with
    # nine positions leaves only 3/3/3 -- reached by the target pass alone.
    assert _sorted_loads(result) == [3, 3, 3]


def test_d_target_alone_leaves_distribution_free_below_the_target():
    """Where the target does not pin the shape, balancing is what would --
    and with balancing off, nothing does.

    Six positions, three people, target 5: every distribution with nobody
    above 5 scores zero excess, so 5/1/0 and 2/2/2 are equally good to the
    target pass. The assertion is therefore on what was *optimized*, not on
    which optimum came back.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=False, target_assignments_per_candidate=5,
            balance_candidate_loads=False,
        ),
    )

    assert result.filled_count == 6
    assert result.metrics.target_excess_total == 0
    assert result.metrics.fairness_cost is None
    assert max(result.metrics.load_by_membership.values()) <= 5

    # The same input with balancing on is pinned to the even distribution.
    balanced = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=False, target_assignments_per_candidate=5,
        ),
    )
    assert balanced.filled_count == 6
    assert _sorted_loads(balanced) == [2, 2, 2]
    assert balanced.metrics.fairness_cost == 12


# --------------------------------------------------------------------------
# Ordering: fill first, then target, then balance, then variety
# --------------------------------------------------------------------------


def test_balancing_never_costs_a_filled_position():
    """One person can serve every Sunday; the others only the first.

    The even split (1/1/1 over three of the four Sundays) is fairer and fills
    less. Maximum fill is a constraint by the time balancing runs, so the
    lopsided-but-complete schedule must win.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[
                _candidate(118, available_events=range(0, 4)),
                _candidate(119, available_events=[0]),
                _candidate(120, available_events=[0]),
            ],
        ),
        policy=NO_TARGET_BALANCED,
    )

    assert result.filled_count == 4
    # 118 must take the three Sundays only they can serve.
    assert result.metrics.load_by_membership[118] == 3
    assert sorted(result.metrics.load_by_membership.values()) == [1, 3]


def test_balancing_never_outranks_the_target():
    """Target 1 with six positions and three people. Excess is minimized
    first, and balancing then chooses among the schedules that achieve it --
    it cannot raise excess to flatten the loads further.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=False, target_assignments_per_candidate=1
        ),
    )

    assert result.filled_count == 6
    # Six positions over three people with a target of 1: three unavoidable
    # excess assignments, spread as evenly as that allows.
    assert result.metrics.target_excess_total == 3
    assert _sorted_loads(result) == [2, 2, 2]
    assert result.metrics.fairness_cost == 12


def test_role_variety_still_runs_after_candidate_balancing():
    """Six positions across three roles, three people, no target.

    Balancing pins the loads to 2/2/2; variety then chooses *which* roles
    each person serves among those already-balanced schedules. If variety ran
    first, or balancing had not run at all, the loads would not be even.
    """
    requirements = [
        _requirement(600, event_index=0, ministry_role_id=SUPPORT_A),
        _requirement(601, event_index=0, ministry_role_id=SUPPORT_B),
        _requirement(602, event_index=1, ministry_role_id=SUPPORT_A),
        _requirement(603, event_index=1, ministry_role_id=SUPPORT_B),
        _requirement(604, event_index=2, ministry_role_id=SUPPORT_A),
        _requirement(605, event_index=2, ministry_role_id=SUPPORT_B),
    ]
    people = [
        _candidate(118, qualified=(SUPPORT_A, SUPPORT_B)),
        _candidate(119, qualified=(SUPPORT_A, SUPPORT_B)),
        _candidate(120, qualified=(SUPPORT_A, SUPPORT_B)),
    ]
    result = solve_schedule(
        _input(requirements=requirements, candidates=people),
        policy=SchedulingPolicy(
            allow_no_response=False,
            role_variety_role_ids=frozenset({SUPPORT_A, SUPPORT_B}),
        ),
    )

    assert result.filled_count == 6
    assert _sorted_loads(result) == [2, 2, 2]
    assert result.metrics.fairness_cost == 12
    assert result.metrics.target_excess_total is None
    # Two assignments each, one per role, is variety cost 1+1 per person.
    assert result.metrics.role_variety_cost == 6


def test_variety_cannot_unbalance_candidate_loads():
    """The same board with balancing switched off: variety is free to pile
    work onto fewer people, which is exactly what it must not be able to do
    when balancing is on. Contrasting the two is what shows the ordering.
    """
    requirements = [
        _requirement(600, event_index=0, ministry_role_id=SUPPORT_A),
        _requirement(601, event_index=0, ministry_role_id=SUPPORT_B),
        _requirement(602, event_index=1, ministry_role_id=SUPPORT_A),
        _requirement(603, event_index=1, ministry_role_id=SUPPORT_B),
        _requirement(604, event_index=2, ministry_role_id=SUPPORT_A),
        _requirement(605, event_index=2, ministry_role_id=SUPPORT_B),
    ]
    people = [
        _candidate(118, qualified=(SUPPORT_A, SUPPORT_B)),
        _candidate(119, qualified=(SUPPORT_A, SUPPORT_B)),
        _candidate(120, qualified=(SUPPORT_A, SUPPORT_B)),
    ]
    unbalanced = solve_schedule(
        _input(requirements=requirements, candidates=people),
        policy=SchedulingPolicy(
            allow_no_response=False, balance_candidate_loads=False,
            role_variety_role_ids=frozenset({SUPPORT_A, SUPPORT_B}),
        ),
    )
    assert unbalanced.filled_count == 6
    assert unbalanced.metrics.fairness_cost is None
    # Whatever shape variety chose, balancing was not consulted about it.
    assert sum(unbalanced.metrics.load_by_membership.values()) == 6


# --------------------------------------------------------------------------
# Hard constraints and defaults
# --------------------------------------------------------------------------


def test_balancing_is_on_by_default_for_a_caller_that_says_nothing():
    """An existing client that never heard of this field gets balancing --
    the deliberate, backward-compatible default.
    """
    assert SchedulingPolicy().balance_candidate_loads is True
    assert SchedulingPolicy(allow_no_response=True).balance_candidate_loads is True
    result = _nine_sundays_three_people(SchedulingPolicy(allow_no_response=False))
    assert _sorted_loads(result) == [3, 3, 3]


def test_balancing_respects_qualification_and_availability():
    """Fairness is a preference; qualification and availability are rules.

    120 is qualified but unavailable on every Sunday but one, and 119 is not
    qualified at all. A balanced-looking schedule that used either would be
    illegal, so the loads stay uneven.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[
                _candidate(118, available_events=range(0, 4)),
                _candidate(119, qualified=(SUPPORT_A,), available_events=range(0, 4)),
                _candidate(120, available_events=[3]),
            ],
        ),
        policy=NO_TARGET_BALANCED,
    )

    assert result.filled_count == 4
    assert 119 not in result.metrics.load_by_membership
    assert result.metrics.load_by_membership[118] == 3
    assert result.metrics.load_by_membership[120] == 1


def test_one_assignment_per_person_per_event_survives_balancing():
    """Two positions on one Sunday and one person who could take both: the
    same-event rule is a constraint, so balancing cannot fill both.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, ministry_role_id=SUPPORT_A),
                _requirement(601, event_index=0, ministry_role_id=SUPPORT_B),
            ],
            candidates=[_candidate(118, qualified=(SUPPORT_A, SUPPORT_B))],
        ),
        policy=NO_TARGET_BALANCED,
    )

    assert result.filled_count == 1
    assert result.unfilled_count == 1
    assert result.metrics.load_by_membership[118] == 1
