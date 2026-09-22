"""The same-event support requirement in the pure solver (Task 74).

Hand-written inputs, the real CP-SAT solver, no database.

The rule is **hard** and **conditional**: the subject may hold an assignment at
an event only when enough of their approved supporters hold one at the *same*
event. It is added as a constraint before any objective is set, so every soft
pass chooses only among schedules that already respect it.

Four properties the tests below exist to pin, because each is a way an
implementation could be subtly wrong:

- **subject absent, no requirement** -- the rule never obliges anybody to serve,
  and an event the subject is not on is unconstrained;
- **same event, not the same date** -- two services on one Sunday are two crews,
  and a supporter at the other one does not count;
- **any role counts** -- a supporter satisfies the requirement by being at the
  event, whatever position they fill;
- **directional** -- a supporter is never constrained by the rule, and may serve
  with or without the subject.

The rule is generic: nothing here knows *why* the support is required, and
nothing in the solver could be told.

Every person, event and date is synthetic.
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
    SameEventSupportRequirement,
    SchedulingInput,
)
from app.scheduling.result import DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

FIRST_SUNDAY = datetime.date(2026, 10, 4)

LEAD = 12
SUPPORT_ROLE = 13

SUBJECT = 1
APPROVED = 2
ALSO_APPROVED = 3
UNRELATED = 4

LENIENT = SchedulingPolicy(allow_no_response=True)


def _sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


def _requirement(
    requirement_id: int,
    *,
    event_id: int,
    event_date: datetime.date,
    ministry_role_id: int = SUPPORT_ROLE,
    required_count: int = 1,
) -> RequirementInput:
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count, role_is_active=True,
    )


def _one_event(positions: int, *, event_id: int = 701, start: int = 1):
    return tuple(
        _requirement(start + index, event_id=event_id, event_date=_sunday(0))
        for index in range(positions)
    )


def _candidate(
    membership_id: int,
    *,
    qualified=(SUPPORT_ROLE,),
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
    support=(),
) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=tuple(requirements), candidates=tuple(candidates),
        existing_assignments=tuple(existing),
        support_requirements=tuple(support),
    )


def _rule(
    *supporters, subject: int = SUBJECT, minimum: int = 1
) -> SameEventSupportRequirement:
    return SameEventSupportRequirement(
        subject_membership_id=subject,
        min_supporters=minimum,
        supporter_membership_ids=frozenset(supporters),
    )


def _present_by_event(result, requirements, *, existing=()) -> dict[int, set[int]]:
    """``event_id -> everybody on its roster`` in the finished schedule."""
    by_id = {r.requirement_id: r for r in requirements}
    present: dict[int, set[int]] = {}
    for assignment in existing:
        present.setdefault(by_id[assignment.requirement_id].event_id, set()).add(
            assignment.membership_id
        )
    for proposal in result.proposed_assignments:
        present.setdefault(by_id[proposal.requirement_id].event_id, set()).add(
            proposal.membership_id
        )
    return present


def _assert_support_respected(
    result, requirements, rule: SameEventSupportRequirement, *, existing=()
) -> None:
    """The exact rule, checked over the finished schedule including fixed rows."""
    for event_id, roster in _present_by_event(
        result, requirements, existing=existing
    ).items():
        if rule.subject_membership_id not in roster:
            continue
        present = len(rule.supporter_membership_ids & roster)
        assert present >= rule.min_supporters, (event_id, sorted(roster))


# ==========================================================================
# No requirement configured -- legacy behaviour, unchanged
# ==========================================================================


def test_no_configured_requirement_fills_every_position():
    requirements = _one_event(2)
    candidates = (_candidate(SUBJECT), _candidate(UNRELATED))

    result = solve_schedule(_input(requirements, candidates), policy=LENIENT)

    assert result.filled_count == 2
    assert result.unfilled_requirements == ()


def test_an_inert_requirement_changes_no_placement():
    """The legacy-behaviour guarantee, stated as an equality between two runs.

    A requirement whose supporter is on the crew anyway must produce exactly
    the schedule the same input produces with no rule at all.
    """
    requirements = _one_event(2)
    candidates = (_candidate(SUBJECT), _candidate(APPROVED))

    without = solve_schedule(_input(requirements, candidates), policy=LENIENT)
    with_rule = solve_schedule(
        _input(requirements, candidates, support=(_rule(APPROVED),)),
        policy=LENIENT,
    )

    assert without.filled_count == with_rule.filled_count == 2
    assert without.proposed_assignments == with_rule.proposed_assignments


# ==========================================================================
# Subject absent means no requirement at all
# ==========================================================================


def test_an_event_the_subject_does_not_serve_is_unconstrained():
    """The rule never obliges anybody to serve. Two events, one position each,
    and a subject who can only take the first: the second fills with an
    unrelated member and no supporter is dragged onto it.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(1)),
    )
    candidates = (
        _candidate(SUBJECT, unavailable_events=(702,)),
        _candidate(APPROVED, unavailable_events=(702,)),
        _candidate(UNRELATED, unavailable_events=(701,)),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 2
    rosters = _present_by_event(result, requirements)
    assert rosters[702] == {UNRELATED}
    _assert_support_respected(result, requirements, rule)


def test_a_subject_who_cannot_be_supported_simply_is_not_placed():
    """One position, a subject whose only approved supporter is not a candidate.

    The position comes back unfilled rather than the run failing, which is the
    approved behaviour: best effort, plus explicit unresolved slots.
    """
    requirements = _one_event(1)
    candidates = (_candidate(SUBJECT),)

    result = solve_schedule(
        _input(requirements, candidates, support=(_rule(APPROVED),)),
        policy=LENIENT,
    )

    assert result.proposed_assignments == ()
    assert result.unfilled_count == 1


# ==========================================================================
# Subject present means the support must be there too
# ==========================================================================


def test_the_solver_places_a_supporter_alongside_the_subject():
    requirements = _one_event(2)
    candidates = (_candidate(SUBJECT), _candidate(APPROVED))
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 2
    assert _present_by_event(result, requirements)[701] == {SUBJECT, APPROVED}
    _assert_support_respected(result, requirements, rule)


def test_an_unrelated_member_does_not_satisfy_the_requirement():
    """One position; the only other candidate is not approved. The subject
    cannot take it, and neither can the position be filled by pretending the
    unrelated member counts -- but the unrelated member may take it themselves.
    """
    requirements = _one_event(1)
    candidates = (_candidate(SUBJECT), _candidate(UNRELATED))
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 1
    assert _present_by_event(result, requirements)[701] == {UNRELATED}
    _assert_support_respected(result, requirements, rule)


def test_a_supporter_in_any_role_satisfies_the_requirement():
    """The supporter is qualified only for the lead role and the subject only
    for the other. A rule that looked at roles rather than presence would leave
    the subject off.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0), ministry_role_id=LEAD),
        _requirement(2, event_id=701, event_date=_sunday(0), ministry_role_id=SUPPORT_ROLE),
    )
    candidates = (
        _candidate(SUBJECT, qualified=(SUPPORT_ROLE,)),
        _candidate(APPROVED, qualified=(LEAD,)),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 2
    _assert_support_respected(result, requirements, rule)


def test_two_required_supporters_means_two_on_the_crew():
    requirements = _one_event(3)
    candidates = (
        _candidate(SUBJECT),
        _candidate(APPROVED),
        _candidate(ALSO_APPROVED),
    )
    rule = _rule(APPROVED, ALSO_APPROVED, minimum=2)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 3
    _assert_support_respected(result, requirements, rule)


def test_two_required_supporters_with_only_one_available_leaves_the_subject_off():
    requirements = _one_event(3)
    candidates = (
        _candidate(SUBJECT),
        _candidate(APPROVED),
        _candidate(ALSO_APPROVED, unavailable_events=(701,)),
        _candidate(UNRELATED),
    )
    rule = _rule(APPROVED, ALSO_APPROVED, minimum=2)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    roster = _present_by_event(result, requirements)[701]
    assert SUBJECT not in roster
    _assert_support_respected(result, requirements, rule)


# ==========================================================================
# Same event, never the same date
# ==========================================================================


def test_a_supporter_at_the_other_service_on_one_date_does_not_count():
    """Two events on one Sunday. The subject can only take the first and the
    supporter only the second, so the requirement is unmet and the subject
    stays off -- a date-level reading would have placed them.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(0)),
    )
    candidates = (
        _candidate(SUBJECT, unavailable_events=(702,)),
        _candidate(APPROVED, unavailable_events=(701,)),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    rosters = _present_by_event(result, requirements)
    assert SUBJECT not in rosters.get(701, set())
    assert rosters.get(702) == {APPROVED}
    _assert_support_respected(result, requirements, rule)


# ==========================================================================
# Directional: a supporter is never constrained
# ==========================================================================


def test_a_supporter_may_serve_without_the_subject():
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=702, event_date=_sunday(1)),
    )
    candidates = (
        _candidate(SUBJECT, unavailable_events=(701, 702)),
        _candidate(APPROVED),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.filled_count == 2
    assert {p.membership_id for p in result.proposed_assignments} == {APPROVED}


def test_the_subject_cannot_satisfy_their_own_requirement():
    with pytest.raises(ValueError, match="cannot be its own supporter"):
        SameEventSupportRequirement(
            subject_membership_id=SUBJECT,
            min_supporters=1,
            supporter_membership_ids=frozenset({SUBJECT, APPROVED}),
        )


# ==========================================================================
# Fixed assignments
# ==========================================================================


def test_a_fixed_subject_obliges_the_run_to_find_a_supporter():
    """The subject is already assigned at the event through an existing row
    this engine may not remove. The constraint becomes a demand on the run:
    it must place an approved supporter there, even though load balancing would
    otherwise have spread the second position to the other Sunday's candidate.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    )
    candidates = (
        _candidate(SUBJECT),
        _candidate(APPROVED),
        _candidate(UNRELATED),
    )
    existing = (
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=1, membership_id=SUBJECT,
            event_id=701,
        ),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, support=(rule,)),
        policy=LENIENT,
    )

    assert {p.membership_id for p in result.proposed_assignments} == {APPROVED}
    _assert_support_respected(result, requirements, rule, existing=existing)


def test_a_fixed_supporter_satisfies_the_requirement():
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    )
    candidates = (_candidate(SUBJECT), _candidate(APPROVED))
    existing = (
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=1, membership_id=APPROVED,
            event_id=701,
        ),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, support=(rule,)),
        policy=LENIENT,
    )

    assert {p.membership_id for p in result.proposed_assignments} == {SUBJECT}
    _assert_support_respected(result, requirements, rule, existing=existing)


def test_a_fixed_subject_who_cannot_be_supported_is_left_exactly_as_found():
    """A pre-existing violation is preserved, never repaired and never fatal.

    The subject already holds a row at an event where no approved supporter
    can be placed. An infeasible model would fail the whole generation, and
    deleting the row would destroy a decision a person made -- so the run
    simply adds what it can and leaves the violation for the finalization gate.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
    )
    candidates = (_candidate(SUBJECT), _candidate(UNRELATED))
    existing = (
        ExistingAssignmentInput(
            assignment_id=9001, requirement_id=1, membership_id=SUBJECT,
            event_id=701,
        ),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, existing=existing, support=(rule,)),
        policy=LENIENT,
    )

    # The run still fills what it legitimately can, and removes nothing.
    assert {p.membership_id for p in result.proposed_assignments} == {UNRELATED}


