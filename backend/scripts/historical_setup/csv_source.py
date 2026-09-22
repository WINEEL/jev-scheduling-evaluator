"""Read a real Setup period from git-ignored CSV/TSV files into the IR.

**This is the only module that touches a real spreadsheet.** It prints
*structure* -- filenames, row counts, column headers, detected shape, date
ranges, people/event counts -- and never prints cell contents or names. It
writes nothing.

The exact column layout of a church's Setup sheet is not knowable in advance,
so this reader is driven by a :class:`SourceConfig` and falls back to
autodetection for the common "wide grid" shapes:

*Availability grid* -- one row per volunteer, one column per Sunday::

    Name , Lead , 2025-10-05 , 2025-10-12 , ...
    ...  ,  Y   ,     X      ,            , ...

*Availability grid, date-major* -- the same data transposed, which is how a
sheet meant to be read a week at a time is usually laid out::

    (blank) , Person A , Person B , ... , Notes
    10/5/25 ,    O     ,    X     , ... , WBS   , X , Not Available

The trailing pair on a body row is a **legend**, not data: it states what a
token means. The reader checks the legend against the semantics table it was
given and stops if the two disagree, so a sheet whose ``X`` meant the opposite
of the assumed convention cannot be read silently.

*Schedule grid* -- one row per Sunday, one column per role::

    Date       , Setup Lead , Setup 2 , Setup 3 , Setup 4 , Setup 5
    2025-10-05 ,   ...

*Long schedule* -- one row per assignment::

    Date , Role , Person

If the reader meets a shape or token it cannot explain, it raises
:class:`SourceError`; the runner turns that into a BLOCKED report rather than
guessing.
"""

from __future__ import annotations

import csv
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.scheduling.input import AvailabilityState

from scripts.historical_setup.model import (
    HistoricalAssignment,
    HistoricalDataset,
    HistoricalRequirement,
    HistoricalRole,
    HistoricalVolunteer,
)
from scripts.historical_setup.parsing import (
    SETUP_AVAILABILITY_SEMANTICS,
    AvailabilitySemantics,
    parse_schedule_date,
)
from scripts.role_vocabulary import SETUP_VOCABULARY, RoleVocabulary, build_roles_for

__all__ = [
    "SourceError",
    "SourceConfig",
    "FileStructure",
    "inspect_sources",
    "load_dataset",
    "build_roles",
]


class SourceError(RuntimeError):
    """A real source file could not be interpreted without guessing."""


@dataclass(slots=True)
class SourceConfig:
    """How to read one directory of Setup CSVs. Every field has a default the
    autodetector can fill; a caller overrides only what it must.
    """

    period_label: str = "unlabelled Setup period"
    year_hint: int = datetime.date.today().year
    availability_file: str | None = None
    schedule_file: str | None = None
    lead_qualification_file: str | None = None
    conflicts_file: str | None = None
    name_column_aliases: tuple[str, ...] = (
        "name", "volunteer", "person", "member", "who",
    )
    lead_column_aliases: tuple[str, ...] = (
        "lead", "setup lead", "lead qualified", "lead-qualified", "leader",
    )
    date_column_aliases: tuple[str, ...] = ("date", "sunday", "service", "week")
    role_column_aliases: tuple[str, ...] = ("role", "position", "slot")
    #: Which ministry's roles this source is read against. Supplied by the
    #: caller, who knows the ministry's approved positions; nothing here infers
    #: one from a spreadsheet, and an unknown label still stops the run.
    #: Setup's is the default, so every caller written before vocabularies
    #: existed reads exactly what it read before.
    role_vocabulary: RoleVocabulary = SETUP_VOCABULARY
    availability_semantics: AvailabilitySemantics = SETUP_AVAILABILITY_SEMANTICS
    #: Tokens in a lead column that mean "qualified".
    lead_true_tokens: frozenset[str] = frozenset(
        {"y", "yes", "true", "1", "x", "lead", "✓", "qualified"}
    )
    headcount_per_sunday: int | None = None
    notes_column_aliases: tuple[str, ...] = ("notes", "note", "comment", "comments")
    #: Dates to read but hold out of this solve, mapped to the reason. Used for
    #: an ad-hoc event that must be validated on its own terms rather than
    #: folded into the recurring-Sunday run.
    exclude_dates: dict[datetime.date, str] = field(default_factory=dict)
    #: Take Lead qualification from the *observed* Setup Lead column of the
    #: final schedule. A deliberate, caller-requested proxy for a real
    #: Head-maintained list -- never a default, and never authoritative.
    lead_from_schedule: bool = False
    #: Restrict the run to these dates only. Empty means "every date found".
    only_dates: frozenset[datetime.date] = frozenset()
    #: Take each date's required positions from the roles the final schedule
    #: actually *staffed*, rather than from the grid's full set of role columns.
    #:
    #: Off by default, and deliberately so: on a recurring Sunday an empty cell
    #: is a position the church could not fill, and turning it into a position
    #: the church did not need would quietly hide the shortfall. It is correct
    #: only for an ad-hoc event, whose staffing is whatever that one event
    #: called for -- and even then it describes that event alone.
    requirements_from_filled_cells: bool = False


