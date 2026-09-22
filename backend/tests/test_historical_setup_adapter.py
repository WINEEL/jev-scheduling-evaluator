"""Task 43 -- generic parsing / adapter behaviour for the historical validator.

**No real data.** Every name here is obviously synthetic ("Synthetic Volunteer
N"), every spreadsheet is a handful of hand-written rows. These tests exercise
the format-independent machinery -- token semantics, role and date parsing,
duplicate detection, the IR -> SchedulingInput mapping, and the privacy
guarantees -- not any real church period.
"""

from __future__ import annotations

import datetime
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from app.scheduling.input import AvailabilityState
from app.scheduling.solver import solve_schedule

from scripts.historical_setup.adapter import build_scheduling_input
from scripts.historical_setup.checks import run_all_checks
from scripts.historical_setup.csv_source import (
    SourceConfig,
    SourceError,
    build_roles,
    inspect_sources,
    load_dataset,
)
from scripts.historical_setup.model import (
    CANONICAL_ROLE_NAMES,
    SETUP_TARGET_PER_PERIOD,
    HistoricalAssignment,
    HistoricalDataset,
    HistoricalRequirement,
    HistoricalVolunteer,
)
from scripts.historical_setup.parsing import (
    SETUP_AVAILABILITY_SEMANTICS,
    AmbiguousDateError,
    AvailabilitySemantics,
    UnknownRoleError,
    normalize_role_name,
    parse_availability_token,
    parse_schedule_date,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_PREFIX = "Synthetic Volunteer "


def _syn(n: int) -> str:
    return f"{SYNTHETIC_PREFIX}{n}"


# ==========================================================================
# 1 / 10: real-data and report locations are git-ignored
# ==========================================================================


@pytest.mark.parametrize(
    "relpath",
    [
        "local_data/",
        "local_data/historical_setup/availability.csv",
        "local_data/historical_setup/final_schedule.csv",
        "local_data/reports/setup_generated_schedule.txt",
        "local_data/reports/setup_privacy_safe_summary.txt",
    ],
)
def test_real_data_and_report_paths_are_git_ignored(relpath):
    result = subprocess.run(
        ["git", "check-ignore", "-v", relpath],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{relpath} is NOT git-ignored -- real church data or a real-name report"
        f" could be committed.\n{result.stdout}{result.stderr}"
    )


# ==========================================================================
# 2: O / X / blank availability semantics (the confirmed Setup convention)
# ==========================================================================


@pytest.mark.parametrize(
    "token, expected",
    [
        ("O", AvailabilityState.AVAILABLE),
        ("o", AvailabilityState.AVAILABLE),
        ("Yes", AvailabilityState.AVAILABLE),
        ("✓", AvailabilityState.AVAILABLE),
        ("X", AvailabilityState.UNAVAILABLE),
        ("x", AvailabilityState.UNAVAILABLE),
        ("no", AvailabilityState.UNAVAILABLE),
        ("away", AvailabilityState.UNAVAILABLE),
    ],
)
def test_availability_tokens_map_to_states(token, expected):
    assert parse_availability_token(token) is expected


def test_blank_is_not_a_row():
    # Blank -> None -> no availability row written -> NO_RESPONSE downstream.
    assert parse_availability_token("") is None
    assert parse_availability_token("   ") is None


def test_unknown_availability_token_is_rejected():
    with pytest.raises(ValueError):
        parse_availability_token("maybe?")


def test_alternate_semantics_can_treat_blank_as_available():
    semantics = AvailabilitySemantics(
        available_tokens=frozenset({"y"}),
        unavailable_tokens=frozenset({"n"}),
        blank_is_row=True,
    )
    assert semantics.classify("") is AvailabilityState.AVAILABLE


# ==========================================================================
# 3: role-name normalization
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Setup Lead", "Setup Lead"),
        ("setup lead", "Setup Lead"),
        ("LEAD", "Setup Lead"),
        ("Set-up 1", "Setup Lead"),
        ("Setup 2", "Setup 2"),
        ("setup #3", "Setup 3"),
        ("SETUP FOUR", "Setup 4"),
        ("Setup_5", "Setup 5"),
    ],
)
def test_role_name_normalization(raw, expected):
    assert normalize_role_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "Greeter", "Setup 6", "Parking"])
