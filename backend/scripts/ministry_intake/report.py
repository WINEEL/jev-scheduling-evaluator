"""What a dry run found, and how it reads on a terminal.

The report is a value, not a print statement. Everything the run learned is on
the dataclass; :func:`render` turns it into lines. That split is what lets
:mod:`scripts.ministry_intake.readiness` evaluate a run without re-reading a
file, and lets tests assert on findings rather than on formatting.

**Names.** Most of this report is counts. Names appear in exactly the places a
person has to act on a specific row -- an unresolved identity, a contradicted
assignment, a missing approval -- because "3 unresolved people" is not
something anybody can go and fix. ``show_names=False`` renders those as counts
for a terminal that must stay name-free; the findings themselves are unchanged
either way, so readiness never depends on the rendering.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from scripts.ministry_intake.attestation import Attestation
from scripts.ministry_intake.findings import Finding, Severity

__all__ = ["Contradiction", "DoubleBooking", "DryRunReport", "render"]


@dataclass(frozen=True, slots=True)
class Contradiction:
    """A prepared assignment that disagrees with the same sheet's availability.

    Never resolved here, and never resolved anywhere in this package. Either
    the availability answer is stale or the assignment was a deliberate
    override under scarcity, and only the Ministry Head knows which.
    """

    event_date: datetime.date
    role: str
    person: str
    stated_availability: str


@dataclass(frozen=True, slots=True)
class DoubleBooking:
    """One person in two of this ministry's roles on one date.

    A hard-rule contradiction in the source itself: the engine allows at most
    one position per person per event, so a source stating otherwise cannot be
    imported as written.
    """

    event_date: datetime.date
    person: str
    roles: tuple[str, ...]


@dataclass(slots=True)
class DryRunReport:
    """Everything one dry run observed. Writes nothing, anywhere."""

    ministry_label: str
    config_path: str
    source_path: str
    declared_format: str
    declared_shape: str
    role_attestation: Attestation
    tab: str | None = None
    reading: object | None = None
    reconciliation: object | None = None
    matrix: object | None = None
    #: ``match_key -> roles served``. **NON-AUTHORITATIVE**, reference only.
    historical_roles: dict[str, frozenset[str]] = field(default_factory=dict)
    availability_contradictions: tuple[Contradiction, ...] = ()
    double_bookings: tuple[DoubleBooking, ...] = ()
    #: Roster people the approval matrix has no row for.
    qualification_gaps: tuple[str, ...] = ()
    #: Names appearing in the prepared schedule that are not roster columns.
    unmatched_assignment_names: tuple[str, ...] = ()
    #: Prepared placements the attested matrix does not approve.
    unapproved_prepared_assignments: tuple[Contradiction, ...] = ()
    findings: tuple[Finding, ...] = ()
    #: Set when the source could not be read at all. Everything downstream is
    #: then absent rather than empty, and the two must not look alike.
    fatal_error: str | None = None

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKER)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def notes(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.INFO)

    @property
    def head_questions(self) -> tuple[Finding, ...]:
        """Findings only the Ministry Head can close, in report order."""
        from scripts.ministry_intake.findings import (
            NEEDS_HEAD_DECISION,
            NEEDS_HEAD_DEFINITION,
        )

        return tuple(
            f
            for f in self.findings
            if f.category in {NEEDS_HEAD_DECISION, NEEDS_HEAD_DEFINITION}
        )


def _names(values, show_names: bool, limit: int = 20) -> list[str]:
    values = list(values)
    if not values:
        return []
    if not show_names:
        return [f"({len(values)} record(s); names withheld)"]
    shown = [str(v) for v in values[:limit]]
    if len(values) > limit:
        shown.append(f"... and {len(values) - limit} more")
    return shown


def render(report: DryRunReport, *, show_names: bool = True) -> str:
    """The dry run as a terminal report."""
    out: list[str] = []
    add = out.append

    add("=" * 72)
    add(f"DRY RUN -- {report.ministry_label} intake validation")
    add("=" * 72)
    add("NO DATABASE WAS OPENED AND NOTHING WAS WRITTEN.")
    add("")

    add("-- Declared source shape ----------------------------------------")
    add(f"  config:           {report.config_path}")
    add(f"  source:           {report.source_path}")
    add(f"  tab:              {report.tab or '(single grid)'}")
    add(f"  declared format:  {report.declared_format}")
    add(f"  declared shape:   {report.declared_shape}")
    add(f"  role list:        {report.role_attestation.describe(what='the role list')}")

    if report.fatal_error:
        add("")
        add("-- Source could NOT be read -------------------------------------")
        add(f"  {report.fatal_error}")
        add("")
        add(_render_findings(report, show_names))
        return "\n".join(out)

    reading = report.reading
    add("")
    add("-- Source as read -----------------------------------------------")
    add(f"  header columns:   {len(reading.header)}")
    add(f"  events found:     {len(reading.dates)}"
        f"  ({reading.dates[0]} .. {reading.dates[-1]})")
    add(f"  people found:     {len(reading.people)}")
    add(f"  rows with no readable date (skipped): {reading.undated_rows}")
    add(f"  availability tokens seen: {sorted(reading.availability_tokens_seen) or '(none)'}")
    add(f"  blank availability cells: {reading.blank_availability_cells}")
    add(f"  ignored columns:  {len(reading.ignored_columns)}")
    for index, reason in reading.ignored_columns:
        add(f"      column {index}: {reason}")

    add("")
    add("-- Configured roles ---------------------------------------------")
    add(f"  staffing positions:   {list(reading.staffing_roles)}")
    add(f"  recorded, not staffed: {list(reading.non_staffing_roles) or '(none)'}")
    if reading.unknown_role_labels:
        add("  UNKNOWN role labels in declared role columns:")
        for index, text in reading.unknown_role_labels:
            add(f"      column {index}: {text!r}")
    else:
        add("  unknown role labels:  none")

    add("")
    add("-- Staffing data discovered -------------------------------------")
    staffing = [a for a in reading.assignments if a.staffing]
    non_staffing = [a for a in reading.assignments if not a.staffing]
    add(f"  prepared placements (staffing):     {len(staffing)}")
    add(f"  prepared records (non-staffing):    {len(non_staffing)}")
    per_date: dict[datetime.date, int] = {}
    for placement in staffing:
        per_date[placement.event_date] = per_date.get(placement.event_date, 0) + 1
    sizes = sorted(set(per_date.values()))
    add(f"  staffed positions per date:         {sizes}"
        + ("  (uniform)" if len(sizes) == 1 else "  (DATE-SPECIFIC)"))
    add("  NOTE: a prepared schedule states what the ministry did, never how")
    add("        many people it needs. Staffing requirements remain the Head's.")

    identity = report.reconciliation
    add("")
    add("-- Identity reconciliation --------------------------------------")
    add(f"  resolved to an existing canonical Person: {len(identity.resolved)}")
    add(f"  declared new people:                      {len(identity.to_create)}")
    add(f"  UNRESOLVED:                               {len(identity.unresolved)}")
    for line in _names(
        (f"{name}  -- {reason}" for name, reason in identity.unresolved), show_names
    ):
        add(f"      {line}")
    if identity.not_in_source:
        add(f"  rows naming nobody in this source:        {len(identity.not_in_source)}")
        for line in _names(identity.not_in_source, show_names):
            add(f"      {line}")
    add(f"  cross-ministry identity mappings:         {len(identity.cross_ministry)}")
    for person, ministry in sorted(identity.cross_ministry.items()):
        add(f"      {person if show_names else '(name withheld)'} -> also {ministry}")
    add(f"  duplicate people (case-only spellings):   {len(identity.case_duplicates)}")
    for group in identity.case_duplicates:
        add(f"      {list(group) if show_names else '(names withheld)'}")
    if report.unmatched_assignment_names:
        add(f"  names in the prepared schedule that are not roster columns:"
            f" {len(report.unmatched_assignment_names)}")
        for line in _names(report.unmatched_assignment_names, show_names):
            add(f"      {line}")
    add("  NOTE: no two people are ever merged because their names look alike.")

    add("")
    add("-- Qualifications ------------------------------------------------")
    matrix = report.matrix
    if matrix is None:
        add("  NO approval matrix supplied.")
        add("  Qualifications are MISSING for every person in this source.")
    else:
        add(f"  attestation:      {matrix.attestation.describe(what='the matrix')}")
        add(f"  people approved:  {len(matrix.approvals)}")
        for role in reading.staffing_roles:
            add(f"      {role:<24} approved: {len(matrix.approved_for(role))}")
        if matrix.unknown_role_columns:
            add(f"  UNKNOWN role columns: {list(matrix.unknown_role_columns)}")
        if matrix.missing_role_columns:
            add(f"  MISSING role columns: {list(matrix.missing_role_columns)}")
        if matrix.duplicate_rows:
            add(f"  duplicate rows (identical): {len(matrix.duplicate_rows)}")
        if matrix.conflicting_rows:
            add(f"  CONFLICTING rows: {len(matrix.conflicting_rows)}")
            for line in _names(matrix.conflicting_rows, show_names):
                add(f"      {line}")
    if report.qualification_gaps:
        add(f"  people in the source with NO approval row: "
            f"{len(report.qualification_gaps)}")
        for line in _names(report.qualification_gaps, show_names):
            add(f"      {line}")

    add("")
    add("-- Historical reference (NON-AUTHORITATIVE) ----------------------")
    add("  Roles each person has actually been placed in, in THIS source.")
    add("  This is NOT a qualification list and is never used as one: it")
    add("  under-reports anybody who has not had a turn, and over-reports")
    add("  anybody pressed into a slot once under scarcity.")
    add(f"  people with >=1 historical placement: {len(report.historical_roles)}")
    for role in reading.staffing_roles:
        count = sum(1 for roles in report.historical_roles.values() if role in roles)
        add(f"      {role:<24} historically placed: {count}")

    add("")
    add("-- Contradictions -------------------------------------------------")
    add(f"  prepared assignment vs stated availability: "
        f"{len(report.availability_contradictions)}")
    for item in report.availability_contradictions:
        who = item.person if show_names else "(name withheld)"
        add(f"      {item.event_date} {item.role}: {who} is marked"
            f" {item.stated_availability}")
    add(f"  one person twice on one date: {len(report.double_bookings)}")
    for booking in report.double_bookings:
        who = booking.person if show_names else "(name withheld)"
        add(f"      {booking.event_date}: {who} in {list(booking.roles)}")
    add(f"  prepared placements the attested matrix does not approve: "
        f"{len(report.unapproved_prepared_assignments)}")
    for item in report.unapproved_prepared_assignments:
        who = item.person if show_names else "(name withheld)"
        add(f"      {item.event_date} {item.role}: {who}")

    if reading.ambiguous_columns:
        add("")
        add("-- Columns whose meaning is undefined -----------------------------")
        for column in reading.ambiguous_columns:
            label = column.header or "(no header)"
            add(f"  column {column.index} {label!r}:"
                f" {column.non_empty_cells} non-empty cell(s)")
            add(f"      Q: {column.question}")

    add("")
    add(_render_findings(report, show_names))
    return "\n".join(out)


def _render_finding(finding: Finding, show_names: bool) -> list[str]:
    """One finding, with its detail lines withheld when names are suppressed.

    A finding's detail is where the specific rows live -- the unresolved names,
    the contradicted placements -- so suppressing names means suppressing
    detail. Summarizing it instead of dropping it silently is what keeps
    ``--no-names`` honest: the reader still sees that there were nine lines.
    """
    if show_names or not finding.detail:
        return finding.render()
    return [
        f"[{finding.severity.value}] {finding.category}: {finding.message}",
        f"    ({len(finding.detail)} detail line(s) withheld; re-run without"
        " --no-names to see them)",
    ]


def _render_findings(report: DryRunReport, show_names: bool) -> str:
    lines: list[str] = []
    add = lines.append

    add("-- BLOCKERS -------------------------------------------------------")
    if not report.blockers:
        add("  none")
    for finding in report.blockers:
        for line in _render_finding(finding, show_names):
            add(f"  {line}")

    add("")
    add("-- WARNINGS -------------------------------------------------------")
    if not report.warnings:
        add("  none")
    for finding in report.warnings:
        for line in _render_finding(finding, show_names):
            add(f"  {line}")

    questions = report.head_questions
    add("")
    add("-- STILL REQUIRED FROM THE MINISTRY HEAD --------------------------")
    if report.fatal_error:
        add("  not determined -- the source could not be read, so the church")
        add("  facts it depends on were never reached")
    elif not questions:
        add("  nothing -- every church fact this source depends on is on record")
    for finding in questions:
        add(f"  [{finding.category}] {finding.message}")
    return "\n".join(lines)
