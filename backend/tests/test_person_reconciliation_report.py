"""The canonical-person reconciliation worksheet (Task 78, item 5).

Offline: no PostgreSQL, no network. Every identity here is synthetic.

**What this protects.** The worksheet exists because a person map has to be
written by hand against a fifty-name roster, and the failure it must never
enable is the one :mod:`scripts.person_mapping` was built to prevent: a name
match silently becoming an identity. So these tests pin the two properties that
make a *generator* of that file safe to run --

- **it accepts nothing.** A single same-named Person is reported as a question
  (``needs-human-confirmation``) and never as an answer, and the output shape is
  deliberately not the importer's input shape;
- **it reads the roster the source actually states**, and stops where the
  roster stops, so a role label from an adjacent block in the same spreadsheet
  row cannot be offered up as a person.

The database-backed classifications are exercised through the pure helpers;
what needs a Session is the lookup itself, which is
:func:`scripts.reconcile_person_identities._active_people_named` -- one
case-folded equality, and the integration suite covers the engine.
"""

from __future__ import annotations

import csv

import pytest

from scripts.reconcile_person_identities import (
    CONFLICT,
    EXPLICITLY_MAPPED,
    NEEDS_CONFIRMATION,
    NEW_PERSON,
    OUTPUT_COLUMNS,
    Row,
    SourceReadError,
    read_source_names,
    write_report,
)


@pytest.fixture
def write(tmp_path):
    def _write(text: str, name: str = "roster.csv"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path
    return _write


# ==========================================================================
# 1-5 -- Reading the roster the source states, and no more
# ==========================================================================


def test_01_names_down_the_first_column_are_read_in_order(write):
    source = write("\n".join([
        ",10/4/2026,10/11/2026",
        "Alice Example,YES,NO",
        "Bob Example,NO,YES",
    ]))

    names, skipped = read_source_names(source, "first-column")

    assert names == ["Alice Example", "Bob Example"]
    assert skipped == 0


def test_02_names_across_the_header_are_read_in_order(write):
    source = write("\n".join([
        ",Alice Example,Bob Example",
        "10/4/2026,YES,NO",
    ]))

    names, skipped = read_source_names(source, "header")

    assert names == ["Alice Example", "Bob Example"]
    assert skipped == 0


def test_03_a_blank_cell_ends_the_roster_and_the_rest_is_not_read(write):
    # The AV-shaped sheet: availability answers and the prepared assignments
    # share a row, separated by an empty column. Reading through the separator
    # would offer "Soundboard" as somebody to reconcile.
    source = write("\n".join([
        ",Alice Example,Bob Example,,Lead,Soundboard,Slides",
        "10/4/2026,YES,NO,,Alice Example,Bob Example,Alice Example",
    ]))

    names, skipped = read_source_names(source, "header")

    assert names == ["Alice Example", "Bob Example"]
    assert "Soundboard" not in names
    # And the operator is told it happened rather than left with a short roster.
    assert skipped == 4


def test_04_the_corner_cell_of_a_transposed_grid_does_not_end_the_roster(write):
    # It is blank by construction, and is dropped before the separator rule.
    source = write(",Alice Example,Bob Example\n10/4/2026,YES,NO")

    names, _ = read_source_names(source, "header")

    assert names == ["Alice Example", "Bob Example"]


def test_05_an_empty_source_is_refused_rather_than_read_as_no_roster(write):
    with pytest.raises(SourceReadError):
        read_source_names(write(""), "first-column")


# ==========================================================================
# 6-9 -- The worksheet is a question, never an answer
# ==========================================================================


def test_06_the_output_is_not_a_person_map(tmp_path):
    # The importer reads exactly `source_name,person_id`. This file must not be
    # mistakable for one, or a worksheet full of unconfirmed candidates could be
    # passed straight to --person-map.
    assert OUTPUT_COLUMNS[0] == "classification"
    assert set(OUTPUT_COLUMNS) != {"source_name", "person_id"}
    assert "person_id" not in OUTPUT_COLUMNS


def test_07_conflicts_are_written_first(tmp_path):
    path = tmp_path / "report.csv"
    write_report(path, [
        Row(NEW_PERSON, "Bob Example", "", ""),
        Row(EXPLICITLY_MAPPED, "Alice Example", "22", ""),
        Row(CONFLICT, "Carol Example", "31 32", "two people bear this name"),
        Row(NEEDS_CONFIRMATION, "Dan Example", "40", ""),
    ])

    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))

    assert [row["classification"] for row in rows] == [
        CONFLICT,
        NEEDS_CONFIRMATION,
        NEW_PERSON,
        EXPLICITLY_MAPPED,
    ]


def test_08_a_name_containing_a_comma_survives_the_round_trip(tmp_path):
    path = tmp_path / "report.csv"
    write_report(path, [Row(NEW_PERSON, "Example, Alice", "", "")])

    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))

    assert rows[0]["source_name"] == "Example, Alice"


def test_09_a_lone_same_named_person_is_a_question_not_a_match(tmp_path):
    # The single most important line in the file: one identically-named Person
    # is a coincidence until a human says otherwise, so the classification is a
    # request for confirmation and the id is offered as a candidate only.
    path = tmp_path / "report.csv"
    write_report(path, [Row(NEEDS_CONFIRMATION, "Alice Example", "22", "confirm by hand")])

    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))

    assert rows[0]["classification"] == NEEDS_CONFIRMATION
    assert rows[0]["candidate_person_id"] == "22"
    assert rows[0]["classification"] != EXPLICITLY_MAPPED
