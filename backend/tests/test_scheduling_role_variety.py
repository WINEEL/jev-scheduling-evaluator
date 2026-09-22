"""Role variety, the third soft layer (Task 33).

Hand-written inputs, the real CP-SAT solver, no database.

Role variety is the *last* thing the engine considers, so almost every test
here also pins what must not have moved: the fill count, and -- where a target
is configured -- the target-excess and fairness optima. A variety win bought
with any of those is not a win.

"Variety" here means variety of **role** only. Nothing in this task expresses a
preference about which Sundays a person serves, and several tests exist purely
to hold that line.
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
from app.scheduling.result import SolutionMetrics
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12       # deliberately outside the variety set, like Setup Lead
SETUP_2 = 13
SETUP_3 = 14
SETUP_4 = 15

VARIETY_ROLES = frozenset({SETUP_2, SETUP_3, SETUP_4})

# Both switch balancing off explicitly: it is independent of the target and
# on by default, so "nothing soft configured" now has to say so.
NO_SOFT = SchedulingPolicy(
    allow_no_response=False, balance_candidate_loads=False
)
VARIETY_ONLY = SchedulingPolicy(
    allow_no_response=False, balance_candidate_loads=False,
    role_variety_role_ids=VARIETY_ROLES,
)
TARGET_ONLY = SchedulingPolicy(
    allow_no_response=False, target_assignments_per_candidate=3
)
TARGET_AND_VARIETY = SchedulingPolicy(
    allow_no_response=False, target_assignments_per_candidate=3,
    role_variety_role_ids=VARIETY_ROLES,
)

ALL_ROLES = (LEAD, SETUP_2, SETUP_3, SETUP_4)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int, *, event_index: int = 0, ministry_role_id: int = SETUP_2,
    required_count: int = 1, role_is_active: bool = True,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=700 + event_index,
        event_date=_sunday(event_index), ministry_role_id=ministry_role_id,
        ministry_id=3, required_count=required_count, role_is_active=role_is_active,
    )


def _candidate(
    membership_id: int, *, qualified=ALL_ROLES, available_events=range(0, 12),
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


def _roles_by_membership(scheduling_input, result) -> dict[int, Counter]:
    """What role each person ended up serving, proposals only."""
    by_requirement = {r.requirement_id: r for r in scheduling_input.requirements}
    out: dict[int, Counter] = {}
    for proposal in result.proposed_assignments:
        role_id = by_requirement[proposal.requirement_id].ministry_role_id
        out.setdefault(proposal.membership_id, Counter())[role_id] += 1
    return out


# --------------------------------------------------------------------------
# 1-4 -- Backward compatibility
# --------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [None, frozenset(), set()])
def test_01_02_no_variety_configuration_preserves_existing_behavior(roles):
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118)],
    )
    policy = SchedulingPolicy(allow_no_response=False, role_variety_role_ids=roles)

    result = solve_schedule(scheduling_input, policy=policy)

    assert result.filled_count == 2
    assert result.metrics.role_variety_cost is None


def test_03_task_31_single_pass_behavior_survives_when_nothing_soft_is_configured():
    """Six equivalent Sundays, three people, no policy at all: the engine must
    still not invent a preference of any kind.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=NO_SOFT,
    )

    assert result.filled_count == 6
    assert result.metrics.target_excess_total is None
    assert result.metrics.fairness_cost is None
    assert result.metrics.role_variety_cost is None


