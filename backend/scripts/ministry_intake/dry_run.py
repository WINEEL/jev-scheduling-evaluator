"""One dry run: read everything, decide nothing, write nowhere.

This module imports no SQLAlchemy session, no ORM model and no service, and
there is no parameter that turns it into an import. That is the guarantee
worth having, and it is structural rather than promised -- a test asserts on
the import graph (``test_ministry_intake_dry_run.py``), so a future edit that
reaches for a ``Session`` fails the suite rather than surprising somebody on a
real database.

What it does is assemble the four inputs a ministry needs -- source shape,
identity, approvals, and the contradictions between them -- into one
:class:`~scripts.ministry_intake.report.DryRunReport`, with every problem
classified as a blocker or a warning. It resolves nothing it finds: a
contradiction between a prepared assignment and a stated availability is a
church fact with two candidate answers, and picking one here would be this
code deciding a ministry's business.
"""

from __future__ import annotations

from pathlib import Path

from app.scheduling.input import AvailabilityState

from scripts.ministry_intake import identity as identity_module
from scripts.ministry_intake import qualifications as qualification_module
from scripts.ministry_intake.config import IntakeConfig
from scripts.ministry_intake.findings import (
    CONTRADICTION,
    CROSS_MINISTRY,
    IDENTITY,
    NEEDS_HEAD_DECISION,
    NEEDS_HEAD_DEFINITION,
    QUALIFICATION,
    SOURCE_STRUCTURE,
    Finding,
    blocker,
    info,
    warning,
)
from scripts.ministry_intake.reader import SourceReadError, read_source
from scripts.ministry_intake.report import Contradiction, DoubleBooking, DryRunReport

__all__ = ["run_dry_run"]


