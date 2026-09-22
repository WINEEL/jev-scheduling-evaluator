"""Read one date-major grid exactly as the config declares it.

The reader does no detection at all. It resolves every configured column
against the header row, checks that **every** column in that row is accounted
for, and then reads cells. Where it cannot proceed it raises
:class:`SourceReadError`; it never falls back, never re-detects, and never
treats an unexplained column as empty.

Three refusals are worth naming, because each is a way a real sheet goes wrong:

- **An unconfigured column.** The sheet has a column the config does not
  mention. That is either a column the ministry must explain or a config that
  is out of date, and reading around it would drop or misread data.
- **A declared role column whose header says something else.** The config says
  column 31 is Soundboard and the header reads ``Slides``. One of the two is
  wrong and it is not safe to decide which.
- **An availability token the config does not list.** Guessing what a new
  answer means is how somebody ends up scheduled on a cell nobody read.
"""

from __future__ import annotations

import csv
import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.scheduling.input import AvailabilityState

# A minimal, read-only .xlsx reader. It lives under ``historical_av`` because
# that is the task that needed one first; nothing in it is AV-specific, and
# moving it is a tidy-up for its own task rather than something to fold into an
# intake run.
from scripts.historical_av.workbook import Workbook, excel_serial_to_date
from scripts.historical_setup.parsing import parse_schedule_date
from scripts.ministry_intake.config import (
    GridSpec,
    IntakeConfig,
    IntakeConfigError,
    UnknownConfiguredRoleError,
    apply_normalizers,
)

__all__ = [
    "SourceReadError",
    "SourcePerson",
    "PreparedAssignment",
    "AmbiguousColumn",
    "IntakeReading",
    "read_source",
    "read_grid",
]


class SourceReadError(RuntimeError):
    """A source could not be read the way the config says it is shaped."""


@dataclass(frozen=True, slots=True)
class SourcePerson:
    """One roster column: the name as written, and the key it matches on."""

    display_name: str
    match_key: str
    column_index: int


@dataclass(frozen=True, slots=True)
class PreparedAssignment:
    """One cell of the schedule the church had already prepared.

    ``match_key`` is the person as the sheet wrote them, normalized the same
    way a roster column is. Whether that key corresponds to anybody is an
    identity question and is answered elsewhere -- this is a record of what the
    cell said, not a claim about who it meant.
    """

    event_date: datetime.date
    role: str
    raw_name: str
    match_key: str
    staffing: bool


@dataclass(frozen=True, slots=True)
class AmbiguousColumn:
    """A column the operator declared as not-yet-understood."""

    index: int
    header: str
    question: str
    non_empty_cells: int


@dataclass(slots=True)
class IntakeReading:
    """One tab or file, as the config says to read it."""

    ministry_label: str
    source_label: str
    tab: str | None
    header: tuple[str, ...]
    people: tuple[SourcePerson, ...]
    dates: tuple[datetime.date, ...]
    availability: dict[tuple[str, datetime.date], AvailabilityState] = field(
        default_factory=dict
    )
    assignments: tuple[PreparedAssignment, ...] = ()
    notes: dict[datetime.date, str] = field(default_factory=dict)
    ambiguous_columns: tuple[AmbiguousColumn, ...] = ()
    ignored_columns: tuple[tuple[int, str], ...] = ()
    #: Header text inside a declared role column that is not one of this
    #: ministry's declared roles at all.
    unknown_role_labels: tuple[tuple[int, str], ...] = ()
    blank_availability_cells: int = 0
    availability_tokens_seen: frozenset[str] = frozenset()
    undated_rows: int = 0
    staffing_roles: tuple[str, ...] = ()
    non_staffing_roles: tuple[str, ...] = ()

    @property
    def roster_keys(self) -> frozenset[str]:
        return frozenset(person.match_key for person in self.people)


