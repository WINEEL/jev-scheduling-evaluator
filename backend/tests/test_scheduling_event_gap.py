"""The ministry event-gap rule in the pure solver (Task 71).

Hand-written inputs, the real CP-SAT solver, no database.

The rule is **hard**: it is added as a constraint before any objective is set,
so every soft pass -- target excess, load balancing, role variety -- chooses
only among schedules that already respect it. These tests therefore assert two
different kinds of property, and keep them apart:

- that nobody ever serves two events closer together than the configured gap,
  which is exact and always assertable;
- that nothing changes at all when the rule is unconfigured, which is the
  legacy-behaviour guarantee the whole feature rests on.

**Counted in events, never in days**, is the property most of these tests exist
to pin. Several deliberately place a special event one day after an ordinary
one, or two ordinary events a fortnight apart, so that a "more than seven days"
implementation would pass the easy cases and fail here.

Where several schedules are equally good the assertions are on
objective-relevant shape -- which events each person ends up at, how many
positions are unfilled -- never on which particular person got which slot.

Every person, event and date is synthetic.
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

import pytest

from app.scheduling.input import (
    AdjacentEventInput,
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import (
    DIAGNOSTIC_ALL_AT_PERIOD_LIMIT,
    DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,
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


def _sundays(count: int, *, role: int = LEAD, required_count: int = 1):
    """``count`` ordinary Sundays, one position each, ids 1.. and 701..."""
    return tuple(
        _requirement(
            index + 1,
            event_id=701 + index,
            event_date=_sunday(index),
            ministry_role_id=role,
            required_count=required_count,
        )
        for index in range(count)
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


def _input(
    requirements=(),
    candidates=(),
    existing=(),
    pairs=(),
    min_intervening_events=None,
    preceding=(),
    following=(),
) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
        same_date_exclusions=tuple(pairs),
        min_intervening_events=min_intervening_events,
        preceding_events=tuple(preceding),
        following_events=tuple(following),
    )


def _events_by_membership(result, requirements) -> dict[int, list[int]]:
    """``membership_id -> the event ids this run placed them at``."""
    by_id = {r.requirement_id: r for r in requirements}
    events: dict[int, list[int]] = {}
    for proposal in result.proposed_assignments:
        events.setdefault(proposal.membership_id, []).append(
            by_id[proposal.requirement_id].event_id
        )
    return {key: sorted(value) for key, value in events.items()}


def _positions(result, requirements, sequence_event_ids) -> dict[int, list[int]]:
    """``membership_id -> the sequence positions they occupy`` after the run."""
    index_by_event = {event_id: i for i, event_id in enumerate(sequence_event_ids)}
    return {
        membership_id: sorted(index_by_event[event_id] for event_id in event_ids)
        for membership_id, event_ids in _events_by_membership(
            result, requirements
        ).items()
    }


def _assert_gap_respected(positions: dict[int, list[int]], gap: int) -> None:
    for membership_id, occupied in positions.items():
        for earlier, later in zip(occupied, occupied[1:]):
            assert later - earlier > gap, (membership_id, occupied)


# ==========================================================================
# The setting is absent -- legacy behaviour, unchanged
# ==========================================================================


def test_no_configured_rule_leaves_consecutive_assignments_alone():
    """The whole feature's safety property: a period that never asks for the
    rule behaves exactly as it did before the rule existed, including letting
    one person take every Sunday when nobody else can.
    """
    requirements = _sundays(4)
    result = solve_schedule(
        _input(requirements, (_candidate(A),)), policy=LENIENT
    )

    assert len(result.proposed_assignments) == 4
    assert result.unfilled_requirements == ()


def test_an_empty_preceding_history_with_no_rule_is_inert():
    """History loaded without a rule constrains nothing. The builder never
    produces this, but a hand-written input may, and it must not quietly
    behave like a gap of one.
    """
    requirements = _sundays(3)
    preceding = (
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), preceding=preceding), policy=LENIENT
    )

    assert len(result.proposed_assignments) == 3


# ==========================================================================
# A gap of one -- no consecutive assignments
# ==========================================================================


def test_a_gap_of_one_blocks_the_immediately_following_event():
    """One volunteer, three Sundays: they may take the first and the third,
    never two in a row. The middle position comes back unfilled rather than
    the run failing.
    """
    requirements = _sundays(3)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    positions = _positions(result, requirements, [701, 702, 703])
    assert positions == {A: [0, 2]}
    assert [u.requirement_id for u in result.unfilled_requirements] == [2]


def test_after_skipping_one_event_the_person_is_eligible_again():
    """Stated as its own test because it is the half of the rule most easily
    lost: the gap expires, it does not accumulate.
    """
    requirements = _sundays(5)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    positions = _positions(result, requirements, [701, 702, 703, 704, 705])
    assert positions == {A: [0, 2, 4]}


def test_two_people_alternate_and_every_position_is_filled():
    """The rule costs no fill when there are enough people: two volunteers
    across four consecutive events fill all four, each taking alternate ones.
    """
    requirements = _sundays(4)
    result = solve_schedule(
        _input(requirements, (_candidate(A), _candidate(B)), min_intervening_events=1),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 4
    assert result.unfilled_requirements == ()
    _assert_gap_respected(_positions(result, requirements, [701, 702, 703, 704]), 1)


def test_different_people_on_consecutive_events_are_allowed():
    """The rule is per person, not per event: two consecutive events staffed by
    two different volunteers is exactly what it is meant to produce.
    """
    requirements = _sundays(2)
    result = solve_schedule(
        _input(requirements, (_candidate(A), _candidate(B)), min_intervening_events=1),
        policy=LENIENT,
    )

    placed = {p.membership_id for p in result.proposed_assignments}
    assert len(result.proposed_assignments) == 2
    assert len(placed) == 2


# ==========================================================================
# Different roles still count
# ==========================================================================


def test_serving_a_different_role_at_the_next_event_is_still_blocked():
    """The rule is about *serving*, not about a role. Two consecutive events,
    each needing a different role, with one person qualified for both: they may
    take only one of the two.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=702, event_date=_sunday(1), ministry_role_id=SUPPORT),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A, qualified=(LEAD, SUPPORT)),),
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


