"""A ministry's source shape, declared rather than detected.

Read from one TOML file (``tomllib``, stdlib -- no dependency was added for
this). Everything a reader would otherwise have to guess is a field here, and
**every field is closed**: an unknown shape family, an unknown normalizer name,
an unknown blank-cell meaning, a role alias pointing at a role this ministry
does not have, a column the declaration does not account for -- each is a
:class:`IntakeConfigError`, not a fallback.

Why declaration rather than detection
-------------------------------------
Detection works while a sheet looks like one of the shapes the detector knows.
AV's real quarter file does not: availability and prepared assignments share
one row, and **four of the role columns carry no header text at all**. No
detector can name a column that is not labelled. So the config names it, by
**position**, and the operator who wrote that position is the one asserting it
-- which is the property that separates "configured" from "guessed".

Column accounting is total
--------------------------
Every column in the header row must be one of: the date column, a volunteer
column, a role column, a notes column, an explicitly ignored column (with a
stated reason), or an explicitly ambiguous column (with the question the
Ministry Head has to answer). Anything left over stops the run. That is what
makes "unknown structure fails closed" a property of the design rather than a
promise: there is no path through the reader for a column nobody described.

What is *not* here
------------------
Who may serve which role. A config names a ministry's **positions**; the
qualification matrix names its **people**, is written by the Ministry Head, and
lives in its own file (:mod:`scripts.ministry_intake.qualifications`). Nothing
in this module can grant anybody anything.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.ministry_intake.attestation import Attestation
from scripts.role_vocabulary import RoleVocabulary

__all__ = [
    "IntakeConfigError",
    "UnknownConfiguredRoleError",
    "NORMALIZERS",
    "BLANK_MEANINGS",
    "SUPPORTED_SHAPES",
    "SUPPORTED_FORMATS",
    "ColumnRef",
    "RoleColumn",
    "DeclaredColumn",
    "VolunteerColumns",
    "GridSpec",
    "AvailabilitySpec",
    "RoleSpec",
    "IntakeConfig",
    "load_config",
    "parse_config",
]


class IntakeConfigError(RuntimeError):
    """A config that cannot be used without guessing what it meant."""


class UnknownConfiguredRoleError(ValueError):
    """A source label is not one of this ministry's declared roles.

    A distinct type, carried on the vocabulary, because the shared reader has
    to tell "not one of ours" apart from a genuine bug in a normalizer.
    """


# --------------------------------------------------------------------------
# Normalizers: a closed registry, never an arbitrary expression
# --------------------------------------------------------------------------

_POSITION_CODE = re.compile(r"\(\s*[a-z]{1,4}\s*\d+\s*\)", re.I)
_TIME_OF_DAY = re.compile(r"\d{1,2}\s*:\s*\d{2}\s*(?:[ap]\.?m\.?)?", re.I)
_TRAILING_PARENTHETICAL = re.compile(r"\([^)]*\)\s*$")

#: The transformations a config may name, and nothing else. A closed registry
#: rather than a callable path or a regex the config supplies: a config file is
#: data, and data that can run arbitrary code is not data.
NORMALIZERS: Mapping[str, Callable[[str], str]] = {
    "strip": lambda s: s.strip(),
    "collapse_whitespace": lambda s: re.sub(r"\s+", " ", s).strip(),
    "casefold": lambda s: s.casefold(),
    "strip_position_code": lambda s: _POSITION_CODE.sub(" ", s),
    "strip_time_of_day": lambda s: _TIME_OF_DAY.sub(" ", s),
    "drop_trailing_parenthetical": lambda s: _TRAILING_PARENTHETICAL.sub("", s),
    "strip_punctuation": lambda s: re.sub(r"[^0-9a-zA-Z\s]", " ", s),
    "letters_only": lambda s: re.sub(r"[^a-zA-Z]", "", s),
}

#: What a blank availability cell means. There is no default that is safe for
#: every ministry, so the config states one. ``no_response`` writes no row and
#: lets the run's policy decide, which is the only reading that does not put
#: somebody on a schedule on the strength of an empty cell.
BLANK_MEANINGS = ("no_response", "available", "unavailable", "backup")

#: Shape families the reader implements. A config naming anything else is
#: refused rather than read hopefully.
SUPPORTED_SHAPES = ("date_major_grid",)
SUPPORTED_FORMATS = ("csv", "xlsx")

_DEFAULT_DISPLAY_NORMALIZERS = ("strip", "collapse_whitespace")
_DEFAULT_MATCH_NORMALIZERS = ("strip", "collapse_whitespace", "casefold")
_DEFAULT_ROLE_NORMALIZERS = (
    "strip",
    "strip_position_code",
    "strip_time_of_day",
    "collapse_whitespace",
    "casefold",
)


def apply_normalizers(raw: str | None, pipeline: tuple[str, ...]) -> str:
    """Run one declared pipeline over one cell value, in order."""
    text = "" if raw is None else str(raw)
    for name in pipeline:
        text = NORMALIZERS[name](text)
    return text


def _check_normalizers(names: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise IntakeConfigError(f"{where} must be a list of normalizer names")
    unknown = [n for n in names if n not in NORMALIZERS]
    if unknown:
        raise IntakeConfigError(
            f"{where} names unknown normalizer(s) {unknown}."
            f" Known: {sorted(NORMALIZERS)}"
        )
    return tuple(names)


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """One column, named either by position or by header text.

    ``index`` is 0-based and is the form that works for a column with **no
    header**, which is why it exists: AV's role columns are unlabelled in the
    header row and could not be referred to any other way. ``header`` is
    matched against the normalized header text and is the friendlier form where
    a sheet does label its columns; it fails closed when no header matches, and
    when more than one does.
    """

    index: int | None = None
    header: str | None = None

    def __post_init__(self) -> None:
        if (self.index is None) == (self.header is None):
            raise IntakeConfigError(
                "a column must be given exactly one of index or header"
            )
        if self.index is not None and self.index < 0:
            raise IntakeConfigError(f"column index {self.index} is negative")

    @property
    def label(self) -> str:
        return f"index {self.index}" if self.index is not None else f"header {self.header!r}"

    def resolve(self, headers: tuple[str, ...], normalized: tuple[str, ...]) -> int:
        """The 0-based column this ref names, or a refusal."""
        if self.index is not None:
            if self.index >= len(headers):
                raise IntakeConfigError(
                    f"configured column index {self.index} is past the end of the"
                    f" header row, which has {len(headers)} columns"
                )
            return self.index
        wanted = re.sub(r"\s+", " ", (self.header or "").strip().casefold())
        hits = [i for i, text in enumerate(normalized) if text == wanted]
        if not hits:
            raise IntakeConfigError(
                f"no column header matches {self.header!r}."
                " Declare it by index if the sheet does not label it."
            )
        if len(hits) > 1:
            raise IntakeConfigError(
                f"header {self.header!r} matches {len(hits)} columns"
                f" ({hits}); declare it by index instead"
            )
        return hits[0]


def _column_ref(raw: Any, *, where: str) -> ColumnRef:
    if not isinstance(raw, dict):
        raise IntakeConfigError(f"{where} must be a table with index or header")
    unknown = set(raw) - {"index", "header"}
    if unknown:
        raise IntakeConfigError(f"{where} has unknown key(s) {sorted(unknown)}")
    index = raw.get("index")
    header = raw.get("header")
    if index is not None and not isinstance(index, int):
        raise IntakeConfigError(f"{where}: index must be a whole number")
    if header is not None and not isinstance(header, str):
        raise IntakeConfigError(f"{where}: header must be a string")
    return ColumnRef(index=index, header=header)


@dataclass(frozen=True, slots=True)
class RoleColumn:
    """A column holding the person assigned to one role.

    ``cell_normalizers`` run over the cell **before** the person pipelines do,
    for a column whose cells are not bare names. AV's ``Shadow`` column is
    written ``Name (AV - Soundboard)``; without a declared way to drop that
    qualifier the name matches nobody on the roster, and the alternative --
    stripping trailing parentheses from every column just in case -- would
    silently rewrite a real name that happens to contain one.
    """

    role: str
    ref: ColumnRef
    cell_normalizers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeclaredColumn:
    """A column the operator has explicitly accounted for without reading it.

    Two kinds, and the difference decides readiness:

    - ``ignored`` -- the ministry has stated it is irrelevant, and ``reason``
      records who said so. Required, because "ignored" with no reason is
      indistinguishable from "forgotten".
    - ``ambiguous`` -- nobody knows what it means yet. ``question`` is what the
      Ministry Head has to answer, and the column is a **blocker** until they
      do. Moving it to ``ignore_columns`` with a reason is how "explicitly
      declared irrelevant" is expressed.
    """

    ref: ColumnRef
    note: str
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class VolunteerColumns:
    """Which columns hold the roster's people.

    Two forms, both explicit. ``first_index``/``last_index`` states the block
    outright. ``between_date_and_roles`` derives it from the *configured*
    positions of the date and role columns -- which is a derivation from the
    operator's own declaration, not from the data.
    """

    mode: str
    first_index: int | None = None
    last_index: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"explicit_range", "between_date_and_roles"}:
            raise IntakeConfigError(
                f"volunteer_columns mode {self.mode!r} is not one of"
                " 'explicit_range' or 'between_date_and_roles'"
            )
        if self.mode == "explicit_range":
            if self.first_index is None or self.last_index is None:
                raise IntakeConfigError(
                    "volunteer_columns explicit_range needs first_index and"
                    " last_index"
                )
            if self.first_index < 0 or self.last_index < self.first_index:
                raise IntakeConfigError(
                    "volunteer_columns range must be non-negative and ascending"
                )


@dataclass(frozen=True, slots=True)
class GridSpec:
    """One date-major grid: where everything in it is."""

    date_column: ColumnRef
    role_columns: tuple[RoleColumn, ...]
    volunteer_columns: VolunteerColumns
    header_row: int = 1
    notes_columns: tuple[ColumnRef, ...] = ()
    declared_columns: tuple[DeclaredColumn, ...] = ()

    def __post_init__(self) -> None:
        if self.header_row < 1:
            raise IntakeConfigError("header_row is 1-based and must be >= 1")
        if not self.role_columns:
            raise IntakeConfigError("a grid must declare at least one role column")
        seen: set[str] = set()
        for column in self.role_columns:
            if column.role in seen:
                raise IntakeConfigError(
                    f"role {column.role!r} is declared in two columns"
                )
            seen.add(column.role)


# --------------------------------------------------------------------------
# Availability and roles
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AvailabilitySpec:
    """What a cell in the availability block means, stated by the ministry."""

    available: frozenset[str]
    unavailable: frozenset[str]
    backup: frozenset[str] = frozenset()
    blank: str = "no_response"

    def __post_init__(self) -> None:
        if self.blank not in BLANK_MEANINGS:
            raise IntakeConfigError(
                f"availability.blank must be one of {list(BLANK_MEANINGS)},"
                f" not {self.blank!r}"
            )
        if not self.available:
            raise IntakeConfigError("availability.available lists no tokens")
        pairs = (
            ("available", "unavailable", self.available & self.unavailable),
            ("available", "backup", self.available & self.backup),
            ("unavailable", "backup", self.unavailable & self.backup),
        )
        for left, right, overlap in pairs:
            if overlap:
                raise IntakeConfigError(
                    f"token(s) {sorted(overlap)} are listed as both {left} and"
                    f" {right}"
                )


@dataclass(frozen=True, slots=True)
class RoleSpec:
    """A ministry's declared positions, and the labels its sheets use for them."""

    staffing: tuple[str, ...]
    recorded_non_staffing: tuple[str, ...] = ()
    lead_role: str | None = None
    variety_roles: tuple[str, ...] = ()
    #: ``normalized source label -> canonical role name``. Supplied by whoever
    #: knows the ministry's spellings; never derived from a sheet.
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: Who confirmed that :attr:`staffing` is the ministry's real list of
    #: positions, and when. Unattested, the config is still perfectly readable
    #: -- which is the point, since a shape has to be readable before anyone
    #: can be shown what it produced -- but the ministry is not ready to
    #: import. Kids is exactly this case: two spellings of its role list are in
    #: circulation and neither is signed.
    attestation: Attestation = Attestation()

    def __post_init__(self) -> None:
        if not self.staffing:
            raise IntakeConfigError(
                "roles.staffing is empty. A ministry with no authoritative role"
                " list cannot be read; leave the config unfinished rather than"
                " inventing one."
            )
        all_names = (*self.staffing, *self.recorded_non_staffing)
        if len(set(all_names)) != len(all_names):
            raise IntakeConfigError("role names must be unique within a ministry")
        if self.lead_role is not None and self.lead_role not in self.staffing:
            raise IntakeConfigError(
                f"lead_role {self.lead_role!r} is not one of roles.staffing"
            )
        stray = set(self.variety_roles) - set(self.staffing)
        if stray:
            raise IntakeConfigError(
                f"variety_roles {sorted(stray)} are not in roles.staffing"
            )
        unknown = {v for v in self.aliases.values() if v not in all_names}
        if unknown:
            raise IntakeConfigError(
                f"role aliases point at role(s) this ministry does not have:"
                f" {sorted(unknown)}"
            )

    @property
    def all_names(self) -> tuple[str, ...]:
        return (*self.staffing, *self.recorded_non_staffing)


