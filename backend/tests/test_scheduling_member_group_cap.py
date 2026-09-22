"""The member-group per-event cap in the pure solver (Task 74).

Hand-written inputs, the real CP-SAT solver, no database.

The rule is **hard**: it is added as a constraint before any objective is set,
so every soft pass -- backup avoidance, target excess, load balancing, role
variety -- chooses only among schedules that already respect it. These tests
therefore assert two different kinds of property, and keep them apart:

- that no event ever carries more of a capped group than its number allows,
  which is exact and always assertable;
- that nothing changes at all when no cap is configured, which is the
  legacy-behaviour guarantee the whole feature rests on.

**Counted per event, and each member once whatever role they serve**, is the
property most of these tests exist to pin. Several deliberately give group
members two different roles at one event, so an implementation that counted
*positions* rather than *people* would pass the easy cases and fail here.

The rule is generic: nothing here knows what a group means, and nothing in the
solver could be told. The groups below are numbered, not named.

Where several schedules are equally good the assertions are on
objective-relevant shape -- how many group members end up at each event, how
many positions are unfilled -- never on which particular person got which slot.

Every person, event and group is synthetic.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    MemberGroupCap,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import (
    DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT,
    DIAGNOSTIC_ALL_AT_PERIOD_LIMIT,
    DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,
)
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
SUPPORT = 13

GROUP = 90
OTHER_GROUP = 91

LENIENT = SchedulingPolicy(allow_no_response=True)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int,
    *,
    event_id: int,
    event_date: datetime.date,
    ministry_role_id: int = SUPPORT,
    required_count: int = 1,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count, role_is_active=True,
    )


def _one_event(positions: int, *, role: int = SUPPORT):
    """One event with ``positions`` required positions in one role."""
    return tuple(
        _requirement(index + 1, event_id=701, event_date=_sunday(0),
                     ministry_role_id=role)
        for index in range(positions)
    )


def _candidate(
    membership_id: int,
    *,
    qualified=(SUPPORT,),
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


def _input(
    requirements=(),
    candidates=(),
    existing=(),
    caps=(),
    min_intervening_events=None,
) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
        member_group_caps=tuple(caps),
        min_intervening_events=min_intervening_events,
    )


def _cap(max_per_event: int, members, *, group_id: int = GROUP) -> MemberGroupCap:
    return MemberGroupCap(
        member_group_id=group_id,
        max_per_event=max_per_event,
        member_membership_ids=frozenset(members),
    )


def _present_by_event(result, requirements) -> dict[int, set[int]]:
    """``event_id -> the memberships this run placed there``."""
    by_id = {r.requirement_id: r for r in requirements}
    present: dict[int, set[int]] = {}
    for proposal in result.proposed_assignments:
        present.setdefault(by_id[proposal.requirement_id].event_id, set()).add(
            proposal.membership_id
        )
    return present


def _assert_cap_respected(result, requirements, cap: MemberGroupCap, *, existing=()) -> None:
    """The exact rule, checked over the finished schedule including fixed rows."""
    by_id = {r.requirement_id: r for r in requirements}
    present: dict[int, set[int]] = {}
    for assignment in existing:
        present.setdefault(by_id[assignment.requirement_id].event_id, set()).add(
            assignment.membership_id
        )
    for event_id, memberships in _present_by_event(result, requirements).items():
        present.setdefault(event_id, set()).update(memberships)
    for event_id, memberships in present.items():
        count = len(cap.member_membership_ids & memberships)
        assert count <= cap.max_per_event, (event_id, count)


# ==========================================================================
# No cap configured -- legacy behaviour, unchanged
# ==========================================================================


def test_no_configured_cap_fills_every_position():
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates), policy=LENIENT
    )

    assert result.filled_count == 3
    assert result.unfilled_requirements == ()


def test_an_empty_cap_tuple_is_the_default_and_adds_no_constraint():
    """The legacy-behaviour guarantee, stated as an equality between two runs.

    A schedule built with no caps at all and one built with a cap so generous
    it can never bind must be the same schedule -- same fill, same placements.
    """
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))

    without = solve_schedule(_input(requirements, candidates), policy=LENIENT)
    with_inert = solve_schedule(
        _input(requirements, candidates, caps=(_cap(9, (1, 2, 3)),)),
        policy=LENIENT,
    )

    assert without.filled_count == with_inert.filled_count == 3
    assert without.proposed_assignments == with_inert.proposed_assignments


def test_a_group_nobody_is_in_caps_nothing():
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(_cap(1, ()),)), policy=LENIENT
    )

    assert result.filled_count == 3


# ==========================================================================
# The cap allows exactly N, and refuses N + 1
# ==========================================================================


def test_a_cap_of_two_allows_exactly_two_group_members_on_one_event():
    requirements = _one_event(3)
    # Three positions, three candidates, all three in the capped group: the
    # third position can only be filled by breaking the cap, so it stays open.
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    cap = _cap(2, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 2
    assert result.unfilled_count == 1
    _assert_cap_respected(result, requirements, cap)


def test_a_cap_of_two_still_fills_three_when_a_non_member_is_available():
    """The cap limits the group, never the event: an ungrouped candidate fills
    the position the group may not.
    """
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3, 4))
    cap = _cap(2, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 3
    assert result.unfilled_requirements == ()
    _assert_cap_respected(result, requirements, cap)
    # The one candidate outside the group must be on it, since only two of the
    # other three may be.
    assert 4 in _present_by_event(result, requirements)[701]


def test_a_cap_of_one_allows_exactly_one():
    requirements = _one_event(2)
    candidates = tuple(_candidate(m) for m in (1, 2))
    cap = _cap(1, (1, 2))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 1
    _assert_cap_respected(result, requirements, cap)


def test_the_cap_binds_each_event_separately_not_the_period():
    """Two events, a cap of one, two group members: one on each event is fine.

    This is the difference between a per-event cap and a serving limit, and an
    implementation that summed across the period would fill only one of the two.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(1)),
    )
    candidates = tuple(_candidate(m) for m in (1, 2))
    cap = _cap(1, (1, 2))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, cap)


