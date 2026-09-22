"""The dry run and the readiness gate (Task 82).

Offline, synthetic. Every fixture below is built in code; no real church file
is read, and none is needed.

Two things are under test and they are different. The **dry run** reports what
it found, separating blockers from warnings. The **readiness gate** decides,
and the failure mode it exists to prevent is the seductive one: a source that
parses cleanly feeling like a source that is ready. It is not -- parsing proves
the shape is understood, and says nothing about whether the church facts inside
it are known.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.ministry_intake.config import parse_config
from scripts.ministry_intake.dry_run import run_dry_run
from scripts.ministry_intake.readiness import evaluate_readiness
from scripts.ministry_intake.report import render

PACKAGE = Path(__file__).resolve().parents[1] / "scripts" / "ministry_intake"

ATTESTED = "# approved_by: A Head\n# approved_on: 2026-09-20\n"
IDENTITY_HEADER = "source_name,decision,canonical_person_id,existing_ministry,note\n"

GRID = [
    ["Date", "Ann Placeholder", "Ben Placeholder", "Cal Placeholder", "Lead", "Support"],
    ["2026-10-04", "yes", "yes", "yes", "Ann Placeholder", "Ben Placeholder"],
    ["2026-10-11", "yes", "yes", "yes", "Cal Placeholder", "Ann Placeholder"],
]


def raw_config(**grid_overrides) -> dict:
    grid = {
        "date_column": {"index": 0},
        "volunteer_columns": {
            "mode": "explicit_range",
            "first_index": 1,
            "last_index": 3,
        },
        "role_columns": [
            {"role": "Lead", "index": 4},
            {"role": "Support", "index": 5},
        ],
    }
    grid.update(grid_overrides)
    return {
        "ministry": {"label": "Example"},
        "roles": {
            "staffing": ["Lead", "Support"],
            "lead_role": "Lead",
            "approved_by": "A Head",
            "approved_on": "2026-09-20",
        },
        "source": {
            "format": "csv",
            "shape": "date_major_grid",
            "year_hint": 2026,
            "grid": grid,
        },
        "availability": {"available": ["yes"], "unavailable": ["no"]},
    }


def write_csv(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("\n".join(",".join(cell for cell in row) for row in rows) + "\n")
    return path


def complete(tmp_path, *, rows=None, raw=None):
    """A ministry with every authoritative input present and consistent."""
    source = write_csv(tmp_path, "source.csv", rows or GRID)
    identity = tmp_path / "identity.csv"
    identity.write_text(
        IDENTITY_HEADER
        + "Ann Placeholder,map_to_existing,101,Setup,\n"
        + "Ben Placeholder,create_new,,,\n"
        + "Cal Placeholder,create_new,,,\n",
        encoding="utf-8",
    )
    approvals = tmp_path / "approvals.csv"
    approvals.write_text(
        ATTESTED
        + "source_name,Lead,Support\n"
        + "Ann Placeholder,Y,Y\n"
        + "Ben Placeholder,,Y\n"
        + "Cal Placeholder,Y,Y\n",
        encoding="utf-8",
    )
    return dict(
        config=parse_config(raw or raw_config()),
        source=source,
        identity_file=identity,
        qualification_file=approvals,
    )


def codes(report):
    return {f.code for f in report.findings}


# -- the gate ------------------------------------------------------------


def test_a_fully_supplied_ministry_is_ready(tmp_path):
    report = run_dry_run(**complete(tmp_path))
    assert report.blockers == ()
    verdict = evaluate_readiness(report)
    assert verdict.ready is True
    assert verdict.unmet == ()


def test_parsing_alone_is_not_readiness(tmp_path):
    """The whole point of the gate: the source reads, and nothing else is known."""
    source = write_csv(tmp_path, "source.csv", GRID)
    report = run_dry_run(config=parse_config(raw_config()), source=source)
    assert report.fatal_error is None
    assert report.reading is not None and len(report.reading.people) == 3

    verdict = evaluate_readiness(report)
    assert verdict.ready is False
    unmet = {c.name for c in verdict.unmet}
    assert "source structure validates" not in unmet
    assert "identity reconciliation has no unresolved people" in unmet
    assert "qualifications are authoritative" in unmet


@pytest.mark.parametrize(
    "drop, criterion",
    [
        ("identity_file", "identity reconciliation has no unresolved people"),
        ("qualification_file", "qualifications are authoritative"),
    ],
)
def test_readiness_stays_false_with_any_authoritative_input_missing(
    tmp_path, drop, criterion
):
    inputs = complete(tmp_path)
    inputs[drop] = None
    verdict = evaluate_readiness(run_dry_run(**inputs))
    assert verdict.ready is False
    assert criterion in {c.name for c in verdict.unmet}


def test_an_unattested_role_list_keeps_a_ministry_blocked(tmp_path):
    raw = raw_config()
    raw["roles"]["approved_by"] = "<the Ministry Head's name>"
    report = run_dry_run(**complete(tmp_path, raw=raw))
    assert "ROLE_LIST_NOT_ATTESTED" in codes(report)
    verdict = evaluate_readiness(report)
    assert verdict.ready is False
    assert "role vocabulary is authoritative" in {c.name for c in verdict.unmet}


def test_an_unattested_matrix_keeps_a_ministry_blocked(tmp_path):
    inputs = complete(tmp_path)
    inputs["qualification_file"].write_text(
        "source_name,Lead,Support\nAnn Placeholder,Y,Y\nBen Placeholder,,Y\n"
        "Cal Placeholder,Y,Y\n",
        encoding="utf-8",
    )
    report = run_dry_run(**inputs)
    assert "QUALIFICATIONS_NOT_ATTESTED" in codes(report)
    assert evaluate_readiness(report).ready is False


def test_blockers_and_warnings_are_returned_separately(tmp_path):
    rows = [list(r) for r in GRID] + [["Total Serving", "2", "", "", "", ""]]
    inputs = complete(tmp_path, rows=rows)
    inputs["identity_file"] = None
    report = run_dry_run(**inputs)

    assert "IDENTITY_FILE_MISSING" in {f.code for f in report.blockers}
    assert "ROWS_WITHOUT_A_DATE" in {f.code for f in report.warnings}
    assert not (set(report.blockers) & set(report.warnings))

    verdict = evaluate_readiness(report)
    assert verdict.blockers and verdict.warnings
    assert verdict.ready is False


def test_a_warning_alone_never_changes_the_verdict(tmp_path):
    rows = [list(r) for r in GRID] + [["Total Serving", "2", "", "", "", ""]]
    report = run_dry_run(**complete(tmp_path, rows=rows))
    assert "ROWS_WITHOUT_A_DATE" in {f.code for f in report.warnings}
    assert report.blockers == ()
    assert evaluate_readiness(report).ready is True


# -- contradictions ------------------------------------------------------


def test_an_assignment_contradicting_availability_is_reported_not_resolved(tmp_path):
    rows = [list(r) for r in GRID]
    rows[1][1] = "no"  # Ann is unavailable on the date she is rostered to Lead
    report = run_dry_run(**complete(tmp_path, rows=rows))

    assert len(report.availability_contradictions) == 1
    contradiction = report.availability_contradictions[0]
    assert contradiction.role == "Lead"
    assert contradiction.person == "Ann Placeholder"
    assert contradiction.stated_availability == "UNAVAILABLE"

    finding = next(
        f for f in report.findings if f.code == "ASSIGNMENT_CONTRADICTS_AVAILABILITY"
    )
    assert finding.category == "NEEDS MINISTRY-HEAD DECISION"
    assert finding in report.blockers
    assert evaluate_readiness(report).ready is False

    # Reported, and left exactly as found -- the source is not rewritten.
    assert report.reading.assignments[0].raw_name == "Ann Placeholder"


def test_an_ambiguous_column_is_reported_as_needing_a_definition(tmp_path):
    rows = [GRID[0] + ["Setup"]] + [r + ["x"] for r in GRID[1:]]
    raw = raw_config(
        ambiguous_columns=[
            {"index": 6, "question": "the Setup ministry, or an internal task?"}
        ]
    )
    report = run_dry_run(**complete(tmp_path, rows=rows, raw=raw))
    finding = next(f for f in report.findings if f.code == "AMBIGUOUS_SOURCE_COLUMN")
    assert finding.category == "NEEDS MINISTRY-HEAD DEFINITION"
    assert finding in report.blockers
    assert evaluate_readiness(report).ready is False


def test_declaring_that_column_irrelevant_with_a_reason_clears_the_gate(tmp_path):
    """'Resolved OR explicitly declared irrelevant' -- the second half."""
    rows = [GRID[0] + ["Setup"]] + [r + ["x"] for r in GRID[1:]]
    raw = raw_config(
        ignore_columns=[
            {"index": 6, "reason": "the Head confirms this column is unused"}
        ]
    )
    report = run_dry_run(**complete(tmp_path, rows=rows, raw=raw))
    assert "AMBIGUOUS_SOURCE_COLUMN" not in codes(report)
    assert evaluate_readiness(report).ready is True


def test_one_person_twice_on_one_date_is_a_hard_rule_contradiction(tmp_path):
    rows = [list(r) for r in GRID]
    rows[1][5] = "Ann Placeholder"  # already the Lead that date
    report = run_dry_run(**complete(tmp_path, rows=rows))
    assert len(report.double_bookings) == 1
    assert report.double_bookings[0].roles == ("Lead", "Support")
    assert "SOURCE_BREAKS_ONE_POSITION_PER_EVENT" in {f.code for f in report.blockers}
    assert evaluate_readiness(report).ready is False


def test_an_unreadable_source_is_a_blocker_and_stops_everything_downstream(tmp_path):
    rows = [r + ["Setup"] for r in GRID]
    report = run_dry_run(**complete(tmp_path, rows=rows))
    assert report.fatal_error is not None
    assert "SOURCE_STRUCTURE_UNREADABLE" in {f.code for f in report.blockers}
    assert report.reading is None
    verdict = evaluate_readiness(report)
    assert verdict.ready is False
    assert "source structure validates" in {c.name for c in verdict.unmet}


# -- identity ------------------------------------------------------------


def test_an_unresolved_identity_is_named_and_blocks(tmp_path):
    inputs = complete(tmp_path)
    inputs["identity_file"].write_text(
        IDENTITY_HEADER
        + "Ann Placeholder,map_to_existing,101,,\n"
        + "Ben Placeholder,create_new,,,\n"
        + "Cal Placeholder,unresolved,,,two people could be meant\n",
        encoding="utf-8",
    )
    report = run_dry_run(**inputs)
    finding = next(f for f in report.findings if f.code == "IDENTITY_UNRESOLVED")
    assert "Cal Placeholder" in " ".join(finding.detail)
    assert finding in report.blockers
    assert evaluate_readiness(report).ready is False


def test_a_prepared_name_that_is_not_on_the_roster_is_never_matched_by_similarity(
    tmp_path,
):
    rows = [list(r) for r in GRID]
    rows[1][4] = "Ann"  # a bare first name, where the roster has the full one
    report = run_dry_run(**complete(tmp_path, rows=rows))
    finding = next(
        f for f in report.findings if f.code == "PREPARED_NAME_NOT_ON_ROSTER"
    )
    assert "Ann" in finding.detail
    assert finding in report.blockers
    assert evaluate_readiness(report).ready is False


def test_a_cross_ministry_mapping_is_reported_and_does_not_block(tmp_path):
    report = run_dry_run(**complete(tmp_path))
    finding = next(f for f in report.findings if f.code == "CROSS_MINISTRY_IDENTITY")
    assert "Setup" in " ".join(finding.detail)
    assert finding not in report.blockers
    assert evaluate_readiness(report).ready is True


# -- qualifications ------------------------------------------------------


def test_history_is_reported_but_never_becomes_an_approval(tmp_path):
    inputs = complete(tmp_path)
    inputs["qualification_file"] = None
    report = run_dry_run(**inputs)

    # Everybody in the source has a historical placement...
    assert report.historical_roles["ann placeholder"] == frozenset({"Lead", "Support"})
    # ...and it buys them nothing.
    assert report.matrix is None
    assert "QUALIFICATIONS_MISSING" in {f.code for f in report.blockers}
    assert evaluate_readiness(report).ready is False


def test_a_person_with_no_approval_row_blocks_rather_than_defaulting(tmp_path):
    inputs = complete(tmp_path)
    inputs["qualification_file"].write_text(
        ATTESTED + "source_name,Lead,Support\nAnn Placeholder,Y,Y\n", encoding="utf-8"
    )
    report = run_dry_run(**inputs)
    finding = next(
        f for f in report.findings if f.code == "QUALIFICATIONS_INCOMPLETE"
    )
    assert "Ben Placeholder" in finding.detail
    assert finding in report.blockers


def test_a_prepared_placement_the_matrix_does_not_approve_is_a_warning(tmp_path):
    """The matrix and the sheet disagree. That does not make the matrix wrong."""
    inputs = complete(tmp_path)
    inputs["qualification_file"].write_text(
        ATTESTED
        + "source_name,Lead,Support\n"
        + "Ann Placeholder,,Y\n"
        + "Ben Placeholder,,Y\n"
        + "Cal Placeholder,Y,Y\n",
        encoding="utf-8",
    )
    report = run_dry_run(**inputs)
    assert len(report.unapproved_prepared_assignments) == 1
    finding = next(
        f for f in report.findings if f.code == "PREPARED_PLACEMENT_NOT_APPROVED"
    )
    assert finding in report.warnings
    assert finding.category == "NEEDS MINISTRY-HEAD DECISION"
    assert evaluate_readiness(report).ready is True


# -- the guarantee -------------------------------------------------------


def test_the_intake_package_imports_no_database_machinery():
    """Structural, so it cannot be promised and then quietly broken."""
    forbidden = ("sqlalchemy", "app.models", "app.services", "app.db")
    offenders: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(name == f or name.startswith(f + ".") for f in forbidden):
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], (
        "the dry-run path must have no way to reach a database: " + str(offenders)
    )


def test_a_dry_run_creates_no_file_beside_its_inputs(tmp_path):
    inputs = complete(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    run_dry_run(**inputs)
    assert {p.name for p in tmp_path.iterdir()} == before


def test_the_report_can_be_rendered_without_any_name(tmp_path):
    inputs = complete(tmp_path)
    inputs["identity_file"] = None
    report = run_dry_run(**inputs)
    text = render(report, show_names=False)
    assert "Ann Placeholder" not in text
    assert "Ben Placeholder" not in text
    assert "withheld" in text
    # And with names, the rows somebody has to act on are actually visible.
    assert "Ann Placeholder" in render(report, show_names=True)
