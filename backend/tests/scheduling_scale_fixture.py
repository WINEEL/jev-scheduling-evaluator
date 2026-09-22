"""A synthetic ministry quarter shaped like a real one, built from a seed.

Task 68. The performance cliff the solver used to hit only appeared at the
scale of a genuine quarter -- roughly fifty volunteers, thirteen Sundays, ten
specialized positions each -- which is larger than any hand-written fixture in
the suite and, in its real form, private church data that must never enter the
repository.

So this reproduces the *shape* and nothing else: counts, sparsity and the mix
of availability answers, generated deterministically from a seed. It carries
no names, no dates anyone recognizes and no real qualification list, and it is
the input both the equivalence tests and the benchmark run against.

Deterministic by construction: the same seed yields the same input on every
machine, which is what lets a timing or an optimum be compared across a change.
"""

from __future__ import annotations

import datetime
import random
from types import MappingProxyType

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    RequirementInput,
    SchedulingInput,
)

__all__ = ["build_scale_input", "KIDS_SHAPE"]

#: The structural profile measured from a real thirteen-Sunday ministry
#: quarter: how many people answered, how many positions needed filling, and
#: in what proportion the three availability answers came back.
KIDS_SHAPE = {
    "candidates": 51,
    "events": 13,
    "roles": 10,
    "available_share": 0.33,
    "backup_share": 0.09,
    "min_roles_per_candidate": 0,
    "max_roles_per_candidate": 3,
}

_MINISTRY_ID = 1
_FIRST_EVENT = datetime.date(2030, 1, 6)


def build_scale_input(
    *,
    seed: int = 0,
    candidates: int = KIDS_SHAPE["candidates"],
    events: int = KIDS_SHAPE["events"],
    roles: int = KIDS_SHAPE["roles"],
    available_share: float = KIDS_SHAPE["available_share"],
    backup_share: float = KIDS_SHAPE["backup_share"],
    min_roles_per_candidate: int = KIDS_SHAPE["min_roles_per_candidate"],
    max_roles_per_candidate: int = KIDS_SHAPE["max_roles_per_candidate"],
) -> SchedulingInput:
    """One synthetic quarter: ``events x roles`` positions, one person each.

    Qualifications are sparse on purpose. A ministry with specialized
    positions does not have everyone eligible for everything, and that
    sparsity is what makes the balance pass hard -- a dense roster has so many
    interchangeable options that any spread is reachable, while a sparse one
    forces the solver to prove that no better distribution exists.

    Some candidates are generated qualified for nothing, which is realistic:
    people answer the availability form whose role history the ministry has
    never recorded. They are still candidates; they simply cannot be placed.
    """
    rng = random.Random(seed)

    role_ids = list(range(1, roles + 1))
    # A caller asking for a narrower ministry than the default profile must not
    # be able to request more roles per person than the ministry has.
    max_roles_per_candidate = min(max_roles_per_candidate, roles)
    min_roles_per_candidate = min(min_roles_per_candidate, max_roles_per_candidate)
    event_ids = list(range(1000, 1000 + events))
    event_dates = {
        event_id: _FIRST_EVENT + datetime.timedelta(days=7 * index)
        for index, event_id in enumerate(event_ids)
    }

    requirements = []
    requirement_id = 1
    for event_id in event_ids:
        for role_id in role_ids:
            requirements.append(
                RequirementInput(
                    requirement_id=requirement_id,
                    event_id=event_id,
                    event_date=event_dates[event_id],
                    ministry_role_id=role_id,
                    ministry_id=_MINISTRY_ID,
                    required_count=1,
                )
            )
            requirement_id += 1

    built = []
    for membership_id in range(1, candidates + 1):
        how_many = rng.randint(min_roles_per_candidate, max_roles_per_candidate)
        qualified = frozenset(rng.sample(role_ids, how_many)) if how_many else frozenset()

        answers: dict[int, AvailabilityState] = {}
        for event_id in event_ids:
            roll = rng.random()
            if roll < available_share:
                answers[event_id] = AvailabilityState.AVAILABLE
            elif roll < available_share + backup_share:
                answers[event_id] = AvailabilityState.BACKUP
            else:
                answers[event_id] = AvailabilityState.UNAVAILABLE

        built.append(
            CandidateInput(
                membership_id=membership_id,
                person_id=membership_id,
                display_name=f"Candidate {membership_id}",
                qualified_role_ids=qualified,
                availability_by_event=MappingProxyType(answers),
            )
        )

    return SchedulingInput(
        schedule_version_id=1,
        scheduling_period_id=1,
        ministry_id=_MINISTRY_ID,
        requirements=tuple(requirements),
        candidates=tuple(built),
    )
