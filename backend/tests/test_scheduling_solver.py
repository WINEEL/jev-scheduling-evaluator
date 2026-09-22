"""The CP-SAT scheduling engine (Task 31).

Every input here is written by hand: no database, no Session, no fixtures from
the ORM layer. That is the point of the package, and it is also what makes
these tests the primary verification for this task.

The real optimizer runs in every test -- nothing about CP-SAT is mocked.
Assertions are about *constraints and counts* rather than which particular
person got picked, except where exactly one optimum exists; inventing a
tie-break to make a test convenient would be inventing a product rule.
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
    DIAGNOSTIC_ALL_CHURCH_CONFLICTED,
    DIAGNOSTIC_ALL_UNAVAILABLE,
    DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES,
    DIAGNOSTIC_NO_QUALIFIED_CANDIDATES,
    DIAGNOSTIC_NO_RESPONSE_DISALLOWED,
    DIAGNOSTIC_ROLE_INACTIVE,
    DIAGNOSTIC_SAME_EVENT_CONTENTION,
    ProposedAssignment,
    SchedulingResult,
    UnfilledRequirement,
)
from app.scheduling.solver import (
    SchedulingEngineError,
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)

STRICT = SchedulingPolicy(allow_no_response=False)
LENIENT = SchedulingPolicy(allow_no_response=True)

LEAD = 12
ASSIST = 13


def _requirement(
    requirement_id: int, *, event_id: int = 700, event_date: datetime.date = NOV_15,
    ministry_role_id: int = LEAD, required_count: int = 1, role_is_active: bool = True,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count, role_is_active=role_is_active,
    )


def _candidate(
    membership_id: int, *, person_id: int | None = None, name: str | None = None,
    qualified=(LEAD,), availability=None, blocked=(),
) -> CandidateInput:
    return CandidateInput(
        membership_id=membership_id,
        person_id=person_id if person_id is not None else membership_id,
        display_name=name or f"Member {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(dict(availability or {})),
        blocked_dates=frozenset(blocked),
    )


def _input(requirements=(), candidates=(), existing=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
    )


def _available(*event_ids):
    return {event_id: AvailabilityState.AVAILABLE for event_id in event_ids}


def _unavailable(*event_ids):
    return {event_id: AvailabilityState.UNAVAILABLE for event_id in event_ids}


def _placed(result: SchedulingResult) -> set[tuple[int, int]]:
    return {(p.requirement_id, p.membership_id) for p in result.proposed_assignments}


# --------------------------------------------------------------------------
# 1-4 -- Basics
# --------------------------------------------------------------------------


def test_01_one_requirement_and_one_eligible_candidate_is_assigned():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == (
        ProposedAssignment(requirement_id=600, membership_id=118, event_id=700),
    )
    assert result.is_complete is True
    assert result.filled_count == 1
    assert result.unfilled_count == 0


def test_02_required_count_two_with_two_candidates_fills_both():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[
                _candidate(118, availability=_available(700)),
                _candidate(119, availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118), (600, 119)}
    assert result.is_complete is True


def test_03_an_impossible_full_schedule_returns_a_partial_result_not_an_exception():
    """Approved behavior: best effort plus explicit unresolved slots."""
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=3)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1
    assert result.is_complete is False
    assert result.unfilled_requirements[0].requirement_id == 600
    assert result.unfilled_requirements[0].missing_count == 2
    assert result.unfilled_count == 2


def test_04_zero_requirements_is_a_complete_empty_result():
    result = solve_schedule(_input(candidates=[_candidate(118)]), policy=STRICT)

    assert result.proposed_assignments == ()
    assert result.unfilled_requirements == ()
    assert result.is_complete is True


# --------------------------------------------------------------------------
# 5-7 -- Qualification and role activity
# --------------------------------------------------------------------------


def test_05_06_only_qualified_candidates_are_proposed():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, ministry_role_id=LEAD)],
            candidates=[
                _candidate(118, qualified=(ASSIST,), availability=_available(700)),
                _candidate(119, qualified=(LEAD,), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 119)}


def test_06b_nobody_qualified_leaves_the_requirement_unfilled():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, qualified=(), availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert result.unfilled_requirements[0].missing_count == 1


def test_07_an_inactive_role_produces_no_proposal():
    """Automatic work never places anyone into a deactivated role -- Task 22
    treats that as an overridable blocker, and this engine overrides nothing.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, role_is_active=False)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert DIAGNOSTIC_ROLE_INACTIVE in result.unfilled_requirements[0].diagnostic_codes


