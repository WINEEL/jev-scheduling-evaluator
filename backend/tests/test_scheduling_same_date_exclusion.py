"""The linked-pair same-date exclusion in the pure solver (Task 50).

Hand-written inputs, the real CP-SAT solver, no database.

The exclusion is a **hard** rule: it is added as a constraint before any
objective is set, so every soft pass -- target excess, load balancing, role
variety -- chooses only among schedules that already respect it. These tests
therefore assert two different kinds of property, and keep them apart:

- that no linked pair ever shares a date, which is exact and always
  assertable;
- that fill is not reduced when the rule does not bind, which is about the
  objective.

**Same calendar date, not the same event**, is the property most of these
tests exist to pin: several of them place two events on one date so that a
"not both in one event" implementation would pass while the real rule fails.

Where several schedules are equally good the assertions are on
objective-relevant shape -- loads, counts, which requirements are unfilled --
never on which particular person got which slot.

Every person is synthetic. Nothing here describes a real couple, household or
relationship: the solver is never told why two candidates are linked, and
these tests could not tell it if they wanted to.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import (
    DIAGNOSTIC_ALL_AT_PERIOD_LIMIT,
    DIAGNOSTIC_ALL_UNAVAILABLE,
    DIAGNOSTIC_LINKED_DATE_CONFLICT,
)
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
SUPPORT = 13

#: Two linked volunteers, and a third unrelated one.
A = 200
B = 201
C = 202

LENIENT = SchedulingPolicy(allow_no_response=True)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int,
    *,
    event_id: int,
    event_date: datetime.date,
    ministry_role_id: int = LEAD,
    required_count: int = 1,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count, role_is_active=True,
    )


def _candidate(
    membership_id: int,
    *,
    qualified=(LEAD,),
    unavailable_events=(),
    maximum: int | None = None,
) -> CandidateInput:
    return CandidateInput(
        membership_id=membership_id, person_id=membership_id,
        display_name=f"Synthetic Volunteer {membership_id}",
        qualified_role_ids=frozenset(qualified),
        availability_by_event=MappingProxyType(
            {event_id: AvailabilityState.UNAVAILABLE for event_id in unavailable_events}
        ),
        blocked_dates=frozenset(),
        max_assignments_in_period=maximum,
    )


def _input(requirements=(), candidates=(), existing=(), pairs=()) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
        same_date_exclusions=tuple(pairs),
    )


def _dates_by_membership(result, requirements) -> dict[int, list]:
    by_id = {r.requirement_id: r for r in requirements}
    dates: dict[int, list] = {}
    for proposal in result.proposed_assignments:
        dates.setdefault(proposal.membership_id, []).append(
            by_id[proposal.requirement_id].event_date
        )
    return dates


def _assert_never_share_a_date(result, requirements, *, pair=(A, B), existing=()):
    """The invariant every test in this file relies on, stated once.

    Existing assignments count as presence, so they are folded in before the
    comparison -- otherwise a "violation" involving a fixed row would slip
    past unnoticed.
    """
    by_id = {r.requirement_id: r for r in requirements}
    presence: dict[int, set] = {}
    for assignment in existing:
        presence.setdefault(assignment.membership_id, set()).add(
            by_id[assignment.requirement_id].event_date
        )
    for proposal in result.proposed_assignments:
        presence.setdefault(proposal.membership_id, set()).add(
            by_id[proposal.requirement_id].event_date
        )
    shared = presence.get(pair[0], set()) & presence.get(pair[1], set())
    assert not shared, f"linked pair {pair} share date(s) {sorted(shared)}"


# ==========================================================================
# 13 -- the pure input carries the rule
# ==========================================================================


def test_13_the_input_carries_the_pair_and_answers_from_either_side():
    scheduling_input = _input(pairs=[LinkedMembershipPair(B, A)])

    # Canonicalized on construction, whichever way round it was written.
    assert scheduling_input.same_date_exclusions == (LinkedMembershipPair(A, B),)
    assert scheduling_input.linked_membership_ids(A) == frozenset({B})
    assert scheduling_input.linked_membership_ids(B) == frozenset({A})
    assert scheduling_input.linked_membership_ids(C) == frozenset()


def test_13b_no_pair_is_the_ordinary_case_and_means_no_rule():
    assert _input().same_date_exclusions == ()
    assert _input().linked_membership_ids(A) == frozenset()


def test_13c_the_pure_pair_carries_two_ids_and_nothing_else():
    """No relationship type, no reason, no strength -- there is nowhere to put
    one, which is how the solver is kept from ever learning why."""
    assert set(LinkedMembershipPair.__dataclass_fields__) == {
        "membership_a_id",
        "membership_b_id",
    }


def test_13d_a_duplicated_pair_is_a_builder_bug_and_is_refused():
    with pytest.raises(SchedulingInputError, match="more than once"):
        solve_schedule(
            _input(pairs=[LinkedMembershipPair(A, B), LinkedMembershipPair(B, A)]),
            policy=LENIENT,
        )


def test_13e_a_pair_naming_an_unknown_membership_is_inert_not_an_error():
    """A member deactivated mid-period stops being a candidate while their
    rule stays configured. The rule cannot bind -- they have no variables --
    and refusing the whole run over it would block generation for nothing.
    """
    requirements = [_requirement(1, event_id=700, event_date=_sunday(0))]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A)],
            pairs=[LinkedMembershipPair(A, 999)],
        ),
        policy=LENIENT,
    )
    assert result.filled_count == 1


# ==========================================================================
# 14-15 -- the core rule
# ==========================================================================


def test_14_the_solver_never_places_both_linked_candidates_on_one_date():
    """Two positions on one date, and only the linked pair to fill them."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    # One of the two positions is filled; the other cannot be, and that is the
    # rule working rather than a failure.
    assert result.filled_count == 1
    assert result.unfilled_count == 1


