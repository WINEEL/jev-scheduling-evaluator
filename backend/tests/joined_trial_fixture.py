"""The Task 81 joined multi-ministry development scenario, built in code.

Three ministries, five Sundays, and a roster of canonical people some of whom
belong to two ministries. Entirely **synthetic**: every person, role, date and
qualification here was invented to exercise the church-wide rule, and none of
it describes anyone's real ministry, roster or availability. That is the point
-- the authoritative data for two of the three real ministries this shape is
modelled on is not yet trustworthy, and a development trial must not pretend
otherwise.

The three ministries are named after the *shapes* they imitate, not after the
church's data:

- **Setup-like**: one lead plus a few interchangeable helpers per Sunday, a
  soft target, role variety on across the helper roles.
- **AV-like**: four distinct specialisms, scarce qualification, role variety
  deliberately off (rotating a specialist fights how the ministry works).
- **Kids-like**: two roles with a higher headcount, and a per-period serving
  maximum on several volunteers.

What the scenario is built to force, and where:

- ``SHARED_BOTH_SUNDAYS`` is qualified in the Setup-like and the AV-like
  ministry, and available on every Sunday. Both want them.
- ``SHARED_SCARCE`` is the **only** person qualified for an AV-like specialism
  and is also a Setup-like lead, so one Sunday genuinely cannot satisfy both.
- ``SHARED_SPREAD`` is eligible in two ministries across different Sundays, so
  the rule permits them to serve both -- on different dates.
- ``OVERSUBSCRIBED_SUNDAY`` asks for more Kids-like positions than the joined
  roster can fill once the church-wide rule is applied, so the run must report
  the shortfall rather than break the rule.

Ids are laid out in blocks so a failure message reads: ministry 1/2/3, roles
1xx/2xx/3xx, events 1xxx/2xxx/3xxx, requirements 1xxxx/2xxxx/3xxxx, people
9xxx, memberships in the same block as their ministry.
"""

from __future__ import annotations

import datetime

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.joined import JoinedMinistryInput
from app.scheduling.solver import SchedulingPolicy

__all__ = [
    "AV_LIKE",
    "FIRST_SUNDAY",
    "KIDS_LIKE",
    "SETUP_LIKE",
    "SHARED_BOTH_SUNDAYS",
    "SHARED_SCARCE",
    "SHARED_SPREAD",
    "SUNDAYS",
    "build_joined_scenario",
    "membership_for",
    "sunday",
]

FIRST_SUNDAY = datetime.date(2030, 1, 6)
SUNDAY_COUNT = 5

SETUP_LIKE = 1
AV_LIKE = 2
KIDS_LIKE = 3

# Roles, one block per ministry.
SETUP_LEAD = 101
SETUP_HELPER_A = 102
SETUP_HELPER_B = 103

AV_LEAD = 201
AV_SOUND = 202
AV_SLIDES = 203
AV_VIDEO = 204

KIDS_LEAD = 301
KIDS_HELPER = 302

# Canonical people. Every one of these is a Person.id, and the church-wide
# rule is about *these* numbers -- never about a membership and never about a
# name.
SHARED_BOTH_SUNDAYS = 9001
SHARED_SCARCE = 9002
SHARED_SPREAD = 9003
_SETUP_ONLY = tuple(range(9010, 9018))
_AV_ONLY = tuple(range(9030, 9038))
_KIDS_ONLY = tuple(range(9050, 9060))

#: Which Sunday index the scarce shared person is contended on: both the
#: Setup-like lead slot and the AV-like specialism want them here.
CONTENDED_SUNDAY = 0
#: The Sunday whose Kids-like demand exceeds what the joined roster can fill.
OVERSUBSCRIBED_SUNDAY = 4

_MEMBERSHIP_OFFSET = {SETUP_LIKE: 10_000, AV_LIKE: 20_000, KIDS_LIKE: 30_000}


def sunday(index: int) -> datetime.date:
    return FIRST_SUNDAY + datetime.timedelta(days=7 * index)


SUNDAYS = tuple(sunday(index) for index in range(SUNDAY_COUNT))