@dataclass(slots=True)
class FileStructure:
    """Structure of one source file. ``columns`` is safe to print.

    A date-major availability grid puts *people* in its header, so the header
    is redacted here rather than at the print site: the runner promises a
    name-free stdout, and a promise kept by every caller remembering to redact
    is not kept.
    """

    path: str
    rows: int
    columns: list[str]
    detected_shape: str
    notes: list[str] = field(default_factory=list)
    #: Header cells withheld because they are volunteer names.
    redacted_columns: int = 0


# --------------------------------------------------------------------------
# Low-level CSV helpers
# --------------------------------------------------------------------------


def _read_rows(path: Path) -> list[list[str]]:
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [row for row in csv.reader(fh, delimiter=delimiter)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _looks_like_date(text: str, year_hint: int) -> bool:
    try:
        parse_schedule_date(text, year_hint=year_hint)
        return True
    except ValueError:
        return False


def _match_alias(header: str, aliases: tuple[str, ...]) -> bool:
    h = _norm(header)
    return any(h == a or h.startswith(a) for a in aliases)


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


def _csv_files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {".csv", ".tsv", ".tab"}
    )


def inspect_sources(directory: Path, config: SourceConfig) -> list[FileStructure]:
    """Describe every CSV in ``directory`` -- structure only, no contents."""
    out: list[FileStructure] = []
    for path in _csv_files(directory):
        rows = _read_rows(path)
        header = rows[0] if rows else []
        body = rows[1:]
        shape, notes = _classify(header, body, config)
        columns = [c.strip() for c in header]
        redacted = 0
        if shape == "availability-grid-transposed":
            people = {i for i, _ in _person_columns(header, config)}
            redacted = len(people)
            columns = [
                "<volunteer>" if i in people else c
                for i, c in enumerate(columns)
            ]
        out.append(
            FileStructure(
                path=path.name,
                rows=len(body),
                columns=columns,
                detected_shape=shape,
                notes=notes,
                redacted_columns=redacted,
            )
        )
    return out


def _notes_index(header: list[str], config: SourceConfig) -> int | None:
    for i, h in enumerate(header):
        if _match_alias(h, config.notes_column_aliases):
            return i
    return None


def _person_columns(
    header: list[str], config: SourceConfig
) -> list[tuple[int, str]]:
    """The volunteer-name columns of a date-major grid, left to right.

    Everything after the notes column -- or after the first empty header once
    names have started -- is legend or padding, not a person.
    """
    stop = _notes_index(header, config)
    out: list[tuple[int, str]] = []
    for i, raw in enumerate(header):
        if i == 0 or (stop is not None and i >= stop):
            continue
        name = raw.strip()
        if not name:
            if out:
                break
            continue
        out.append((i, name))
    return out


def _date_rows(body: list[list[str]], config: SourceConfig) -> int:
    return sum(
        1 for row in body if row and _looks_like_date(row[0], config.year_hint)
    )