# ==========================================================================
# Roles are irrelevant: people are counted, not positions
# ==========================================================================


def test_members_in_different_roles_still_count_towards_the_cap():
    """One lead position and two support positions on one event, all three
    candidates in the capped group. A cap of two must leave one open however
    the roles are arranged -- an implementation that counted per role would
    happily fill all three.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT),
        _requirement(3, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT),
    )
    candidates = tuple(
        _candidate(m, qualified=(LEAD, SUPPORT)) for m in (1, 2, 3)
    )
    cap = _cap(2, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, cap)


def test_a_member_qualified_for_two_roles_is_still_one_person():
    """Two positions, two candidates, a cap of one. The capped member may take
    one position; the other candidate takes the other.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT),
    )
    candidates = (
        _candidate(1, qualified=(LEAD, SUPPORT)),
        _candidate(2, qualified=(LEAD, SUPPORT)),
    )
    cap = _cap(1, (1,))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, cap)


# ==========================================================================
# Fixed assignments consume the allowance
# ==========================================================================


def test_an_existing_group_assignment_consumes_the_cap():
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    existing = (
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=1, membership_id=1, event_id=701
        ),
    )
    cap = _cap(2, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, caps=(cap,)),
        policy=LENIENT,
    )

    # One of the two allowed places is already taken, so only one more may be
    # filled and the third position stays open.
    assert result.filled_count == 1
    assert result.unfilled_count == 1
    _assert_cap_respected(result, requirements, cap, existing=existing)