# --------------------------------------------------------------------------
# The config itself
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntakeConfig:
    """Everything needed to read one ministry's source without guessing."""

    ministry_label: str
    source_format: str
    shape: str
    year_hint: int
    roles: RoleSpec
    availability: AvailabilitySpec
    grid: GridSpec
    #: ``tab name -> grid``. A workbook whose tabs legitimately differ says so
    #: here; a tab with no entry uses :attr:`grid`. This is how header and
    #: column-position drift across years is handled without a reader that
    #: shrugs and re-detects.
    tab_grids: Mapping[str, GridSpec] = field(default_factory=dict)
    display_normalizers: tuple[str, ...] = _DEFAULT_DISPLAY_NORMALIZERS
    match_normalizers: tuple[str, ...] = _DEFAULT_MATCH_NORMALIZERS
    role_normalizers: tuple[str, ...] = _DEFAULT_ROLE_NORMALIZERS
    source_path: str = "<in memory>"

    # -- normalization ------------------------------------------------
    def display_name(self, raw: str | None) -> str:
        return apply_normalizers(raw, self.display_normalizers)

    def match_key(self, raw: str | None) -> str:
        return apply_normalizers(raw, self.match_normalizers)

    def normalize_role_label(self, raw: str) -> str:
        """Map a source label onto one declared role, or refuse.

        Matching is against the **declared** alias table and the declared role
        names, both put through the same pipeline. There is no similarity
        scoring and no partial match: a label either is one of this ministry's
        spellings or it is not one of its roles.
        """
        key = apply_normalizers(raw, self.role_normalizers)
        if not key:
            raise UnknownConfiguredRoleError("role label is empty")
        alias = self.roles.aliases.get(key)
        if alias is not None:
            return alias
        for name in self.roles.all_names:
            if apply_normalizers(name, self.role_normalizers) == key:
                return name
        raise UnknownConfiguredRoleError(
            f"{raw!r} is not one of {self.ministry_label}'s declared roles"
        )

    def try_normalize_role_label(self, raw: str) -> str | None:
        try:
            return self.normalize_role_label(raw)
        except UnknownConfiguredRoleError:
            return None

    def grid_for(self, tab: str | None) -> GridSpec:
        if tab is None:
            return self.grid
        return self.tab_grids.get(tab, self.grid)

    def vocabulary(self) -> RoleVocabulary:
        """This ministry's staffing positions, for the shared source reader.

        Only the staffing positions: a recorded-but-not-staffed role (AV's
        ``Shadow``) must never become a requirement, so it is not a position
        the reader may staff.
        """
        return RoleVocabulary(
            canonical_names=self.roles.staffing,
            normalize=self.normalize_role_label,
            unknown_role_error=UnknownConfiguredRoleError,
            role_words=tuple(
                sorted(
                    {
                        word
                        for name in self.roles.all_names
                        for word in apply_normalizers(
                            name, self.role_normalizers
                        ).split()
                    }
                    | {key for key in self.roles.aliases}
                )
            ),
            lead_role_name=self.roles.lead_role,
            variety_role_names=self.roles.variety_roles,
        )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _require(table: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in table:
        raise IntakeConfigError(f"{where} is missing required key {key!r}")
    return table[key]


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], *, where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise IntakeConfigError(
            f"{where} has unknown key(s) {sorted(unknown)}."
            f" Allowed: {sorted(allowed)}"
        )