def _names_a_role(header: str, config: SourceConfig) -> bool:
    """True only when a header actually spells out one of this ministry's
    roles.

    Which words mark a header as a role label is part of the
    :class:`~scripts.role_vocabulary.RoleVocabulary`, because a normalizer is
    deliberately generous once it already knows a column is a role -- Setup's
    reads a bare ``"3"`` as ``Setup 3``, which is right for a known role column
    and wrong for deciding whether a ``"Volunteer 3"`` header is one.
    """
    return config.role_vocabulary.names_a_role(header)


def _looks_date_major(
    header: list[str], body: list[list[str]], config: SourceConfig
) -> bool:
    """A grid whose *rows* are dates and whose *columns* are people.

    Recognized by shape alone: a first column that is not itself a date, at
    least two body rows whose first cell parses as a date, and header cells
    that are labels rather than dates.
    """
    if _looks_like_date(header[0] if header else "", config.year_hint):
        return False
    if any(_looks_like_date(h, config.year_hint) for h in header):
        return False
    if _date_rows(body, config) < 2:
        return False
    # A schedule grid is also rows-of-dates; what separates the two is what the
    # header names. Role labels mean the cells are *assignments*, so reading it
    # as availability would turn people's names into availability tokens.
    if sum(1 for h in header if _names_a_role(h, config)) >= 2:
        return False
    return len(_person_columns(header, config)) >= 2


def _classify(
    header: list[str], body: list[list[str]], config: SourceConfig
) -> tuple[str, list[str]]:
    notes: list[str] = []
    date_like_headers = [
        h for h in header if _looks_like_date(h, config.year_hint)
    ]
    has_name = any(_match_alias(h, config.name_column_aliases) for h in header)
    has_date_col = any(_match_alias(h, config.date_column_aliases) for h in header)
    has_role_col = any(_match_alias(h, config.role_column_aliases) for h in header)
    role_like_headers = []
    for h in header:
        role_name = config.role_vocabulary.try_normalize(h)
        if role_name is not None:
            role_like_headers.append(role_name)

    if has_name and len(date_like_headers) >= 2:
        notes.append(f"{len(date_like_headers)} date columns detected")
        if any(_match_alias(h, config.lead_column_aliases) for h in header):
            notes.append("a lead-qualification column is present")
        return "availability-grid", notes
    if has_date_col and len(role_like_headers) >= 2:
        notes.append(f"role columns: {sorted(set(role_like_headers))}")
        return "schedule-grid", notes
    if has_date_col and has_role_col:
        return "schedule-long", notes
    if _looks_date_major(header, body, config):
        people = _person_columns(header, config)
        notes.append(f"date-major: {len(people)} volunteer columns (names redacted)")
        notes.append(f"{_date_rows(body, config)} date rows detected")
        if _notes_index(header, config) is not None:
            notes.append("a per-date notes column is present")
        return "availability-grid-transposed", notes
    if has_name and not date_like_headers and len(header) <= 3:
        return "name-list (lead qualification?)", notes
    return "unrecognized", notes


# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------


def build_roles(
    vocabulary: RoleVocabulary = SETUP_VOCABULARY,
) -> tuple[HistoricalRole, ...]:
    """The vocabulary's roles with synthetic ids 1..N, Setup's by default.

    For Setup that is the approved five roles: Lead is role 1 and is *not* in
    the variety set; Setup 2-5 are roles 2..5 and are.
    """
    return build_roles_for(vocabulary)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Grid:
    header: list[str]
    body: list[list[str]]


def _load_grid(path: Path) -> _Grid:
    rows = _read_rows(path)
    if not rows:
        raise SourceError(f"{path.name} is empty")
    return _Grid(header=[c.strip() for c in rows[0]], body=rows[1:])


def _pick_file(
    directory: Path, explicit: str | None, wanted_shape: str, config: SourceConfig
) -> Path | None:
    if explicit:
        p = directory / explicit
        if not p.exists():
            raise SourceError(f"configured file {explicit} not found")
        return p
    for path in _csv_files(directory):
        grid = _load_grid(path)
        shape, _ = _classify(grid.header, grid.body, config)
        if shape == wanted_shape:
            return path
    return None