def test_a_group_already_over_its_cap_gets_no_new_placements_and_no_deletion():
    """A pre-existing violation is preserved, never repaired.

    A head raised the group's membership, or lowered the number, after the
    assignments existed. The engine must not respond by deleting somebody's
    assignment, and must not fail the whole run either: it simply adds nothing
    that would make the event worse.
    """
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    existing = (
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=1, membership_id=1, event_id=701
        ),
        ExistingAssignmentInput(
            assignment_id=9002, requirement_id=2, membership_id=2, event_id=701
        ),
    )
    cap = _cap(1, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, caps=(cap,)),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()
    assert result.unfilled_count == 1


# ==========================================================================
# Several caps at once, and caps that overlap
# ==========================================================================


def test_a_member_in_two_capped_groups_must_satisfy_both():
    """The hard rules are a conjunction, not a ranking: the tighter cap binds.

    Three positions; candidates 1 and 2 are in both groups, candidate 3 only in
    the first. A cap of two on the first group and one on the second means at
    most one of {1, 2} may serve -- and candidate 3 fills a second position,
    because the second group does not constrain them.
    """
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    wide = _cap(2, (1, 2, 3), group_id=GROUP)
    narrow = _cap(1, (1, 2), group_id=OTHER_GROUP)

    result = solve_schedule(
        _input(requirements, candidates, caps=(wide, narrow)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, wide)
    _assert_cap_respected(result, requirements, narrow)


def test_two_disjoint_groups_are_capped_independently():
    requirements = _one_event(4)
    candidates = tuple(_candidate(m) for m in (1, 2, 3, 4))
    first = _cap(1, (1, 2), group_id=GROUP)
    second = _cap(1, (3, 4), group_id=OTHER_GROUP)

    result = solve_schedule(
        _input(requirements, candidates, caps=(first, second)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, first)
    _assert_cap_respected(result, requirements, second)


# ==========================================================================
# Composition with the other hard rules
# ==========================================================================


def test_the_cap_composes_with_the_serving_maximum():
    """Both rules bind, and neither is traded away for the other."""
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=702, event_date=_sunday(1)),
    )
    candidates = (
        _candidate(1, maximum=1),
        _candidate(2, maximum=1),
        _candidate(3, maximum=1),
    )
    cap = _cap(1, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)), policy=LENIENT
    )

    # One per event by the cap, one each by the maximum: two positions fill.
    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, cap)
    assert all(load <= 1 for load in result.metrics.load_by_membership.values())


def test_the_cap_composes_with_the_event_gap_rule():
    """Three consecutive events, a gap of one and a cap of one, two group
    members and one ungrouped candidate. Both rules hold over the result.
    """
    requirements = tuple(
        _requirement(index + 1, event_id=701 + index, event_date=_sunday(index))
        for index in range(3)
    )
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    cap = _cap(1, (1, 2))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,), min_intervening_events=1),
        policy=LENIENT,
    )

    _assert_cap_respected(result, requirements, cap)
    events = {}
    for proposal in result.proposed_assignments:
        events.setdefault(proposal.membership_id, []).append(proposal.event_id)
    for membership_id, event_ids in events.items():
        occupied = sorted(event_ids)
        for earlier, later in zip(occupied, occupied[1:]):
            assert later - earlier > 1, (membership_id, occupied)


def test_the_cap_is_not_traded_away_by_load_balancing():
    """Load balancing would prefer to spread work over the three group members;
    the cap forbids it, and the cap is a constraint rather than a preference.
    """
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))
    cap = _cap(2, (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(cap,)),
        policy=SchedulingPolicy(
            allow_no_response=True,
            balance_candidate_loads=True,
            target_assignments_per_candidate=1,
            role_variety_role_ids=frozenset({SUPPORT}),
        ),
    )

    assert result.filled_count == 2
    _assert_cap_respected(result, requirements, cap)


# ==========================================================================
# Diagnostics
# ==========================================================================