def test_unknown_role_is_rejected(raw):
    with pytest.raises(UnknownRoleError):
        normalize_role_name(raw)


# ==========================================================================
# 4: date parsing
# ==========================================================================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2025-10-05", datetime.date(2025, 10, 5)),
        ("10/5/2025", datetime.date(2025, 10, 5)),
        ("10/5", datetime.date(2025, 10, 5)),
        ("Oct 5", datetime.date(2025, 10, 5)),
        ("October 5, 2025", datetime.date(2025, 10, 5)),
        ("5 Oct", datetime.date(2025, 10, 5)),
    ],
)
def test_schedule_date_parsing(raw, expected):
    assert parse_schedule_date(raw, year_hint=2025) == expected


def test_ambiguous_date_is_rejected():
    with pytest.raises(AmbiguousDateError):
        parse_schedule_date("not a date", year_hint=2025)


def test_bare_date_far_from_period_is_rejected():
    with pytest.raises(AmbiguousDateError):
        parse_schedule_date("1/1/2000", year_hint=2025)


# ==========================================================================
# CSV fixtures
# ==========================================================================


def _write(path: Path, rows: list[list[str]]) -> None:
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")


def _availability_csv(path: Path) -> None:
    _write(
        path,
        [
            ["Name", "Lead", "2025-10-05", "2025-10-12", "2025-10-19"],
            [_syn(1), "Y", "O", "", "X"],
            [_syn(2), "", "", "X", "O"],
            [_syn(3), "", "X", "O", ""],
            [_syn(4), "yes", "O", "O", "O"],
        ],
    )


def _schedule_csv(path: Path) -> None:
    _write(
        path,
        [
            ["Date", "Setup Lead", "Setup 2", "Setup 3", "Setup 4", "Setup 5"],
            ["2025-10-05", _syn(1), _syn(2), _syn(3), _syn(4), ""],
            ["2025-10-12", _syn(4), _syn(3), _syn(1), _syn(2), ""],
            ["2025-10-19", _syn(2), _syn(4), _syn(1), _syn(3), ""],
        ],
    )


# ==========================================================================
# 5: duplicate person detection
# ==========================================================================


def test_duplicate_volunteer_row_is_rejected(tmp_path):
    path = tmp_path / "availability.csv"
    _write(
        path,
        [
            ["Name", "2025-10-05", "2025-10-12"],
            [_syn(1), "O", "X"],
            [_syn(1), "X", "O"],
        ],
    )
    with pytest.raises(SourceError, match="duplicate volunteer"):
        load_dataset(tmp_path, SourceConfig(year_hint=2025))