def test_two_roles_at_one_event_are_one_position_not_two_events():
    """One event needing two roles is one position in the sequence. The
    one-position-per-event rule already stops one person filling both, and the
    gap rule must not additionally forbid somebody from serving the event at
    all.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT),
    )
    result = solve_schedule(
        _input(
            requirements,
            (
                _candidate(A, qualified=(LEAD, SUPPORT)),
                _candidate(B, qualified=(LEAD, SUPPORT)),
            ),
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 2
    assert len({p.membership_id for p in result.proposed_assignments}) == 2


# ==========================================================================
# Special events participate in the sequence -- days are never counted
# ==========================================================================


def test_a_special_event_between_two_sundays_participates_in_the_ordering():
    """A special event held mid-week sits between the Sundays either side of
    it. A volunteer may therefore serve both Sundays, because the special event
    is the one intervening event the rule requires -- which a day-based rule
    would refuse, the two Sundays being only seven days apart.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=750, event_date=_sunday(0) + datetime.timedelta(days=3)),
        _requirement(3, event_id=702, event_date=_sunday(1)),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    assert _events_by_membership(result, requirements) == {A: [701, 702]}


def test_sunday_then_a_special_event_the_next_day_is_blocked():
    """Consecutive in the sequence, one day apart. A rule counting days would
    allow this; the rule counting events must not.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=750, event_date=_sunday(0) + datetime.timedelta(days=1)),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


def test_a_special_event_then_the_following_sunday_is_blocked():
    """The other direction, and the case the ministry actually described:
    serving the special event blocks the next ordinary service after it.
    """
    requirements = (
        _requirement(1, event_id=750, event_date=_sunday(0) - datetime.timedelta(days=1)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


def test_two_events_on_one_date_are_consecutive_and_ordered_by_id():
    """A morning and an evening service on one Sunday are two events with
    nothing between them, so they are consecutive. The event model stores no
    time of day, so the id is the tie-break -- which is what makes the sequence
    deterministic here at all.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(0)),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


