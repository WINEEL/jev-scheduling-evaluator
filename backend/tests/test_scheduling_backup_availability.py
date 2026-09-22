"""The generic BACKUP availability tier in the pure solver (Task 52).

Hand-written inputs, the real CP-SAT solver, no database.

BACKUP is a **feasible** availability answer, not a hard blocker: it gets a
placement variable exactly like AVAILABLE. What distinguishes it is a soft
preference -- ranked directly below maximum fill and above every
policy-gated preference (target excess, load balancing, role variety) -- that
minimizes how many new placements draw on it. A position must never be left
unfilled merely to avoid a BACKUP placement.

Every person is synthetic.
"""

from __future__ import annotations

import datetime
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
    DIAGNOSTIC_ALL_UNAVAILABLE,
    DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES,
    DIAGNOSTIC_NO_RESPONSE_DISALLOWED,
    DIAGNOSTIC_SAME_EVENT_CONTENTION,
    ProposedAssignment,
    SchedulingResult,
)
from app.scheduling.solver import SchedulingPolicy, _availability_allows, solve_schedule

NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)

STRICT = SchedulingPolicy(allow_no_response=False)
LENIENT = SchedulingPolicy(allow_no_response=True)

LEAD = 12


def _requirement(
    requirement_id: int, *, event_id: int = 700, event_date: datetime.date = NOV_15,
    ministry_role_id: int = LEAD, required_count: int = 1,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count,
    )


def _candidate(
    membership_id: int, *, qualified=(LEAD,), availability=None,
    max_assignments_in_period: int | None = None,
) -> CandidateInput:
    return CandidateInput(
        membership_id=membership_id, person_id=membership_id,
        display_name=f"Member {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(dict(availability or {})),
        max_assignments_in_period=max_assignments_in_period,
    )


def _input(requirements=(), candidates=(), existing=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
    )


def _backup(*event_ids):
    return {event_id: AvailabilityState.BACKUP for event_id in event_ids}


def _available(*event_ids):
    return {event_id: AvailabilityState.AVAILABLE for event_id in event_ids}


def _placed(result: SchedulingResult) -> set[tuple[int, int]]:
    return {(p.requirement_id, p.membership_id) for p in result.proposed_assignments}


# --------------------------------------------------------------------------
# Feasibility: BACKUP is never a hard blocker
# --------------------------------------------------------------------------


def test_a_backup_only_candidate_fills_the_position():
    """Nobody else exists; the position is filled anyway. BACKUP is feasible,
    not a fallback to be avoided at the cost of leaving a slot empty.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_backup(700))],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118)}
    assert result.is_complete is True
    assert result.metrics.backup_placement_total == 1


def test_backup_is_not_gated_by_allow_no_response():
    """Unlike NO_RESPONSE, BACKUP is usable even under the strict policy --
    it is an explicit answer, not silence.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_backup(700))],
        ),
        policy=SchedulingPolicy(allow_no_response=False),
    )

    assert _placed(result) == {(600, 118)}


def test_availability_allows_treats_backup_like_available():
    assert _availability_allows(AvailabilityState.BACKUP, STRICT) is True
    assert _availability_allows(AvailabilityState.BACKUP, LENIENT) is True


def test_availability_allows_is_exhaustive_and_raises_for_an_unhandled_state():
    """Task 51 flagged the pre-Task-52 version of this function as an
    implicit-else silent-fallthrough hazard. It must now fail loudly for
    anything that is not one of the four known members, rather than quietly
    treating it as NO_RESPONSE.
    """

    class _NotARealAvailabilityState:
        """A stand-in for a hypothetical fifth member that was never added
        to the real enum -- exercised by identity comparison alone, so no
        change to the real enum is needed to prove the raise path works.
        """

    with pytest.raises(AssertionError):
        _availability_allows(_NotARealAvailabilityState(), STRICT)


# --------------------------------------------------------------------------
# Preference: AVAILABLE is used over BACKUP when both could fill the position
# --------------------------------------------------------------------------


def test_an_available_candidate_is_preferred_over_a_backup_candidate():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[
                _candidate(118, availability=_backup(700)),
                _candidate(119, availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 119)}
    assert result.metrics.backup_placement_total == 0


def test_backup_avoidance_never_costs_a_filled_position():
    """Two positions, one AVAILABLE candidate and one BACKUP candidate: both
    are filled. Backup avoidance is ranked below fill, so it can never leave
    a position open to dodge the one BACKUP placement it has no way around.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[
                _candidate(118, availability=_backup(700)),
                _candidate(119, availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118), (600, 119)}
    assert result.is_complete is True
    assert result.metrics.backup_placement_total == 1


def test_backup_avoidance_counts_placements_not_distinct_people():
    """One BACKUP candidate fills two different events' positions -- allowed,
    since only *one event* per person is restricted, not one person per
    period. If the objective counted distinct backup-drawing people it would
    read 1; it must read 2, because it is counting *placements*.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700),
                _requirement(601, event_id=701),
            ],
            candidates=[_candidate(118, availability=_backup(700) | _backup(701))],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118), (601, 118)}
    assert result.is_complete is True
    assert result.metrics.backup_placement_total == 2