def load_dataset(directory: Path, config: SourceConfig) -> HistoricalDataset:
    """Read the directory into a :class:`HistoricalDataset`.

    Requires at least an availability grid. A schedule file (grid or long) is
    optional; without it there is no historical roster to compare against, and
    required positions fall back to ``headcount_per_sunday``, or, absent that,
    to every role in the configured vocabulary (five, for Setup).
    """
    roles = build_roles(config.role_vocabulary)
    role_names = {r.name for r in roles}

    avail_path = _pick_file(
        directory, config.availability_file, "availability-grid", config
    ) or _pick_file(
        directory, config.availability_file, "availability-grid-transposed", config
    )
    if avail_path is None:
        raise SourceError(
            "no availability grid found; set SourceConfig.availability_file"
        )
    # An explicitly configured file is taken on trust as *the* availability
    # grid, but never on trust as to its orientation -- that is read off the
    # file itself, so naming the file cannot pick the wrong reader.
    avail_grid = _load_grid(avail_path)
    avail_shape, _ = _classify(avail_grid.header, avail_grid.body, config)
    if avail_shape not in {"availability-grid", "availability-grid-transposed"}:
        raise SourceError(
            f"{avail_path.name}: not recognizable as an availability grid"
            f" (detected {avail_shape})"
        )
    transposed = avail_shape == "availability-grid-transposed"

    event_notes: dict[datetime.date, str] = {}
    blank_cells = 0
    tokens_seen: set[str] = set()
    if transposed:
        (
            volunteers,
            availability,
            sundays_from_avail,
            event_notes,
            blank_cells,
            tokens_seen,
        ) = _read_availability_transposed(avail_path, config)
    else:
        volunteers, availability, sundays_from_avail = _read_availability(
            avail_path, config
        )

    # Optional explicit lead-qualification list overrides / augments.
    if config.lead_qualification_file:
        _apply_lead_list(directory / config.lead_qualification_file, volunteers, config)

    schedule_path = _pick_file(
        directory, config.schedule_file, "schedule-grid", config
    ) or _pick_file(directory, config.schedule_file, "schedule-long", config)

    historical: list[HistoricalAssignment] = []
    sundays: tuple[datetime.date, ...] = sundays_from_avail
    if schedule_path is not None:
        grid = _load_grid(schedule_path)
        shape, _ = _classify(grid.header, grid.body, config)
        if shape == "schedule-grid":
            historical, sched_sundays = _read_schedule_grid(grid, config, role_names)
        else:
            historical, sched_sundays = _read_schedule_long(grid, config, role_names)
        sundays = tuple(sorted(set(sundays) | set(sched_sundays)))

    lead_source = (
        "explicit lead column in the availability grid"
        if any(v.lead_qualified for v in volunteers)
        else "not supplied"
    )
    if config.lead_qualification_file:
        lead_source = f"lead-qualification file {config.lead_qualification_file}"
    if config.lead_from_schedule:
        if not historical:
            raise SourceError(
                "lead_from_schedule was requested but no final schedule was read"
            )
        lead_role_name = config.role_vocabulary.lead_role_name
        if lead_role_name is None:
            raise SourceError(
                "lead_from_schedule was requested but this ministry's role"
                " vocabulary has no lead position"
            )
        _apply_observed_leads(
            volunteers, historical, lead_role_name=lead_role_name
        )
        lead_source = (
            "OBSERVED PROXY -- people appearing in the final schedule's"
            f" {config.role_vocabulary.lead_role_name} column; not a"
            " Head-maintained qualification list"
        )

    if config.only_dates:
        unknown_only = set(config.only_dates) - set(sundays)
        if unknown_only:
            raise SourceError(
                "only_dates names dates absent from the source: "
                + ", ".join(sorted(d.isoformat() for d in unknown_only))
            )
        sundays = tuple(d for d in sundays if d in config.only_dates)
        historical = [h for h in historical if h.event_date in config.only_dates]
        availability = {
            key: state
            for key, state in availability.items()
            if key[1] in config.only_dates
        }

    excluded = {d: why for d, why in config.exclude_dates.items() if d in sundays}
    unknown_exclusions = set(config.exclude_dates) - set(sundays)
    if unknown_exclusions:
        raise SourceError(
            "exclude_dates names dates absent from the source: "
            + ", ".join(sorted(d.isoformat() for d in unknown_exclusions))
        )
    if excluded:
        sundays = tuple(d for d in sundays if d not in excluded)
        historical = [h for h in historical if h.event_date not in excluded]
        availability = {
            key: state for key, state in availability.items() if key[1] not in excluded
        }

    if not sundays:
        raise SourceError("no Sundays could be determined from the source")

    headcount = (
        config.headcount_per_sunday or config.role_vocabulary.default_headcount
    )
    requirements: list[HistoricalRequirement] = []
    # Prefer per-Sunday role structure from a schedule grid if present.
    grid_roles_by_date = _schedule_grid_roles(schedule_path, config, role_names)
    if config.requirements_from_filled_cells:
        if not historical:
            raise SourceError(
                "requirements_from_filled_cells was requested but no staffed"
                " schedule rows were read"
            )
        staffed: dict[datetime.date, list[str]] = {}
        for h in historical:
            staffed.setdefault(h.event_date, []).append(h.role_name)
        grid_roles_by_date = {
            day: sorted(
                roles, key=config.role_vocabulary.order_of
            )
            for day, roles in staffed.items()
        }
    for day in sundays:
        day_roles = grid_roles_by_date.get(day) if grid_roles_by_date else None
        if day_roles:
            for role_name in day_roles:
                requirements.append(HistoricalRequirement(day, role_name, 1))
        else:
            for role_name in config.role_vocabulary.canonical_names[:headcount]:
                requirements.append(HistoricalRequirement(day, role_name, 1))

    membership_by_name = {
        v.display_name.strip().lower(): v.membership_id for v in volunteers
    }
    resolved_hist = tuple(
        HistoricalAssignment(
            event_date=h.event_date,
            role_name=h.role_name,
            display_name=h.display_name,
            membership_id=membership_by_name.get(h.display_name.strip().lower()),
        )
        for h in historical
    )

    conflict_available = bool(config.conflicts_file)
    blocked: dict[int, frozenset[datetime.date]] = {}
    if config.conflicts_file:
        blocked = _read_conflicts(
            directory / config.conflicts_file, membership_by_name, config
        )

    notes = [
        f"availability file: {avail_path.name}"
        + (" (date-major)" if transposed else " (person-major)"),
        f"schedule file: {schedule_path.name if schedule_path else '(none supplied)'}",
        f"headcount per Sunday: "
        + (
            "from the roles each event actually staffed"
            if config.requirements_from_filled_cells
            else "from schedule grid"
            if grid_roles_by_date
            else str(headcount)
        ),
    ]
    if not conflict_available:
        notes.append(
            "cross-ministry conflict data: NOT AVAILABLE from the supplied files"
        )
    notes.append(f"availability tokens present: {sorted(tokens_seen) or '(none)'}")
    notes.append(f"blank availability cells: {blank_cells}")
    notes.append(f"Lead qualification source: {lead_source}")
    for day, why in sorted(excluded.items()):
        notes.append(f"held out of this solve: {day.isoformat()} ({why})")

    return HistoricalDataset(
        period_label=config.period_label,
        sundays=sundays,
        roles=roles,
        volunteers=tuple(volunteers),
        requirements=tuple(requirements),
        availability=availability,
        historical_assignments=resolved_hist,
        blocked_dates=blocked,
        availability_blank_cells=blank_cells,
        availability_tokens_seen=frozenset(tokens_seen),
        event_notes=event_notes,
        excluded_dates=excluded,
        lead_qualification_source=lead_source,
        conflict_data_available=conflict_available,
        source_notes=tuple(notes),
    )