def membership_for(person_id: int, ministry_id: int) -> int:
    """This person's membership id in this ministry.

    Derived rather than stored so a test can name a membership without a
    lookup table, and so the two memberships of a shared person are visibly
    *different numbers for the same human* -- which is exactly the distinction
    the church-wide rule has to get right.
    """
    return _MEMBERSHIP_OFFSET[ministry_id] + person_id


def _event_id(ministry_id: int, index: int) -> int:
    return ministry_id * 1_000 + index


def _requirement_id(ministry_id: int, index: int, role_id: int) -> int:
    return ministry_id * 10_000 + index * 100 + (role_id % 100)


def _candidate(
    person_id: int,
    ministry_id: int,
    *,
    roles: tuple[int, ...],
    unavailable_indices: tuple[int, ...] = (),
    backup_indices: tuple[int, ...] = (),
    maximum: int | None = None,
) -> CandidateInput:
    availability = {}
    for index in range(SUNDAY_COUNT):
        event_id = _event_id(ministry_id, index)
        if index in unavailable_indices:
            availability[event_id] = AvailabilityState.UNAVAILABLE
        elif index in backup_indices:
            availability[event_id] = AvailabilityState.BACKUP
        else:
            availability[event_id] = AvailabilityState.AVAILABLE
    return CandidateInput(
        membership_id=membership_for(person_id, ministry_id),
        person_id=person_id,
        display_name=f"person-{person_id}",
        qualified_role_ids=frozenset(roles),
        availability_by_event=availability,
        max_assignments_in_period=maximum,
    )


def _requirements(
    ministry_id: int, plan: dict[int, tuple[tuple[int, int], ...]]
) -> tuple[RequirementInput, ...]:
    """``{sunday index: ((role id, count), ...)}`` -> requirement values."""
    rows = []
    for index in sorted(plan):
        for role_id, count in plan[index]:
            rows.append(
                RequirementInput(
                    requirement_id=_requirement_id(ministry_id, index, role_id),
                    event_id=_event_id(ministry_id, index),
                    event_date=sunday(index),
                    ministry_role_id=role_id,
                    ministry_id=ministry_id,
                    required_count=count,
                )
            )
    return tuple(rows)


def _setup_like(*, helper_count: int = 2) -> JoinedMinistryInput:
    plan = {
        index: ((SETUP_LEAD, 1), (SETUP_HELPER_A, helper_count))
        for index in range(SUNDAY_COUNT)
    }
    candidates = [
        # Qualified to lead, and the only lead-qualified person free on the
        # contended Sunday -- so the AV-like ministry wanting them that day is
        # a real conflict, not a cosmetic one.
        _candidate(SHARED_SCARCE, SETUP_LIKE, roles=(SETUP_LEAD,)),
        _candidate(
            SHARED_BOTH_SUNDAYS,
            SETUP_LIKE,
            roles=(SETUP_LEAD, SETUP_HELPER_A, SETUP_HELPER_B),
        ),
        # Eligible only in the second half of the period here, and only in the
        # first half in the Kids-like ministry, so the same person may serve
        # both -- on different Sundays.
        _candidate(
            SHARED_SPREAD,
            SETUP_LIKE,
            roles=(SETUP_HELPER_A,),
            unavailable_indices=(0, 1),
        ),
    ]
    for offset, person_id in enumerate(_SETUP_ONLY):
        roles = (SETUP_LEAD, SETUP_HELPER_A) if offset < 2 else (SETUP_HELPER_A,)
        candidates.append(
            _candidate(
                person_id,
                SETUP_LIKE,
                roles=roles,
                # The two other lead-qualified people are away on the
                # contended Sunday, which is what makes SHARED_SCARCE scarce.
                unavailable_indices=(CONTENDED_SUNDAY,) if offset < 2 else (),
            )
        )
    return JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=SETUP_LIKE,
            scheduling_period_id=SETUP_LIKE,
            ministry_id=SETUP_LIKE,
            requirements=_requirements(SETUP_LIKE, plan),
            candidates=tuple(candidates),
        ),
        policy=SchedulingPolicy(
            allow_no_response=False,
            target_assignments_per_candidate=2,
            balance_candidate_loads=True,
            role_variety_role_ids=frozenset({SETUP_HELPER_A, SETUP_HELPER_B}),
        ),
    )