# --------------------------------------------------------------------------
# 8-12 -- Availability and the no-response policy
# --------------------------------------------------------------------------


def test_08_09_available_is_proposed_and_unavailable_never_is():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[
                _candidate(118, availability=_available(700)),
                _candidate(119, availability=_unavailable(700)),
            ],
        ),
        policy=LENIENT,  # even the permissive policy must not take a "no"
    )

    assert _placed(result) == {(600, 118)}
    assert result.unfilled_requirements[0].missing_count == 1


@pytest.mark.parametrize(
    "policy,expected", [(LENIENT, {(600, 118)}), (STRICT, set())]
)
def test_10_11_no_response_follows_the_run_policy(policy, expected):
    """The same input, two ministries' rules, two different answers -- which is
    exactly why this is a policy value and not a hard-coded constant.
    """
    scheduling_input = _input(
        requirements=[_requirement(600)],
        candidates=[_candidate(118, availability={})],  # never answered
    )

    result = solve_schedule(scheduling_input, policy=policy)

    assert _placed(result) == expected


def test_12_absence_stays_no_response_and_the_input_is_not_mutated():
    """The engine reads the tri-state; it never rewrites silence into a yes."""
    candidate = _candidate(118, availability={})
    scheduling_input = _input(requirements=[_requirement(600)], candidates=[candidate])
    assert candidate.availability_for(700) is AvailabilityState.NO_RESPONSE

    solve_schedule(scheduling_input, policy=LENIENT)

    assert candidate.availability_for(700) is AvailabilityState.NO_RESPONSE
    assert candidate.availability_by_event == {}


def test_12b_a_mixed_roster_respects_each_state_separately():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=3)],
            candidates=[
                _candidate(118, availability=_available(700)),
                _candidate(119, availability=_unavailable(700)),
                _candidate(120, availability={}),
            ],
        ),
        policy=LENIENT,
    )

    assert _placed(result) == {(600, 118), (600, 120)}
    assert result.unfilled_requirements[0].missing_count == 1


# --------------------------------------------------------------------------
# 13-15 -- Church-wide conflicts
# --------------------------------------------------------------------------


def test_13_a_blocked_date_prevents_a_proposal():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, event_date=NOV_15)],
            candidates=[
                _candidate(118, availability=_available(700), blocked=[NOV_15]),
            ],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert DIAGNOSTIC_ALL_CHURCH_CONFLICTED in result.unfilled_requirements[0].diagnostic_codes


def test_14_a_candidate_blocked_on_another_date_remains_usable():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, event_date=NOV_15)],
            candidates=[
                _candidate(118, availability=_available(700), blocked=[NOV_22]),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118)}


def test_15_the_solver_contains_no_conflict_or_sql_logic():
    """ADR 0002/0003 resolution happens once, upstream, and arrives here as a
    set of dates.
    """
    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    assert "get_person_sunday_conflicts" not in source
    assert "DISTINCT ON" not in source
    assert "select(" not in source
    assert not hasattr(module, "get_person_sunday_conflicts")


# --------------------------------------------------------------------------
# 16-21 -- Existing assignments are fixed
# --------------------------------------------------------------------------


def test_16_17_an_existing_assignment_consumes_a_slot_and_is_not_re_emitted():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[
                _candidate(118, availability=_available(700)),
                _candidate(119, availability=_available(700)),
            ],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118,
                    event_id=700,
                ),
            ],
        ),
        policy=STRICT,
    )

    # One slot left, filled by the other candidate; 118 is not proposed again.
    assert _placed(result) == {(600, 119)}
    assert result.is_complete is True
    assert result.filled_count == 1  # new work only


def test_18_a_fully_filled_requirement_gets_no_new_proposal():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=1)],
            candidates=[
                _candidate(118, availability=_available(700)),
                _candidate(119, availability=_available(700)),
            ],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118,
                    event_id=700,
                ),
            ],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert result.is_complete is True