# ==========================================================================
# Composition with the other hard rules
# ==========================================================================


def test_the_requirement_composes_with_the_serving_maximum():
    """The supporter's maximum is one, so they can support the subject at only
    one of the two events -- and the subject may serve only that one.
    """
    requirements = (
        _requirement(1, event_id=701, event_date=_sunday(0)),
        _requirement(2, event_id=701, event_date=_sunday(0)),
        _requirement(3, event_id=702, event_date=_sunday(1)),
        _requirement(4, event_id=702, event_date=_sunday(1)),
    )
    candidates = (
        _candidate(SUBJECT),
        _candidate(APPROVED, maximum=1),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    assert result.metrics.load_by_membership.get(APPROVED, 0) <= 1
    _assert_support_respected(result, requirements, rule)


def test_the_requirement_is_not_traded_away_by_load_balancing():
    requirements = _one_event(2)
    candidates = (
        _candidate(SUBJECT),
        _candidate(APPROVED),
        _candidate(UNRELATED),
    )
    rule = _rule(APPROVED)

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)),
        policy=SchedulingPolicy(
            allow_no_response=True,
            balance_candidate_loads=True,
            target_assignments_per_candidate=1,
            role_variety_role_ids=frozenset({SUPPORT_ROLE}),
        ),
    )

    _assert_support_respected(result, requirements, rule)