def test_04_task_32_behavior_is_unchanged_when_variety_is_disabled():
    result = solve_schedule(
        _input(
            requirements=[_requirement(600 + i, event_index=i) for i in range(6)],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=TARGET_ONLY,
    )

    assert sorted(result.metrics.load_by_membership.values()) == [2, 2, 2]
    assert result.metrics.fairness_cost == 12
    assert result.metrics.target_excess_total == 0
    assert result.metrics.role_variety_cost is None


# --------------------------------------------------------------------------
# 5-9 -- Basic role variety
# --------------------------------------------------------------------------


def test_05_06_one_candidate_with_two_slots_is_spread_across_two_roles():
    """Two Sundays, each offering Setup 2 and Setup 3, one volunteer. Doubling
    up costs 4; one of each costs 2.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=0, ministry_role_id=SETUP_3),
            _requirement(602, event_index=1, ministry_role_id=SETUP_2),
            _requirement(603, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118)],
    )

    varied = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert varied.filled_count == 2  # one per Sunday, same-event rule
    assert sorted(_roles_by_membership(scheduling_input, varied)[118]) == [
        SETUP_2, SETUP_3
    ]
    assert varied.metrics.role_variety_cost == 2  # 1^2 + 1^2, not 2^2


def test_07_several_candidates_each_get_variety():
    """Two Sundays x two roles, two volunteers: each should end up with one of
    each role rather than one person doing Setup 2 twice.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=0, ministry_role_id=SETUP_3),
            _requirement(602, event_index=1, ministry_role_id=SETUP_2),
            _requirement(603, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118), _candidate(119)],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 4
    roles = _roles_by_membership(scheduling_input, result)
    for membership_id, counter in roles.items():
        assert sorted(counter) == [SETUP_2, SETUP_3], membership_id
    assert result.metrics.role_variety_cost == 4  # four people-roles at 1 each


def test_08_09_a_role_outside_the_configured_set_carries_no_variety_penalty():
    """Two Lead Sundays and one volunteer, with Lead deliberately excluded
    from the variety set. Serving Lead twice must cost nothing, and must not
    push the engine to do something else.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=LEAD),
            _requirement(601, event_index=1, ministry_role_id=LEAD),
        ],
        candidates=[_candidate(118)],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 2
    assert result.metrics.role_variety_cost == 0  # Lead contributes nothing


def test_09b_only_configured_roles_are_counted_when_both_kinds_are_served():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=LEAD),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118)],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 3
    # Two Setup 2s cost 4; the two Lead assignments cost nothing at all.
    assert result.metrics.role_variety_cost == 4


def test_09d_an_unconfigured_role_is_genuinely_free_even_when_it_would_be_penalized():
    """A has already served Lead twice. A new Sunday offers Lead or Setup 2.

    Because Lead is outside the configured set, taking it again costs nothing
    (0) while Setup 2 would cost 1 -- so the engine takes Lead. An engine that
    counted every role would see Lead at 2 and pick Setup 2 instead, a
    different and uniquely-optimal answer in that broken model, so this cannot
    pass on a tie.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=LEAD),
            _requirement(601, event_index=1, ministry_role_id=LEAD),
            _requirement(602, event_index=2, ministry_role_id=LEAD),
            _requirement(603, event_index=2, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
            ExistingAssignmentInput(
                assignment_id=901, requirement_id=601, membership_id=118, event_id=701,
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 1
    assert _roles_by_membership(scheduling_input, result)[118] == Counter({LEAD: 1})
    assert result.metrics.role_variety_cost == 0


def test_11b_existing_role_load_changes_which_schedule_is_optimal():
    """A has served Setup 3 twice already. Two new Sundays each offer Setup 2
    or Setup 3.

    Counting the history, both new slots should be Setup 2: that costs
    2^2 + 2^2 = 8, where splitting them costs 1 + 3^2 = 10. An engine that
    ignored existing role load would see two empty roles and split one each --
    again a different unique optimum, so a tie cannot mask the difference.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_3),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
            _requirement(603, event_index=2, ministry_role_id=SETUP_3),
            _requirement(604, event_index=3, ministry_role_id=SETUP_2),
            _requirement(605, event_index=3, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
            ExistingAssignmentInput(
                assignment_id=901, requirement_id=601, membership_id=118, event_id=701,
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 2
    assert _roles_by_membership(scheduling_input, result)[118] == Counter({SETUP_2: 2})
    assert result.metrics.role_variety_cost == 8


def test_09c_variety_prefers_the_configured_role_that_balances_the_others():
    """Three configured roles, one volunteer, three Sundays each offering all
    three: one of each beats any doubling.
    """
    requirements = []
    rid = 600
    for event_index in range(3):
        for role in (SETUP_2, SETUP_3, SETUP_4):
            requirements.append(
                _requirement(rid, event_index=event_index, ministry_role_id=role)
            )
            rid += 1
    scheduling_input = _input(requirements=requirements, candidates=[_candidate(118)])

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 3  # one per Sunday
    counter = _roles_by_membership(scheduling_input, result)[118]
    assert sorted(counter) == [SETUP_2, SETUP_3, SETUP_4]
    assert result.metrics.role_variety_cost == 3


# --------------------------------------------------------------------------
# 10-13 -- Existing assignments
# --------------------------------------------------------------------------


def test_10_11_existing_role_usage_steers_the_new_choice():
    """A has already served Setup 2 twice. Offered Setup 2 or Setup 3 on a new
    Sunday, variety should send them to Setup 3.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
            _requirement(603, event_index=2, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
            ExistingAssignmentInput(
                assignment_id=901, requirement_id=601, membership_id=118, event_id=701,
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 1  # the new Sunday only
    assert _roles_by_membership(scheduling_input, result)[118] == Counter({SETUP_3: 1})
    # Two existing Setup 2s plus one new Setup 3: 2^2 + 1^2.
    assert result.metrics.role_variety_cost == 5


def test_12_existing_assignments_are_never_moved_to_improve_variety():
    """The fairest role spread would undo one of A's two Setup 2s. Those are
    decisions people made, and they stay.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118), _candidate(119)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
            ExistingAssignmentInput(
                assignment_id=901, requirement_id=601, membership_id=118, event_id=701,
            ),
        ],
    )
    before = scheduling_input.existing_assignments

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.proposed_assignments == ()  # nothing left to fill
    assert scheduling_input.existing_assignments == before
    assert all(a.membership_id == 118 for a in scheduling_input.existing_assignments)
    assert result.metrics.role_variety_cost == 4  # the concentration is reported, not fixed


def test_13_existing_assignments_outside_the_configured_set_do_not_count():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=LEAD),
            _requirement(601, event_index=1, ministry_role_id=LEAD),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
            ExistingAssignmentInput(
                assignment_id=901, requirement_id=601, membership_id=118, event_id=701,
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    # Two existing Leads contribute nothing; only the new Setup 2 counts.
    assert result.metrics.role_variety_cost == 1


# --------------------------------------------------------------------------
# 14-19 -- Hard constraints stay dominant
# --------------------------------------------------------------------------


def test_14_19_variety_never_leaves_a_fillable_slot_empty():
    """Three Setup 2 Sundays and one volunteer. Perfect variety would be one
    assignment; filling all three is what matters.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600 + i, event_index=i, ministry_role_id=SETUP_2)
                for i in range(3)
            ],
            candidates=[_candidate(118)],
        ),
        policy=VARIETY_ONLY,
    )

    assert result.filled_count == 3
    assert result.is_complete is True
    assert result.metrics.role_variety_cost == 9  # 3^2, and correctly so


