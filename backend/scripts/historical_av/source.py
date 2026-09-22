"""Read a real AV quarter -- availability *and* prepared assignments, one grid.

AV's sheet is not shaped like Setup's. One row is one Sunday, and that row
carries both halves of the problem::

    (blank) , Person A , Person B , ... , (gap) , Lead , Soundboard , Slides , Video , Shadow , Notes
    10/4/26 ,   YES    ,   NO     , ... ,       , ...  ,    ...     ,  ...   , ...   ,        ,

The left block is availability; the right block is what the church actually
prepared. A reader that assumed one or the other would silently take a column
of names for a column of availability tokens, so the two blocks are located
explicitly: the assignment block starts at the first header cell that names an
AV role, and everything between the date column and that point is a volunteer.

Below the dated rows a sheet may carry summary and lookup rows (a "Total
Serving" tally, a name/number index). Those are not dates and are skipped.
"""

from __future__ import annotations

import collections
import csv
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.scheduling.input import AvailabilityState

from scripts.historical_av.roles import (
    AV_STAFFING_ROLE_NAMES,
    SHADOW_ROLE_NAME,
    build_av_roles,
    try_normalize_av_role_name,
)
from scripts.historical_av.workbook import Workbook, excel_serial_to_date
from scripts.historical_setup.model import (
    HistoricalAssignment,
    HistoricalDataset,
    HistoricalRequirement,
    HistoricalVolunteer,
)

__all__ = [
    "AvSourceError",
    "AvSemantics",
    "AV_SEMANTICS",
    "ShadowRecord",
    "TabVerdict",
    "AvQuarter",
    "read_quarter_from_csv",
    "read_quarter_from_sheet",
    "classify_workbook_tabs",
    "build_observed_eligibility",
    "build_dataset",
]


class AvSourceError(RuntimeError):
    """An AV source could not be interpreted without guessing."""


# --------------------------------------------------------------------------
# Availability semantics
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AvSemantics:
    """What an AV availability cell means.

    AV answers in words, not Setup's ``O``/``X``, and the two vocabularies are
    kept apart deliberately: reusing Setup's table here would mean an AV sheet
    was being read through another ministry's convention.

    A **blank** writes no availability row at all, so the person is
    ``NO_RESPONSE`` and the run's policy -- not this reader -- decides what
    that means. Nothing in the AV sheet states what a blank stands for, and
    guessing "available" would put someone on a schedule on the strength of an
    empty cell.
    """

    available_tokens: frozenset[str] = frozenset({"yes", "y", "available", "x", "✓"})
    unavailable_tokens: frozenset[str] = frozenset({"no", "n", "unavailable", "away"})

    def classify(self, raw: str | None) -> AvailabilityState | None:
        token = (raw or "").strip().lower()
        if not token:
            return None
        if token in self.unavailable_tokens:
            return AvailabilityState.UNAVAILABLE
        if token in self.available_tokens:
            return AvailabilityState.AVAILABLE
        raise AvSourceError(f"unrecognized AV availability token: {raw!r}")


AV_SEMANTICS = AvSemantics()


# --------------------------------------------------------------------------
# One quarter, as read
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    """Someone shadowing on one date, and the role they shadowed.

    Kept for reporting only. A shadow record never becomes a required
    position, and it never grants role eligibility: watching a role is not
    evidence of being cleared to serve it, and the source says nothing more
    than that they watched.
    """

    event_date: datetime.date
    display_name: str
    shadowed_role: str | None
    raw: str


@dataclass(slots=True)
class AvQuarter:
    """One quarter tab: roster, availability, prepared assignments, shadows."""

    label: str
    volunteers: tuple[str, ...]
    dates: tuple[datetime.date, ...]
    #: The staffing roles this sheet's header defines -- the positions the
    #: quarter is expected to fill, independent of which ones got filled.
    staffing_roles: tuple[str, ...] = AV_STAFFING_ROLE_NAMES
    #: ``(display_name, date) -> state``; a blank cell is absent, not a row.
    availability: dict[tuple[str, datetime.date], AvailabilityState] = field(
        default_factory=dict
    )
    #: ``(date, role) -> display_name`` for the four staffing roles.
    assignments: dict[tuple[datetime.date, str], str] = field(default_factory=dict)
    shadows: tuple[ShadowRecord, ...] = ()
    blank_cells: int = 0
    tokens_seen: frozenset[str] = frozenset()
    notes: dict[datetime.date, str] = field(default_factory=dict)