def _string_tuple(raw: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise IntakeConfigError(f"{where} must be a list of strings")
    return tuple(raw)


def _token_set(raw: Any, *, where: str) -> frozenset[str]:
    return frozenset(v.strip().casefold() for v in _string_tuple(raw, where=where))


def _parse_grid(table: Mapping[str, Any], *, where: str, roles: RoleSpec) -> GridSpec:
    _reject_unknown(
        table,
        {
            "header_row",
            "date_column",
            "volunteer_columns",
            "role_columns",
            "notes_columns",
            "ignore_columns",
            "ambiguous_columns",
        },
        where=where,
    )

    header_row = table.get("header_row", 1)
    if not isinstance(header_row, int):
        raise IntakeConfigError(f"{where}.header_row must be a whole number")

    date_column = _column_ref(
        _require(table, "date_column", where=where), where=f"{where}.date_column"
    )

    raw_roles = _require(table, "role_columns", where=where)
    if not isinstance(raw_roles, list) or not raw_roles:
        raise IntakeConfigError(f"{where}.role_columns must be a non-empty list")
    role_columns: list[RoleColumn] = []
    for position, entry in enumerate(raw_roles):
        spot = f"{where}.role_columns[{position}]"
        if not isinstance(entry, dict):
            raise IntakeConfigError(f"{spot} must be a table")
        _reject_unknown(
            entry, {"role", "index", "header", "cell_normalizers"}, where=spot
        )
        role = _require(entry, "role", where=spot)
        if not isinstance(role, str) or not role.strip():
            raise IntakeConfigError(f"{spot}.role must be a non-empty string")
        if role not in roles.all_names:
            raise IntakeConfigError(
                f"{spot} declares role {role!r}, which is not in roles.staffing"
                " or roles.recorded_non_staffing"
            )
        cell_pipeline = (
            _check_normalizers(
                entry["cell_normalizers"], where=f"{spot}.cell_normalizers"
            )
            if "cell_normalizers" in entry
            else ()
        )
        ref = _column_ref(
            {
                k: v
                for k, v in entry.items()
                if k not in {"role", "cell_normalizers"}
            },
            where=spot,
        )
        role_columns.append(
            RoleColumn(role=role, ref=ref, cell_normalizers=cell_pipeline)
        )

    raw_volunteers = _require(table, "volunteer_columns", where=where)
    if not isinstance(raw_volunteers, dict):
        raise IntakeConfigError(f"{where}.volunteer_columns must be a table")
    _reject_unknown(
        raw_volunteers,
        {"mode", "first_index", "last_index"},
        where=f"{where}.volunteer_columns",
    )
    volunteers = VolunteerColumns(
        mode=raw_volunteers.get("mode", "explicit_range"),
        first_index=raw_volunteers.get("first_index"),
        last_index=raw_volunteers.get("last_index"),
    )

    notes = tuple(
        _column_ref(entry, where=f"{where}.notes_columns[{i}]")
        for i, entry in enumerate(table.get("notes_columns", []))
    )

    declared: list[DeclaredColumn] = []
    for i, entry in enumerate(table.get("ignore_columns", [])):
        spot = f"{where}.ignore_columns[{i}]"
        if not isinstance(entry, dict):
            raise IntakeConfigError(f"{spot} must be a table")
        _reject_unknown(entry, {"index", "header", "reason"}, where=spot)
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise IntakeConfigError(
                f"{spot} needs a non-empty reason. An ignored column with no"
                " stated reason is indistinguishable from a forgotten one."
            )
        declared.append(
            DeclaredColumn(
                ref=_column_ref(
                    {k: v for k, v in entry.items() if k != "reason"}, where=spot
                ),
                note=reason,
                ambiguous=False,
            )
        )
    for i, entry in enumerate(table.get("ambiguous_columns", [])):
        spot = f"{where}.ambiguous_columns[{i}]"
        if not isinstance(entry, dict):
            raise IntakeConfigError(f"{spot} must be a table")
        _reject_unknown(entry, {"index", "header", "question"}, where=spot)
        question = entry.get("question")
        if not isinstance(question, str) or not question.strip():
            raise IntakeConfigError(
                f"{spot} needs a non-empty question -- the thing the Ministry"
                " Head has to answer about this column."
            )
        declared.append(
            DeclaredColumn(
                ref=_column_ref(
                    {k: v for k, v in entry.items() if k != "question"}, where=spot
                ),
                note=question,
                ambiguous=True,
            )
        )

    return GridSpec(
        date_column=date_column,
        role_columns=tuple(role_columns),
        volunteer_columns=volunteers,
        header_row=header_row,
        notes_columns=notes,
        declared_columns=tuple(declared),
    )


def parse_config(raw: Mapping[str, Any], *, source_path: str = "<in memory>") -> IntakeConfig:
    """Turn a parsed TOML mapping into a config, or refuse it."""
    _reject_unknown(
        raw,
        {"ministry", "roles", "source", "availability", "normalizers"},
        where="the config",
    )

    ministry = raw.get("ministry", {})
    if not isinstance(ministry, dict):
        raise IntakeConfigError("[ministry] must be a table")
    _reject_unknown(ministry, {"label"}, where="[ministry]")
    label = _require(ministry, "label", where="[ministry]")
    if not isinstance(label, str) or not label.strip():
        raise IntakeConfigError("[ministry].label must be a non-empty string")

    roles_table = raw.get("roles")
    if not isinstance(roles_table, dict):
        raise IntakeConfigError("[roles] is required")
    _reject_unknown(
        roles_table,
        {
            "staffing",
            "recorded_non_staffing",
            "lead_role",
            "variety_roles",
            "aliases",
            "approved_by",
            "approved_on",
        },
        where="[roles]",
    )
    aliases_raw = roles_table.get("aliases", {})
    if not isinstance(aliases_raw, dict):
        raise IntakeConfigError("[roles.aliases] must be a table")

    normalizers = raw.get("normalizers", {})
    if not isinstance(normalizers, dict):
        raise IntakeConfigError("[normalizers] must be a table")
    _reject_unknown(
        normalizers,
        {"person_display", "person_match_key", "role_label"},
        where="[normalizers]",
    )
    display_pipeline = (
        _check_normalizers(normalizers["person_display"], where="[normalizers].person_display")
        if "person_display" in normalizers
        else _DEFAULT_DISPLAY_NORMALIZERS
    )
    match_pipeline = (
        _check_normalizers(
            normalizers["person_match_key"], where="[normalizers].person_match_key"
        )
        if "person_match_key" in normalizers
        else _DEFAULT_MATCH_NORMALIZERS
    )
    role_pipeline = (
        _check_normalizers(normalizers["role_label"], where="[normalizers].role_label")
        if "role_label" in normalizers
        else _DEFAULT_ROLE_NORMALIZERS
    )

    aliases = {}
    for key, value in aliases_raw.items():
        if not isinstance(value, str):
            raise IntakeConfigError(f"[roles.aliases].{key} must be a string")
        aliases[apply_normalizers(key, role_pipeline)] = value

    roles = RoleSpec(
        staffing=_string_tuple(
            _require(roles_table, "staffing", where="[roles]"), where="[roles].staffing"
        ),
        recorded_non_staffing=_string_tuple(
            roles_table.get("recorded_non_staffing", []),
            where="[roles].recorded_non_staffing",
        ),
        lead_role=roles_table.get("lead_role"),
        variety_roles=_string_tuple(
            roles_table.get("variety_roles", []), where="[roles].variety_roles"
        ),
        aliases=aliases,
        attestation=Attestation(
            approved_by=str(roles_table.get("approved_by", "")),
            approved_on=str(roles_table.get("approved_on", "")),
        ),
    )

    source = raw.get("source")
    if not isinstance(source, dict):
        raise IntakeConfigError("[source] is required")
    _reject_unknown(
        source, {"format", "shape", "year_hint", "grid", "tabs"}, where="[source]"
    )
    source_format = _require(source, "format", where="[source]")
    if source_format not in SUPPORTED_FORMATS:
        raise IntakeConfigError(
            f"[source].format {source_format!r} is not supported."
            f" Known: {list(SUPPORTED_FORMATS)}"
        )
    shape = _require(source, "shape", where="[source]")
    if shape not in SUPPORTED_SHAPES:
        raise IntakeConfigError(
            f"[source].shape {shape!r} is not a shape this reader implements."
            f" Known: {list(SUPPORTED_SHAPES)}. An unrecognized shape stops the"
            " run rather than being read hopefully."
        )
    year_hint = source.get("year_hint")
    if not isinstance(year_hint, int):
        raise IntakeConfigError(
            "[source].year_hint must be a whole number -- the year a bare M/D"
            " date belongs to. There is no safe default."
        )

    grid_table = source.get("grid")
    if not isinstance(grid_table, dict):
        raise IntakeConfigError("[source.grid] is required")
    grid = _parse_grid(grid_table, where="[source.grid]", roles=roles)

    tab_grids: dict[str, GridSpec] = {}
    for i, entry in enumerate(source.get("tabs", [])):
        spot = f"[[source.tabs]][{i}]"
        if not isinstance(entry, dict):
            raise IntakeConfigError(f"{spot} must be a table")
        _reject_unknown(entry, {"name", "grid"}, where=spot)
        name = _require(entry, "name", where=spot)
        if not isinstance(name, str) or not name.strip():
            raise IntakeConfigError(f"{spot}.name must be a non-empty string")
        if name in tab_grids:
            raise IntakeConfigError(f"{spot}: tab {name!r} is declared twice")
        tab_table = entry.get("grid")
        if not isinstance(tab_table, dict):
            raise IntakeConfigError(f"{spot} must carry a [source.tabs.grid] table")
        tab_grids[name] = _parse_grid(tab_table, where=f"{spot}.grid", roles=roles)

    availability_table = raw.get("availability")
    if not isinstance(availability_table, dict):
        raise IntakeConfigError("[availability] is required")
    _reject_unknown(
        availability_table,
        {"available", "unavailable", "backup", "blank"},
        where="[availability]",
    )
    availability = AvailabilitySpec(
        available=_token_set(
            _require(availability_table, "available", where="[availability]"),
            where="[availability].available",
        ),
        unavailable=_token_set(
            _require(availability_table, "unavailable", where="[availability]"),
            where="[availability].unavailable",
        ),
        backup=_token_set(
            availability_table.get("backup", []), where="[availability].backup"
        ),
        blank=availability_table.get("blank", "no_response"),
    )

    return IntakeConfig(
        ministry_label=label.strip(),
        source_format=source_format,
        shape=shape,
        year_hint=year_hint,
        roles=roles,
        availability=availability,
        grid=grid,
        tab_grids=tab_grids,
        display_normalizers=display_pipeline,
        match_normalizers=match_pipeline,
        role_normalizers=role_pipeline,
        source_path=source_path,
    )


def load_config(path: Path) -> IntakeConfig:
    """Read one config file. Committable: it names columns, never people."""
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise IntakeConfigError(f"could not read the config: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise IntakeConfigError(f"{Path(path).name} is not valid TOML: {error}") from None
    return parse_config(raw, source_path=str(path))