# ==========================================================================
# Diagnostics
# ==========================================================================


def test_a_position_left_open_by_the_support_rule_says_so():
    requirements = _one_event(1)
    candidates = (_candidate(SUBJECT),)

    result = solve_schedule(
        _input(requirements, candidates, support=(_rule(APPROVED),)),
        policy=LENIENT,
    )

    (unfilled,) = result.unfilled_requirements
    assert DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT in unfilled.diagnostic_codes


def test_an_impossible_requirement_yields_a_useful_diagnostic():
    """No supporter is approved at all, so the subject can never be placed.

    The engine reports the position as open with the support code rather than
    refusing to run: a configuration a head can see and fix beats a crash they
    cannot.
    """
    requirements = _one_event(1)
    candidates = (_candidate(SUBJECT),)
    rule = SameEventSupportRequirement(
        subject_membership_id=SUBJECT,
        min_supporters=1,
        supporter_membership_ids=frozenset(),
    )
    assert not rule.is_satisfiable

    result = solve_schedule(
        _input(requirements, candidates, support=(rule,)), policy=LENIENT
    )

    (unfilled,) = result.unfilled_requirements
    assert DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT in unfilled.diagnostic_codes


def test_the_support_diagnostic_does_not_appear_when_the_rule_did_not_bite():
    requirements = _one_event(2)
    candidates = (_candidate(SUBJECT), _candidate(APPROVED))
    # Two positions, both filled -- nothing is unfilled at all.
    result = solve_schedule(
        _input(requirements, candidates, support=(_rule(APPROVED),)),
        policy=LENIENT,
    )

    assert result.unfilled_requirements == ()