_SHADOW_FORM = re.compile(r"^(?P<name>[^(]+?)\s*(?:\(\s*(?P<role>[^)]*?)\s*\))?$")
_DATE_TEXT = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")


def _parse_date_cell(raw: str) -> datetime.date | None:
    """A dated row's first cell: text ``M/D/YY`` or an Excel serial."""
    text = (raw or "").strip()
    if not text:
        return None
    match = _DATE_TEXT.match(text)
    if match:
        month, day, year = (int(g) for g in match.groups())
        return datetime.date(year + 2000 if year < 100 else year, month, day)
    return excel_serial_to_date(text)


def _split_blocks(header: list[str]) -> tuple[list[tuple[int, str]], dict[str, int]]:
    """Locate the volunteer block and the role block in one header row.

    The assignment block begins at the first header cell naming an AV role;
    the volunteer block is everything before it, excluding the date column.
    """
    role_columns: dict[str, int] = {}
    first_role: int | None = None
    for index, cell in enumerate(header):
        if index == 0:
            continue
        role = try_normalize_av_role_name(cell)
        if role is None:
            continue
        if first_role is None:
            first_role = index
        role_columns.setdefault(role, index)

    if first_role is None:
        raise AvSourceError("no AV role columns found in the header row")
    missing = [r for r in AV_STAFFING_ROLE_NAMES if r not in role_columns]
    if missing:
        raise AvSourceError(
            f"header is missing AV staffing roles: {missing}"
        )

    volunteers = [
        (index, cell.strip())
        for index, cell in enumerate(header)
        if 0 < index < first_role and cell.strip()
    ]
    if len(volunteers) < 2:
        raise AvSourceError(
            f"expected >=2 volunteer columns before the role block,"
            f" found {len(volunteers)}"
        )
    return volunteers, role_columns


def _read_grid(label: str, rows: list[list[str]]) -> AvQuarter:
    if not rows:
        raise AvSourceError(f"{label}: sheet is empty")
    header = [c.strip() for c in rows[0]]
    volunteers, role_columns = _split_blocks(header)

    seen: set[str] = set()
    for _index, name in volunteers:
        key = name.lower()
        if key in seen:
            raise AvSourceError(f"{label}: duplicate volunteer column {name!r}")
        seen.add(key)

    shadow_column = role_columns.get(SHADOW_ROLE_NAME)
    notes_column = next(
        (
            i
            for i, cell in enumerate(header)
            if cell.strip().lower() in {"notes", "note", "comment", "comments"}
        ),
        None,
    )

    quarter = AvQuarter(
        label=label,
        volunteers=tuple(name for _i, name in volunteers),
        dates=(),
        staffing_roles=tuple(
            role for role in AV_STAFFING_ROLE_NAMES if role in role_columns
        ),
    )
    dates: list[datetime.date] = []
    tokens: set[str] = set()
    shadows: list[ShadowRecord] = []
    blanks = 0

    for row_number, raw_row in enumerate(rows[1:], start=2):
        row = [c.strip() for c in raw_row]
        day = _parse_date_cell(row[0] if row else "")
        if day is None:
            # Summary tallies and lookup tables live below the dated rows.
            continue
        if day in dates:
            raise AvSourceError(f"{label} row {row_number}: duplicate date {day}")
        dates.append(day)

        for index, name in volunteers:
            cell = row[index] if index < len(row) else ""
            if cell:
                tokens.add(cell.lower())
            else:
                blanks += 1
            try:
                state = AV_SEMANTICS.classify(cell)
            except AvSourceError as exc:
                raise AvSourceError(f"{label} row {row_number}: {exc}") from exc
            if state is not None:
                quarter.availability[(name, day)] = state

        for role in AV_STAFFING_ROLE_NAMES:
            index = role_columns[role]
            person = row[index] if index < len(row) else ""
            if person:
                quarter.assignments[(day, role)] = person

        if shadow_column is not None and shadow_column < len(row) and row[shadow_column]:
            shadows.append(_parse_shadow(day, row[shadow_column]))
        if notes_column is not None and notes_column < len(row) and row[notes_column]:
            quarter.notes[day] = row[notes_column]

    if not dates:
        raise AvSourceError(f"{label}: no dated rows found")

    quarter.dates = tuple(sorted(dates))
    quarter.shadows = tuple(shadows)
    quarter.blank_cells = blanks
    quarter.tokens_seen = frozenset(tokens)
    return quarter