def test_15_a_linked_pair_may_serve_on_different_dates():
    """The rule removes a *coincidence*, never either person's eligibility."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(1)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A), _candidate(B)],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    assert result.filled_count == 2
    assert result.is_complete
    dates = _dates_by_membership(result, requirements)
    assert sorted(dates[A] + dates[B]) == [_sunday(0), _sunday(1)]


def test_15b_one_linked_member_may_still_take_two_positions_on_one_date():
    """The pair rule constrains the *pair*, not either individual.

    One ministry, two events on one Sunday: the existing one-position-per-event
    rule permits the same person at both, and the pair rule must not quietly
    forbid that -- it would be a rule nobody agreed to and a needless loss of
    fill.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A), _candidate(B, unavailable_events=(700, 701))],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 2
    assert {p.membership_id for p in result.proposed_assignments} == {A}


# ==========================================================================
# 16 -- two different events on one date
# ==========================================================================


def test_16_the_rule_applies_across_two_events_on_the_same_date():
    """The test a "not both in one event" implementation would fail.

    Two separate events -- a morning and an evening service -- share one
    calendar date. Nothing about the same *event* is violated by putting one
    linked member at each, and the rule must still block it.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A), _candidate(B)],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    # Both positions can still be filled -- by one of them twice, which the
    # one-position-per-*event* rule permits.
    assert result.filled_count == 2
    placed = {p.membership_id for p in result.proposed_assignments}
    assert len(placed) == 1


def test_16b_with_one_member_unavailable_at_one_of_two_same_date_events():
    """A sharper form: B can only serve the evening event, A only the morning,
    and the two events share a date -- so exactly one position is fillable.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, unavailable_events=(701,)),
                _candidate(B, unavailable_events=(700,)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    assert result.filled_count == 1
    assert result.unfilled_count == 1


# ==========================================================================
# 17 -- another ministry is unaffected
# ==========================================================================


def test_17_a_pair_configured_elsewhere_does_not_reach_this_run():
    """One :class:`SchedulingInput` is one ministry's one period.

    A rule configured for another ministry simply is not in this input --
    there is no field it could arrive through -- so the same two people
    schedule freely here. This test states that property from the run's side;
    the builder's period scoping is what supplies it (see the builder tests).
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
            ],
            # No pair: this ministry's period has none configured.
            pairs=(),
        ),
        policy=LENIENT,
    )

    assert result.filled_count == 2
    assert result.is_complete


# ==========================================================================
# 18-19 -- fixed assignments count as presence
# ==========================================================================


def test_18_a_fixed_assignment_for_a_blocks_a_new_b_on_that_date():
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    existing = [
        ExistingAssignmentInput(
            assignment_id=1, requirement_id=1, membership_id=A, event_id=700
        )
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            # B is the only candidate left for the second position -- A is
            # unavailable at that event -- and A is already fixed on its date.
            candidates=[
                _candidate(A, unavailable_events=(701,)),
                _candidate(B, unavailable_events=(700,)),
            ],
            existing=existing,
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements, existing=existing)
    assert result.filled_count == 0
    assert [u.requirement_id for u in result.unfilled_requirements] == [2]


def test_19_a_fixed_assignment_for_b_blocks_a_new_a_on_that_date():
    """The symmetric case, asserted separately: the rule is symmetric, and a
    one-sided implementation would pass the test above.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    existing = [
        ExistingAssignmentInput(
            assignment_id=1, requirement_id=1, membership_id=B, event_id=700
        )
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(B, unavailable_events=(701,)),
                _candidate(A, unavailable_events=(700,)),
            ],
            existing=existing,
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements, existing=existing)
    assert result.filled_count == 0