def run_dry_run(
    *,
    config: IntakeConfig,
    source: Path,
    tab: str | None = None,
    identity_file: Path | None = None,
    qualification_file: Path | None = None,
) -> DryRunReport:
    """Validate one ministry's source against everything supplied about it."""
    findings: list[Finding] = []
    report = DryRunReport(
        ministry_label=config.ministry_label,
        config_path=config.source_path,
        source_path=Path(source).name,
        declared_format=config.source_format,
        declared_shape=config.shape,
        role_attestation=config.roles.attestation,
        tab=tab,
    )

    # -- the role vocabulary itself -----------------------------------
    if not config.roles.attestation.present:
        findings.append(
            blocker(
                "ROLE_LIST_NOT_ATTESTED",
                QUALIFICATION,
                f"{config.ministry_label}'s role list is declared but not"
                " attested: [roles] carries no approved_by / approved_on.",
                [
                    "A role list is what every later answer is indexed by, so an"
                    " unsigned one makes every approval provisional.",
                    f"Declared: {list(config.roles.staffing)}",
                ],
            )
        )

    # -- the source ---------------------------------------------------
    try:
        reading = read_source(config, Path(source), tab=tab)
    except SourceReadError as error:
        report.fatal_error = str(error)
        findings.append(
            blocker(
                "SOURCE_STRUCTURE_UNREADABLE",
                SOURCE_STRUCTURE,
                "the source does not match the declared shape, so nothing was"
                " read from it",
                [str(error)],
            )
        )
        report.findings = tuple(findings)
        return report

    report.reading = reading

    if reading.unknown_role_labels:
        findings.append(
            blocker(
                "UNKNOWN_ROLE_LABEL",
                SOURCE_STRUCTURE,
                "a declared role column carries a header that is not one of"
                " this ministry's declared roles",
                [
                    f"column {index}: {text!r}"
                    for index, text in reading.unknown_role_labels
                ],
            )
        )

    missing_roles = [
        role for role in config.roles.staffing if role not in reading.staffing_roles
    ]
    if missing_roles:
        findings.append(
            blocker(
                "STAFFING_ROLE_HAS_NO_COLUMN",
                SOURCE_STRUCTURE,
                "the source has no column for every declared staffing position",
                [f"missing: {missing_roles}"],
            )
        )

    for column in reading.ambiguous_columns:
        findings.append(
            blocker(
                "AMBIGUOUS_SOURCE_COLUMN",
                NEEDS_HEAD_DEFINITION,
                f"column {column.index}"
                f" {column.header or '(no header)'!r} has no defined meaning:"
                f" {column.question}",
                [
                    f"{column.non_empty_cells} non-empty cell(s) in this column",
                    "Resolve it by defining what it holds, or declare it"
                    " irrelevant in [source.grid].ignore_columns with a reason.",
                ],
            )
        )

    if reading.undated_rows:
        findings.append(
            warning(
                "ROWS_WITHOUT_A_DATE",
                SOURCE_STRUCTURE,
                f"{reading.undated_rows} non-empty row(s) carry no readable date"
                " and were skipped",
                [
                    "Usually a tally or lookup block below the grid. Confirm"
                    " none of them is an event.",
                ],
            )
        )

    # -- identity -----------------------------------------------------
    decisions = None
    if identity_file is not None:
        try:
            decisions = identity_module.load_decisions(
                Path(identity_file), match_key=config.match_key
            )
        except identity_module.IdentityFileError as error:
            findings.append(
                blocker(
                    "IDENTITY_FILE_INVALID",
                    IDENTITY,
                    "the identity reconciliation file could not be trusted",
                    [str(error)],
                )
            )

    roster_keys = reading.roster_keys
    assignment_keys = {a.match_key for a in reading.assignments}
    off_roster = sorted(
        {
            a.raw_name
            for a in reading.assignments
            if a.match_key not in roster_keys
        }
    )
    reconciliation = identity_module.reconcile(
        reading.people, decisions, also_known=frozenset(assignment_keys)
    )
    report.reconciliation = reconciliation
    report.unmatched_assignment_names = tuple(off_roster)

    if decisions is None:
        findings.append(
            blocker(
                "IDENTITY_FILE_MISSING",
                IDENTITY,
                "no identity reconciliation file was supplied, so every person"
                " in this source is unresolved",
                [
                    f"{len(reading.people)} source people need a decision.",
                    "Silence is not consent to create somebody: an unmapped"
                    " name would become a second canonical Person and split"
                    " that volunteer's schedule in two.",
                ],
            )
        )
    elif reconciliation.unresolved:
        findings.append(
            blocker(
                "IDENTITY_UNRESOLVED",
                IDENTITY,
                f"{len(reconciliation.unresolved)} source person(s) are not"
                " reconciled to a canonical Person",
                [f"{name}: {reason}" for name, reason in reconciliation.unresolved],
            )
        )

    if reconciliation.not_in_source:
        findings.append(
            blocker(
                "IDENTITY_ROW_NAMES_NOBODY",
                IDENTITY,
                "the identity file has row(s) naming people this source does not"
                " contain",
                list(reconciliation.not_in_source),
            )
        )

    if off_roster:
        findings.append(
            blocker(
                "PREPARED_NAME_NOT_ON_ROSTER",
                IDENTITY,
                f"{len(off_roster)} name(s) in the prepared schedule do not"
                " match any roster column",
                [
                    *off_roster,
                    "Not merged with anybody who looks similar. Either the"
                    " roster is missing them or the sheet spells them two ways;"
                    " a person has to say which.",
                ],
            )
        )

    if reconciliation.case_duplicates:
        findings.append(
            warning(
                "DUPLICATE_PEOPLE_IN_SOURCE",
                IDENTITY,
                f"{len(reconciliation.case_duplicates)} person(s) appear under"
                " spellings that differ only by case",
                [str(list(group)) for group in reconciliation.case_duplicates],
            )
        )

    if reconciliation.cross_ministry:
        findings.append(
            info(
                "CROSS_MINISTRY_IDENTITY",
                IDENTITY,
                f"{len(reconciliation.cross_ministry)} person(s) are declared to"
                " already serve in another ministry",
                [
                    f"{person} -> also {ministry}"
                    for person, ministry in sorted(
                        reconciliation.cross_ministry.items()
                    )
                ],
            )
        )

    # -- qualifications -----------------------------------------------
    matrix = None
    if qualification_file is not None:
        try:
            matrix = qualification_module.load_matrix(
                Path(qualification_file), config=config
            )
        except qualification_module.QualificationFileError as error:
            findings.append(
                blocker(
                    "QUALIFICATION_FILE_INVALID",
                    QUALIFICATION,
                    "the qualification matrix could not be read as written",
                    [str(error)],
                )
            )
    report.matrix = matrix

    report.historical_roles = qualification_module.historical_reference(reading)

    if matrix is None:
        findings.append(
            blocker(
                "QUALIFICATIONS_MISSING",
                QUALIFICATION,
                "no Ministry Head approval matrix was supplied",
                [
                    "Historical placements are reported separately and are"
                    " NON-AUTHORITATIVE. They are never promoted into"
                    " approvals: history under-reports anybody who has not had"
                    " a turn, and over-reports anybody pressed into a slot once.",
                ],
            )
        )
    else:
        if not matrix.attestation.present:
            findings.append(
                blocker(
                    "QUALIFICATIONS_NOT_ATTESTED",
                    QUALIFICATION,
                    "the approval matrix carries no approved_by / approved_on,"
                    " so it is a draft rather than the ministry's approved list",
                )
            )
        if matrix.unknown_role_columns:
            findings.append(
                blocker(
                    "QUALIFICATION_UNKNOWN_ROLE",
                    QUALIFICATION,
                    "the approval matrix has column(s) that are not declared"
                    " roles of this ministry",
                    list(matrix.unknown_role_columns),
                )
            )
        if matrix.missing_role_columns:
            findings.append(
                blocker(
                    "QUALIFICATION_ROLE_NOT_COVERED",
                    QUALIFICATION,
                    "the approval matrix has no column for every declared"
                    " staffing position",
                    [f"missing: {list(matrix.missing_role_columns)}"],
                )
            )
        if matrix.conflicting_rows:
            findings.append(
                blocker(
                    "QUALIFICATION_ROWS_CONFLICT",
                    NEEDS_HEAD_DECISION,
                    f"{len(matrix.conflicting_rows)} person(s) appear twice in"
                    " the approval matrix with different answers",
                    list(matrix.conflicting_rows),
                )
            )
        if matrix.duplicate_rows:
            findings.append(
                warning(
                    "QUALIFICATION_ROWS_DUPLICATED",
                    QUALIFICATION,
                    f"{len(matrix.duplicate_rows)} person(s) appear twice in the"
                    " approval matrix with identical answers",
                    list(matrix.duplicate_rows),
                )
            )

        gaps = sorted(
            person.display_name
            for person in reading.people
            if person.match_key not in matrix.approvals
        )
        report.qualification_gaps = tuple(gaps)
        if gaps:
            findings.append(
                blocker(
                    "QUALIFICATIONS_INCOMPLETE",
                    QUALIFICATION,
                    f"{len(gaps)} person(s) in this source have no row in the"
                    " approval matrix",
                    [
                        *gaps,
                        "A missing row is not the same as 'approved for"
                        " nothing'. List them with every cell blank if that is"
                        " what you mean.",
                    ],
                )
            )

    # -- contradictions -----------------------------------------------
    contradictions: list[Contradiction] = []
    no_response: list[Contradiction] = []
    unapproved: list[Contradiction] = []
    by_person_date: dict[tuple[str, object], list[str]] = {}

    for placement in reading.assignments:
        if not placement.staffing:
            continue
        if placement.match_key not in roster_keys:
            continue
        by_person_date.setdefault(
            (placement.raw_name, placement.event_date), []
        ).append(placement.role)

        state = reading.availability.get((placement.match_key, placement.event_date))
        if state is AvailabilityState.UNAVAILABLE:
            contradictions.append(
                Contradiction(
                    event_date=placement.event_date,
                    role=placement.role,
                    person=placement.raw_name,
                    stated_availability="UNAVAILABLE",
                )
            )
        elif state is None:
            no_response.append(
                Contradiction(
                    event_date=placement.event_date,
                    role=placement.role,
                    person=placement.raw_name,
                    stated_availability="NO RESPONSE",
                )
            )

        if matrix is not None and matrix.attestation.present:
            approved = matrix.approvals.get(placement.match_key)
            if approved is not None and placement.role not in approved:
                unapproved.append(
                    Contradiction(
                        event_date=placement.event_date,
                        role=placement.role,
                        person=placement.raw_name,
                        stated_availability="not approved for this role",
                    )
                )

    report.availability_contradictions = tuple(contradictions)
    report.unapproved_prepared_assignments = tuple(unapproved)
    report.double_bookings = tuple(
        DoubleBooking(event_date=date, person=person, roles=tuple(sorted(roles)))
        for (person, date), roles in sorted(
            by_person_date.items(), key=lambda kv: (kv[0][1], kv[0][0])
        )
        if len(roles) > 1
    )

    if contradictions:
        findings.append(
            blocker(
                "ASSIGNMENT_CONTRADICTS_AVAILABILITY",
                NEEDS_HEAD_DECISION,
                f"{len(contradictions)} prepared assignment(s) place somebody"
                " who is marked unavailable on that date",
                [
                    *(
                        f"{c.event_date} {c.role}: {c.person} is marked"
                        f" {c.stated_availability}"
                        for c in contradictions
                    ),
                    "Not resolved here. Either the availability answer is stale"
                    " or the placement was a deliberate override under"
                    " scarcity, and only the Ministry Head knows which.",
                ],
            )
        )

    if report.double_bookings:
        findings.append(
            blocker(
                "SOURCE_BREAKS_ONE_POSITION_PER_EVENT",
                CONTRADICTION,
                f"{len(report.double_bookings)} person/date pair(s) hold two"
                " positions on one date, which the engine's rules forbid",
                [
                    f"{b.event_date}: {b.person} in {list(b.roles)}"
                    for b in report.double_bookings
                ],
            )
        )

    if no_response:
        findings.append(
            warning(
                "ASSIGNMENT_WITHOUT_AN_ANSWER",
                CONTRADICTION,
                f"{len(no_response)} prepared assignment(s) place somebody who"
                " gave no availability answer for that date",
                [
                    f"{c.event_date} {c.role}: {c.person}"
                    for c in no_response[:20]
                ],
            )
        )

    if unapproved:
        findings.append(
            warning(
                "PREPARED_PLACEMENT_NOT_APPROVED",
                NEEDS_HEAD_DECISION,
                f"{len(unapproved)} prepared placement(s) are for a role the"
                " attested matrix does not approve that person for",
                [
                    *(
                        f"{c.event_date} {c.role}: {c.person}"
                        for c in unapproved[:20]
                    ),
                    "History is not authority: this says the matrix and the"
                    " sheet disagree, not that the matrix is wrong.",
                ],
            )
        )

    findings.append(
        info(
            "CROSS_MINISTRY_DATA_ABSENT",
            CROSS_MINISTRY,
            "this source carries no other-ministry commitment data",
            [
                "The church-wide one-ministry-per-person-per-Sunday rule is"
                " enforced at schedule time from the database, not from this"
                " file. Nothing here can confirm or deny it.",
            ],
        )
    )

    report.findings = tuple(findings)
    return report