def _parse_shadow(day: datetime.date, raw: str) -> ShadowRecord:
    """Read ``Name`` or ``Name (AV - Soundboard)`` into a shadow record."""
    match = _SHADOW_FORM.match(raw.strip())
    if not match:
        return ShadowRecord(day, raw.strip(), None, raw)
    name = match.group("name").strip()
    parenthetical = match.group("role")
    role = None
    if parenthetical:
        # "AV - Soundboard" -> "Soundboard"; an unreadable qualifier is kept
        # raw rather than guessed, since nothing downstream depends on it.
        tail = parenthetical.split("-")[-1]
        role = try_normalize_av_role_name(tail)
    return ShadowRecord(day, name, role, raw)


def read_quarter_from_csv(path: Path, *, label: str | None = None) -> AvQuarter:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.reader(handle)]
    return _read_grid(label or Path(path).stem, rows)


def read_quarter_from_sheet(workbook: Workbook, name: str) -> AvQuarter:
    sheet = workbook.sheet(name)
    return _read_grid(name, [list(r) for r in sheet.rows])


# --------------------------------------------------------------------------
# Workbook tabs: which are usable evidence, and which are refused
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TabVerdict:
    name: str
    usable: bool
    shape: str
    dated_rows: int
    reason: str = ""


def classify_workbook_tabs(workbook: Workbook) -> list[TabVerdict]:
    """Decide, per tab, whether its assignments are safe to mine.

    Two shapes are accepted: the wide quarter grid, and the long
    ``Date, Lead, Soundboard, Slides, Video`` history. A tab is refused unless
    it names **all four** staffing roles -- a tab carrying only some of them
    describes different work (the workbook has a ``Date, Video, Audio``
    post-production log), and reading it as AV duty would credit people with
    roles they have never served.
    """
    verdicts: list[TabVerdict] = []
    for name in workbook.sheet_names:
        sheet = workbook.sheet(name)
        rows = [r for r in sheet.rows if any(c for c in r)]
        if not rows:
            verdicts.append(TabVerdict(name, False, "empty", 0, "no rows"))
            continue
        header = [c.strip() for c in rows[0]]
        found = {
            role: i
            for i, cell in enumerate(header)
            if (role := try_normalize_av_role_name(cell)) is not None
        }
        dated = sum(1 for r in rows[1:] if _parse_date_cell(r[0] if r else "") is not None)
        missing = [r for r in AV_STAFFING_ROLE_NAMES if r not in found]
        if missing:
            verdicts.append(
                TabVerdict(
                    name, False, "unrecognized", dated,
                    f"does not name all four AV staffing roles (missing {missing})",
                )
            )
            continue
        if not dated:
            verdicts.append(
                TabVerdict(name, False, "no dated rows", 0, "no date column values")
            )
            continue
        first_role = min(found.values())
        shape = "quarter-grid" if first_role > 1 else "long-history"
        verdicts.append(TabVerdict(name, True, shape, dated))
    return verdicts


