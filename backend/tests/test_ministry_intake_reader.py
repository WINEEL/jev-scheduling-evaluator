"""Reading a grid exactly as declared -- and refusing anything else (Task 82).

Offline, and on **synthetic** grids only: every name below is a placeholder,
and no real church file is read, committed or needed by this suite.

The three refusals under test are the three ways a real sheet goes wrong: a
column nobody described, a role column whose header contradicts the config,
and an availability answer the config does not list. Each of them, read
hopefully, silently changes who gets scheduled.
"""

from __future__ import annotations

import csv

import pytest

from app.scheduling.input import AvailabilityState
from scripts.ministry_intake.config import parse_config
from scripts.ministry_intake.reader import SourceReadError, read_grid, read_source


def base_config(**grid_overrides) -> dict:
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
        "roles": {"staffing": ["Lead", "Support"], "lead_role": "Lead"},
        "source": {
            "format": "csv",
            "shape": "date_major_grid",
            "year_hint": 2026,
            "grid": grid,
        },
        "availability": {
            "available": ["yes"],
            "unavailable": ["no"],
            "blank": "no_response",
        },
    }


#: header, then two dated rows. Columns 4 and 5 are the assignment block.
GRID = [
    ["Date", "Ann Placeholder", "Ben Placeholder", "Cal Placeholder", "Lead", "Support"],
    ["2026-10-04", "yes", "yes", "no", "Ann Placeholder", "Ben Placeholder"],
    ["2026-10-11", "no", "yes", "yes", "Cal Placeholder", "Ben Placeholder"],
]


def read(rows, raw=None):
    config = parse_config(raw or base_config())
    return read_grid(config, [list(r) for r in rows], source_label="example.csv")


def test_a_declared_grid_reads_people_dates_availability_and_assignments():
    reading = read(GRID)
    assert [p.display_name for p in reading.people] == [
        "Ann Placeholder",
        "Ben Placeholder",
        "Cal Placeholder",
    ]
    assert len(reading.dates) == 2
    assert reading.availability[("ann placeholder", reading.dates[0])] is (
        AvailabilityState.AVAILABLE
    )
    assert reading.availability[("cal placeholder", reading.dates[0])] is (
        AvailabilityState.UNAVAILABLE
    )
    assert len(reading.assignments) == 4
    assert reading.staffing_roles == ("Lead", "Support")


def test_role_columns_are_found_by_position_when_the_header_does_not_name_them():
    """The case no detector can handle: the role columns carry no header."""
    rows = [list(r) for r in GRID]
    rows[0][4] = ""
    rows[0][5] = ""
    reading = read(rows)
    assert {a.role for a in reading.assignments} == {"Lead", "Support"}
    assert reading.unknown_role_labels == ()


def test_a_column_the_config_does_not_account_for_stops_the_run():
    rows = [r + ["Setup"] for r in GRID[:1]] + [
        r + ["x"] for r in GRID[1:]
    ]
    with pytest.raises(SourceReadError, match="not accounted for"):
        read(rows)


def test_an_unexplained_column_can_be_declared_irrelevant_with_a_reason():
    rows = [GRID[0] + ["Setup"]] + [r + ["x"] for r in GRID[1:]]
    raw = base_config(
        ignore_columns=[{"index": 6, "reason": "the Head says this is unused"}]
    )
    reading = read(rows, raw)
    assert reading.ignored_columns == ((6, "the Head says this is unused"),)
    assert reading.ambiguous_columns == ()


def test_an_unexplained_column_can_be_declared_ambiguous_and_is_reported():
    rows = [GRID[0] + ["Setup"]] + [r + ["x"] for r in GRID[1:]]
    raw = base_config(
        ambiguous_columns=[
            {"index": 6, "question": "the Setup ministry, or an internal task?"}
        ]
    )
    reading = read(rows, raw)
    assert len(reading.ambiguous_columns) == 1
    column = reading.ambiguous_columns[0]
    assert column.index == 6
    assert column.header == "Setup"
    assert column.non_empty_cells == 2
    assert "internal task" in column.question


def test_a_role_columns_header_contradicting_the_config_stops_the_run():
    """The config says column 4 is Lead and the sheet says Support."""
    rows = [list(r) for r in GRID]
    rows[0][4] = "Support"
    rows[0][5] = "Lead"
    with pytest.raises(SourceReadError, match="not safe to decide which"):
        read(rows)


def test_an_unlisted_availability_token_stops_the_run():
    rows = [list(r) for r in GRID]
    rows[1][1] = "maybe"
    with pytest.raises(SourceReadError, match="not listed in \\[availability\\]"):
        read(rows)


def test_a_blank_cell_writes_no_row_when_that_is_what_the_config_declares():
    rows = [list(r) for r in GRID]
    rows[1][1] = ""
    reading = read(rows)
    assert ("ann placeholder", reading.dates[0]) not in reading.availability
    assert reading.blank_availability_cells == 1