def test_19b_both_already_fixed_on_one_date_does_not_fail_the_run():
    """A version that already violates the rule is a real state.

    A head may configure the exclusion after a draft was built. Generation
    must not respond by failing, and must never delete either assignment: the
    violation is reported at finalization and repaired by a person. The run
    simply cannot make it worse -- no new placement adds a second violation to
    a date that already has one.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=702, event_date=_sunday(1)),
    ]
    existing = [
        ExistingAssignmentInput(
            assignment_id=1, requirement_id=1, membership_id=A, event_id=700
        ),
        ExistingAssignmentInput(
            assignment_id=2, requirement_id=2, membership_id=B, event_id=701
        ),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A), _candidate(B), _candidate(C)],
            existing=existing,
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    # The run succeeded, nothing was removed (the solver emits proposals only),
    # and the remaining Sunday was still filled.
    assert result.is_complete
    assert all(p.requirement_id == 3 for p in result.proposed_assignments)


# ==========================================================================
# 20-21 -- soft preferences cannot reach the rule
# ==========================================================================


def test_20_target_and_fairness_cannot_buy_a_violation():
    """Two dates, four positions, and only the linked pair to fill them.

    Load balancing would dearly like A and B to take two each. The rule caps
    each *date* at one of them, so the honest best is two filled and two open,
    and no fairness gain may buy the third.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
        _requirement(3, event_id=701, event_date=_sunday(1)),
        _requirement(4, event_id=701, event_date=_sunday(1), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True,
            target_assignments_per_candidate=2,
            balance_candidate_loads=True,
        ),
    )

    _assert_never_share_a_date(result, requirements)
    assert result.filled_count == 2
    assert result.unfilled_count == 2