def test_15_an_unavailable_candidate_is_never_used_for_variety():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[
            _candidate(118),
            _candidate(
                119,
                availability={700 + i: AvailabilityState.UNAVAILABLE for i in range(2)},
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert {p.membership_id for p in result.proposed_assignments} == {118}


def test_16_an_unqualified_candidate_is_never_placed_for_variety():
    """A is only qualified for Setup 2 and would love the variety of Setup 3.
    Qualification is not negotiable.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
            _requirement(602, event_index=2, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118, qualified=(SETUP_2,))],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    roles = _roles_by_membership(scheduling_input, result)[118]
    assert roles == Counter({SETUP_2: 2})
    assert result.unfilled_count == 1  # the Setup 3 Sunday stays open


def test_17_a_church_blocked_candidate_is_never_used_for_variety():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[
            _candidate(118),
            _candidate(119, blocked=[_sunday(0), _sunday(1)]),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert {p.membership_id for p in result.proposed_assignments} == {118}


def test_18_the_same_event_rule_still_dominates_variety():
    """Both roles are on one Sunday and only one person exists. Perfect
    variety is unreachable, and the rule holds.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=0, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118)],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 1
    assert result.unfilled_count == 1


def test_19b_a_scarce_specialist_repeats_a_role_rather_than_leaving_it_empty():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
        ],
        candidates=[
            _candidate(118, qualified=(SETUP_2,)),
            _candidate(119, qualified=(SETUP_3,)),  # cannot help
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.is_complete is True
    assert _roles_by_membership(scheduling_input, result)[118] == Counter({SETUP_2: 3})


# --------------------------------------------------------------------------
# 20-24 -- Lexicographic priority
# --------------------------------------------------------------------------


def test_20_variety_may_not_create_avoidable_target_excess():
    """Four Sundays, target 1, two volunteers. Spreading two each keeps excess
    at 2; giving one person more variety by loading them up would raise it.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
            _requirement(603, event_index=3, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118), _candidate(119)],
    )

    result = solve_schedule(
        scheduling_input,
        policy=SchedulingPolicy(
            allow_no_response=False, target_assignments_per_candidate=1,
            role_variety_role_ids=VARIETY_ROLES,
        ),
    )

    assert result.filled_count == 4
    assert sorted(result.metrics.load_by_membership.values()) == [2, 2]
    assert result.metrics.target_excess_total == 2  # the minimum available


def test_21_candidate_fairness_outranks_role_variety():
    """The load-bearing ordering. Three volunteers, three Sundays, one role
    each Sunday but the roles repeat. A schedule of loads 2,1,0 could give
    better per-person role spread than 1,1,1 -- and 1,1,1 must still win,
    because fairness is fixed before variety is considered.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
            _requirement(602, event_index=2, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118), _candidate(119), _candidate(120)],
    )

    result = solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY)

    assert result.filled_count == 3
    # Fairness first: nobody carries two while somebody carries none.
    assert sorted(result.metrics.load_by_membership.values()) == [1, 1, 1]
    assert result.metrics.fairness_cost == 3
    # And with one assignment each, every role load is 1.
    assert result.metrics.role_variety_cost == 3