def test_a_fortnight_between_events_is_still_consecutive():
    """Nothing between them means consecutive, however far apart. Fourteen days
    would satisfy any plausible day-based reading and must still be refused.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(2)),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


# ==========================================================================
# The history boundary
# ==========================================================================


def test_serving_the_event_before_the_period_blocks_its_first_event():
    """The case the rule exists for at a quarter boundary. The volunteer served
    the last event of the previous period, so the first event of this one is
    the *next* event in the sequence and is blocked.
    """
    requirements = _sundays(3)
    preceding = (
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
        ),
        policy=LENIENT,
    )

    assert 701 not in _events_by_membership(result, requirements).get(A, [])
    assert 1 in {u.requirement_id for u in result.unfilled_requirements}


def test_history_blocks_only_the_person_who_actually_served():
    """The preceding event names memberships, and nobody else is affected by
    it: somebody who did not serve there may take the first event freely.
    """
    requirements = _sundays(2)
    preceding = (
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B)),
            min_intervening_events=1,
            preceding=preceding,
        ),
        policy=LENIENT,
    )

    events = _events_by_membership(result, requirements)
    assert events[B] == [701]
    assert events.get(A, []) == [702]


def test_older_history_outside_the_gap_does_not_block():
    """Two preceding events with a gap of one: only the nearer one can bind.
    Serving the *older* one leaves the first scheduled event perfectly
    available, because an event intervenes between them.

    One scheduled event, so the answer is not a choice between equally good
    schedules -- either the position is filled or the older history wrongly
    blocked it.
    """
    requirements = _sundays(1)
    preceding = (
        AdjacentEventInput(
            event_id=698,
            event_date=_sunday(-2),
            assigned_membership_ids=frozenset({A}),
        ),
        AdjacentEventInput(
            event_id=699, event_date=_sunday(-1), assigned_membership_ids=frozenset()
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
        ),
        policy=LENIENT,
    )

    assert _events_by_membership(result, requirements) == {A: [701]}


def test_the_nearest_history_event_does_block():
    """The companion to the test above, so "history was loaded but ignored"
    cannot pass both: the same shape with the volunteer on the *nearer*
    preceding event leaves the position open.
    """
    requirements = _sundays(1)
    preceding = (
        AdjacentEventInput(
            event_id=698, event_date=_sunday(-2), assigned_membership_ids=frozenset()
        ),
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
        ),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()


def test_a_preceding_event_nobody_served_still_counts_as_intervening():
    """An event whose schedule was never finalized blocks nobody, but it is
    still part of the sequence -- which is precisely what makes the test above
    come out the way it does. Stated separately so the two halves cannot be
    confused.
    """
    requirements = _sundays(1)
    preceding = (
        AdjacentEventInput(
            event_id=699, event_date=_sunday(-1), assigned_membership_ids=frozenset()
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


# ==========================================================================
# The forward boundary
# ==========================================================================


def test_serving_the_event_after_the_period_blocks_its_last_event():
    """The mirror of the history case, and the one the first pass missed. The
    volunteer is already published on the first event of the *next* quarter, so
    the last event of this one is the event immediately before it and is
    blocked.
    """
    requirements = _sundays(3)
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(3),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            following=following,
        ),
        policy=LENIENT,
    )

    assert 703 not in _events_by_membership(result, requirements).get(A, [])
    assert 3 in {u.requirement_id for u in result.unfilled_requirements}


def test_a_following_event_blocks_only_the_person_who_serves_it():
    requirements = _sundays(2)
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(2),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B)),
            min_intervening_events=1,
            following=following,
        ),
        policy=LENIENT,
    )

    events = _events_by_membership(result, requirements)
    assert events[B] == [702]
    assert events.get(A, []) == [701]


def test_a_following_event_beyond_the_gap_does_not_block():
    """Two events after the window with a gap of one: only the nearer one can
    bind, and it is empty. One scheduled event, so the answer is not a choice
    between equally good schedules.
    """
    requirements = _sundays(1)
    following = (
        AdjacentEventInput(
            event_id=704, event_date=_sunday(1), assigned_membership_ids=frozenset()
        ),
        AdjacentEventInput(
            event_id=705,
            event_date=_sunday(2),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            following=following,
        ),
        policy=LENIENT,
    )

    assert _events_by_membership(result, requirements) == {A: [701]}


def test_the_nearest_following_event_does_block():
    """The companion to the test above, so "loaded but ignored" cannot pass
    both: the same shape with the volunteer on the *nearer* following event
    leaves the position open.
    """
    requirements = _sundays(1)
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(1),
            assigned_membership_ids=frozenset({A}),
        ),
        AdjacentEventInput(
            event_id=705, event_date=_sunday(2), assigned_membership_ids=frozenset()
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            following=following,
        ),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()


def test_a_following_event_nobody_serves_still_counts_as_intervening():
    """An unfinalized next quarter blocks nobody, but it is still part of the
    sequence -- which is exactly what makes the beyond-the-gap test above come
    out the way it does.
    """
    requirements = _sundays(1)
    following = (
        AdjacentEventInput(
            event_id=704, event_date=_sunday(1), assigned_membership_ids=frozenset()
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            following=following,
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


@pytest.mark.parametrize("gap", [2, 3])
def test_a_larger_gap_reaches_further_forward(gap: int):
    """The forward reach scales with the configured number exactly as the
    backward one does -- it is not hard-wired to "the next event"."""
    requirements = _sundays(1)
    following = tuple(
        AdjacentEventInput(
            event_id=704 + offset,
            event_date=_sunday(1 + offset),
            # Only the furthest one is served, at distance ``gap`` from the
            # single scheduled event.
            assigned_membership_ids=frozenset({A}) if offset == gap - 1 else frozenset(),
        )
        for offset in range(gap)
    )
    blocked = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=gap,
            following=following,
        ),
        policy=LENIENT,
    )
    assert blocked.proposed_assignments == ()

    # One short of that reach, and the same commitment is out of range.
    allowed = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=gap - 1,
            following=following,
        ),
        policy=LENIENT,
    )
    assert len(allowed.proposed_assignments) == 1


def test_both_boundaries_bind_at_once():
    """A window with a committed event on each side and a gap of one: the
    volunteer may take neither the first nor the last scheduled event, only the
    middle one. Neither side is being enforced at the other's expense.
    """
    requirements = _sundays(3)
    preceding = (
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(3),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
            following=following,
        ),
        policy=LENIENT,
    )

    assert _events_by_membership(result, requirements) == {A: [702]}


def test_both_boundaries_can_close_a_window_completely():
    """Two scheduled events between two committed ones, gap of one: there is no
    position left for this volunteer at all, and the run still completes rather
    than failing.
    """
    requirements = _sundays(2)
    preceding = (
        AdjacentEventInput(
            event_id=699,
            event_date=_sunday(-1),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(2),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A),),
            min_intervening_events=1,
            preceding=preceding,
            following=following,
        ),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()
    assert len(result.unfilled_requirements) == 2
    for unfilled in result.unfilled_requirements:
        assert unfilled.diagnostic_codes == (DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,)


def test_a_following_event_is_inert_when_the_rule_is_unconfigured():
    """The legacy guarantee holds on the forward side too: events loaded
    without a rule constrain nothing.
    """
    requirements = _sundays(3)
    following = (
        AdjacentEventInput(
            event_id=704,
            event_date=_sunday(3),
            assigned_membership_ids=frozenset({A}),
        ),
    )
    result = solve_schedule(
        _input(requirements, (_candidate(A),), following=following), policy=LENIENT
    )

    assert len(result.proposed_assignments) == 3


# ==========================================================================
# Fixed and existing assignments
# ==========================================================================


def test_an_existing_assignment_blocks_the_adjacent_event():
    """Existing rows are fixed inputs the engine never moves. One at the first
    event means the second is unavailable to that person, exactly as if the run
    had placed them there itself.
    """
    requirements = _sundays(2)
    existing = (
        ExistingAssignmentInput(
            assignment_id=9000, requirement_id=1, membership_id=A, event_id=701
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B)),
            existing=existing,
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    assert _events_by_membership(result, requirements) == {B: [702]}


def test_two_fixed_assignments_already_in_conflict_do_not_fail_the_run():
    """A version that already breaks the rule -- because the gap was configured
    after the assignments existed -- is left exactly as bad as it was found. The
    model must stay feasible, and nothing may be deleted; the violation is
    reported at finalization instead.
    """
    requirements = _sundays(3)
    existing = (
        ExistingAssignmentInput(
            assignment_id=9000, requirement_id=1, membership_id=A, event_id=701
        ),
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=2, membership_id=A, event_id=702
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B)),
            existing=existing,
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    # The run completes, the third position is filled by somebody else, and no
    # existing row was touched.
    assert _events_by_membership(result, requirements) == {B: [703]}


def test_a_pre_existing_violation_is_never_made_worse():
    """The third consecutive event is not added to a pair that already
    conflicts: forcing the variables to zero, rather than skipping the window,
    is what guarantees that.
    """
    requirements = _sundays(3)
    existing = (
        ExistingAssignmentInput(
            assignment_id=9000, requirement_id=1, membership_id=A, event_id=701
        ),
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=2, membership_id=A, event_id=702
        ),
    )
    result = solve_schedule(
        _input(
            requirements, (_candidate(A),), existing=existing, min_intervening_events=1
        ),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()
    assert [u.requirement_id for u in result.unfilled_requirements] == [3]


# ==========================================================================
# Generalized gaps
# ==========================================================================


@pytest.mark.parametrize("gap", [1, 2, 3])
def test_a_generalized_gap_is_respected(gap: int):
    """The design is not special-cased to one. With a gap of ``N`` one person
    across eight consecutive events takes every ``N + 1``-th of them.
    """
    requirements = _sundays(8)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=gap),
        policy=LENIENT,
    )

    positions = _positions(result, requirements, [701 + i for i in range(8)])
    _assert_gap_respected(positions, gap)
    # Greedy from the front is optimal for a single candidate on a line, so the
    # count is exact rather than a lower bound.
    assert len(positions[A]) == len(range(0, 8, gap + 1))


def test_a_gap_of_two_requires_two_intervening_events():
    """Named separately from the parametrized test above, because "two" is the
    value most likely to be implemented as "one, twice": three consecutive
    events yield one assignment, where a gap of one would yield two.
    """
    requirements = _sundays(3)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=2),
        policy=LENIENT,
    )
    assert len(result.proposed_assignments) == 1

    looser = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )
    assert len(looser.proposed_assignments) == 2


def test_a_gap_longer_than_the_period_allows_one_assignment():
    """A sequence shorter than one window still gets the rule applied to what
    it contains, rather than to nothing.
    """
    requirements = _sundays(3)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=10),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


# ==========================================================================
# Composition with the other hard rules
# ==========================================================================


def test_the_gap_composes_with_a_serving_limit():
    """Both are hard, and the tighter one wins without either being weakened. A
    limit of one across five events yields one assignment, not the three the
    gap alone would allow.
    """
    requirements = _sundays(5)
    result = solve_schedule(
        _input(
            requirements, (_candidate(A, maximum=1),), min_intervening_events=1
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 1


def test_the_gap_composes_with_a_linked_pair_exclusion():
    """Two linked volunteers and a gap of one, across two events on one date
    plus a later one. Neither rule is traded away: the pair never shares a date
    and neither serves two consecutive events.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(0)),
        _requirement(3, event_id=703, event_date=_sunday(1)),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B)),
            pairs=(LinkedMembershipPair(A, B),),
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    events = _events_by_membership(result, requirements)
    on_first_date = {
        membership_id
        for membership_id, event_ids in events.items()
        if {701, 702} & set(event_ids)
    }
    assert len(on_first_date) <= 1
    _assert_gap_respected(_positions(result, requirements, [701, 702, 703]), 1)