def test_19_an_overfilled_requirement_adds_nothing_and_does_not_crash():
    """A historical capacity override left three people in a two-person
    position. Validating that is Task 26's job; here it simply means no room.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=2)],
            candidates=[_candidate(i, availability=_available(700)) for i in (118, 119, 120, 121)],
            existing=[
                ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                ExistingAssignmentInput(assignment_id=901, requirement_id=600, membership_id=119, event_id=700),
                ExistingAssignmentInput(assignment_id=902, requirement_id=600, membership_id=120, event_id=700),
            ],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert result.unfilled_requirements == ()
    assert result.is_complete is True


def test_20_an_existing_same_event_assignment_blocks_another_role_in_that_event():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700)),
            ],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118,
                    event_id=700,
                ),
            ],
        ),
        policy=STRICT,
    )

    assert result.proposed_assignments == ()
    assert [u.requirement_id for u in result.unfilled_requirements] == [601]


def test_21_an_existing_assignment_in_another_event_on_the_same_date_does_not_block():
    """One *ministry* per Sunday, not one event -- so serving two of this
    ministry's events on one date is allowed (ADR 0002).
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, event_date=NOV_15),
                _requirement(601, event_id=701, event_date=NOV_15),
            ],
            candidates=[
                _candidate(118, availability=_available(700, 701)),
            ],
            existing=[
                ExistingAssignmentInput(
                    assignment_id=900, requirement_id=600, membership_id=118,
                    event_id=700,
                ),
            ],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(601, 118)}
    assert result.is_complete is True


# --------------------------------------------------------------------------
# 22-24 -- The same-event rule
# --------------------------------------------------------------------------


def test_22_one_candidate_cannot_fill_two_requirements_in_one_event():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1  # one of the two, never both
    assert result.unfilled_count == 1


def test_23_two_candidates_fill_two_roles_in_one_event():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700)),
                _candidate(119, qualified=(LEAD, ASSIST), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert result.is_complete is True
    assert {p.requirement_id for p in result.proposed_assignments} == {600, 601}
    assert len({p.membership_id for p in result.proposed_assignments}) == 2


def test_24_one_candidate_may_serve_two_events_on_the_same_date():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, event_date=NOV_15),
                _requirement(601, event_id=701, event_date=NOV_15),
            ],
            candidates=[_candidate(118, availability=_available(700, 701))],
        ),
        policy=STRICT,
    )

    assert _placed(result) == {(600, 118), (601, 118)}
    assert result.is_complete is True


# --------------------------------------------------------------------------
# 25-30 -- Objective, capacity and determinism
# --------------------------------------------------------------------------


def test_25_the_objective_maximizes_filled_positions():
    """A greedy engine that spent its only dual-qualified member on the role
    nobody else can cover would fill one position; the optimum fills two.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700)),
                _candidate(119, qualified=(LEAD,), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 2
    assert _placed(result) == {(600, 119), (601, 118)}  # the only optimum
    assert result.is_complete is True


def test_26_competing_requirements_yield_the_best_partial_solution():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
                _requirement(602, event_id=700, ministry_role_id=ASSIST + 1),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST, ASSIST + 1), availability=_available(700)),
                _candidate(119, qualified=(LEAD, ASSIST, ASSIST + 1), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 2  # two people, three positions, one event
    assert result.unfilled_count == 1


def test_27_missing_counts_are_reported_correctly():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, required_count=3, event_id=700),
                _requirement(601, required_count=2, event_id=701, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700, 701)),
            ],
        ),
        policy=STRICT,
    )

    missing = {u.requirement_id: u.missing_count for u in result.unfilled_requirements}
    assert missing == {600: 2, 601: 1}
    assert result.unfilled_count == 3
    assert result.filled_count == 2


def test_28_identical_input_yields_an_identical_result():
    """Single worker, fixed seed, no wall-clock limit -- so the same input
    produces the same schedule every time, which a roster people rely on must.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_id=700, ministry_role_id=LEAD),
            _requirement(601, event_id=700, ministry_role_id=ASSIST),
            _requirement(602, event_id=701, event_date=NOV_22, ministry_role_id=LEAD),
        ],
        candidates=[
            _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700, 701)),
            _candidate(119, qualified=(LEAD, ASSIST), availability=_available(700, 701)),
            _candidate(120, qualified=(LEAD,), availability=_available(700, 701)),
        ],
    )

    results = [solve_schedule(scheduling_input, policy=STRICT) for _ in range(5)]

    assert all(r == results[0] for r in results)
    assert all(
        r.proposed_assignments == results[0].proposed_assignments for r in results
    )


def test_29_never_proposes_beyond_required_count():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=1)],
            candidates=[_candidate(i, availability=_available(700)) for i in (118, 119, 120)],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1


