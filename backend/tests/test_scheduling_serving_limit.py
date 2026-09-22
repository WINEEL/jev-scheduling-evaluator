"""The per-person, per-period hard serving maximum in the pure solver (Task 47).

Hand-written inputs, the real CP-SAT solver, no database.

The maximum is a **hard** rule: it is added as a constraint before any
objective is set, so every soft pass -- target excess, load balancing, role
variety -- chooses only among schedules that already respect it. These tests
therefore assert two different kinds of property, and keep them apart:

- that a cap is never exceeded, which is exact and always assertable;
- that fill is not reduced when the cap does not bind, which is about the
  objective.

Where several schedules are equally good the assertions are on objective-
relevant shape -- loads, counts, which requirements are unfilled -- never on
which particular person got which slot. Multiple optima exist here by
construction.

Every person is synthetic.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import DIAGNOSTIC_ALL_AT_PERIOD_LIMIT
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
SUPPORT_A = 13
SUPPORT_B = 14

LENIENT = SchedulingPolicy(allow_no_response=True)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(requirement_id: int, *, event_index: int,
                 ministry_role_id: int = LEAD) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=700 + event_index,
        event_date=_sunday(event_index), ministry_role_id=ministry_role_id,
        ministry_id=3, required_count=1, role_is_active=True,
    )


def _candidate(membership_id: int, *, maximum: int | None = None,
               qualified=(LEAD,), available_events=range(0, 14)) -> CandidateInput:
    return CandidateInput(
        membership_id=membership_id, person_id=membership_id,
        display_name=f"Synthetic Volunteer {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(
            {700 + i: AvailabilityState.AVAILABLE for i in available_events}
        ),
        blocked_dates=frozenset(),
        max_assignments_in_period=maximum,
    )


def _input(requirements=(), candidates=(), existing=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
    )


def _loads(result) -> dict[int, int]:
    return dict(result.metrics.load_by_membership)


# ==========================================================================
# 11-12 -- the input carries the maximum
# ==========================================================================


def test_11_a_candidate_carries_its_configured_maximum():
    candidate = _candidate(118, maximum=4)
    assert candidate.max_assignments_in_period == 4
    # Four allowed, one already held -> three more.
    assert candidate.remaining_capacity(1) == 3
    assert candidate.remaining_capacity(4) == 0
    # Already over the maximum yields zero, never a negative allowance: the
    # solver can only decide how many *more* to add.
    assert candidate.remaining_capacity(6) == 0


def test_12_a_candidate_without_a_maximum_is_uncapped():
    candidate = _candidate(118)
    assert candidate.max_assignments_in_period is None
    assert candidate.remaining_capacity(0) is None
    assert candidate.remaining_capacity(99) is None


def test_12b_a_non_positive_maximum_is_refused_by_the_engine():
    with pytest.raises(SchedulingInputError, match="non-positive"):
        solve_schedule(
            _input(
                requirements=[_requirement(600, event_index=0)],
                candidates=[_candidate(118, maximum=0)],
            ),
            policy=LENIENT,
        )


# ==========================================================================
# 13-16 -- the solver respects it
# ==========================================================================


def test_13_the_solver_never_exceeds_a_configured_maximum():
    """Nine interchangeable Sundays; one capped volunteer, one uncapped."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(9)],
            candidates=[_candidate(118, maximum=2), _candidate(119)],
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 9
    assert _loads(result)[118] <= 2


def test_14_a_maximum_may_leave_a_position_unresolved():
    """Four positions, two volunteers, each capped at one.

    Best effort is preserved: two positions are filled and two come back
    unresolved rather than the run failing or the caps being bent.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118, maximum=1), _candidate(119, maximum=1)],
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 2
    assert result.unfilled_count == 2
    assert all(load <= 1 for load in _loads(result).values())


def test_14b_an_unresolved_position_says_the_candidates_are_at_their_limit():
    """The diagnostic must not blame availability.

    Everyone is available and qualified; they are simply out of allowance.
    Reporting ALL_UNAVAILABLE here would send a head to the wrong screen --
    and to the wrong conversation.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(4)],
            candidates=[_candidate(118, maximum=1), _candidate(119, maximum=1)],
        ),
        policy=LENIENT,
    )

    codes = {code for u in result.unfilled_requirements for code in u.diagnostic_codes}
    assert DIAGNOSTIC_ALL_AT_PERIOD_LIMIT in codes