def build_observed_eligibility(
    workbook: Workbook,
    verdicts: list[TabVerdict],
    roster: set[str],
) -> tuple[dict[str, frozenset[str]], dict[str, int], list[str]]:
    """The OBSERVED ROLE-ELIGIBILITY PROXY.

    Someone is treated as eligible for a role because they have actually been
    assigned that role in a supplied historical AV schedule. This is a proxy
    standing in for a Head-maintained qualification matrix that does not
    exist, and it is wrong in a knowable direction: it can only under-report.
    Someone Frank would clear for a role they have not yet served does not
    appear, and no amount of history can reveal them.

    Only the four staffing roles count. A ``Shadow`` cell is ignored on
    purpose, and so is any column outside AV's vocabulary -- the workbook's
    long tabs carry an ``AV setup`` column that is not one of the four.

    Returns the proxy, the per-role evidence counts, and the tab labels used.
    """
    observed: dict[str, set[str]] = collections.defaultdict(set)
    evidence: dict[str, int] = collections.Counter()
    used: list[str] = []
    lowered = {name.lower(): name for name in roster}

    for verdict in verdicts:
        if not verdict.usable:
            continue
        sheet = workbook.sheet(verdict.name)
        rows = [r for r in sheet.rows if any(c for c in r)]
        header = [c.strip() for c in rows[0]]
        columns = {
            role: i
            for i, cell in enumerate(header)
            if (role := try_normalize_av_role_name(cell)) is not None
            and role in AV_STAFFING_ROLE_NAMES
        }
        matched = 0
        for row in rows[1:]:
            if _parse_date_cell(row[0] if row else "") is None:
                continue
            for role, index in columns.items():
                cell = (row[index] if index < len(row) else "").strip()
                canonical = lowered.get(cell.lower())
                if canonical is None:
                    continue
                observed[canonical].add(role)
                evidence[role] += 1
                matched += 1
        if matched:
            used.append(verdict.name)

    return (
        {name: frozenset(roles) for name, roles in observed.items()},
        dict(evidence),
        used,
    )


# --------------------------------------------------------------------------
# Dataset assembly
# --------------------------------------------------------------------------


def build_dataset(
    quarter: AvQuarter,
    observed: dict[str, frozenset[str]],
    *,
    period_label: str,
    eligibility_source: str,
) -> HistoricalDataset:
    """Assemble the neutral IR the Task 43 pipeline already knows how to run.

    Requirements come from the roles the quarter's prepared schedule actually
    staffs, **per date** -- so a date that genuinely needed something different
    is represented as that date's staffing rather than as a rule. ``Shadow``
    is never a requirement.
    """
    roles = build_av_roles()
    volunteers: list[HistoricalVolunteer] = []
    membership_by_name: dict[str, int] = {}
    for index, name in enumerate(quarter.volunteers, start=1):
        membership_by_name[name] = index
        volunteers.append(
            HistoricalVolunteer(
                membership_id=index,
                person_id=100 + index,
                display_name=name,
                lead_qualified=False,
                qualified_role_names=observed.get(name, frozenset()),
            )
        )

    # Every date requires every staffing position the sheet defines. An empty
    # cell is therefore a position the church could not fill, not a position it
    # did not need: deriving requirements from the *filled* cells instead would
    # erase the shortfall, and a schedule that hides what it could not staff is
    # worse than one that reports it. Genuinely event-specific staffing needs
    # its own evidence in the source, which a blank cell is not.
    requirements = [
        HistoricalRequirement(day, role, 1)
        for day in quarter.dates
        for role in quarter.staffing_roles
    ]

    availability = {
        (membership_by_name[name], day): state
        for (name, day), state in quarter.availability.items()
        if name in membership_by_name
    }

    historical = tuple(
        HistoricalAssignment(
            event_date=day,
            role_name=role,
            display_name=person,
            membership_id=membership_by_name.get(person),
        )
        for (day, role), person in sorted(quarter.assignments.items())
    )

    notes = [
        f"availability tokens present: {sorted(quarter.tokens_seen) or '(none)'}",
        f"blank availability cells: {quarter.blank_cells}",
        f"role eligibility source: {eligibility_source}",
        f"shadow records preserved: {len(quarter.shadows)}"
        " (never a required position, never evidence of qualification)",
        "cross-ministry conflict data: NOT AVAILABLE from the supplied files",
    ]

    return HistoricalDataset(
        period_label=period_label,
        sundays=quarter.dates,
        roles=roles,
        volunteers=tuple(volunteers),
        requirements=tuple(requirements),
        availability=availability,
        historical_assignments=historical,
        availability_blank_cells=quarter.blank_cells,
        availability_tokens_seen=quarter.tokens_seen,
        event_notes=dict(quarter.notes),
        lead_qualification_source=eligibility_source,
        conflict_data_available=False,
        source_notes=tuple(notes),
        ministry_label="AV",
    )