def _read_availability(
    path: Path, config: SourceConfig
) -> tuple[list[HistoricalVolunteer], dict[tuple[int, datetime.date], AvailabilityState], tuple[datetime.date, ...]]:
    grid = _load_grid(path)
    header = grid.header

    name_idx = _first_index(header, config.name_column_aliases)
    if name_idx is None:
        raise SourceError(f"{path.name}: no volunteer-name column found")
    lead_idx = _first_index(header, config.lead_column_aliases)

    date_cols: list[tuple[int, datetime.date]] = []
    for i, h in enumerate(header):
        if i in {name_idx, lead_idx}:
            continue
        try:
            date_cols.append((i, parse_schedule_date(h, year_hint=config.year_hint)))
        except ValueError:
            continue
    if len(date_cols) < 2:
        raise SourceError(
            f"{path.name}: expected >=2 date columns, found {len(date_cols)}"
        )

    volunteers: list[HistoricalVolunteer] = []
    availability: dict[tuple[int, datetime.date], AvailabilityState] = {}
    seen_names: set[str] = set()
    for row_no, row in enumerate(grid.body, start=2):
        if not any(cell.strip() for cell in row):
            continue
        name = row[name_idx].strip() if name_idx < len(row) else ""
        if not name:
            raise SourceError(f"{path.name} row {row_no}: blank volunteer name")
        key = name.strip().lower()
        if key in seen_names:
            raise SourceError(
                f"{path.name} row {row_no}: duplicate volunteer '{name}'"
            )
        seen_names.add(key)
        membership_id = len(volunteers) + 1
        person_id = 100 + membership_id

        lead_qualified = False
        if lead_idx is not None and lead_idx < len(row):
            lead_qualified = _norm(row[lead_idx]) in {
                _norm(t) for t in config.lead_true_tokens
            }

        for col_idx, day in date_cols:
            raw = row[col_idx] if col_idx < len(row) else ""
            try:
                state = config.availability_semantics.classify(raw)
            except ValueError as exc:
                raise SourceError(
                    f"{path.name} row {row_no}: {exc} -- availability semantics"
                    " differ from the known Setup convention; stopping"
                ) from exc
            if state is not None:
                availability[(membership_id, day)] = state

        volunteers.append(
            HistoricalVolunteer(
                membership_id=membership_id,
                person_id=person_id,
                display_name=name,
                lead_qualified=lead_qualified,
            )
        )

    sundays = tuple(sorted(day for _, day in date_cols))
    return volunteers, availability, sundays