def test_15_a_maximum_does_not_reduce_fill_when_others_can_serve():
    """Six positions; one volunteer capped at one, two others uncapped.

    The cap constrains who takes what, and must not cost a filled position.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[
                _candidate(118, maximum=1), _candidate(119), _candidate(120),
            ],
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 6
    assert _loads(result).get(118, 0) <= 1


def test_16_existing_assignments_count_toward_the_maximum():
    """118 already holds two of their maximum of three, so at most one more.

    Existing rows are fixed inputs: the engine counts them and never removes
    one to make room.
    """
    requirements = [_requirement(600 + i, event_index=i) for i in range(6)]
    existing = [
        ExistingAssignmentInput(
            assignment_id=900, requirement_id=600, membership_id=118, event_id=700
        ),
        ExistingAssignmentInput(
            assignment_id=901, requirement_id=601, membership_id=118, event_id=701
        ),
    ]
    result = solve_schedule(
        _input(requirements=requirements, candidates=[_candidate(118, maximum=3)],
               existing=existing),
        policy=LENIENT,
    )

    # Total load, existing included, never passes the maximum.
    assert _loads(result)[118] == 3
    # Exactly one new proposal: two were already held.
    assert len(result.proposed_assignments) == 1


def test_16b_a_candidate_already_over_their_maximum_receives_nothing_new():
    """A head may lower a maximum after assigning. Nothing is deleted, and no
    further work is added."""
    requirements = [_requirement(600 + i, event_index=i) for i in range(5)]
    existing = [
        ExistingAssignmentInput(
            assignment_id=900 + i, requirement_id=600 + i,
            membership_id=118, event_id=700 + i,
        )
        for i in range(3)
    ]
    result = solve_schedule(
        _input(requirements=requirements, candidates=[_candidate(118, maximum=2)],
               existing=existing),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()
    # The three existing rows are untouched -- the engine reports, never repairs.
    assert _loads(result)[118] == 3


# ==========================================================================
# 17-19 -- no soft preference may exceed it
# ==========================================================================


def test_17_a_target_cannot_push_a_candidate_over_their_maximum():
    """Target 5 against a maximum of 2: the hard rule wins, always.

    The target is a soft preference optimized after fill; the maximum is a
    constraint the model carries from the start, so there is no arithmetic in
    which the target could outbid it.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(8)],
            candidates=[_candidate(118, maximum=2), _candidate(119)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True, target_assignments_per_candidate=5
        ),
    )

    assert _loads(result)[118] <= 2
    assert result.filled_count == 8


def test_17b_load_balancing_cannot_push_a_candidate_over_their_maximum():
    """Balancing wants equal loads; the cap forbids it, and the cap wins."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(8)],
            candidates=[_candidate(118, maximum=1), _candidate(119)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True, balance_candidate_loads=True
        ),
    )

    assert _loads(result)[118] <= 1
    assert result.filled_count == 8
    # The other volunteer absorbs the imbalance rather than the cap bending.
    assert _loads(result)[119] == 7


def test_18_role_variety_cannot_push_a_candidate_over_their_maximum():
    requirements = [
        _requirement(600, event_index=0, ministry_role_id=SUPPORT_A),
        _requirement(601, event_index=1, ministry_role_id=SUPPORT_B),
        _requirement(602, event_index=2, ministry_role_id=SUPPORT_A),
        _requirement(603, event_index=3, ministry_role_id=SUPPORT_B),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(118, maximum=1, qualified=(SUPPORT_A, SUPPORT_B)),
                _candidate(119, qualified=(SUPPORT_A, SUPPORT_B)),
            ],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True,
            role_variety_role_ids=frozenset({SUPPORT_A, SUPPORT_B}),
        ),
    )

    assert _loads(result)[118] <= 1
    assert result.filled_count == 4


def test_19_no_target_with_balancing_on_still_respects_the_maximum():
    """AV's live configuration: no numeric target, balancing on, variety off."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(9)],
            candidates=[
                _candidate(118, maximum=2), _candidate(119), _candidate(120),
            ],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True,
            target_assignments_per_candidate=None,
            balance_candidate_loads=True,
        ),
    )

    assert result.filled_count == 9
    assert _loads(result)[118] <= 2


def test_19b_every_capped_candidate_is_respected_at_once():
    """Several different maxima in one run, all binding simultaneously."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(10)],
            candidates=[
                _candidate(118, maximum=1),
                _candidate(119, maximum=2),
                _candidate(120, maximum=3),
                _candidate(121),
            ],
        ),
        policy=LENIENT,
    )

    loads = _loads(result)
    assert loads.get(118, 0) <= 1
    assert loads.get(119, 0) <= 2
    assert loads.get(120, 0) <= 3
    assert result.filled_count == 10


def test_19c_the_cap_is_a_period_total_not_a_per_event_rule():
    """Ten separate Sundays and a maximum of three: the cap counts the whole
    period, so seven positions go unfilled rather than the cap applying per
    event."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(10)],
            candidates=[_candidate(118, maximum=3)],
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 3
    assert result.unfilled_count == 7