def test_21b_fairness_optimum_is_identical_with_and_without_variety():
    """Turning variety on must not perturb the fairness optimum at all -- it
    may only choose among the schedules that already achieve it.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600 + i, event_index=i,
                         ministry_role_id=SETUP_2 if i % 2 else SETUP_3)
            for i in range(6)
        ],
        candidates=[_candidate(118), _candidate(119), _candidate(120)],
    )

    without = solve_schedule(scheduling_input, policy=TARGET_ONLY)
    with_variety = solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY)

    assert without.filled_count == with_variety.filled_count == 6
    assert without.metrics.target_excess_total == with_variety.metrics.target_excess_total
    assert without.metrics.fairness_cost == with_variety.metrics.fairness_cost
    assert sorted(with_variety.metrics.load_by_membership.values()) == [2, 2, 2]


def test_22_variety_only_resolves_schedules_tied_on_higher_objectives():
    """Same input, variety off then on: identical fill and fairness, strictly
    better role concentration.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=0, ministry_role_id=SETUP_3),
            _requirement(602, event_index=1, ministry_role_id=SETUP_2),
            _requirement(603, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118), _candidate(119)],
    )

    plain = solve_schedule(scheduling_input, policy=TARGET_ONLY)
    varied = solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY)

    assert plain.filled_count == varied.filled_count == 4
    assert plain.metrics.fairness_cost == varied.metrics.fairness_cost
    assert varied.metrics.role_variety_cost == 4  # everybody one of each