def _read_availability_transposed(
    path: Path, config: SourceConfig
) -> tuple[
    list[HistoricalVolunteer],
    dict[tuple[int, datetime.date], AvailabilityState],
    tuple[datetime.date, ...],
    dict[datetime.date, str],
    int,
    set[str],
]:
    """Read a date-major availability grid: rows are dates, columns are people.

    Returns the same triple as :func:`_read_availability` plus the per-date
    notes, the number of blank availability cells, and the raw tokens seen --
    the three things a caller needs to describe the source honestly instead of
    assuming a convention it never stated.
    """
    grid = _load_grid(path)
    header = grid.header

    people = _person_columns(header, config)
    if len(people) < 2:
        raise SourceError(
            f"{path.name}: expected >=2 volunteer columns, found {len(people)}"
        )
    notes_idx = _notes_index(header, config)
    legend_start = (notes_idx + 1) if notes_idx is not None else len(header)

    volunteers: list[HistoricalVolunteer] = []
    membership_by_column: dict[int, int] = {}
    seen_names: set[str] = set()
    for col_idx, name in people:
        key = name.strip().lower()
        if key in seen_names:
            raise SourceError(f"{path.name}: duplicate volunteer column '{name}'")
        seen_names.add(key)
        membership_id = len(volunteers) + 1
        membership_by_column[col_idx] = membership_id
        volunteers.append(
            HistoricalVolunteer(
                membership_id=membership_id,
                person_id=100 + membership_id,
                display_name=name,
                lead_qualified=False,
            )
        )

    availability: dict[tuple[int, datetime.date], AvailabilityState] = {}
    event_notes: dict[datetime.date, str] = {}
    days: list[datetime.date] = []
    blank_cells = 0
    tokens_seen: set[str] = set()
    for row_no, row in enumerate(grid.body, start=2):
        if not any(cell.strip() for cell in row):
            continue
        if not row or not row[0].strip():
            raise SourceError(f"{path.name} row {row_no}: blank date cell")
        day = parse_schedule_date(row[0], year_hint=config.year_hint)
        if day in days:
            raise SourceError(f"{path.name} row {row_no}: duplicate date {day}")
        days.append(day)

        _check_legend(path, row[legend_start:], config)

        if notes_idx is not None and notes_idx < len(row) and row[notes_idx].strip():
            event_notes[day] = row[notes_idx].strip()

        for col_idx, _name in people:
            raw = row[col_idx] if col_idx < len(row) else ""
            token = raw.strip()
            if not token:
                blank_cells += 1
            else:
                tokens_seen.add(token.lower())
            try:
                state = config.availability_semantics.classify(raw)
            except ValueError as exc:
                raise SourceError(
                    f"{path.name} row {row_no}: {exc} -- availability semantics"
                    " differ from the known Setup convention; stopping"
                ) from exc
            if state is not None:
                availability[(membership_by_column[col_idx], day)] = state

    if len(days) < 2:
        raise SourceError(f"{path.name}: expected >=2 date rows, found {len(days)}")

    return (
        volunteers,
        availability,
        tuple(sorted(days)),
        event_notes,
        blank_cells,
        tokens_seen,
    )


