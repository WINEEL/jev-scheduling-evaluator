"""The neutral in-memory shape a real Setup period is read into.

Deliberately *not* the solver's input and *not* the ORM: a small set of plain
values that a spreadsheet reader can populate and that the adapter can turn
into a :class:`~app.scheduling.input.SchedulingInput`. Keeping this layer
separate is what lets every downstream module (adapter, checks, report) be
tested with tiny synthetic datasets and no spreadsheet at all.

Nothing here carries a real name into the repository: instances are built at
run time from git-ignored files.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from app.scheduling.input import AvailabilityState

__all__ = [
    "CANONICAL_ROLE_NAMES",
    "LEAD_ROLE_NAME",
    "VARIETY_ROLE_NAMES",
    "SETUP_TARGET_PER_PERIOD",
    "HistoricalRole",
    "HistoricalVolunteer",
    "HistoricalRequirement",
    "HistoricalAssignment",
    "HistoricalDataset",
]

#: The approved Setup role model (requirements §9). Order is display order.
LEAD_ROLE_NAME = "Setup Lead"
VARIETY_ROLE_NAMES = ("Setup 2", "Setup 3", "Setup 4", "Setup 5")
CANONICAL_ROLE_NAMES = (LEAD_ROLE_NAME, *VARIETY_ROLE_NAMES)

#: Setup's approved soft target -- assignments per volunteer per period
#: (requirements §9). A soft target, never a cap; supplied to the policy, not
#: written into the engine.
SETUP_TARGET_PER_PERIOD = 3


@dataclass(frozen=True, slots=True)
class HistoricalRole:
    """One ministry role, with a synthetic id local to this run."""

    role_id: int
    name: str
    is_lead: bool
    in_variety_set: bool
    #: Whether this role is a *staffing position* the schedule must fill.
    #: False marks a role the source records but never requires -- AV's
    #: Shadow, which is training that rides along with a real assignment.
    #: A non-staffing role must never become a requirement, or a schedule
    #: would report shortfalls for positions the ministry never needed.
    is_staffing_position: bool = True


@dataclass(frozen=True, slots=True)
class HistoricalVolunteer:
    """One Setup volunteer, with synthetic ids local to this run.

    ``lead_qualified`` comes from the Head-approved Setup Lead list. Everyone
    is treated as qualified for the interchangeable Setup 2-5 roles unless the
    source data says otherwise (requirements §9: "interchangeable for
    eligibility").
    """

    membership_id: int
    person_id: int
    display_name: str
    lead_qualified: bool
    #: If the source gives per-role Setup 2-5 qualification, the canonical role
    #: names this person may serve. Empty means "all Setup 2-5" (the default).
    restricted_support_roles: frozenset[str] = frozenset()
    #: The complete set of role names this person may serve, when the source
    #: states qualification per role rather than "lead, plus everything else".
    #:
    #: ``None`` keeps the Setup reading above. A frozenset -- **including an
    #: empty one** -- is authoritative and replaces it entirely: a ministry
    #: whose volunteers specialize (AV) has no "qualified for the rest by
    #: default" tier, and an empty set means this person may serve nothing,
    #: which is a real answer and not a missing one.
    qualified_role_names: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class HistoricalRequirement:
    """One required position for one Sunday and one role."""

    event_date: datetime.date
    role_name: str
    required_count: int = 1


@dataclass(frozen=True, slots=True)
class HistoricalAssignment:
    """One line of the church's actual final historical roster.

    ``membership_id`` is resolved against the volunteer list; an assignment
    whose person could not be matched keeps ``membership_id=None`` and is
    reported as unmatched rather than dropped.
    """

    event_date: datetime.date
    role_name: str
    display_name: str
    membership_id: int | None = None


@dataclass(slots=True)
class HistoricalDataset:
    """Everything one historical validation run needs, before adaptation.

    ``availability`` holds only what the spreadsheet *explicitly* marked, keyed
    ``(membership_id, event_date)``. A blank cell is deliberately absent here:
    the adapter turns absence into ``NO_RESPONSE`` and the run is configured
    with ``allow_no_response=True`` (requirements §9), so a blank reproduces
    the spreadsheet's "assume available" convention without being persisted as
    ``AVAILABLE``.
    """

    period_label: str
    sundays: tuple[datetime.date, ...]
    roles: tuple[HistoricalRole, ...]
    volunteers: tuple[HistoricalVolunteer, ...]
    requirements: tuple[HistoricalRequirement, ...]
    availability: dict[tuple[int, datetime.date], AvailabilityState] = field(
        default_factory=dict
    )
    historical_assignments: tuple[HistoricalAssignment, ...] = ()
    #: ``membership_id -> frozenset(dates)`` a church-wide conflict blocks, when
    #: real cross-ministry commitment data was supplied. Empty dict *and*
    #: ``conflict_data_available = False`` means "not knowable from the source".
    blocked_dates: dict[int, frozenset[datetime.date]] = field(default_factory=dict)
    #: How many availability cells the source left blank. Zero means the sheet
    #: answered every person/date pair explicitly, so nothing in it can confirm
    #: what a blank would have meant -- and a run must not assume.
    availability_blank_cells: int = 0
    #: Which availability tokens the grid actually contained, lowercased.
    availability_tokens_seen: frozenset[str] = frozenset()
    #: Free-text note the source carried against a date ("NLF", "Members
    #: Meeting"). Descriptive only -- never turned into a staffing rule.
    event_notes: dict[datetime.date, str] = field(default_factory=dict)
    #: Dates present in the source but deliberately held out of this solve
    #: (an ad-hoc event validated separately), and why.
    excluded_dates: dict[datetime.date, str] = field(default_factory=dict)
    #: Where ``lead_qualified`` came from, in words, for the report to quote.
    lead_qualification_source: str = "not supplied"
    #: The ministry this dataset belongs to, used for report headings only.
    ministry_label: str = "Setup"
    conflict_data_available: bool = False
    #: Free-text notes the reader wants surfaced in the final report
    #: (e.g. "target column present, value 3").
    source_notes: tuple[str, ...] = ()

    def role_by_name(self, name: str) -> HistoricalRole:
        for role in self.roles:
            if role.name == name:
                return role
        raise KeyError(name)
