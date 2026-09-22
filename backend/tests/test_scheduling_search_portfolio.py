"""Naming a search strategy changes the search, never the answer (Task 68).

Pinning CP-SAT to its symmetry-aware linear-relaxation strategy turned a
seventeen-minute balance pass on a real thirteen-Sunday quarter into a quarter
of a second. That is only legitimate if it is a *search* change: every pass
must still run to proven optimality and reach the same optima CP-SAT's
general-purpose default reached.

So these tests solve the same input twice -- once as the engine ships, once
with the strategy cleared so CP-SAT falls back to the default it used before
Task 68 -- and require the two runs to agree on every optimized quantity: fill,
unfilled count, BACKUP placements, target excess, fairness cost, role-variety
cost, and the shape of the load distribution.

**They deliberately do not compare person-for-person schedules.** Where several
schedules are equally optimal, which one comes back is not a product rule, and
a different strategy may legitimately reach a different one first. Asserting
the roster would be asserting an accident. What must never differ is the score.

The one thing that *is* asserted person-for-person is repeatability: the same
input must return the same schedule every time, because a ministry regenerating
a draft must not watch the names shuffle.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from app.scheduling import solver as solver_module
from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.solver import (
    SchedulingEngineError,
    SchedulingPolicy,
    solve_schedule,
)

from tests.scheduling_scale_fixture import build_scale_input

FIRST = datetime.date(2030, 1, 6)
MINISTRY = 1


def _sunday(index: int) -> datetime.date:
    return FIRST + datetime.timedelta(days=7 * index)


def _scores(result) -> tuple:
    """Everything the engine claims to have optimized, in priority order."""
    return (
        len(result.proposed_assignments),
        sum(u.missing_count for u in result.unfilled_requirements),
        result.metrics.backup_placement_total,
        result.metrics.target_excess_total,
        result.metrics.fairness_cost,
        result.metrics.role_variety_cost,
        sorted(result.metrics.load_by_membership.values()),
    )


class _DefaultStrategySolver(solver_module.cp_model.CpSolver):
    """A solver that ignores the named strategy, as the engine used to.

    Clearing ``subsolvers`` is what restores CP-SAT's general-purpose default,
    which is the behaviour every optimum below is checked against.
    """

    def Solve(self, model, *args, **kwargs):  # noqa: N802 - CP-SAT's spelling
        self.parameters.subsolvers.clear()
        return super().Solve(model, *args, **kwargs)


def _solve_with_default_strategy(scheduling_input, policy):
    original = solver_module.cp_model.CpSolver
    solver_module.cp_model.CpSolver = _DefaultStrategySolver
    try:
        return solve_schedule(scheduling_input, policy=policy)
    finally:
        solver_module.cp_model.CpSolver = original


def assert_same_optimum(scheduling_input, policy):
    """The shipped strategy and CP-SAT's default must score identically."""
    shipped = solve_schedule(scheduling_input, policy=policy)
    default = _solve_with_default_strategy(scheduling_input, policy)
    assert _scores(shipped) == _scores(default)
    return shipped


# ---------------------------------------------------------------- small cases

BALANCE = SchedulingPolicy(allow_no_response=False, balance_candidate_loads=True)


def _requirements(events, roles, *, start=1):
    out, rid = [], start
    for event_index in range(events):
        for role_id in range(1, roles + 1):
            out.append(
                RequirementInput(
                    requirement_id=rid,
                    event_id=1000 + event_index,
                    event_date=_sunday(event_index),
                    ministry_role_id=role_id,
                    ministry_id=MINISTRY,
                    required_count=1,
                )
            )
            rid += 1
    return tuple(out)


def _candidate(membership_id, roles, events, state=AvailabilityState.AVAILABLE,
               **kwargs):
    return CandidateInput(
        membership_id=membership_id,
        person_id=membership_id,
        display_name=f"C{membership_id}",
        qualified_role_ids=frozenset(roles),
        availability_by_event=MappingProxyType(
            {1000 + i: state for i in range(events)}
        ),
        **kwargs,
    )


def test_no_placements_possible_scores_identically():
    """Nobody is qualified for anything: zero placements, and both agree."""
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(3, 2),
        candidates=(_candidate(1, (), 3),),
    )
    result = assert_same_optimum(scheduling_input, BALANCE)
    assert result.proposed_assignments == ()


def test_single_candidate():
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(4, 1),
        candidates=(_candidate(1, (1,), 4),),
    )
    result = assert_same_optimum(scheduling_input, BALANCE)
    assert len(result.proposed_assignments) == 4


def test_perfectly_divisible_load():
    """Nine positions over three interchangeable people: three each."""
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(9, 1),
        candidates=tuple(_candidate(m, (1,), 9) for m in (1, 2, 3)),
    )
    result = assert_same_optimum(scheduling_input, BALANCE)
    assert sorted(result.metrics.load_by_membership.values()) == [3, 3, 3]


def test_indivisible_load():
    """Ten over three cannot be even; the optimum is 3/3/4 either way."""
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(10, 1),
        candidates=tuple(_candidate(m, (1,), 10) for m in (1, 2, 3)),
    )
    result = assert_same_optimum(scheduling_input, BALANCE)
    assert sorted(result.metrics.load_by_membership.values()) == [3, 3, 4]


