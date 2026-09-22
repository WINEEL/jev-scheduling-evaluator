"""Is this ministry ready for a real import? One deterministic answer.

The failure mode this exists to prevent is the obvious one: a parser that runs
to completion feels like success, so "the importer read the file" quietly
becomes "the ministry is ready". It is not. Reading a file proves the *shape*
is understood; it proves nothing about whether the church facts inside it are
known, and those are what an import writes.

So readiness is a conjunction of named criteria, each of which has to be
independently true, and **none of them is "the source parsed"** on its own.
Every criterion is derived from the dry-run report and nothing else, so the
verdict is reproducible: the same inputs give the same answer, with no clock,
no environment and no database in it.

Blockers and warnings come back separately. A warning never changes the
verdict -- if it could, it would be a blocker.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.ministry_intake.findings import Finding
from scripts.ministry_intake.report import DryRunReport

__all__ = ["Criterion", "ReadinessVerdict", "evaluate_readiness", "render_verdict"]


@dataclass(frozen=True, slots=True)
class Criterion:
    """One named condition, and whether this run met it."""

    name: str
    met: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ReadinessVerdict:
    """The answer, the criteria behind it, and the work remaining."""

    ministry_label: str
    ready: bool
    criteria: tuple[Criterion, ...]
    blockers: tuple[Finding, ...]
    warnings: tuple[Finding, ...]

    @property
    def unmet(self) -> tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if not c.met)

    @property
    def verdict_line(self) -> str:
        if self.ready:
            return f"{self.ministry_label}: READY FOR PRODUCTION IMPORT"
        return (
            f"{self.ministry_label}: NOT READY --"
            f" {len(self.unmet)} criteria unmet,"
            f" {len(self.blockers)} blocker(s)"
        )


def evaluate_readiness(report: DryRunReport) -> ReadinessVerdict:
    """Evaluate one dry run against every gate an import has to clear."""
    reading = report.reading
    matrix = report.matrix
    identity = report.reconciliation

    structure_ok = report.fatal_error is None and reading is not None
    # Every declared staffing position must actually have a column. A source
    # missing one is readable and still wrong: it would import a ministry that
    # silently never staffs that role.
    roles_covered = structure_ok and not reading.unknown_role_labels
    # A criterion that depends on having read the source is not "failed"
    # because the answer is zero -- it was never asked. Saying so keeps a
    # structural failure from reading like seven separate data problems.
    _unevaluated = "not evaluated -- the source could not be read"
    identity_ok = (
        identity is not None
        and identity.fully_reconciled
        and not report.unmatched_assignment_names
    )

    criteria: list[Criterion] = [
        Criterion(
            "source structure validates",
            bool(roles_covered),
            report.fatal_error
            or (
                f"{len(reading.unknown_role_labels)} unknown role label(s)"
                if structure_ok and reading.unknown_role_labels
                else "the declared shape read cleanly"
            ),
        ),
        Criterion(
            "role vocabulary is authoritative",
            report.role_attestation.present,
            report.role_attestation.describe(what="the role list"),
        ),
        Criterion(
            "identity reconciliation has no unresolved people",
            identity_ok,
            "every source person maps to a canonical Person or is declared new"
            if identity_ok
            else "unresolved or unmatched source people remain",
        ),
        Criterion(
            "qualifications are authoritative",
            matrix is not None
            and matrix.authoritative
            and not report.qualification_gaps,
            matrix.attestation.describe(what="the matrix")
            if matrix is not None
            else "no approval matrix was supplied",
        ),
        Criterion(
            "assignment/availability contradictions resolved",
            structure_ok and not report.availability_contradictions,
            _unevaluated
            if not structure_ok
            else f"{len(report.availability_contradictions)} contradiction(s)"
            " awaiting a Ministry-Head decision",
        ),
        Criterion(
            "ambiguous source columns resolved or declared irrelevant",
            structure_ok and not reading.ambiguous_columns,
            _unevaluated
            if not structure_ok
            else f"{len(reading.ambiguous_columns)} column(s) awaiting a"
            " Ministry-Head definition",
        ),
        Criterion(
            "no hard-rule contradictions in the source",
            structure_ok and not report.double_bookings,
            _unevaluated
            if not structure_ok
            else f"{len(report.double_bookings)} person/date pair(s) hold two"
            " positions on one date",
        ),
        Criterion(
            "dry run produced zero blocking errors",
            not report.blockers,
            f"{len(report.blockers)} blocker(s)",
        ),
    ]

    return ReadinessVerdict(
        ministry_label=report.ministry_label,
        ready=all(c.met for c in criteria),
        criteria=tuple(criteria),
        blockers=report.blockers,
        warnings=report.warnings,
    )


def render_verdict(verdict: ReadinessVerdict) -> str:
    lines = [
        "-- PRODUCTION-READINESS GATE ---------------------------------------",
    ]
    for criterion in verdict.criteria:
        mark = "PASS" if criterion.met else "FAIL"
        lines.append(f"  [{mark}] {criterion.name}")
        if not criterion.met:
            lines.append(f"         {criterion.detail}")
    lines.append("")
    lines.append(f"  {verdict.verdict_line}")
    if not verdict.ready:
        lines.append(
            "  Parsing the file is not readiness. Every criterion above has to"
        )
        lines.append(
            "  hold, and most of them are church facts rather than code."
        )
    return "\n".join(lines)