def test_30_capacity_accounts_for_existing_occupancy():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, required_count=3)],
            candidates=[_candidate(i, availability=_available(700)) for i in (118, 119, 120, 121)],
            existing=[
                ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                ExistingAssignmentInput(assignment_id=901, requirement_id=600, membership_id=119, event_id=700),
            ],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1  # one slot remained
    assert result.is_complete is True
    assert 118 not in {p.membership_id for p in result.proposed_assignments}


def test_results_are_sorted_deterministically():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(602, event_id=702, event_date=NOV_22, required_count=2),
                _requirement(600, event_id=700, required_count=2),
            ],
            candidates=[
                _candidate(120, availability=_available(700, 702)),
                _candidate(118, availability=_available(700, 702)),
            ],
        ),
        policy=STRICT,
    )

    keys = [(p.requirement_id, p.membership_id) for p in result.proposed_assignments]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------
# 31-36 -- Diagnostics
# --------------------------------------------------------------------------


def test_31_role_inactive_diagnostic():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, role_is_active=False)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert DIAGNOSTIC_ROLE_INACTIVE in result.unfilled_requirements[0].diagnostic_codes


def test_32_no_qualified_candidates_diagnostic():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, ministry_role_id=LEAD)],
            candidates=[_candidate(118, qualified=(ASSIST,), availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.unfilled_requirements[0].diagnostic_codes == (
        DIAGNOSTIC_NO_QUALIFIED_CANDIDATES,
    )


def test_33_all_unavailable_diagnostic():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_unavailable(700))],
        ),
        policy=LENIENT,
    )

    assert DIAGNOSTIC_ALL_UNAVAILABLE in result.unfilled_requirements[0].diagnostic_codes


def test_33b_no_response_disallowed_diagnostic():
    """Distinguished from ALL_UNAVAILABLE on purpose: one says people declined,
    the other says the run's own policy refused their silence.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability={})],
        ),
        policy=STRICT,
    )

    codes = result.unfilled_requirements[0].diagnostic_codes
    assert DIAGNOSTIC_NO_RESPONSE_DISALLOWED in codes
    assert DIAGNOSTIC_ALL_UNAVAILABLE not in codes


def test_34_church_conflict_diagnostic():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, event_date=NOV_15)],
            candidates=[_candidate(118, availability=_available(700), blocked=[NOV_15])],
        ),
        policy=STRICT,
    )

    assert result.unfilled_requirements[0].diagnostic_codes == (
        DIAGNOSTIC_ALL_CHURCH_CONFLICTED,
    )


def test_35_contention_diagnostic_when_candidates_exist_but_compete():
    """Nobody is individually ineligible -- there simply are not enough people
    for one event, so the code names competition rather than a false cause.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_id=700, ministry_role_id=LEAD),
                _requirement(601, event_id=700, ministry_role_id=ASSIST),
            ],
            candidates=[
                _candidate(118, qualified=(LEAD, ASSIST), availability=_available(700)),
            ],
        ),
        policy=STRICT,
    )

    unfilled = result.unfilled_requirements[0]
    assert DIAGNOSTIC_SAME_EVENT_CONTENTION in unfilled.diagnostic_codes
    assert DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES in unfilled.diagnostic_codes
    assert DIAGNOSTIC_NO_QUALIFIED_CANDIDATES not in unfilled.diagnostic_codes