def test_23_maximum_fill_is_fixed_before_variety_when_no_target_is_set():
    """Variety alone must not reduce fill either: the fill constraint is
    installed whenever any soft pass runs, not only for a target.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600, event_index=0, ministry_role_id=SETUP_2),
                _requirement(601, event_index=1, ministry_role_id=SETUP_2),
                _requirement(602, event_index=2, ministry_role_id=SETUP_2),
            ],
            candidates=[_candidate(118)],
        ),
        policy=VARIETY_ONLY,
    )

    assert result.filled_count == 3
    assert result.is_complete is True


def test_24_variety_alone_does_not_switch_on_candidate_fairness():
    """Six Sundays and three people with variety configured, no target, and
    balancing off: configuring variety must not quietly switch balancing on.
    Variety is about which roles a person serves, balance about how many.
    """
    result = solve_schedule(
        _input(
            requirements=[
                _requirement(600 + i, event_index=i, ministry_role_id=LEAD)
                for i in range(6)
            ],
            candidates=[_candidate(118), _candidate(119), _candidate(120)],
        ),
        policy=VARIETY_ONLY,
    )

    assert result.filled_count == 6
    assert result.metrics.target_excess_total is None
    assert result.metrics.fairness_cost is None
    assert result.metrics.role_variety_cost == 0  # Lead is not a variety role


# --------------------------------------------------------------------------
# 25-28 -- Policy
# --------------------------------------------------------------------------


def test_25_the_configured_role_set_is_immutable():
    mutable = {SETUP_2, SETUP_3}
    policy = SchedulingPolicy(allow_no_response=False, role_variety_role_ids=mutable)

    assert isinstance(policy.role_variety_role_ids, frozenset)
    mutable.add(SETUP_4)  # the caller's set moves on; the policy does not
    assert policy.role_variety_role_ids == frozenset({SETUP_2, SETUP_3})
    with pytest.raises(Exception):
        policy.role_variety_role_ids = frozenset()


@pytest.mark.parametrize("bad", [{-1}, {0}, {SETUP_2, -5}, {"13"}])
def test_26_invalid_role_ids_are_rejected(bad):
    with pytest.raises(SchedulingInputError, match="positive role ids"):
        solve_schedule(
            _input(
                requirements=[_requirement(600)], candidates=[_candidate(118)],
            ),
            policy=SchedulingPolicy(allow_no_response=False, role_variety_role_ids=bad),
        )


def test_27_a_configured_role_absent_from_this_period_is_harmless():
    """A ministry's configuration should not need editing for a quarter that
    happens not to need one of its roles.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600, ministry_role_id=SETUP_2)],
            candidates=[_candidate(118)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=False,
            role_variety_role_ids=frozenset({SETUP_2, 9999}),  # 9999 unused
        ),
    )

    assert result.filled_count == 1
    assert result.metrics.role_variety_cost == 1


def _code_literals(module) -> list:
    """Every literal in a module's *code*, with docstrings excluded.

    Checked this way rather than by grepping the file: the docstrings
    legitimately name ministries and roles while explaining that the engine
    does not.
    """
    import ast

    tree = ast.parse(Path(module.__file__).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in docstrings
    ]


def test_28_no_ministry_specific_name_or_role_id_is_built_into_the_solver():
    import app.scheduling.solver as module

    for literal in _code_literals(module):
        if isinstance(literal, str):
            lowered = literal.lower()
            for smell in ("setup", "lead", "sound", "slides", "av"):
                assert smell not in lowered.split("_"), literal
    # No role-id constant of any kind lives in the module namespace.
    assert not [
        name for name, value in vars(module).items()
        if name.isupper() and isinstance(value, int)
    ]
    # The variety roles come only from the policy the caller supplied.
    assert "policy.role_variety_role_ids" in Path(module.__file__).read_text()


# --------------------------------------------------------------------------
# 29-33 -- Metrics and determinism
# --------------------------------------------------------------------------


def test_29_the_metric_reflects_existing_plus_proposed_role_load():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118)],
        existing=[
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
            ),
        ],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    # One existing + one proposed Setup 2 = load 2, cost 4.
    assert result.filled_count == 1
    assert result.metrics.role_variety_cost == 4


def test_30_the_metric_is_none_when_variety_is_disabled():
    """None, not zero: zero would say the preference was evaluated and
    perfectly satisfied.
    """
    result = solve_schedule(
        _input(
            requirements=[_requirement(600)], candidates=[_candidate(118)],
        ),
        policy=TARGET_ONLY,
    )

    assert result.metrics.role_variety_cost is None
    assert result.metrics.fairness_cost is not None  # the target *was* evaluated


def test_31_32_repeated_solves_are_stable():
    scheduling_input = _input(
        requirements=[
            _requirement(600 + i, event_index=i // 2,
                         ministry_role_id=SETUP_2 if i % 2 else SETUP_3)
            for i in range(6)
        ],
        candidates=[_candidate(118), _candidate(119), _candidate(120)],
    )

    results = [
        solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY) for _ in range(5)
    ]

    assert all(r == results[0] for r in results)
    assert all(
        r.metrics.role_variety_cost == results[0].metrics.role_variety_cost
        for r in results
    )


def test_33_result_ordering_is_unchanged():
    scheduling_input = _input(
        requirements=[
            _requirement(602, event_index=2, ministry_role_id=SETUP_3),
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(120), _candidate(118)],
    )

    result = solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY)

    keys = [(p.requirement_id, p.membership_id) for p in result.proposed_assignments]
    assert keys == sorted(keys)