# ==========================================================================
# Structural validation of hand-written input
# ==========================================================================


def test_two_requirements_for_one_subject_are_refused():
    with pytest.raises(SchedulingInputError, match="more than one support requirement"):
        solve_schedule(
            _input(
                _one_event(1),
                (_candidate(SUBJECT),),
                support=(_rule(APPROVED), _rule(ALSO_APPROVED)),
            ),
            policy=LENIENT,
        )


@pytest.mark.parametrize("minimum", [0, -1])
def test_a_non_positive_count_is_refused(minimum):
    with pytest.raises(SchedulingInputError, match="non-positive min_supporters"):
        solve_schedule(
            _input(
                _one_event(1),
                (_candidate(SUBJECT),),
                support=(_rule(APPROVED, minimum=minimum),),
            ),
            policy=LENIENT,
        )


def test_a_boolean_count_is_refused_rather_than_read_as_one():
    with pytest.raises(SchedulingInputError, match="non-integer min_supporters"):
        solve_schedule(
            _input(
                _one_event(1),
                (_candidate(SUBJECT),),
                support=(_rule(APPROVED, minimum=True),),
            ),
            policy=LENIENT,
        )


def test_a_requirement_naming_a_non_candidate_subject_is_inert():
    """A member deactivated part-way through the period stops being a candidate
    while their rule stays configured. Refusing the input would block
    generation over a rule that cannot affect it.
    """
    requirements = _one_event(1)
    candidates = (_candidate(UNRELATED),)

    result = solve_schedule(
        _input(requirements, candidates, support=(_rule(APPROVED, subject=999),)),
        policy=LENIENT,
    )

    assert result.filled_count == 1


def test_the_supporter_set_is_frozen_on_construction():
    supporters = {APPROVED}
    rule = SameEventSupportRequirement(
        subject_membership_id=SUBJECT,
        min_supporters=1,
        supporter_membership_ids=supporters,
    )
    supporters.add(ALSO_APPROVED)

    assert rule.supporter_membership_ids == frozenset({APPROVED})
    assert isinstance(rule.supporter_membership_ids, frozenset)