def test_35b_several_true_causes_are_all_reported():
    """No guessing at a single cause: the role is inactive *and* nobody is
    qualified, and claiming just one would be misleading.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, ministry_role_id=LEAD, role_is_active=False)],
            candidates=[_candidate(118, qualified=(ASSIST,), availability=_available(700))],
        ),
        policy=STRICT,
    )

    codes = result.unfilled_requirements[0].diagnostic_codes
    assert DIAGNOSTIC_ROLE_INACTIVE in codes
    assert DIAGNOSTIC_NO_QUALIFIED_CANDIDATES in codes


def test_36_diagnostic_codes_and_unfilled_rows_are_ordered_deterministically():
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(602, event_id=702, ministry_role_id=LEAD, role_is_active=False),
                _requirement(600, event_id=700, ministry_role_id=ASSIST + 5),
            ],
            candidates=[_candidate(118, availability=_available(700, 702))],
        ),
        policy=STRICT,
    )

    assert [u.requirement_id for u in result.unfilled_requirements] == [600, 602]
    for unfilled in result.unfilled_requirements:
        assert list(unfilled.diagnostic_codes) == sorted(unfilled.diagnostic_codes)


def test_a_filled_requirement_carries_no_diagnostics():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.unfilled_requirements == ()


# --------------------------------------------------------------------------
# 37-42 -- Input validation
# --------------------------------------------------------------------------


def test_37_duplicate_requirement_ids_are_rejected():
    with pytest.raises(SchedulingInputError, match="requirement ids must be unique"):
        solve_schedule(
            _input(requirements=[_requirement(600), _requirement(600, event_id=701)]),
            policy=STRICT,
        )


def test_38_duplicate_candidate_membership_ids_are_rejected():
    with pytest.raises(SchedulingInputError, match="membership ids must be unique"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)],
                candidates=[_candidate(118), _candidate(118, person_id=43)],
            ),
            policy=STRICT,
        )


def test_39_an_existing_assignment_for_an_unknown_requirement_is_rejected():
    with pytest.raises(SchedulingInputError, match="unknown requirement"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)],
                candidates=[_candidate(118)],
                existing=[
                    ExistingAssignmentInput(
                        assignment_id=900, requirement_id=999, membership_id=118,
                        event_id=700,
                    ),
                ],
            ),
            policy=STRICT,
        )


def test_40_an_existing_assignment_for_an_unknown_candidate_is_rejected():
    with pytest.raises(SchedulingInputError, match="unknown candidate"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)],
                candidates=[_candidate(118)],
                existing=[
                    ExistingAssignmentInput(
                        assignment_id=900, requirement_id=600, membership_id=999,
                        event_id=700,
                    ),
                ],
            ),
            policy=STRICT,
        )


def test_41_an_existing_assignment_event_mismatch_is_rejected():
    with pytest.raises(SchedulingInputError, match="claims event"):
        solve_schedule(
            _input(
                requirements=[_requirement(600, event_id=700)],
                candidates=[_candidate(118)],
                existing=[
                    ExistingAssignmentInput(
                        assignment_id=900, requirement_id=600, membership_id=118,
                        event_id=701,
                    ),
                ],
            ),
            policy=STRICT,
        )


def test_42_the_same_membership_twice_in_one_event_is_rejected():
    """Impossible state the database prevents; modelling around it would mean
    accepting a schedule that cannot exist.
    """
    with pytest.raises(SchedulingInputError, match="more than once in event"):
        solve_schedule(
            _input(
                requirements=[
                    _requirement(600, event_id=700, ministry_role_id=LEAD),
                    _requirement(601, event_id=700, ministry_role_id=ASSIST),
                ],
                candidates=[_candidate(118, qualified=(LEAD, ASSIST))],
                existing=[
                    ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                    ExistingAssignmentInput(assignment_id=901, requirement_id=601, membership_id=118, event_id=700),
                ],
            ),
            policy=STRICT,
        )


def test_42b_duplicate_existing_assignment_ids_are_rejected():
    with pytest.raises(SchedulingInputError, match="assignment ids must be unique"):
        solve_schedule(
            _input(
                requirements=[_requirement(600, required_count=2)],
                candidates=[_candidate(118), _candidate(119)],
                existing=[
                    ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=118, event_id=700),
                    ExistingAssignmentInput(assignment_id=900, requirement_id=600, membership_id=119, event_id=700),
                ],
            ),
            policy=STRICT,
        )


@pytest.mark.parametrize("count", [0, -1])
def test_a_non_positive_required_count_is_rejected(count):
    with pytest.raises(SchedulingInputError, match="non-positive required_count"):
        solve_schedule(
            _input(requirements=[_requirement(600, required_count=count)]),
            policy=STRICT,
        )


def test_an_unexpected_cp_sat_status_raises_rather_than_returning_an_empty_schedule(
    monkeypatch,
):
    """The one place the real optimizer is stood aside, and only for its
    status code.

    Ordinary scheduling data is always feasible -- the all-empty assignment
    satisfies every constraint -- so this branch cannot be reached with honest
    input. Forcing the status is the only way to prove that a MODEL_INVALID
    run is not mistaken for "nobody could be scheduled", which would silently
    publish an empty roster. Every other test in this module runs CP-SAT for
    real.
    """
    from ortools.sat.python import cp_model

    monkeypatch.setattr(cp_model.CpSolver, "Solve", lambda self, model: cp_model.MODEL_INVALID)

    with pytest.raises(SchedulingEngineError, match="MODEL_INVALID"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)],
                candidates=[_candidate(118, availability=_available(700))],
            ),
            policy=STRICT,
        )


def test_a_genuinely_feasible_model_reports_optimal_and_is_not_an_error():
    """The complement of the test above: the normal path really does come back
    with a status this engine accepts.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)],
            candidates=[_candidate(118, availability=_available(700))],
        ),
        policy=STRICT,
    )

    assert result.filled_count == 1


