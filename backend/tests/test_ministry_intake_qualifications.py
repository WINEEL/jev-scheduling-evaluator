"""The approval matrix, and the wall between it and history (Task 82).

Offline, synthetic names only.

The single property worth more than the rest: **history never becomes
authority**. There is no flag, no fallback and no seeding path from observed
placements into approvals, and the last test here asserts that by construction
rather than by reading the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.ministry_intake.config import parse_config
from scripts.ministry_intake.qualifications import (
    QualificationFileError,
    historical_reference,
    load_matrix,
    template_text,
)
from scripts.ministry_intake.reader import read_grid

TEMPLATES = Path(__file__).resolve().parents[1] / "scripts" / "intake_templates"


def config():
    return parse_config(
        {
            "ministry": {"label": "Example"},
            "roles": {
                "staffing": ["Lead", "Support"],
                "recorded_non_staffing": ["Shadow"],
                "lead_role": "Lead",
                "aliases": {"team lead": "Lead"},
            },
            "source": {
                "format": "csv",
                "shape": "date_major_grid",
                "year_hint": 2026,
                "grid": {
                    "date_column": {"index": 0},
                    "volunteer_columns": {
                        "mode": "explicit_range",
                        "first_index": 1,
                        "last_index": 3,
                    },
                    "role_columns": [
                        {"role": "Lead", "index": 4},
                        {"role": "Support", "index": 5},
                        {"role": "Shadow", "index": 6},
                    ],
                },
            },
            "availability": {"available": ["yes"], "unavailable": ["no"]},
        }
    )


ATTESTED = "# approved_by: A Head\n# approved_on: 2026-09-20\n"


def write(tmp_path, text):
    path = tmp_path / "approvals.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_attested_matrix_is_authoritative(tmp_path):
    path = write(
        tmp_path,
        ATTESTED
        + "source_name,Lead,Support\n"
        + "Ann Placeholder,Y,Y\n"
        + "Ben Placeholder,,Y\n",
    )
    matrix = load_matrix(path, config=config())
    assert matrix.authoritative is True
    assert matrix.approvals["ann placeholder"] == frozenset({"Lead", "Support"})
    assert matrix.approvals["ben placeholder"] == frozenset({"Support"})
    assert matrix.approved_for("Lead") == ("Ann Placeholder",)


def test_a_matrix_nobody_signed_is_a_draft(tmp_path):
    path = write(tmp_path, "source_name,Lead,Support\nAnn Placeholder,Y,\n")
    matrix = load_matrix(path, config=config())
    assert matrix.attestation.present is False
    assert matrix.authoritative is False
    assert "NOT ATTESTED" in matrix.attestation.describe()


def test_a_template_placeholder_is_not_an_attestation(tmp_path):
    path = write(
        tmp_path,
        "# approved_by: <the Ministry Head's name>\n"
        "# approved_on: <date, as YYYY-MM-DD>\n"
        "source_name,Lead,Support\nAnn Placeholder,Y,\n",
    )
    assert load_matrix(path, config=config()).attestation.present is False


def test_a_row_of_blanks_is_a_real_answer(tmp_path):
    """'Approved for nothing' and 'not considered' must not look alike."""
    path = write(
        tmp_path, ATTESTED + "source_name,Lead,Support\nAnn Placeholder,,\n"
    )
    matrix = load_matrix(path, config=config())
    assert matrix.approvals["ann placeholder"] == frozenset()


def test_a_column_that_is_not_a_declared_role_is_reported_not_accepted(tmp_path):
    path = write(
        tmp_path,
        ATTESTED + "source_name,Lead,Support,Camera\nAnn Placeholder,Y,,Y\n",
    )
    matrix = load_matrix(path, config=config())
    assert matrix.unknown_role_columns == ("Camera",)
    assert matrix.authoritative is False


def test_a_missing_role_column_is_reported(tmp_path):
    path = write(tmp_path, ATTESTED + "source_name,Lead\nAnn Placeholder,Y\n")
    matrix = load_matrix(path, config=config())
    assert matrix.missing_role_columns == ("Support",)
    assert matrix.authoritative is False


def test_a_non_staffing_role_may_not_be_approved_for(tmp_path):
    """Shadow is recorded, never staffed; it is not a position to approve."""
    path = write(
        tmp_path, ATTESTED + "source_name,Lead,Support,Shadow\nAnn Placeholder,Y,,Y\n"
    )
    matrix = load_matrix(path, config=config())
    assert matrix.unknown_role_columns == ("Shadow",)


def test_a_declared_alias_names_the_same_column(tmp_path):
    path = write(
        tmp_path, ATTESTED + "source_name,Team Lead,Support\nAnn Placeholder,Y,\n"
    )
    matrix = load_matrix(path, config=config())
    assert matrix.unknown_role_columns == ()
    assert matrix.approvals["ann placeholder"] == frozenset({"Lead"})


def test_the_same_person_twice_with_the_same_answer_is_untidy(tmp_path):
    path = write(
        tmp_path,
        ATTESTED
        + "source_name,Lead,Support\nAnn Placeholder,Y,\nann placeholder,Y,\n",
    )
    matrix = load_matrix(path, config=config())
    assert matrix.duplicate_rows == ("Ann Placeholder",)
    assert matrix.conflicting_rows == ()
    assert matrix.authoritative is True


def test_the_same_person_twice_with_different_answers_is_not_authoritative(tmp_path):
    path = write(
        tmp_path,
        ATTESTED
        + "source_name,Lead,Support\nAnn Placeholder,Y,\nAnn Placeholder,,Y\n",
    )
    matrix = load_matrix(path, config=config())
    assert matrix.conflicting_rows == ("Ann Placeholder",)
    assert matrix.authoritative is False, (
        "a signature on a file that says two different things is not authority"
    )


def test_an_unreadable_approval_token_stops_the_run(tmp_path):
    path = write(
        tmp_path, ATTESTED + "source_name,Lead,Support\nAnn Placeholder,sometimes,\n"
    )
    with pytest.raises(QualificationFileError, match="neither an approval nor"):
        load_matrix(path, config=config())


def test_a_row_with_no_name_stops_the_run(tmp_path):
    path = write(tmp_path, ATTESTED + "source_name,Lead,Support\n,Y,Y\n")
    with pytest.raises(QualificationFileError, match="source_name is blank"):
        load_matrix(path, config=config())


def test_a_file_without_a_source_name_column_stops_the_run(tmp_path):
    path = write(tmp_path, ATTESTED + "who,Lead\nAnn Placeholder,Y\n")
    with pytest.raises(QualificationFileError, match="needs a 'source_name'"):
        load_matrix(path, config=config())


# -- history is reference, never authority -------------------------------

GRID = [
    ["Date", "Ann Placeholder", "Ben Placeholder", "Cal Placeholder",
     "Lead", "Support", "Shadow"],
    ["2026-10-04", "yes", "yes", "yes",
     "Ann Placeholder", "Ben Placeholder", "Cal Placeholder"],
]


def test_historical_placements_are_reported_separately(tmp_path):
    reading = read_grid(config(), [list(r) for r in GRID], source_label="x.csv")
    observed = historical_reference(reading)
    assert observed["ann placeholder"] == frozenset({"Lead"})
    assert observed["ben placeholder"] == frozenset({"Support"})


def test_shadowing_a_role_grants_nothing():
    reading = read_grid(config(), [list(r) for r in GRID], source_label="x.csv")
    observed = historical_reference(reading)
    assert "cal placeholder" not in observed, (
        "watching a role is not evidence of being cleared to serve it"
    )


def test_history_and_approvals_disagree_and_the_matrix_is_what_stands(tmp_path):
    """The wall, stated as behaviour: history loses, every time."""
    reading = read_grid(config(), [list(r) for r in GRID], source_label="x.csv")
    observed = historical_reference(reading)
    path = write(tmp_path, ATTESTED + "source_name,Lead,Support\nAnn Placeholder,,Y\n")
    matrix = load_matrix(path, config=config())

    # History says Ann has led. The matrix says Support only. Nothing
    # reconciles them, and the matrix is untouched by the disagreement.
    assert observed["ann placeholder"] == frozenset({"Lead"})
    assert matrix.approvals["ann placeholder"] == frozenset({"Support"})
    assert matrix.authoritative is True

    # The two are different types, so one cannot be passed where the other is
    # expected: history is a plain dict and carries no attestation to borrow.
    assert isinstance(observed, dict)
    assert not hasattr(observed, "attestation")


def test_the_emitted_template_has_one_column_per_declared_staffing_role():
    text = template_text(config())
    header = [line for line in text.splitlines() if line.startswith("source_name")][0]
    assert header == "source_name,Lead,Support,notes"
    assert "approved_by" in text and "approved_on" in text
    assert "Shadow" not in header


def test_the_shipped_csv_templates_carry_placeholders_and_no_attestation():
    """A committed template must be unusable as data and unsigned as authority."""
    for path in TEMPLATES.glob("*.csv"):
        text = path.read_text(encoding="utf-8")
        data_rows = [
            line
            for line in text.splitlines()
            if line and not line.startswith("#") and "," in line
        ][1:]
        for row in data_rows:
            assert "Placeholder" in row or "Example" in row, (
                f"{path.name} carries a row that is not obviously a placeholder"
            )
        for line in text.splitlines():
            if line.startswith("# approved_by:") or line.startswith("# approved_on:"):
                assert "<" in line, f"{path.name} ships a filled-in attestation"