def _av_like() -> JoinedMinistryInput:
    plan = {
        index: (
            (AV_LEAD, 1),
            (AV_SOUND, 1),
            (AV_SLIDES, 1),
            (AV_VIDEO, 1),
        )
        for index in range(SUNDAY_COUNT)
    }
    candidates = [
        # The only person cleared for the sound desk. Also a Setup-like lead,
        # and both ministries want them on the contended Sunday.
        _candidate(SHARED_SCARCE, AV_LIKE, roles=(AV_SOUND,)),
        _candidate(SHARED_BOTH_SUNDAYS, AV_LIKE, roles=(AV_SLIDES, AV_VIDEO)),
    ]
    specialisms = ((AV_LEAD,), (AV_LEAD, AV_SLIDES), (AV_VIDEO,), (AV_SLIDES,))
    for offset, person_id in enumerate(_AV_ONLY):
        candidates.append(
            _candidate(
                person_id,
                AV_LIKE,
                roles=specialisms[offset % len(specialisms)],
                backup_indices=(offset % SUNDAY_COUNT,),
            )
        )
    return JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=AV_LIKE,
            scheduling_period_id=AV_LIKE,
            ministry_id=AV_LIKE,
            requirements=_requirements(AV_LIKE, plan),
            candidates=tuple(candidates),
        ),
        # No documented target and no role variety: a specialist is not
        # rotated, and no number is invented for a ministry that has none.
        policy=SchedulingPolicy(
            allow_no_response=False,
            target_assignments_per_candidate=None,
            balance_candidate_loads=True,
            role_variety_role_ids=None,
        ),
    )


def _kids_like(*, final_sunday_helpers: int = 9) -> JoinedMinistryInput:
    plan = {
        index: (
            (KIDS_LEAD, 1),
            (
                KIDS_HELPER,
                final_sunday_helpers if index == OVERSUBSCRIBED_SUNDAY else 3,
            ),
        )
        for index in range(SUNDAY_COUNT)
    }
    candidates = [
        _candidate(
            SHARED_SPREAD,
            KIDS_LIKE,
            roles=(KIDS_HELPER,),
            unavailable_indices=(2, 3, 4),
        ),
    ]
    for offset, person_id in enumerate(_KIDS_ONLY):
        candidates.append(
            _candidate(
                person_id,
                KIDS_LIKE,
                roles=(KIDS_LEAD, KIDS_HELPER) if offset < 3 else (KIDS_HELPER,),
                # Several volunteers have agreed a personal maximum, which the
                # joined run must respect exactly as a separate run would.
                maximum=2 if offset % 3 == 0 else None,
            )
        )
    return JoinedMinistryInput(
        scheduling_input=SchedulingInput(
            schedule_version_id=KIDS_LIKE,
            scheduling_period_id=KIDS_LIKE,
            ministry_id=KIDS_LIKE,
            requirements=_requirements(KIDS_LIKE, plan),
            candidates=tuple(candidates),
        ),
        policy=SchedulingPolicy(
            allow_no_response=False,
            target_assignments_per_candidate=None,
            balance_candidate_loads=True,
            role_variety_role_ids=None,
        ),
    )


def build_joined_scenario(
    *,
    setup_helpers_per_sunday: int = 2,
    kids_final_sunday_helpers: int = 9,
) -> tuple[JoinedMinistryInput, ...]:
    """The three ministries, ready to hand to ``solve_joined_schedule``.

    The two keyword arguments exist so a test can change **one ministry's
    requirements** and observe the effect on another through the people they
    share -- which is the coupling a joined solve is supposed to have and a
    sequence of separate solves cannot express.
    """
    return (
        _setup_like(helper_count=setup_helpers_per_sunday),
        _av_like(),
        _kids_like(final_sunday_helpers=kids_final_sunday_helpers),
    )