def test_sparse_qualifications():
    """Specialists, not a dense roster -- the shape that made the pass hard."""
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(6, 3),
        candidates=(
            _candidate(1, (1,), 6),
            _candidate(2, (1, 2), 6),
            _candidate(3, (2,), 6),
            _candidate(4, (3,), 6),
            _candidate(5, (2, 3), 6),
        ),
    )
    assert_same_optimum(scheduling_input, BALANCE)


def test_existing_assignments_and_uneven_starting_loads():
    """Fixed rows are constants in the objective; both runs must see that."""
    requirements = _requirements(5, 2)
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=requirements,
        candidates=tuple(_candidate(m, (1, 2), 5) for m in (1, 2, 3)),
        existing_assignments=(
            ExistingAssignmentInput(
                assignment_id=1, requirement_id=requirements[0].requirement_id,
                membership_id=1, event_id=requirements[0].event_id,
            ),
            ExistingAssignmentInput(
                assignment_id=2, requirement_id=requirements[2].requirement_id,
                membership_id=1, event_id=requirements[2].event_id,
            ),
        ),
    )
    assert_same_optimum(scheduling_input, BALANCE)


def test_backup_candidates():
    """BACKUP is feasible but second choice; the tier order must survive."""
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(6, 2),
        candidates=(
            _candidate(1, (1, 2), 6),
            _candidate(2, (1, 2), 6, state=AvailabilityState.BACKUP),
            _candidate(3, (1, 2), 6, state=AvailabilityState.BACKUP),
        ),
    )
    assert_same_optimum(scheduling_input, BALANCE)


def test_serving_maximums():
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(8, 1),
        candidates=(
            _candidate(1, (1,), 8, max_assignments_in_period=2),
            _candidate(2, (1,), 8, max_assignments_in_period=3),
            _candidate(3, (1,), 8),
        ),
    )
    result = assert_same_optimum(scheduling_input, BALANCE)
    assert result.metrics.load_by_membership.get(1, 0) <= 2
    assert result.metrics.load_by_membership.get(2, 0) <= 3


def test_linked_pair_exclusion():
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(6, 2),
        candidates=tuple(_candidate(m, (1, 2), 6) for m in (1, 2, 3)),
        same_date_exclusions=(LinkedMembershipPair(1, 2),),
    )
    assert_same_optimum(scheduling_input, BALANCE)


def test_with_target_and_role_variety():
    """All four soft passes at once, so every optimum is compared."""
    policy = SchedulingPolicy(
        allow_no_response=False,
        target_assignments_per_candidate=2,
        balance_candidate_loads=True,
        role_variety_role_ids=frozenset({1, 2}),
    )
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(7, 2),
        candidates=tuple(_candidate(m, (1, 2), 7) for m in (1, 2, 3, 4)),
    )
    result = assert_same_optimum(scheduling_input, policy)
    assert result.metrics.target_excess_total is not None
    assert result.metrics.role_variety_cost is not None


# ----------------------------------------------------- generated small cases

@pytest.mark.parametrize("seed", range(12))
def test_generated_quarters_agree(seed):
    """Many small random quarters: the two searches must never disagree."""
    scheduling_input = build_scale_input(
        seed=seed, candidates=9, events=4, roles=3, max_roles_per_candidate=2
    )
    assert_same_optimum(scheduling_input, BALANCE)


@pytest.mark.parametrize("seed", range(6))
def test_generated_quarters_agree_with_every_preference(seed):
    policy = SchedulingPolicy(
        allow_no_response=False,
        target_assignments_per_candidate=2,
        balance_candidate_loads=True,
        role_variety_role_ids=frozenset({1, 2}),
    )
    scheduling_input = build_scale_input(
        seed=100 + seed, candidates=10, events=5, roles=3,
        max_roles_per_candidate=2,
    )
    assert_same_optimum(scheduling_input, policy)


# --------------------------------------------------------------- determinism

def test_search_is_reproducible():
    """Same input, same schedule -- not merely the same score.

    The strategy must not have bought speed with a search that wanders. A
    schedule regenerated from unchanged input has to come back identical, or
    the drafts a ministry reviews would shuffle underneath them. This is the
    guarantee that ruled out CP-SAT's parallel portfolio, which was faster
    still but returned a different one of the equally optimal schedules on
    almost every run.
    """
    scheduling_input = build_scale_input(seed=7, candidates=20, events=6, roles=4)
    runs = [
        sorted(
            (p.requirement_id, p.membership_id, p.event_id)
            for p in solve_schedule(scheduling_input, policy=BALANCE).proposed_assignments
        )
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]


def test_unknown_strategy_fails_loudly(monkeypatch):
    """An OR-Tools rename must break visibly, never degrade quietly.

    If the named strategy ever stops existing, CP-SAT rejects the model and the
    very first pass raises. That is the behaviour worth having: a failing suite
    says "this needs attention", where a silent fallback would just make
    schedules slow again and nobody would know why.
    """
    monkeypatch.setattr(solver_module, "_SEARCH_STRATEGY", "no_such_subsolver")
    scheduling_input = SchedulingInput(
        schedule_version_id=1, scheduling_period_id=1, ministry_id=MINISTRY,
        requirements=_requirements(2, 1),
        candidates=(_candidate(1, (1,), 2),),
    )
    with pytest.raises(SchedulingEngineError):
        solve_schedule(scheduling_input, policy=BALANCE)
