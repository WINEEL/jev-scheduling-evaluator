"""Turn a :class:`HistoricalDataset` into the real solver's pure input.

This is the whole point of the architecture boundary Task 43 asks for: the
spreadsheet is adapted into a :class:`~app.scheduling.input.SchedulingInput`
in memory, and from there the *unmodified* ``solve_schedule`` runs exactly as
it would in production. No database, no ORM, no service layer.

Id scheme (all synthetic, local to one run): roles come pre-numbered on the
dataset; events are ``date -> 1000 + index``; requirements are numbered from
1; ``membership_id`` / ``person_id`` come from the dataset.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from types import MappingProxyType

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.solver import SchedulingPolicy

from scripts.historical_setup.model import (
    HistoricalDataset,
    SETUP_TARGET_PER_PERIOD,
)

__all__ = ["AdaptedInput", "build_scheduling_input"]

_MINISTRY_ID = 1
_SCHEDULE_VERSION_ID = 1
_SCHEDULING_PERIOD_ID = 1
_EVENT_ID_BASE = 1000


@dataclass(frozen=True, slots=True)
class AdaptedInput:
    """The pure input plus the lookup tables a report needs to name things."""

    scheduling_input: SchedulingInput
    policy: SchedulingPolicy
    event_id_by_date: dict[datetime.date, int]
    date_by_event_id: dict[int, datetime.date]
    role_name_by_id: dict[int, str]
    role_id_by_name: dict[str, int]
    requirement_role_name: dict[int, str]
    requirement_date: dict[int, datetime.date]
    display_name_by_membership: dict[int, str]
    lead_role_id: int
    variety_role_ids: frozenset[int]


def build_scheduling_input(
    dataset: HistoricalDataset,
    *,
    allow_no_response: bool = True,
    target_assignments_per_candidate: int | None = SETUP_TARGET_PER_PERIOD,
    balance_candidate_loads: bool = True,
    optimize_role_variety: bool = True,
) -> AdaptedInput:
    """Build the solver input and this run's policy.

    Every policy value is the **caller's** decision, because each one is a
    ministry's answer rather than a fact about scheduling:

    ``allow_no_response`` -- Setup's convention is that a blank cell means
    "assume available", but a *particular* sheet only evidences that
    convention if it actually contains blanks; a sheet that answers every cell
    says nothing about what a blank would have meant, and a run over it must
    not quietly assume the permissive reading.

    ``target_assignments_per_candidate`` -- Setup's approved soft target is 3.
    ``None`` means the ministry has no documented target, which is not the same
    as a target of zero: it aims at no particular number rather than inventing
    one the ministry never agreed to.

    ``balance_candidate_loads`` -- whether to spread the work evenly. On by
    default and **independent of the target** (Task 45): a ministry with no
    documented number still wants a sensible spread, and used to have to
    invent one to get it.

    ``optimize_role_variety`` -- spreading a volunteer across roles is right
    where the roles are interchangeable (Setup 2-5) and wrong where people
    specialize (AV), so it is opt-in per run. When on, the variety set is the
    dataset's non-lead roles; when off, no role-variety preference applies.

    The Setup defaults are kept so an existing Setup run is unchanged; a
    ministry that differs states its own.
    """
    event_id_by_date = {
        d: _EVENT_ID_BASE + i for i, d in enumerate(dataset.sundays)
    }
    date_by_event_id = {v: k for k, v in event_id_by_date.items()}

    role_name_by_id = {r.role_id: r.name for r in dataset.roles}
    role_id_by_name = {r.name: r.role_id for r in dataset.roles}
    lead_role_id = next(r.role_id for r in dataset.roles if r.is_lead)
    variety_role_ids = frozenset(
        r.role_id for r in dataset.roles if r.in_variety_set
    )

    # -- Requirements ----------------------------------------------------
    requirements: list[RequirementInput] = []
    requirement_role_name: dict[int, str] = {}
    requirement_date: dict[int, datetime.date] = {}
    next_requirement_id = 1
    for req in sorted(
        dataset.requirements, key=lambda x: (x.event_date, x.role_name)
    ):
        if req.event_date not in event_id_by_date:
            raise ValueError(
                f"requirement references {req.event_date}, not a period Sunday"
            )
        role_id = role_id_by_name.get(req.role_name)
        if role_id is None:
            raise ValueError(f"requirement references unknown role {req.role_name!r}")
        rid = next_requirement_id
        next_requirement_id += 1
        requirements.append(
            RequirementInput(
                requirement_id=rid,
                event_id=event_id_by_date[req.event_date],
                event_date=req.event_date,
                ministry_role_id=role_id,
                ministry_id=_MINISTRY_ID,
                required_count=req.required_count,
                role_is_active=True,
            )
        )
        requirement_role_name[rid] = req.role_name
        requirement_date[rid] = req.event_date

    # -- Candidates -----------------------------------------------------
    support_role_ids = frozenset(
        r.role_id for r in dataset.roles if not r.is_lead
    )
    dataset_role_names = {r.name for r in dataset.roles}
    candidates: list[CandidateInput] = []
    display_name_by_membership: dict[int, str] = {}
    for vol in dataset.volunteers:
        display_name_by_membership[vol.membership_id] = vol.display_name
        qualified: set[int] = set()
        if vol.qualified_role_names is not None:
            # Authoritative per-role qualification, with no "everything else by
            # default" tier behind it. A role this dataset does not define is
            # an error rather than a silent widening.
            unknown = vol.qualified_role_names - dataset_role_names
            if unknown:
                raise ValueError(
                    f"volunteer {vol.membership_id} is marked qualified for"
                    f" roles this dataset does not define: {sorted(unknown)}"
                )
            qualified = {
                role_id_by_name[name] for name in vol.qualified_role_names
            }
        else:
            if vol.lead_qualified:
                qualified.add(lead_role_id)
            if vol.restricted_support_roles:
                for name in vol.restricted_support_roles:
                    if name in role_id_by_name:
                        qualified.add(role_id_by_name[name])
            else:
                qualified |= support_role_ids

        availability_by_event: dict[int, AvailabilityState] = {}
        for (membership_id, event_date), state in dataset.availability.items():
            if membership_id != vol.membership_id:
                continue
            event_id = event_id_by_date.get(event_date)
            if event_id is not None:
                availability_by_event[event_id] = state

        blocked = dataset.blocked_dates.get(vol.membership_id, frozenset())

        candidates.append(
            CandidateInput(
                membership_id=vol.membership_id,
                person_id=vol.person_id,
                display_name=vol.display_name,
                qualified_role_ids=frozenset(qualified),
                availability_by_event=MappingProxyType(availability_by_event),
                blocked_dates=frozenset(blocked),
            )
        )

    scheduling_input = SchedulingInput(
        schedule_version_id=_SCHEDULE_VERSION_ID,
        scheduling_period_id=_SCHEDULING_PERIOD_ID,
        ministry_id=_MINISTRY_ID,
        requirements=tuple(requirements),
        candidates=tuple(candidates),
        existing_assignments=(),
    )

    policy = SchedulingPolicy(
        allow_no_response=allow_no_response,
        target_assignments_per_candidate=target_assignments_per_candidate,
        balance_candidate_loads=balance_candidate_loads,
        role_variety_role_ids=variety_role_ids if optimize_role_variety else None,
    )

    return AdaptedInput(
        scheduling_input=scheduling_input,
        policy=policy,
        event_id_by_date=event_id_by_date,
        date_by_event_id=date_by_event_id,
        role_name_by_id=role_name_by_id,
        role_id_by_name=role_id_by_name,
        requirement_role_name=requirement_role_name,
        requirement_date=requirement_date,
        display_name_by_membership=display_name_by_membership,
        lead_role_id=lead_role_id,
        variety_role_ids=variety_role_ids if optimize_role_variety else frozenset(),
    )