def _normalized_headers(header: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", " ", cell.strip().casefold()) for cell in header)


def _parse_date_cell(raw: str, *, year_hint: int) -> datetime.date | None:
    text = (raw or "").strip()
    if not text:
        return None
    serial = excel_serial_to_date(text)
    if serial is not None:
        return serial
    try:
        return parse_schedule_date(text, year_hint=year_hint)
    except ValueError:
        return None


def _classify_token(config: IntakeConfig, raw: str) -> AvailabilityState | None:
    """One availability cell -> a state, or ``None`` for "write no row"."""
    spec = config.availability
    token = (raw or "").strip().casefold()
    if not token:
        return {
            "no_response": None,
            "available": AvailabilityState.AVAILABLE,
            "unavailable": AvailabilityState.UNAVAILABLE,
            "backup": AvailabilityState.BACKUP,
        }[spec.blank]
    if token in spec.unavailable:
        return AvailabilityState.UNAVAILABLE
    if token in spec.backup:
        return AvailabilityState.BACKUP
    if token in spec.available:
        return AvailabilityState.AVAILABLE
    raise SourceReadError(
        f"availability token {raw!r} is not listed in [availability]."
        " Add it to the config with its meaning, or correct the sheet --"
        " it is not safe to guess what a new answer means."
    )


def read_grid(
    config: IntakeConfig,
    rows: list[list[str]],
    *,
    source_label: str,
    tab: str | None = None,
) -> IntakeReading:
    """Read one grid of already-loaded cells."""
    grid: GridSpec = config.grid_for(tab)
    if len(rows) < grid.header_row:
        raise SourceReadError(
            f"{source_label}: header_row is {grid.header_row} but the sheet has"
            f" {len(rows)} rows"
        )
    header_cells = rows[grid.header_row - 1]
    body = rows[grid.header_row :]
    width = max((len(row) for row in rows), default=0)
    header = tuple(
        (header_cells[i] if i < len(header_cells) else "").strip() for i in range(width)
    )
    if not width:
        raise SourceReadError(f"{source_label}: sheet is empty")
    normalized = _normalized_headers(header)

    # -- resolve every configured column ------------------------------
    # A config that does not fit this sheet is a failure to read *this
    # source*, not a malformed config -- the same file may be exactly right
    # for the tab next door. Reported as one, so the dry run can put it in a
    # report instead of ending in a traceback.
    try:
        date_index = grid.date_column.resolve(header, normalized)
        role_index: dict[str, int] = {}
        role_cell_pipeline: dict[str, tuple[str, ...]] = {}
        for column in grid.role_columns:
            index = column.ref.resolve(header, normalized)
            if index in role_index.values():
                raise SourceReadError(
                    f"{source_label}: two roles are declared in column {index}"
                )
            role_index[column.role] = index
            role_cell_pipeline[column.role] = column.cell_normalizers
        notes_index = [ref.resolve(header, normalized) for ref in grid.notes_columns]

        declared_index: dict[int, tuple[str, bool]] = {}
        for declared in grid.declared_columns:
            declared_index[declared.ref.resolve(header, normalized)] = (
                declared.note,
                declared.ambiguous,
            )
    except IntakeConfigError as error:
        raise SourceReadError(
            f"{source_label}: the declared layout does not fit this sheet --"
            f" {error}. Workbook tabs drift; declare this tab's own layout"
            " under [[source.tabs]] rather than reading it at another year's"
            " offsets."
        ) from None

    # -- volunteer block ----------------------------------------------
    if grid.volunteer_columns.mode == "explicit_range":
        first = grid.volunteer_columns.first_index or 0
        last = grid.volunteer_columns.last_index or 0
        if last >= width:
            raise SourceReadError(
                f"{source_label}: volunteer_columns last_index {last} is past"
                f" the end of the sheet, which has {width} columns"
            )
        volunteer_range = range(first, last + 1)
    else:
        first_role = min(role_index.values())
        if first_role <= date_index:
            raise SourceReadError(
                f"{source_label}: volunteer_columns mode"
                " 'between_date_and_roles' needs the role columns to sit after"
                f" the date column (date at {date_index}, first role at"
                f" {first_role})"
            )
        volunteer_range = range(date_index + 1, first_role)

    reserved = {date_index, *role_index.values(), *notes_index, *declared_index}
    people: list[SourcePerson] = []
    seen_keys: dict[str, str] = {}
    for index in volunteer_range:
        if index in reserved:
            continue
        display = config.display_name(header[index])
        if not display:
            # A blank column inside the declared volunteer block. Not an error
            # -- spreadsheets carry spacers -- but it holds no person.
            continue
        key = config.match_key(header[index])
        if key in seen_keys:
            raise SourceReadError(
                f"{source_label}: {display!r} appears in two roster columns."
                " One column is one person; the duplicate has to be resolved in"
                " the sheet."
            )
        seen_keys[key] = display
        people.append(SourcePerson(display_name=display, match_key=key, column_index=index))

    if not people:
        raise SourceReadError(
            f"{source_label}: the declared volunteer columns hold no names."
            " Check volunteer_columns against the sheet."
        )

    # -- total column accounting: nothing unexplained -----------------
    accounted = reserved | {p.column_index for p in people}
    unaccounted = [
        index
        for index in range(width)
        if index not in accounted
        and (
            header[index]
            or any(
                (row[index].strip() if index < len(row) else "") for row in body
            )
        )
    ]
    if unaccounted:
        described = ", ".join(
            f"{index} ({header[index]!r})" if header[index] else f"{index} (no header)"
            for index in unaccounted
        )
        raise SourceReadError(
            f"{source_label}: column(s) {described} are not accounted for by the"
            " config. Declare each as a role, a notes column, an ignored column"
            " with a reason, or an ambiguous column with the question the"
            " Ministry Head has to answer. An unexplained column is never read"
            " as empty."
        )

    # -- role column headers, checked against what was declared -------
    mismatches: list[tuple[int, str, str]] = []
    unknown_labels: list[tuple[int, str]] = []
    for role, index in sorted(role_index.items(), key=lambda kv: kv[1]):
        text = header[index]
        if not text:
            continue  # a headerless role column is exactly why index exists
        try:
            actual = config.normalize_role_label(text)
        except UnknownConfiguredRoleError:
            unknown_labels.append((index, text))
            continue
        if actual != role:
            mismatches.append((index, text, role))
    if mismatches:
        described = "; ".join(
            f"column {index} is headed {text!r} but declared as {role!r}"
            for index, text, role in mismatches
        )
        raise SourceReadError(
            f"{source_label}: {described}. One of the two is wrong and it is"
            " not safe to decide which."
        )

    # -- body ---------------------------------------------------------
    dates: list[datetime.date] = []
    availability: dict[tuple[str, datetime.date], AvailabilityState] = {}
    assignments: list[PreparedAssignment] = []
    notes: dict[datetime.date, str] = {}
    tokens: set[str] = set()
    blanks = 0
    undated = 0
    ambiguous_cells: dict[int, int] = {index: 0 for index in declared_index}

    staffing = set(config.roles.staffing)

    for row_number, raw_row in enumerate(body, start=grid.header_row + 1):
        row = [cell.strip() for cell in raw_row]

        def cell(index: int) -> str:
            return row[index] if index < len(row) else ""

        day = _parse_date_cell(cell(date_index), year_hint=config.year_hint)
        if day is None:
            if any(row):
                undated += 1
            continue
        if day in dates:
            raise SourceReadError(
                f"{source_label} row {row_number}: {day} appears twice."
                " One row is one event date."
            )
        dates.append(day)

        for person in people:
            value = cell(person.column_index)
            if value:
                tokens.add(value.casefold())
            else:
                blanks += 1
            try:
                state = _classify_token(config, value)
            except SourceReadError as error:
                raise SourceReadError(
                    f"{source_label} row {row_number}: {error}"
                ) from None
            if state is not None:
                availability[(person.match_key, day)] = state

        for role, index in role_index.items():
            value = cell(index)
            if not value:
                continue
            cleaned = apply_normalizers(value, role_cell_pipeline[role])
            assignments.append(
                PreparedAssignment(
                    event_date=day,
                    role=role,
                    raw_name=config.display_name(cleaned),
                    match_key=config.match_key(cleaned),
                    staffing=role in staffing,
                )
            )

        for index in notes_index:
            value = cell(index)
            if value:
                notes[day] = value

        for index in declared_index:
            if cell(index):
                ambiguous_cells[index] += 1

    if not dates:
        raise SourceReadError(
            f"{source_label}: no rows carry a readable date in column"
            f" {date_index}. Check date_column and year_hint."
        )

    ambiguous = tuple(
        AmbiguousColumn(
            index=index,
            header=header[index],
            question=note,
            non_empty_cells=ambiguous_cells[index],
        )
        for index, (note, is_ambiguous) in sorted(declared_index.items())
        if is_ambiguous
    )
    ignored = tuple(
        (index, note)
        for index, (note, is_ambiguous) in sorted(declared_index.items())
        if not is_ambiguous
    )

    return IntakeReading(
        ministry_label=config.ministry_label,
        source_label=source_label,
        tab=tab,
        header=header,
        people=tuple(people),
        dates=tuple(sorted(dates)),
        availability=availability,
        assignments=tuple(assignments),
        notes=notes,
        ambiguous_columns=ambiguous,
        ignored_columns=ignored,
        unknown_role_labels=tuple(unknown_labels),
        blank_availability_cells=blanks,
        availability_tokens_seen=frozenset(tokens),
        undated_rows=undated,
        staffing_roles=tuple(r for r in config.roles.staffing if r in role_index),
        non_staffing_roles=tuple(
            r for r in config.roles.recorded_non_staffing if r in role_index
        ),
    )


def _read_csv(path: Path) -> list[list[str]]:
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [list(row) for row in csv.reader(handle, delimiter=delimiter)]


def read_source(
    config: IntakeConfig, path: Path, *, tab: str | None = None
) -> IntakeReading:
    """Read the file the config describes. No database, no writes."""
    path = Path(path)
    if not path.is_file():
        raise SourceReadError(f"source file does not exist: {path.name}")

    if config.source_format == "csv":
        if tab is not None:
            raise SourceReadError(
                "a CSV has no tabs; drop --tab or point at the workbook"
            )
        return read_grid(config, _read_csv(path), source_label=path.name)

    workbook = Workbook(path)
    if tab is None:
        raise SourceReadError(
            f"{path.name} has {len(workbook.sheet_names)} tabs; name the one to"
            " read with --tab. Reading 'the first one' would be a guess."
        )
    if tab not in workbook.sheet_names:
        raise SourceReadError(f"{path.name} has no tab named {tab!r}")
    sheet = workbook.sheet(tab)
    return read_grid(
        config,
        [list(row) for row in sheet.rows],
        source_label=f"{path.name}:{tab}",
        tab=tab,
    )