def test_backup_avoidance_outranks_target_excess():
    """119 already carries one assignment at a target of one; 118 carries
    none. Minimizing target excess *alone* would favor placing the BACKUP
    candidate (118, cost 0) over the AVAILABLE one (119, cost 1) -- but
    backup avoidance runs first and fixes zero BACKUP placements as a
    constraint before target excess is ever considered, so 119 is chosen
    despite the higher resulting excess.
    """
    policy = SchedulingPolicy(allow_no_response=False, target_assignments_per_candidate=1)
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(599, event_id=701),
                _requirement(600, event_id=702),
            ],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=1, requirement_id=599, membership_id=119, event_id=701,
                ),
            ],
            candidates=[
                _candidate(118, availability=_backup(702)),
                _candidate(119, availability=_available(701) | _available(702)),
            ],
        ),
        policy=policy,
    )

    assert _placed(result) == {(600, 119)}
    assert result.metrics.backup_placement_total == 0
    assert result.metrics.target_excess_total == 1


def test_backup_avoidance_outranks_load_balancing():
    """A perfectly even split would use the BACKUP candidate for one of two
    positions; backup avoidance outranks balancing, so the AVAILABLE
    candidate takes both instead, leaving loads uneven.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700),
                _requirement(601, event_id=701),
            ],
            candidates=[
                _candidate(118, availability=_backup(700) | _backup(701)),
                _candidate(119, availability=_available(700) | _available(701)),
            ],
        ),
        policy=SchedulingPolicy(allow_no_response=False, balance_candidate_loads=True),
    )

    assert _placed(result) == {(600, 119), (601, 119)}
    assert result.metrics.backup_placement_total == 0


# --------------------------------------------------------------------------
# Existing assignments are untouched by tier, whatever the candidate answers
# --------------------------------------------------------------------------


def test_an_existing_assignment_is_unaffected_by_a_backup_answer():
    """The candidate's current BACKUP answer is not re-evaluated against a
    fixed decision: it consumes capacity and blocks the event exactly as an
    AVAILABLE-backed existing assignment would, and is never counted toward
    backup_placement_total (that metric is about new placements only).
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=1)],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=1, requirement_id=600, membership_id=118, event_id=700,
                ),
            ],
            candidates=[_candidate(118, availability=_backup(700))],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert result.is_complete is True
    assert result.metrics.backup_placement_total == 0


# --------------------------------------------------------------------------
# Diagnostics: a BACKUP-only pool must not be misreported
# --------------------------------------------------------------------------


def test_a_backup_only_shortfall_is_reported_as_contention_not_availability():
    """One BACKUP candidate, two required positions: the one position that
    cannot be filled is a contention shortfall (only one candidate exists at
    all), not an availability problem. Before Task 52's diagnostic fix, the
    ``not any(AVAILABLE)`` check would have misreported this as
    NO_RESPONSE_DISALLOWED even though the candidate did answer, and
    answered usably.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[_candidate(118, availability=_backup(700))],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1
    assert result.unfilled_requirements[0].missing_count == 1
    codes = result.unfilled_requirements[0].diagnostic_codes
    assert DIAGNOSTIC_NO_RESPONSE_DISALLOWED not in codes
    assert DIAGNOSTIC_ALL_UNAVAILABLE not in codes
    assert DIAGNOSTIC_SAME_EVENT_CONTENTION in codes or (
        DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES in codes
    )


def test_a_mixed_unavailable_and_disallowed_no_response_pool_still_reports_correctly():
    """Unchanged pre-existing behavior, pinned alongside the BACKUP fix: a
    pool of UNAVAILABLE and disallowed NO_RESPONSE (no AVAILABLE, no BACKUP)
    still reports NO_RESPONSE_DISALLOWED, exactly as before.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[
                _candidate(118, availability={700: AvailabilityState.UNAVAILABLE}),
                _candidate(119, availability={}),  # NO_RESPONSE
            ],
        ),
        policy=STRICT,
    )

    assert result.unfilled_requirements[0].diagnostic_codes == (
        DIAGNOSTIC_NO_RESPONSE_DISALLOWED,
    )


# --------------------------------------------------------------------------
# The generic name is not Kids-specific
# --------------------------------------------------------------------------


def test_the_stored_state_name_is_generic_not_ministry_specific():
    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    for smell in ("IF_NEED_BE", "IfNeedBe", "Kids"):
        assert smell not in source
