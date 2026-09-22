"""The Ministry Head's approval matrix -- and the wall between it and history.

Who may serve a role is a **decision**, and only the Ministry Head makes it. It
is not a fact that can be recovered from a spreadsheet, because a spreadsheet
records who *has* served, which is a different thing in both directions: it
omits everybody the Head would clear who has not had a turn yet, and it
includes anybody who was pressed into a slot once under scarcity.

So this module reads one file, written by the Head, and it reads nothing else.
History is available separately through :func:`historical_reference`, which
returns the same shape wearing a different name and is labelled
``NON-AUTHORITATIVE`` at every point it is printed. **Nothing merges the two.**
There is no flag, no fallback and no "seed the matrix from history" path,
because that path is exactly how a proxy becomes a fact.

The file
--------
A wide CSV -- one row per person, one column per role -- because that is the
shape ministries already fill in by hand::

    # approved_by: <the Ministry Head's name>
    # approved_on: 2026-09-20
    source_name,AV Lead,Soundboard,Slides,Video
    A. Placeholder,Y,Y,,
    B. Placeholder,,,Y,Y

The two ``#`` lines are the **attestation**, and they are what makes the matrix
authoritative. A matrix with either line blank still parses, still validates,
and is reported as ``NOT ATTESTED`` -- readiness stays false. A list of names
with nobody's signature on it is evidence, not authority.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ministry_intake.attestation import Attestation

__all__ = [
    "QualificationFileError",
    "APPROVED_TOKENS",
    "DECLINED_TOKENS",
    "QualificationMatrix",
    "load_matrix",
    "historical_reference",
    "template_text",
]


class QualificationFileError(RuntimeError):
    """A qualification file that cannot be read as written."""


SOURCE_NAME_COLUMN = "source_name"

#: What counts as "the Head approved this person for this role".
APPROVED_TOKENS = frozenset({"y", "yes", "x", "true", "1", "approved", "✓"})
#: What counts as "not approved". Blank is here too, and means the same thing:
#: an empty cell in an approval matrix is an absence of approval.
DECLINED_TOKENS = frozenset({"", "n", "no", "false", "0", "-", "--"})

_ATTESTATION = re.compile(r"^\s*#\s*([a-z_]+)\s*:\s*(.*?)\s*$", re.I)


@dataclass(frozen=True, slots=True)
class QualificationMatrix:
    """One Head-written approval matrix, as read."""

    #: ``match_key -> frozenset(role names approved)``. A person listed with no
    #: approvals keeps an **empty** set, which is a real answer: it says the
    #: Head considered them and approved nothing.
    approvals: dict[str, frozenset[str]] = field(default_factory=dict)
    #: ``match_key -> the name as the Head wrote it``, for reporting.
    display_names: dict[str, str] = field(default_factory=dict)
    attestation: Attestation = Attestation()
    #: Column headers that are not one of this ministry's declared roles.
    unknown_role_columns: tuple[str, ...] = ()
    #: Declared staffing roles the matrix has no column for.
    missing_role_columns: tuple[str, ...] = ()
    #: People listed twice with **identical** answers. Untidy, not wrong.
    duplicate_rows: tuple[str, ...] = ()
    #: People listed twice with **different** answers. Wrong, and unresolvable
    #: without the Head.
    conflicting_rows: tuple[str, ...] = ()
    label: str = "qualification matrix"

    @property
    def authoritative(self) -> bool:
        """Usable as the ministry's approved list.

        Every one of these has to hold. A matrix with a conflicting row is not
        made authoritative by an attestation -- the Head signed a file that
        says two different things.
        """
        return (
            bool(self.approvals)
            and self.attestation.present
            and not self.unknown_role_columns
            and not self.missing_role_columns
            and not self.conflicting_rows
        )

    def approved_for(self, role: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                self.display_names.get(key, key)
                for key, roles in self.approvals.items()
                if role in roles
            )
        )


def _read_attestation(lines: list[str]) -> Attestation:
    values: dict[str, str] = {}
    for line in lines:
        if not line.lstrip().startswith("#"):
            continue
        match = _ATTESTATION.match(line)
        if match:
            values[match.group(1).casefold()] = match.group(2)
    return Attestation(
        approved_by=values.get("approved_by", ""),
        approved_on=values.get("approved_on", ""),
    )


def load_matrix(
    path: Path, *, config, match_key=None, label: str = "qualification matrix"
) -> QualificationMatrix:
    """Read one approval matrix against a ministry's declared roles.

    Role columns are matched through the **config's own** role normalizer, so
    the Head may write ``Sound board`` where the config calls it
    ``Soundboard`` -- but only through a spelling the config declares. An
    undeclared column is reported, never accepted.
    """
    key_of = match_key or config.match_key
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise QualificationFileError(
            f"could not read the qualification file: {error}"
        ) from None

    lines = text.splitlines()
    attestation = _read_attestation(lines)
    reader = csv.reader(line for line in lines if not line.lstrip().startswith("#"))
    rows = [row for row in reader]
    if not rows:
        raise QualificationFileError(f"{Path(path).name} has no rows")

    header = [cell.strip() for cell in rows[0]]
    normalized_header = [cell.casefold() for cell in header]
    if SOURCE_NAME_COLUMN not in normalized_header:
        raise QualificationFileError(
            f"{Path(path).name} needs a {SOURCE_NAME_COLUMN!r} column"
        )
    name_index = normalized_header.index(SOURCE_NAME_COLUMN)

    role_of_column: dict[int, str] = {}
    unknown_columns: list[str] = []
    for index, cell in enumerate(header):
        if index == name_index or not cell:
            continue
        if cell.casefold() in {"note", "notes", "comment", "comments"}:
            continue
        role = config.try_normalize_role_label(cell)
        if role is None or role not in config.roles.staffing:
            unknown_columns.append(cell)
            continue
        if role in role_of_column.values():
            raise QualificationFileError(
                f"{Path(path).name}: role {role!r} has two columns"
            )
        role_of_column[index] = role

    missing = tuple(
        role for role in config.roles.staffing if role not in role_of_column.values()
    )

    approvals: dict[str, frozenset[str]] = {}
    display: dict[str, str] = {}
    duplicates: list[str] = []
    conflicts: list[str] = []

    for line_number, row in enumerate(rows[1:], start=2):
        cells = [cell.strip() for cell in row]
        if not any(cells):
            continue
        name = cells[name_index] if name_index < len(cells) else ""
        if not name:
            raise QualificationFileError(
                f"{Path(path).name} line {line_number}: {SOURCE_NAME_COLUMN} is"
                " blank. A row of approvals with nobody's name on it cannot be"
                " assigned to anyone."
            )
        granted: set[str] = set()
        for index, role in role_of_column.items():
            value = (cells[index] if index < len(cells) else "").strip().casefold()
            if value in APPROVED_TOKENS:
                granted.add(role)
            elif value in DECLINED_TOKENS:
                continue
            else:
                raise QualificationFileError(
                    f"{Path(path).name} line {line_number}: {value!r} under"
                    f" {role!r} is neither an approval nor a refusal."
                    f" Approvals: {sorted(APPROVED_TOKENS)}; leave blank for no."
                )

        key = key_of(name)
        if not key:
            raise QualificationFileError(
                f"{Path(path).name} line {line_number}: {name!r} normalizes to"
                " nothing"
            )
        if key in approvals:
            if approvals[key] == frozenset(granted):
                duplicates.append(display.get(key, name))
            else:
                conflicts.append(display.get(key, name))
            continue
        approvals[key] = frozenset(granted)
        display[key] = name

    return QualificationMatrix(
        approvals=approvals,
        display_names=display,
        attestation=attestation,
        unknown_role_columns=tuple(sorted(set(unknown_columns))),
        missing_role_columns=missing,
        duplicate_rows=tuple(sorted(set(duplicates))),
        conflicting_rows=tuple(sorted(set(conflicts))),
        label=label,
    )


def historical_reference(reading) -> dict[str, frozenset[str]]:
    """Roles each source person has actually been assigned. **Reference only.**

    Returned as its own value with its own name so that it is impossible to
    pass where an approval matrix is expected: the types are different, the
    call sites are different, and every printed use of this is headed
    ``NON-AUTHORITATIVE``.

    It is wrong in a knowable direction -- it can only **under-report** -- and
    it also over-reports in one specific way: somebody placed in a slot once
    under scarcity appears here as though it were routine. Neither error can be
    fixed with more history. Only the Head's list fixes it.

    Only **staffing** placements count. A recorded-but-not-staffed role (AV's
    ``Shadow``) grants nothing: watching a role is not evidence of being
    cleared to serve it.
    """
    observed: dict[str, set[str]] = {}
    roster = reading.roster_keys
    for assignment in reading.assignments:
        if not assignment.staffing:
            continue
        if assignment.match_key not in roster:
            continue
        observed.setdefault(assignment.match_key, set()).add(assignment.role)
    return {key: frozenset(roles) for key, roles in observed.items()}


def template_text(config) -> str:
    """A blank matrix for this ministry's declared roles, ready to send."""
    roles = list(config.roles.staffing)
    lines = [
        f"# {config.ministry_label} role approval matrix",
        "#",
        "# One row per person. Put Y under every role you approve them for.",
        "# Leave a cell blank for 'no'. Do not delete or rename the columns.",
        "#",
        "# These two lines are what make this list authoritative. Until both",
        "# are filled in, the import treats this file as a draft and refuses",
        "# to use it.",
        "#",
        "# approved_by: <the Ministry Head's name>",
        "# approved_on: <date, as YYYY-MM-DD>",
        "",
        ",".join([SOURCE_NAME_COLUMN, *roles, "notes"]),
    ]
    return "\n".join(lines) + "\n"