# --------------------------------------------------------------------------
# 34-35 -- No repeat-Sunday behavior was smuggled in
# --------------------------------------------------------------------------


def test_34_no_event_date_or_consecutive_week_objective_exists():
    """Role variety means variety of *role*. Nothing here expresses a
    preference about which Sundays somebody serves -- that is a separate
    policy choice and a separate task.
    """
    import ast

    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)

    # No date arithmetic is even reachable: the module imports no datetime.
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert "datetime" not in imported
    assert not hasattr(module, "timedelta")

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Minimize", "Maximize"}
        ):
            rendered = ast.unparse(node)
            assert "event_date" not in rendered
            assert "date" not in rendered


def test_35_two_schedules_differing_only_in_sunday_pattern_are_not_ranked():
    """Two Sundays of the same role and two volunteers. Whether one person
    takes both consecutive Sundays or they alternate is not something Task 33
    has an opinion about -- only role concentration is scored, and here every
    outcome that fills both is scored the same way by role.
    """
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_2),
        ],
        candidates=[_candidate(118), _candidate(119)],
    )

    result = solve_schedule(scheduling_input, policy=VARIETY_ONLY)

    assert result.filled_count == 2
    # Either split scores 2 (1+1) or 4 (2+0) purely by role load; the engine
    # never consults which Sunday is which.
    assert result.metrics.role_variety_cost in (2, 4)


# --------------------------------------------------------------------------
# 36-41 -- Purity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ["app.scheduling.solver", "app.scheduling.result", "app.scheduling.input"],
)
def test_36_37_38_the_variety_layer_adds_no_database_imports(module_name):
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


def test_39_40_the_solver_takes_no_session_and_persists_nothing():
    import inspect

    import app.scheduling.solver as module

    assert list(inspect.signature(solve_schedule).parameters) == [
        "scheduling_input", "policy",
    ]
    for forbidden in ("record_audit_event", "assign_member", "Assignment", "Session"):
        assert not hasattr(module, forbidden)


def test_41_solving_with_variety_does_not_mutate_the_input():
    scheduling_input = _input(
        requirements=[
            _requirement(600, event_index=0, ministry_role_id=SETUP_2),
            _requirement(601, event_index=1, ministry_role_id=SETUP_3),
        ],
        candidates=[_candidate(118)],
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

    solve_schedule(scheduling_input, policy=VARIETY_ONLY)
    solve_schedule(scheduling_input, policy=TARGET_AND_VARIETY)

    assert (
        scheduling_input.requirements,
        scheduling_input.candidates,
        scheduling_input.existing_assignments,
    ) == before


# --------------------------------------------------------------------------
# 42-45 -- How the objective is implemented
# --------------------------------------------------------------------------


def test_42_43_44_the_variety_pass_is_lexicographic_and_convex():
    import ast

    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    objectives = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"Maximize", "Minimize", "AddWeightedSum"}
    ]
    # Fill, then backup avoidance, target excess, load balance and role
    # variety -- ordered, not weighted.
    assert objectives == ["Maximize", "Minimize", "Minimize", "Minimize", "Minimize"]

    # Every earlier optimum becomes a constraint before the next objective.
    assert "model.Add(fill == optimal_fill)" in source
    assert "model.Add(total_backup == optimal_backup)" in source
    assert "model.Add(total_excess == optimal_excess)" in source
    assert "model.Add(total_cost == optimal_cost)" in source
    # Convex: role loads are squared, not summed raw.
    assert "AddMultiplicationEquality" in source


def test_45_no_hidden_candidate_or_name_tie_break_follows_variety():
    import ast

    import app.scheduling.solver as module

    source = Path(module.__file__).read_text()
    assert "display_name" not in source

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"Minimize", "Maximize"}
        ):
            rendered = ast.unparse(node)
            assert "membership_id" not in rendered
            assert "name" not in rendered


def test_the_metrics_type_still_carries_the_earlier_fields():
    """Backward compatible: Task 31/32 callers see what they always saw."""
    assert set(SolutionMetrics.__dataclass_fields__) == {
        "load_by_membership", "backup_placement_total", "target_excess_total",
        "fairness_cost", "role_variety_cost",
    }
    assert SolutionMetrics().role_variety_cost is None