def _check_legend(path: Path, cells: list[str], config: SourceConfig) -> None:
    """Verify a trailing ``token, meaning`` legend agrees with our semantics.

    A sheet that spells out what its symbols mean is the best evidence there
    is, and it is worth more than the convention this reader defaults to -- so
    a contradiction stops the run instead of being overruled by the default.
    """
    values = [c.strip() for c in cells if c.strip()]
    if len(values) < 2:
        return
    token, meaning = values[0], _norm(values[1])
    try:
        stated = config.availability_semantics.classify(token)
    except ValueError as exc:
        raise SourceError(f"{path.name}: legend defines unknown token: {exc}") from exc
    says_unavailable = any(
        word in meaning for word in ("not available", "unavailable", "away", "no")
    )
    says_available = meaning in {"available", "avail", "yes", "free", "can serve"}
    if says_unavailable and stated is not AvailabilityState.UNAVAILABLE:
        raise SourceError(
            f"{path.name}: legend says {token!r} means {values[1]!r}, but the"
            " configured semantics read it as available -- refusing to guess"
        )
    if says_available and stated is not AvailabilityState.AVAILABLE:
        raise SourceError(
            f"{path.name}: legend says {token!r} means {values[1]!r}, but the"
            " configured semantics read it as unavailable -- refusing to guess"
        )


def _apply_lead_list(
    path: Path, volunteers: list[HistoricalVolunteer], config: SourceConfig
) -> None:
    if not path.exists():
        raise SourceError(f"lead-qualification file {path.name} not found")
    grid = _load_grid(path)
    name_idx = _first_index(grid.header, config.name_column_aliases) or 0
    qualified_names = {
        row[name_idx].strip().lower()
        for row in grid.body
        if name_idx < len(row) and row[name_idx].strip()
    }
    for i, vol in enumerate(volunteers):
        if vol.display_name.strip().lower() in qualified_names:
            volunteers[i] = HistoricalVolunteer(
                membership_id=vol.membership_id,
                person_id=vol.person_id,
                display_name=vol.display_name,
                lead_qualified=True,
                restricted_support_roles=vol.restricted_support_roles,
            )


def _apply_observed_leads(
    volunteers: list[HistoricalVolunteer],
    historical: list[HistoricalAssignment],
    *,
    lead_role_name: str,
) -> None:
    """Mark as Lead-qualified whoever actually led in the historical roster.

    Evidence that these people were considered suitable to lead at least once,
    and nothing more: it is not the Head's list, it cannot show who else
    qualifies, and it must not outlive one validation run.
    """
    observed = {
        h.display_name.strip().lower()
        for h in historical
        if h.role_name == lead_role_name and h.display_name.strip()
    }
    for i, vol in enumerate(volunteers):
        if vol.display_name.strip().lower() in observed:
            volunteers[i] = HistoricalVolunteer(
                membership_id=vol.membership_id,
                person_id=vol.person_id,
                display_name=vol.display_name,
                lead_qualified=True,
                restricted_support_roles=vol.restricted_support_roles,
            )