def test_a_blank_cell_becomes_a_state_only_when_the_config_says_so():
    rows = [list(r) for r in GRID]
    rows[1][1] = ""
    raw = base_config()
    raw["availability"]["blank"] = "unavailable"
    reading = read(rows, raw)
    assert reading.availability[("ann placeholder", reading.dates[0])] is (
        AvailabilityState.UNAVAILABLE
    )


def test_a_backup_tier_is_read_when_the_ministry_declares_one():
    """Kids' third answer. Generic since Task 52; no Kids-specific state."""
    rows = [list(r) for r in GRID]
    rows[1][1] = "if need be"
    raw = base_config()
    raw["availability"]["backup"] = ["if need be"]
    reading = read(rows, raw)
    assert reading.availability[("ann placeholder", reading.dates[0])] is (
        AvailabilityState.BACKUP
    )


def test_two_roster_columns_for_one_person_stop_the_run():
    rows = [list(r) for r in GRID]
    rows[0][2] = "Ann Placeholder"
    with pytest.raises(SourceReadError, match="two roster columns"):
        read(rows)


def test_a_duplicated_date_stops_the_run():
    rows = [list(r) for r in GRID] + [list(GRID[1])]
    with pytest.raises(SourceReadError, match="appears twice"):
        read(rows)


def test_rows_below_the_grid_are_skipped_and_counted():
    rows = [list(r) for r in GRID] + [["Total Serving", "4", "", "", "", ""]]
    reading = read(rows)
    assert len(reading.dates) == 2
    assert reading.undated_rows == 1


def test_a_non_staffing_role_is_read_and_marked_as_such():
    rows = [GRID[0] + ["Shadow"]] + [
        GRID[1] + ["Cal Placeholder"],
        GRID[2] + [""],
    ]
    raw = base_config(
        role_columns=[
            {"role": "Lead", "index": 4},
            {"role": "Support", "index": 5},
            {"role": "Shadow", "index": 6},
        ]
    )
    raw["roles"]["recorded_non_staffing"] = ["Shadow"]
    reading = read(rows, raw)
    assert reading.non_staffing_roles == ("Shadow",)
    shadow = [a for a in reading.assignments if a.role == "Shadow"]
    assert len(shadow) == 1
    assert shadow[0].staffing is False


def test_a_declared_cell_normalizer_strips_a_trailing_qualifier():
    """AV writes shadow cells 'Name (AV - Soundboard)'."""
    rows = [GRID[0] + ["Shadow"]] + [
        GRID[1] + ["Cal Placeholder (AV - Support)"],
        GRID[2] + [""],
    ]
    raw = base_config(
        role_columns=[
            {"role": "Lead", "index": 4},
            {"role": "Support", "index": 5},
            {
                "role": "Shadow",
                "index": 6,
                "cell_normalizers": ["drop_trailing_parenthetical"],
            },
        ]
    )
    raw["roles"]["recorded_non_staffing"] = ["Shadow"]
    reading = read(rows, raw)
    shadow = [a for a in reading.assignments if a.role == "Shadow"][0]
    assert shadow.match_key == "cal placeholder"
    assert shadow.match_key in reading.roster_keys


def test_the_volunteer_block_can_be_derived_from_the_declared_role_positions():
    raw = base_config(volunteer_columns={"mode": "between_date_and_roles"})
    reading = read(GRID, raw)
    assert len(reading.people) == 3


def test_a_tab_declares_its_own_layout_and_a_mismatched_one_fails_closed(tmp_path):
    """Workbook tabs drift; the declaration moves with them, or it refuses."""
    raw = base_config()
    raw["source"]["tabs"] = [
        {
            "name": "Old Quarter",
            "grid": {
                "date_column": {"index": 0},
                "volunteer_columns": {
                    "mode": "explicit_range",
                    "first_index": 1,
                    "last_index": 2,
                },
                "role_columns": [
                    {"role": "Lead", "index": 3},
                    {"role": "Support", "index": 4},
                ],
            },
        }
    ]
    config = parse_config(raw)
    drifted = [
        ["Date", "Ann Placeholder", "Ben Placeholder", "Lead", "Support"],
        ["2026-10-04", "yes", "yes", "Ann Placeholder", "Ben Placeholder"],
    ]
    reading = read_grid(
        config, drifted, source_label="book:Old Quarter", tab="Old Quarter"
    )
    assert len(reading.people) == 2

    # The same tab read at the fallback offsets is refused, not misread.
    with pytest.raises(SourceReadError, match="does not fit this sheet"):
        read_grid(config, drifted, source_label="book:Other", tab="Other Quarter")


def test_reading_a_csv_from_disk_matches_reading_it_in_memory(tmp_path):
    path = tmp_path / "example.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(GRID)
    config = parse_config(base_config())
    from_disk = read_source(config, path)
    assert len(from_disk.people) == 3
    assert len(from_disk.dates) == 2