def test_the_gap_never_costs_a_position_the_rules_below_it_could_fill():
    """A hard constraint, not a preference -- but equally, not a cap on fill:
    with enough people every position is filled and the gap still holds.
    """
    requirements = _sundays(6)
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B), _candidate(C)),
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    assert len(result.proposed_assignments) == 6
    _assert_gap_respected(
        _positions(result, requirements, [701 + i for i in range(6)]), 1
    )


def test_load_balancing_still_runs_underneath_the_gap():
    """The soft passes choose among schedules the constraint already permits.
    Six events and three volunteers under a gap of one is exactly two each; an
    implementation that added the gap as an objective term could trade that
    away.
    """
    requirements = _sundays(6)
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A), _candidate(B), _candidate(C)),
            min_intervening_events=1,
        ),
        policy=SchedulingPolicy(allow_no_response=True, balance_candidate_loads=True),
    )

    assert sorted(result.metrics.load_by_membership.values()) == [2, 2, 2]


# ==========================================================================
# Diagnostics
# ==========================================================================


def test_the_gap_diagnostic_is_reported_when_it_is_the_whole_reason():
    requirements = _sundays(2)
    result = solve_schedule(
        _input(requirements, (_candidate(A),), min_intervening_events=1),
        policy=LENIENT,
    )

    (unfilled,) = result.unfilled_requirements
    assert unfilled.diagnostic_codes == (DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,)