def test_the_input_error_is_a_plain_value_error():
    """A plain ValueError subclass: importing the service layer's
    InvalidOperationError would break this package's independence.
    """
    assert issubclass(SchedulingInputError, ValueError)
    assert issubclass(SchedulingEngineError, RuntimeError)


# --------------------------------------------------------------------------
# 43-48 -- Purity and read-only behavior
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ["app.scheduling.solver", "app.scheduling.result", "app.scheduling.input"],
)
def test_43_44_45_the_scheduling_modules_import_no_database_machinery(module_name):
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


def test_43b_importing_the_package_loads_no_database_machinery():
    """Checked in a fresh interpreter so this suite's own imports cannot mask
    a leak.
    """
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


def test_46_the_public_api_takes_no_session():
    import inspect

    params = inspect.signature(solve_schedule).parameters

    assert list(params) == ["scheduling_input", "policy"]
    assert "session" not in params
    assert "db" not in params


def test_47_the_result_contains_no_orm_objects():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600), _requirement(601, event_id=701, ministry_role_id=ASSIST)],
            candidates=[_candidate(118, qualified=(LEAD,), availability=_available(700))],
        ),
        policy=STRICT,
    )

    for value in (result, *result.proposed_assignments, *result.unfilled_requirements):
        module_name = type(value).__module__
        assert not module_name.startswith("app.models")
        assert not module_name.startswith("sqlalchemy")
    for proposal in result.proposed_assignments:
        for field_name in proposal.__dataclass_fields__:
            assert isinstance(getattr(proposal, field_name), int)


def test_48_solving_does_not_mutate_the_input():
    scheduling_input = _input(
        requirements=[_requirement(600, required_count=2)],
        candidates=[
            _candidate(118, availability=_available(700)),
            _candidate(119, availability=_unavailable(700)),
        ],
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

    solve_schedule(scheduling_input, policy=STRICT)
    solve_schedule(scheduling_input, policy=LENIENT)

    assert (
        scheduling_input.requirements,
        scheduling_input.candidates,
        scheduling_input.existing_assignments,
    ) == before
    assert scheduling_input.candidates[0].availability_by_event == {
        700: AvailabilityState.AVAILABLE
    }


def test_the_policy_is_generic_and_not_named_for_any_ministry():
    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    assert "SetupPolicy" not in source
    # Generic ministry policy: an availability rule, a target, a balance
    # switch and a variety set -- none named for, nor defaulted to, any one
    # ministry's number.
    assert set(SchedulingPolicy.__dataclass_fields__) == {
        "allow_no_response", "target_assignments_per_candidate",
        "balance_candidate_loads", "role_variety_role_ids",
    }
    assert SchedulingPolicy(allow_no_response=True).target_assignments_per_candidate is None
    assert SchedulingPolicy(allow_no_response=True).role_variety_role_ids is None
    with pytest.raises(Exception):
        SchedulingPolicy(allow_no_response=True).allow_no_response = False
    with pytest.raises(Exception):
        SchedulingPolicy(allow_no_response=True).target_assignments_per_candidate = 3


def test_the_objectives_are_exactly_fill_then_the_soft_passes():
    """Maximum fill first, then the lexicographic soft passes -- and no
    weighted-sum objective anywhere.

    Task 31 asserted a single ``Maximize``; Task 32 added target-excess and
    load-balance minimizations, and Task 33 a role-variety one. Task 52 added
    a backup-avoidance minimization directly after fill. All are ordered
    rather than weighted. Repeat avoidance and lead balancing still arrive as
    their own deliberate tasks.
    """
    import ast

    import app.scheduling.solver as module

    # Checked against the actual calls, not by grepping prose: the docstring
    # legitimately describes the objectives and what is deliberately absent.
    tree = ast.parse(Path(module.__file__).read_text())
    objective_calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Maximize", "Minimize", "AddWeightedSum"}
    ]
    # One fill maximization, then backup-avoidance, target-excess,
    # load-balance and role-variety minimizations.
    assert objective_calls == ["Maximize", "Minimize", "Minimize", "Minimize", "Minimize"]
    assert "AddWeightedSum" not in objective_calls