def test_duplicate_schedule_date_is_rejected(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    _write(
        tmp_path / "final_schedule.csv",
        [
            ["Date", "Setup Lead", "Setup 2"],
            ["2025-10-05", _syn(1), _syn(2)],
            ["2025-10-05", _syn(3), _syn(4)],
        ],
    )
    with pytest.raises(SourceError, match="duplicate date"):
        load_dataset(tmp_path, SourceConfig(year_hint=2025))


# ==========================================================================
# 6 / 7: malformed availability and unknown role rejected
# ==========================================================================


def test_malformed_availability_token_stops_the_load(tmp_path):
    path = tmp_path / "availability.csv"
    _write(
        path,
        [
            ["Name", "2025-10-05", "2025-10-12"],
            [_syn(1), "O", "probably"],
        ],
    )
    with pytest.raises(SourceError, match="availability semantics"):
        load_dataset(tmp_path, SourceConfig(year_hint=2025))


def test_unknown_role_column_is_ignored_but_known_ones_are_read(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    _write(
        tmp_path / "final_schedule.csv",
        [
            ["Date", "Setup Lead", "Setup 2", "Greeter"],
            ["2025-10-05", _syn(1), _syn(2), _syn(3)],
        ],
    )
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    roles_used = {a.role_name for a in dataset.historical_assignments}
    assert roles_used == {"Setup Lead", "Setup 2"}


def test_long_schedule_with_unknown_role_is_rejected(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    _write(
        tmp_path / "roster.csv",
        [
            ["Date", "Role", "Person"],
            ["2025-10-05", "Parking", _syn(1)],
        ],
    )
    with pytest.raises(UnknownRoleError):
        load_dataset(tmp_path, SourceConfig(year_hint=2025))


# ==========================================================================
# 8 / 9: required-position and qualification mapping
# ==========================================================================


def test_required_positions_and_qualification_mapping(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    _schedule_csv(tmp_path / "final_schedule.csv")
    dataset = load_dataset(
        tmp_path, SourceConfig(period_label="Synthetic Q4", year_hint=2025)
    )
    adapted = build_scheduling_input(dataset)
    si = adapted.scheduling_input

    # 3 Sundays x 5 roles from the schedule grid = 15 required positions.
    assert len(si.event_dates) == 3
    assert si.total_required_positions == 15

    by_name = {c.display_name: c for c in si.candidates}
    lead_role_id = adapted.lead_role_id
    # Only the two volunteers marked in the Lead column are lead-qualified.
    assert by_name[_syn(1)].is_qualified_for(lead_role_id)
    assert by_name[_syn(4)].is_qualified_for(lead_role_id)
    assert not by_name[_syn(2)].is_qualified_for(lead_role_id)
    assert not by_name[_syn(3)].is_qualified_for(lead_role_id)
    # Everyone is qualified for the interchangeable Setup 2-5 roles.
    for role_id in adapted.variety_role_ids:
        assert by_name[_syn(2)].is_qualified_for(role_id)


def test_policy_matches_the_approved_setup_configuration(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    adapted = build_scheduling_input(dataset)
    policy = adapted.policy
    assert policy.allow_no_response is True
    assert policy.target_assignments_per_candidate == SETUP_TARGET_PER_PERIOD == 3
    # Variety set is Setup 2-5 only, never Lead.
    assert adapted.lead_role_id not in policy.role_variety_role_ids
    assert len(policy.role_variety_role_ids) == 4


def test_blank_cell_becomes_no_response_not_available(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    adapted = build_scheduling_input(dataset)
    si = adapted.scheduling_input
    by_name = {c.display_name: c for c in si.candidates}
    # Synthetic Volunteer 1 left 2025-10-12 blank.
    event_id = adapted.event_id_by_date[datetime.date(2025, 10, 12)]
    assert by_name[_syn(1)].availability_for(event_id) is AvailabilityState.NO_RESPONSE
    # And explicitly marked O / X the other two weeks.
    assert (
        by_name[_syn(1)].availability_for(
            adapted.event_id_by_date[datetime.date(2025, 10, 5)]
        )
        is AvailabilityState.AVAILABLE
    )
    assert (
        by_name[_syn(1)].availability_for(
            adapted.event_id_by_date[datetime.date(2025, 10, 19)]
        )
        is AvailabilityState.UNAVAILABLE
    )


# ==========================================================================
# End-to-end on synthetic data: the real solver, the real checks
# ==========================================================================


def test_pipeline_runs_the_real_solver_and_passes_checks(tmp_path):
    _availability_csv(tmp_path / "availability.csv")
    _schedule_csv(tmp_path / "final_schedule.csv")
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    adapted = build_scheduling_input(dataset)
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result)
    assert checks.ok, checks.all_violations
    # Filled + unfilled accounting holds.
    assert result.filled_count + result.unfilled_count == 15


def test_solver_never_assigns_an_unavailable_person(tmp_path):
    # Everyone unavailable on the first Sunday -> those positions unfilled,
    # never forced onto an unavailable person.
    path = tmp_path / "availability.csv"
    _write(
        path,
        [
            ["Name", "Lead", "2025-10-05", "2025-10-12"],
            [_syn(1), "Y", "X", "O"],
            [_syn(2), "Y", "X", "O"],
            [_syn(3), "", "X", "O"],
        ],
    )
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025, headcount_per_sunday=2))
    adapted = build_scheduling_input(dataset)
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result)
    assert checks.ok, checks.all_violations
    first_sunday_events = {adapted.event_id_by_date[datetime.date(2025, 10, 5)]}
    for proposal in result.proposed_assignments:
        assert proposal.event_id not in first_sunday_events


# ==========================================================================
# 11: nothing real is embedded in this test module or the package
# ==========================================================================


_EMAIL_RE = __import__("re").compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")


def test_no_real_contact_info_in_committed_validator_files():
    package = REPO_ROOT / "backend" / "scripts" / "historical_setup"
    files = (
        list(package.glob("*.py"))
        + [Path(__file__), REPO_ROOT / "backend" / "scripts" / "validate_historical_setup.py"]
    )
    for f in files:
        for match in _EMAIL_RE.findall(f.read_text(encoding="utf-8")):
            low = match.lower()
            assert "noreply" in low or "example" in low, (
                f"{f.name}: possible real email address {match!r}"
            )


def test_fabricated_people_use_the_synthetic_naming_scheme():
    # Fixtures in this module only ever build names through ``_syn``.
    text = Path(__file__).read_text(encoding="utf-8")
    assert '_syn(1)' in text and 'SYNTHETIC_PREFIX = "Synthetic Volunteer "' in text


def test_inspect_sources_reports_structure_only(tmp_path, capsys):
    _availability_csv(tmp_path / "availability.csv")
    structures = inspect_sources(tmp_path, SourceConfig(year_hint=2025))
    assert len(structures) == 1
    s = structures[0]
    assert s.detected_shape == "availability-grid"
    assert s.rows == 4
    assert "Name" in s.columns
    # Structure description must not carry a volunteer name.
    blob = repr(s)
    assert SYNTHETIC_PREFIX.strip() not in blob


# ==========================================================================
# 11 / 10: the date-major availability grid
# ==========================================================================


def _date_major_csv(path: Path, *, legend: str | None = "Not Available") -> None:
    """A date-major grid: rows are dates, columns are people.

    Shaped like a sheet meant to be read one week at a time -- a blank first
    header cell, a trailing notes column, and a legend tacked onto one row.
    """
    header = [" ", _syn(1), _syn(2), _syn(3), "Notes", "", ""]
    rows = [
        ["10/5/2025", "O", "X", "O", "", "", ""],
        ["10/12/2025", "O", "O", "X", "Members Meeting", "X", legend or ""],
        ["10/19/2025", "X", "O", "O", "", "", ""],
    ]
    lines = [",".join(header)] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _two_role_schedule_csv(path: Path, *, blank_last: bool = False) -> None:
    header = ["Date", "Setup Lead", "Setup 2", "Setup 3"]
    rows = [
        ["10/5/2025", _syn(1), _syn(3), "" if blank_last else _syn(2)],
        ["10/12/2025", _syn(2), _syn(1), ""],
        ["10/19/2025", _syn(3), _syn(2), ""],
    ]
    lines = [",".join(header)] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_date_major_grid_is_detected_and_its_names_are_redacted(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    structures = inspect_sources(tmp_path, SourceConfig(year_hint=2025))
    s = structures[0]
    assert s.detected_shape == "availability-grid-transposed"
    assert s.rows == 3
    assert s.redacted_columns == 3
    # The header of this shape *is* the roster, so it must never be echoed.
    assert SYNTHETIC_PREFIX.strip() not in repr(s)


def test_a_schedule_grid_is_not_mistaken_for_a_date_major_availability_grid(tmp_path):
    # Both shapes are rows-of-dates; role headers are what separates them.
    _two_role_schedule_csv(tmp_path / "schedule.csv")
    structures = inspect_sources(tmp_path, SourceConfig(year_hint=2025))
    assert structures[0].detected_shape == "schedule-grid"


def test_date_major_grid_loads_people_dates_notes_and_tokens(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    assert len(dataset.volunteers) == 3
    assert len(dataset.sundays) == 3
    assert dataset.availability_tokens_seen == frozenset({"o", "x"})
    assert dataset.availability_blank_cells == 0
    assert dataset.event_notes[datetime.date(2025, 10, 12)] == "Members Meeting"
    mid = dataset.volunteers[0].membership_id
    assert (
        dataset.availability[(mid, datetime.date(2025, 10, 19))]
        is AvailabilityState.UNAVAILABLE
    )


def test_a_legend_contradicting_the_semantics_table_stops_the_run(tmp_path):
    # The sheet says X means "Available"; our table reads X as unavailable.
    # Reading it anyway would invert every cell, so it must refuse.
    _date_major_csv(tmp_path / "availability.csv", legend="Available")
    with pytest.raises(SourceError, match="legend"):
        load_dataset(tmp_path, SourceConfig(year_hint=2025))


def test_blank_cells_are_counted_so_a_caller_can_judge_no_response(tmp_path):
    path = tmp_path / "availability.csv"
    _date_major_csv(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("10/19/2025,X,O,O", "10/19/2025,X,,O"),
        encoding="utf-8",
    )
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    assert dataset.availability_blank_cells == 1
    # A blank writes no row at all, so the policy -- not the input -- decides.
    mid = dataset.volunteers[1].membership_id
    assert (mid, datetime.date(2025, 10, 19)) not in dataset.availability


# ==========================================================================
# 12 / 10: the observed Lead proxy, held-out dates, event-specific staffing
# ==========================================================================


def test_observed_lead_proxy_marks_only_people_seen_leading(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    _two_role_schedule_csv(tmp_path / "schedule.csv")
    dataset = load_dataset(
        tmp_path, SourceConfig(year_hint=2025, lead_from_schedule=True)
    )
    qualified = {v.display_name for v in dataset.volunteers if v.lead_qualified}
    assert qualified == {_syn(1), _syn(2), _syn(3)}
    # And it must announce itself as a proxy, so no report can imply otherwise.
    assert "OBSERVED PROXY" in dataset.lead_qualification_source


def test_observed_lead_proxy_without_a_schedule_is_refused(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    with pytest.raises(SourceError, match="no final schedule"):
        load_dataset(tmp_path, SourceConfig(year_hint=2025, lead_from_schedule=True))


def test_excluded_dates_leave_the_run_entirely(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    _two_role_schedule_csv(tmp_path / "schedule.csv")
    held = datetime.date(2025, 10, 12)
    dataset = load_dataset(
        tmp_path,
        SourceConfig(year_hint=2025, exclude_dates={held: "ad-hoc event"}),
    )
    assert held not in dataset.sundays
    assert dataset.excluded_dates == {held: "ad-hoc event"}
    assert all(h.event_date != held for h in dataset.historical_assignments)
    assert all(date != held for _mid, date in dataset.availability)
    assert all(r.event_date != held for r in dataset.requirements)


def test_excluding_a_date_the_source_does_not_have_is_refused(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    with pytest.raises(SourceError, match="absent from the source"):
        load_dataset(
            tmp_path,
            SourceConfig(
                year_hint=2025,
                exclude_dates={datetime.date(2025, 11, 2): "typo"},
            ),
        )


def test_only_dates_narrows_the_run_to_one_event(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    _two_role_schedule_csv(tmp_path / "schedule.csv")
    kept = datetime.date(2025, 10, 12)
    dataset = load_dataset(
        tmp_path, SourceConfig(year_hint=2025, only_dates=frozenset({kept}))
    )
    assert dataset.sundays == (kept,)
    assert {h.event_date for h in dataset.historical_assignments} == {kept}


def test_filled_cell_requirements_describe_one_event_not_a_template(tmp_path):
    # 10/5 staffed three roles, the other two staffed two. Requirements must
    # follow each event, never the widest row seen.
    _date_major_csv(tmp_path / "availability.csv")
    _two_role_schedule_csv(tmp_path / "schedule.csv")
    dataset = load_dataset(
        tmp_path,
        SourceConfig(year_hint=2025, requirements_from_filled_cells=True),
    )
    per_date = Counter(r.event_date for r in dataset.requirements)
    assert per_date[datetime.date(2025, 10, 5)] == 3
    assert per_date[datetime.date(2025, 10, 12)] == 2


def test_an_empty_cell_stays_an_unfilled_position_by_default(tmp_path):
    # The default must keep the church's shortfall visible: a blank Setup slot
    # on a recurring Sunday is a position nobody could fill, not one that was
    # never required.
    _date_major_csv(tmp_path / "availability.csv")
    _two_role_schedule_csv(tmp_path / "schedule.csv", blank_last=True)
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    per_date = Counter(r.event_date for r in dataset.requirements)
    assert per_date[datetime.date(2025, 10, 5)] == 3


def test_allow_no_response_is_the_callers_decision(tmp_path):
    _date_major_csv(tmp_path / "availability.csv")
    dataset = load_dataset(tmp_path, SourceConfig(year_hint=2025))
    strict = build_scheduling_input(dataset, allow_no_response=False)
    assert strict.policy.allow_no_response is False
    assert strict.policy.target_assignments_per_candidate == SETUP_TARGET_PER_PERIOD
    # Lead stays out of role variety however the flag is set.
    assert strict.lead_role_id not in strict.variety_role_ids