def test_a_position_left_open_by_the_cap_says_so():
    requirements = _one_event(3)
    candidates = tuple(_candidate(m) for m in (1, 2, 3))

    result = solve_schedule(
        _input(requirements, candidates, caps=(_cap(2, (1, 2, 3)),)),
        policy=LENIENT,
    )

    (unfilled,) = result.unfilled_requirements
    assert DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT in unfilled.diagnostic_codes


def test_the_cap_diagnostic_does_not_appear_when_the_cap_did_not_bite():
    """A position open for want of anybody at all is not a cap problem."""
    requirements = _one_event(2)
    candidates = (_candidate(1),)

    result = solve_schedule(
        _input(requirements, candidates, caps=(_cap(5, (1,)),)), policy=LENIENT
    )

    (unfilled,) = result.unfilled_requirements
    assert DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT not in unfilled.diagnostic_codes


def test_several_rules_can_be_reported_at_once():
    """Candidates blocked by two different rules for one event's open positions.

    Candidate 1 is at their period maximum, having spent it on the second
    event; candidates 2 and 3 are in a group capped at one per event, so once
    one of them is placed the other is blocked by the cap. Both reasons are
    true of different people, and naming one would understate why the positions
    are open.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=701, event_date=_sunday(0)),
        _requirement(4, event_id=702, event_date=_sunday(1)),
    )
    candidates = (
        _candidate(1, maximum=1),
        _candidate(2),
        _candidate(3),
    )
    existing = (
        # Candidate 1 has spent their one assignment on the second event.
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=4, membership_id=1, event_id=702
        ),
    )
    cap = _cap(1, (2, 3))

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, caps=(cap,)),
        policy=LENIENT,
    )

    codes = {
        code
        for unfilled in result.unfilled_requirements
        for code in unfilled.diagnostic_codes
    }
    assert DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT in codes
    assert DIAGNOSTIC_ALL_AT_PERIOD_LIMIT in codes
    assert DIAGNOSTIC_ALL_WITHIN_EVENT_GAP not in codes


# ==========================================================================
# Structural validation of hand-written input
# ==========================================================================


def test_two_caps_for_one_group_are_refused():
    with pytest.raises(SchedulingInputError, match="capped more than once"):
        solve_schedule(
            _input(
                _one_event(1),
                (_candidate(1),),
                caps=(_cap(1, (1,)), _cap(2, (1,))),
            ),
            policy=LENIENT,
        )


@pytest.mark.parametrize("maximum", [0, -1])
def test_a_non_positive_cap_is_refused(maximum):
    with pytest.raises(SchedulingInputError, match="non-positive max_per_event"):
        solve_schedule(
            _input(_one_event(1), (_candidate(1),), caps=(_cap(maximum, (1,)),)),
            policy=LENIENT,
        )


def test_a_boolean_cap_is_refused_rather_than_read_as_one():
    with pytest.raises(SchedulingInputError, match="non-integer max_per_event"):
        solve_schedule(
            _input(_one_event(1), (_candidate(1),), caps=(_cap(True, (1,)),)),
            policy=LENIENT,
        )


def test_a_cap_naming_a_non_candidate_is_kept_rather_than_refused():
    """A member deactivated part-way through the period stops being a candidate
    while their group membership stays recorded. The rule is then inert --
    somebody with no variables can never consume a cap -- and refusing the input
    would block generation over a rule that cannot affect it.
    """
    requirements = _one_event(1)
    candidates = (_candidate(1),)

    result = solve_schedule(
        _input(requirements, candidates, caps=(_cap(1, (1, 999)),)),
        policy=LENIENT,
    )

    assert result.filled_count == 1


def test_the_membership_set_is_frozen_on_construction():
    """A mutable set handed in would leave the value frozen in name only, and a
    later mutation would silently change what a completed run was constrained
    by.
    """
    members = {1, 2}
    cap = MemberGroupCap(member_group_id=GROUP, max_per_event=1,
                         member_membership_ids=members)
    members.add(3)

    assert cap.member_membership_ids == frozenset({1, 2})
    assert isinstance(cap.member_membership_ids, frozenset)