def test_the_gap_diagnostic_is_not_reported_when_the_rule_is_unconfigured():
    """A position short for ordinary reasons must not acquire a gap
    explanation. Two positions at one event and one candidate is plain
    contention.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), required_count=2),
    )
    result = solve_schedule(_input(requirements, (_candidate(A),)), policy=LENIENT)

    (unfilled,) = result.unfilled_requirements
    assert DIAGNOSTIC_ALL_WITHIN_EVENT_GAP not in unfilled.diagnostic_codes


def test_a_shortfall_explained_by_the_gap_and_the_limit_together_names_both():
    """Two feasible candidates stopped by two *different* hard rules. Naming
    one would understate why the position is open; naming contention would be
    plainly wrong.

    Both candidates are pinned by existing assignments, so the shortfall is not
    one of several equally good outcomes: A is at their maximum of one and is
    three events away, while B is one event away and has no maximum at all.
    """
    requirements = _sundays(4)
    existing = (
        ExistingAssignmentInput(
            assignment_id=9000, requirement_id=1, membership_id=A, event_id=701
        ),
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=3, membership_id=B, event_id=703
        ),
    )
    result = solve_schedule(
        _input(
            requirements,
            (_candidate(A, maximum=1), _candidate(B)),
            existing=existing,
            min_intervening_events=1,
        ),
        policy=LENIENT,
    )

    short = {u.requirement_id: u.diagnostic_codes for u in result.unfilled_requirements}
    # Requirement 4 is the fourth event: A is at their limit, B served the
    # event immediately before it.
    assert set(short[4]) == {
        DIAGNOSTIC_ALL_AT_PERIOD_LIMIT,
        DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,
    }
    assert DIAGNOSTIC_LINKED_DATE_CONFLICT not in short[4]


# ==========================================================================
# Structural validation
# ==========================================================================


def test_a_zero_gap_is_refused():
    """Zero would mean "consecutive assignments are allowed", which ``None``
    already says. Accepting it would give one fact two spellings.
    """
    with pytest.raises(SchedulingInputError, match="must be positive"):
        solve_schedule(
            _input(_sundays(2), (_candidate(A),), min_intervening_events=0),
            policy=LENIENT,
        )


def test_a_negative_gap_is_refused():
    with pytest.raises(SchedulingInputError, match="must be positive"):
        solve_schedule(
            _input(_sundays(2), (_candidate(A),), min_intervening_events=-1),
            policy=LENIENT,
        )


def test_a_boolean_gap_is_refused():
    """``bool`` is an ``int`` subclass, and ``True`` would silently become a
    gap of one.
    """
    with pytest.raises(SchedulingInputError, match="must be an integer"):
        solve_schedule(
            _input(_sundays(2), (_candidate(A),), min_intervening_events=True),
            policy=LENIENT,
        )


def test_duplicate_preceding_events_are_refused():
    preceding = (
        AdjacentEventInput(event_id=699, event_date=_sunday(-1)),
        AdjacentEventInput(event_id=699, event_date=_sunday(-2)),
    )
    with pytest.raises(SchedulingInputError, match="adjacent event ids must be unique"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                preceding=preceding,
            ),
            policy=LENIENT,
        )


def test_an_event_cannot_be_both_history_and_scheduled():
    """It would occupy two positions, and the second would silently shift every
    gap after it.
    """
    preceding = (AdjacentEventInput(event_id=701, event_date=_sunday(-1)),)
    with pytest.raises(SchedulingInputError, match="both adjacent to this run"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                preceding=preceding,
            ),
            policy=LENIENT,
        )


def test_following_events_must_fall_after_the_last_scheduled_event():
    following = (AdjacentEventInput(event_id=799, event_date=_sunday(-5)),)
    with pytest.raises(SchedulingInputError, match="after the last scheduled"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                following=following,
            ),
            policy=LENIENT,
        )


def test_one_event_cannot_be_on_both_sides_at_once():
    """It would occupy two positions and silently shift every gap between
    them."""
    both = (AdjacentEventInput(event_id=699, event_date=_sunday(-1)),)
    with pytest.raises(SchedulingInputError, match="adjacent event ids must be unique"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                preceding=both,
                following=both,
            ),
            policy=LENIENT,
        )


def test_preceding_events_must_fall_before_the_first_scheduled_event():
    preceding = (AdjacentEventInput(event_id=799, event_date=_sunday(5)),)
    with pytest.raises(SchedulingInputError, match="before the first scheduled"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                preceding=preceding,
            ),
            policy=LENIENT,
        )


# ==========================================================================
# The pure input's own sequence arithmetic
# ==========================================================================


def test_a_following_event_cannot_also_be_scheduled():
    following = (AdjacentEventInput(event_id=702, event_date=_sunday(5)),)
    with pytest.raises(SchedulingInputError, match="both adjacent to this run"):
        solve_schedule(
            _input(
                _sundays(2),
                (_candidate(A),),
                min_intervening_events=1,
                following=following,
            ),
            policy=LENIENT,
        )


def test_the_sequence_puts_history_first_and_orders_by_date_then_id():
    requirements = (
        _requirement(1, event_id=702, event_date=_sunday(1)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=700, event_date=_sunday(1)),
    )
    preceding = (
        AdjacentEventInput(event_id=699, event_date=_sunday(-1)),
        AdjacentEventInput(event_id=698, event_date=_sunday(-2)),
    )
    scheduling_input = _input(
        requirements, (_candidate(A),), min_intervening_events=1, preceding=preceding
    )

    # History oldest-first, then the scheduled events by (date, id): the two
    # events on the later Sunday are ordered 700 then 702.
    assert scheduling_input.ministry_event_sequence == (698, 699, 701, 700, 702)


def test_the_sequence_puts_following_events_last():
    """One chronological line through both boundaries, whatever order the
    tuples were built in."""
    requirements = _sundays(2)
    scheduling_input = _input(
        requirements,
        (_candidate(A),),
        min_intervening_events=1,
        preceding=(
            AdjacentEventInput(event_id=699, event_date=_sunday(-1)),
            AdjacentEventInput(event_id=698, event_date=_sunday(-2)),
        ),
        following=(
            AdjacentEventInput(event_id=705, event_date=_sunday(3)),
            AdjacentEventInput(event_id=704, event_date=_sunday(2)),
        ),
    )

    assert scheduling_input.ministry_event_sequence == (698, 699, 701, 702, 704, 705)


def test_two_requirements_at_one_event_are_one_position():
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT),
    )
    assert _input(requirements).scheduled_event_ids == (701,)