def test_21_role_variety_cannot_buy_a_violation():
    """Variety would like each of them in each role on each date. It may not."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=SchedulingPolicy(
            allow_no_response=True,
            role_variety_role_ids=frozenset({LEAD, SUPPORT}),
        ),
    )

    _assert_never_share_a_date(result, requirements)
    assert result.filled_count == 1


# ==========================================================================
# 22-23 -- fill, and honest scarcity
# ==========================================================================


def test_22_maximum_fill_is_preserved_when_an_alternate_candidate_exists():
    """The rule must cost nothing when somebody else can take the position."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
                _candidate(C, qualified=(LEAD, SUPPORT)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    assert result.is_complete
    assert result.filled_count == 2


def test_22b_the_unrelated_volunteer_is_unaffected_by_the_pair():
    """C shares dates with either of them freely; only A+B is constrained."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=700, event_date=_sunday(0), ministry_role_id=SUPPORT),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(C, qualified=(LEAD, SUPPORT)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    assert result.is_complete
    assert {p.membership_id for p in result.proposed_assignments} == {A, C}


def test_23_scarcity_leaves_a_slot_unresolved_rather_than_violating_the_rule():
    """Best effort plus an explicit unresolved slot -- the approved behaviour.

    The diagnostic says a pair rule bit, and says so in its own code: these
    people are neither unavailable nor unqualified, and calling them either
    would send a head to the wrong screen.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                _candidate(A, unavailable_events=(701,)),
                _candidate(B, unavailable_events=(700,)),
            ],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    assert result.unfilled_count == 1
    unfilled = result.unfilled_requirements[0]
    assert DIAGNOSTIC_LINKED_DATE_CONFLICT in unfilled.diagnostic_codes
    # And emphatically not a false availability claim.
    assert DIAGNOSTIC_ALL_UNAVAILABLE not in unfilled.diagnostic_codes


def test_23b_a_shortfall_with_no_pair_configured_reports_no_linked_code():
    """The diagnostic is not emitted speculatively."""
    requirements = [_requirement(1, event_id=700, event_date=_sunday(0))]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A, unavailable_events=(700,))],
            pairs=(),
        ),
        policy=LENIENT,
    )

    codes = result.unfilled_requirements[0].diagnostic_codes
    assert DIAGNOSTIC_LINKED_DATE_CONFLICT not in codes
    assert DIAGNOSTIC_ALL_UNAVAILABLE in codes


def test_23c_both_a_limit_and_a_pair_rule_are_reported_when_both_apply():
    """Neither rule alone explains the shortfall, so neither alone is claimed.

    A is at their agreed maximum and B is linked to somebody already placed
    that date. Naming one would understate why the position is open; naming
    contention would be plainly wrong.
    """
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=702, event_date=_sunday(0)),
    ]
    existing = [
        ExistingAssignmentInput(
            assignment_id=1, requirement_id=1, membership_id=C, event_id=700
        ),
        ExistingAssignmentInput(
            assignment_id=2, requirement_id=2, membership_id=A, event_id=701
        ),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[
                # A is at the maximum they agreed to; B is linked to C, who is
                # already on the roster for this date; C cannot take the open
                # position themselves.
                _candidate(A, maximum=1),
                _candidate(B),
                _candidate(C, unavailable_events=(702,)),
            ],
            existing=existing,
            pairs=[LinkedMembershipPair(B, C)],
        ),
        policy=LENIENT,
    )

    assert result.unfilled_count == 1
    codes = result.unfilled_requirements[0].diagnostic_codes
    assert DIAGNOSTIC_ALL_AT_PERIOD_LIMIT in codes
    assert DIAGNOSTIC_LINKED_DATE_CONFLICT in codes


def test_the_pair_rule_and_a_serving_maximum_hold_together():
    """Two hard rules, both constraints, neither weakening the other."""
    requirements = [
        _requirement(1, event_id=700, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(1)),
        _requirement(3, event_id=702, event_date=_sunday(2)),
    ]
    result = solve_schedule(
        _input(
            requirements=requirements,
            candidates=[_candidate(A, maximum=1), _candidate(B)],
            pairs=[LinkedMembershipPair(A, B)],
        ),
        policy=LENIENT,
    )

    _assert_never_share_a_date(result, requirements)
    loads = dict(result.metrics.load_by_membership)
    assert loads.get(A, 0) <= 1
    assert result.is_complete