def _read_schedule_grid(
    grid: _Grid, config: SourceConfig, role_names: set[str]
) -> tuple[list[HistoricalAssignment], list[datetime.date]]:
    date_idx = _first_index(grid.header, config.date_column_aliases)
    if date_idx is None:
        raise SourceError("schedule grid: no date column")
    role_cols: list[tuple[int, str]] = []
    for i, h in enumerate(grid.header):
        if i == date_idx:
            continue
        role_name = config.role_vocabulary.try_normalize(h)
        if role_name is None:
            continue
        role_cols.append((i, role_name))
    out: list[HistoricalAssignment] = []
    sundays: list[datetime.date] = []
    seen: set[datetime.date] = set()
    for row_no, row in enumerate(grid.body, start=2):
        if date_idx >= len(row) or not row[date_idx].strip():
            continue
        day = parse_schedule_date(row[date_idx], year_hint=config.year_hint)
        if day in seen:
            raise SourceError(f"schedule grid row {row_no}: duplicate date {day}")
        seen.add(day)
        sundays.append(day)
        for col_idx, role_name in role_cols:
            person = row[col_idx].strip() if col_idx < len(row) else ""
            if person:
                out.append(HistoricalAssignment(day, role_name, person))
    return out, sundays


def _read_schedule_long(
    grid: _Grid, config: SourceConfig, role_names: set[str]
) -> tuple[list[HistoricalAssignment], list[datetime.date]]:
    date_idx = _first_index(grid.header, config.date_column_aliases)
    role_idx = _first_index(grid.header, config.role_column_aliases)
    name_idx = _first_index(grid.header, config.name_column_aliases)
    if None in (date_idx, role_idx, name_idx):
        raise SourceError("long schedule: need date, role and person columns")
    out: list[HistoricalAssignment] = []
    sundays: set[datetime.date] = set()
    for row_no, row in enumerate(grid.body, start=2):
        if max(date_idx, role_idx, name_idx) >= len(row):
            continue
        if not row[date_idx].strip():
            continue
        day = parse_schedule_date(row[date_idx], year_hint=config.year_hint)
        role_name = config.role_vocabulary.normalize(row[role_idx])
        person = row[name_idx].strip()
        sundays.add(day)
        if person:
            out.append(HistoricalAssignment(day, role_name, person))
    return out, sorted(sundays)


def _schedule_grid_roles(
    schedule_path: Path | None, config: SourceConfig, role_names: set[str]
) -> dict[datetime.date, list[str]] | None:
    if schedule_path is None:
        return None
    grid = _load_grid(schedule_path)
    shape, _ = _classify(grid.header, grid.body, config)
    if shape != "schedule-grid":
        return None
    date_idx = _first_index(grid.header, config.date_column_aliases)
    role_cols = []
    for i, h in enumerate(grid.header):
        if i == date_idx:
            continue
        role_name = config.role_vocabulary.try_normalize(h)
        if role_name is None:
            continue
        role_cols.append(role_name)
    out: dict[datetime.date, list[str]] = {}
    for row in grid.body:
        if date_idx >= len(row) or not row[date_idx].strip():
            continue
        day = parse_schedule_date(row[date_idx], year_hint=config.year_hint)
        out[day] = list(role_cols)
    return out


def _read_conflicts(
    path: Path, membership_by_name: dict[str, int], config: SourceConfig
) -> dict[int, frozenset[datetime.date]]:
    if not path.exists():
        raise SourceError(f"conflicts file {path.name} not found")
    grid = _load_grid(path)
    name_idx = _first_index(grid.header, config.name_column_aliases)
    date_idx = _first_index(grid.header, config.date_column_aliases)
    if name_idx is None or date_idx is None:
        raise SourceError("conflicts file: need a name column and a date column")
    out: dict[int, set[datetime.date]] = {}
    for row in grid.body:
        if max(name_idx, date_idx) >= len(row):
            continue
        name = row[name_idx].strip().lower()
        if not name or not row[date_idx].strip():
            continue
        mid = membership_by_name.get(name)
        if mid is None:
            continue
        day = parse_schedule_date(row[date_idx], year_hint=config.year_hint)
        out.setdefault(mid, set()).add(day)
    return {k: frozenset(v) for k, v in out.items()}


def _first_index(header: list[str], aliases: tuple[str, ...]) -> int | None:
    for i, h in enumerate(header):
        if _match_alias(h, aliases):
            return i
    return None
