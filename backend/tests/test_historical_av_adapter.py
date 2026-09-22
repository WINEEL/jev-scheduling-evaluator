"""Task 44 -- AV-specific behaviour for the historical validator.

**No real data.** Every volunteer here is obviously synthetic ("AV Volunteer
N"), every sheet is a handful of hand-written rows. These tests cover what AV
adds on top of the Task 43 pipeline: its role vocabulary, the combined
availability+assignment sheet, the workbook reader, the observed
role-eligibility proxy, and Shadow's deliberate exclusion from staffing.
"""

from __future__ import annotations

import datetime
import zipfile
from pathlib import Path

import pytest

from app.scheduling.input import AvailabilityState
from app.scheduling.solver import solve_schedule

from scripts.historical_av.roles import (
    AV_LEAD,
    AV_STAFFING_ROLE_NAMES,
    SHADOW_ROLE_NAME,
    UnknownAvRoleError,
    build_av_roles,
    normalize_av_role_name,
)
from scripts.historical_av.source import (
    AvSourceError,
    build_dataset,
    build_observed_eligibility,
    classify_workbook_tabs,
    read_quarter_from_csv,
    read_quarter_from_sheet,
)
from scripts.historical_av.workbook import (
    Workbook,
    WorkbookError,
    excel_serial_to_date,
)
from scripts.historical_setup.adapter import build_scheduling_input
from scripts.historical_setup.checks import run_all_checks

SYNTHETIC_PREFIX = "AV Volunteer "


def _syn(n: int) -> str:
    return f"{SYNTHETIC_PREFIX}{n}"


# ==========================================================================
# Fixtures: a combined AV sheet, and a minimal real .xlsx
# ==========================================================================


def _quarter_rows(
    *,
    shadow_on_first: str = "",
    blank_for_v4: bool = True,
    lead_label: str = "Lead",
) -> list[list[str]]:
    """Three Sundays, four volunteers, both blocks in one row."""
    header = [
        " ", _syn(1), _syn(2), _syn(3), _syn(4), "",
        lead_label, "Soundboard", "Slides", "Video", "Shadow", "Notes",
    ]
    v4 = "" if blank_for_v4 else "YES"
    return [
        header,
        ["10/4/26", "YES", "YES", "YES", v4, "",
         _syn(1), _syn(2), _syn(3), _syn(1), shadow_on_first, ""],
        ["10/11/26", "YES", "NO", "YES", v4, "",
         _syn(1), _syn(3), _syn(3), _syn(1), "", "Members Meeting"],
        ["10/18/26", "YES", "YES", "NO", v4, "",
         _syn(1), _syn(2), _syn(1), _syn(2), "", ""],
        # Summary and lookup rows sit below the dates and must be skipped.
        ["", "", "", "", "", "", "", "", "", "", "", ""],
        ["Total Serving", "5", "3", "3", "0", "", "", "", "", "", "", ""],
        ["", _syn(1), "2", "", "", "", "", "", "", "", "", ""],
    ]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.write_text(
        "\n".join(",".join(c for c in row) for row in rows) + "\n", encoding="utf-8"
    )


_CONTENT_TYPES = """<?xml version="1.0"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> None:
    """A real, minimal .xlsx -- enough that the reader is genuinely exercised."""

    def col_ref(index: int) -> str:
        letters = ""
        index += 1
        while index:
            index, rem = divmod(index - 1, 26)
            letters = chr(65 + rem) + letters
        return letters

    def esc(text: str) -> str:
        return (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _ROOT_RELS)
        sheet_tags, rel_tags = [], []
        for i, (name, rows) in enumerate(sheets.items(), start=1):
            rid = f"rId{i}"
            sheet_tags.append(
                f'<sheet name="{esc(name)}" sheetId="{i}" r:id="{rid}"/>'
            )
            rel_tags.append(
                f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org'
                f'/officeDocument/2006/relationships/worksheet"'
                f' Target="worksheets/sheet{i}.xml"/>'
            )
            body = []
            for r, row in enumerate(rows, start=1):
                cells = "".join(
                    f'<c r="{col_ref(c)}{r}" t="inlineStr"><is><t>{esc(v)}</t></is></c>'
                    if not v.replace(".", "", 1).isdigit()
                    else f'<c r="{col_ref(c)}{r}"><v>{v}</v></c>'
                    for c, v in enumerate(row)
                    if v
                )
                body.append(f'<row r="{r}">{cells}</row>')
            zf.writestr(
                f"xl/worksheets/sheet{i}.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxml'
                'formats.org/spreadsheetml/2006/main"><sheetData>'
                + "".join(body)
                + "</sheetData></worksheet>",
            )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats'
            '.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxml'
            'formats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(sheet_tags)
            + "</sheets></workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxml'
            'formats.org/package/2006/relationships">'
            + "".join(rel_tags)
            + "</Relationships>",
        )


def _serial(day: datetime.date) -> str:
    return str((day - datetime.date(1899, 12, 30)).days)


# ==========================================================================
# 1: AV role vocabulary, and failing closed
# ==========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Lead", AV_LEAD),
        ("AV Lead", AV_LEAD),
        ("(AV0)\nAV Lead\n8:00 AM", AV_LEAD),
        ("Soundboard", "Soundboard"),
        ("(AV1)\nSound\nboard\n8:00 AM", "Soundboard"),
        ("slides", "Slides"),
        ("VIDEO", "Video"),
        ("Shadow", SHADOW_ROLE_NAME),
    ],
)
def test_av_role_spellings_normalize(raw, expected):
    assert normalize_av_role_name(raw) == expected


@pytest.mark.parametrize(
    "raw", ["Setup", "(AV4)\nAV setup\n8:00 AM", "Greeter", "Setup 2", "", "   "]
)
def test_an_unknown_av_role_label_fails_closed(raw):
    # Reading a label as the wrong role would credit someone with work they
    # have never done, so anything outside AV's vocabulary is refused.
    with pytest.raises(UnknownAvRoleError):
        normalize_av_role_name(raw)


def test_shadow_is_a_recorded_role_but_never_a_staffing_position():
    roles = {r.name: r for r in build_av_roles()}
    assert set(roles) == set(AV_STAFFING_ROLE_NAMES) | {SHADOW_ROLE_NAME}
    assert roles[SHADOW_ROLE_NAME].is_staffing_position is False
    assert all(roles[n].is_staffing_position for n in AV_STAFFING_ROLE_NAMES)
    # AV volunteers specialize: no role may be in the variety set.
    assert not any(r.in_variety_set for r in roles.values())


# ==========================================================================
# 2: the combined availability + assignment sheet
# ==========================================================================


def test_combined_sheet_splits_availability_from_assignments(tmp_path):
    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows())
    q = read_quarter_from_csv(path, label="quarter")

    assert q.volunteers == (_syn(1), _syn(2), _syn(3), _syn(4))
    assert q.dates == (
        datetime.date(2026, 10, 4),
        datetime.date(2026, 10, 11),
        datetime.date(2026, 10, 18),
    )
    # The summary tally and the lookup table below the dates are not dates.
    assert len(q.dates) == 3
    assert q.assignments[(datetime.date(2026, 10, 4), AV_LEAD)] == _syn(1)
    assert q.assignments[(datetime.date(2026, 10, 4), "Video")] == _syn(1)
    assert q.notes[datetime.date(2026, 10, 11)] == "Members Meeting"


def test_yes_no_and_blank_are_three_distinct_answers(tmp_path):
    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows())
    q = read_quarter_from_csv(path, label="quarter")
    day = datetime.date(2026, 10, 11)

    assert q.availability[(_syn(1), day)] is AvailabilityState.AVAILABLE
    assert q.availability[(_syn(2), day)] is AvailabilityState.UNAVAILABLE
    # A blank writes no row at all: the sheet never says what it means, so the
    # policy decides rather than the reader.
    assert (_syn(4), day) not in q.availability
    assert q.blank_cells == 3
    assert q.tokens_seen == frozenset({"yes", "no"})


def test_an_unrecognized_availability_token_stops_the_run(tmp_path):
    rows = _quarter_rows()
    rows[1][1] = "MAYBE"
    path = tmp_path / "quarter.csv"
    _write_csv(path, rows)
    with pytest.raises(AvSourceError, match="MAYBE"):
        read_quarter_from_csv(path, label="quarter")


def test_a_sheet_without_all_four_staffing_roles_is_refused(tmp_path):
    rows = _quarter_rows()
    rows[0][9] = "Notes"  # blank out the Video column header
    path = tmp_path / "quarter.csv"
    _write_csv(path, rows)
    with pytest.raises(AvSourceError, match="missing AV staffing roles"):
        read_quarter_from_csv(path, label="quarter")


# ==========================================================================
# 3: Shadow
# ==========================================================================


def test_shadow_is_preserved_with_the_role_that_was_shadowed(tmp_path):
    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows(shadow_on_first=f"{_syn(4)} (AV - Soundboard)"))
    q = read_quarter_from_csv(path, label="quarter")

    assert len(q.shadows) == 1
    record = q.shadows[0]
    assert record.display_name == _syn(4)
    assert record.shadowed_role == "Soundboard"
    assert record.event_date == datetime.date(2026, 10, 4)


def test_shadowing_a_role_does_not_make_someone_eligible_for_it(tmp_path):
    # V4 shadows Soundboard and is never assigned anything. Watching a role is
    # not evidence of being cleared to serve it.
    path = tmp_path / "quarter.csv"
    _write_csv(
        path,
        _quarter_rows(
            shadow_on_first=f"{_syn(4)} (AV - Soundboard)", blank_for_v4=False
        ),
    )
    q = read_quarter_from_csv(path, label="quarter")

    observed: dict[str, set[str]] = {}
    for (_day, role), person in q.assignments.items():
        observed.setdefault(person, set()).add(role)
    assert _syn(4) not in observed

    dataset = build_dataset(
        q, {k: frozenset(v) for k, v in observed.items()},
        period_label="synthetic", eligibility_source="test",
    )
    v4 = next(v for v in dataset.volunteers if v.display_name == _syn(4))
    assert v4.qualified_role_names == frozenset()


def test_shadow_never_becomes_a_required_position(tmp_path):
    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows(shadow_on_first=f"{_syn(4)} (AV - Soundboard)"))
    q = read_quarter_from_csv(path, label="quarter")
    dataset = build_dataset(
        q, {_syn(1): frozenset(AV_STAFFING_ROLE_NAMES)},
        period_label="synthetic", eligibility_source="test",
    )
    assert all(r.role_name != SHADOW_ROLE_NAME for r in dataset.requirements)
    # 3 dates x 4 staffing roles, and not one more.
    assert len(dataset.requirements) == 12


def test_a_shadow_requirement_would_be_caught_as_a_hard_violation(tmp_path):
    # Guards the check itself: if a future change let Shadow become a
    # requirement, the run must stop rather than report phantom shortfalls.
    from scripts.historical_setup.model import HistoricalRequirement

    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows())
    q = read_quarter_from_csv(path, label="quarter")
    dataset = build_dataset(
        q, {_syn(1): frozenset(AV_STAFFING_ROLE_NAMES)},
        period_label="synthetic", eligibility_source="test",
    )
    dataset.requirements = dataset.requirements + (
        HistoricalRequirement(q.dates[0], SHADOW_ROLE_NAME, 1),
    )
    adapted = build_scheduling_input(
        dataset, allow_no_response=False,
        target_assignments_per_candidate=None, optimize_role_variety=False,
    )
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result, dataset)
    assert checks.non_staffing_requirement_violations
    assert not checks.ok


# ==========================================================================
# 4: the observed role-eligibility proxy
# ==========================================================================


def _workbook_with_history(path: Path) -> None:
    d1, d2 = datetime.date(2025, 1, 5), datetime.date(2025, 1, 12)
    _write_xlsx(
        path,
        {
            # Long history: V2 has led, V4 has done Video -- neither is visible
            # in the current quarter alone.
            "2025": [
                ["Date", "(AV0)\nAV Lead\n8:00 AM", "(AV1)\nSound\nboard\n8:00 AM",
                 "(AV2)\nSlides\n8:00 AM", "(AV3)\nVideo\n8:00 AM",
                 "(AV4)\nAV setup\n8:00 AM"],
                [_serial(d1), _syn(2), _syn(3), _syn(1), _syn(4), _syn(9)],
                [_serial(d2), _syn(2), _syn(1), _syn(3), _syn(4), _syn(9)],
            ],
            # Refused: names only some of the four staffing roles.
            "AudioVideo Processing": [
                ["Date", "Video", "Audio"],
                [_serial(d1), _syn(7), _syn(8)],
            ],
            # Refused: no role columns at all.
            "Sheet12": [["something", "else"], ["1", "2"]],
        },
    )


def test_inconsistent_workbook_tabs_are_refused_rather_than_guessed(tmp_path):
    path = tmp_path / "book.xlsx"
    _workbook_with_history(path)
    verdicts = {v.name: v for v in classify_workbook_tabs(Workbook(path))}

    assert verdicts["2025"].usable is True
    assert verdicts["2025"].shape == "long-history"
    assert verdicts["AudioVideo Processing"].usable is False
    assert "all four" in verdicts["AudioVideo Processing"].reason
    assert verdicts["Sheet12"].usable is False


def test_observed_eligibility_extracts_multi_role_specialization(tmp_path):
    path = tmp_path / "book.xlsx"
    _workbook_with_history(path)
    wb = Workbook(path)
    roster = {_syn(i) for i in range(1, 5)}
    observed, evidence, used = build_observed_eligibility(
        wb, classify_workbook_tabs(wb), roster
    )

    assert observed[_syn(2)] == frozenset({AV_LEAD})
    assert observed[_syn(4)] == frozenset({"Video"})
    # V1 and V3 each appear in two different roles across the two weeks.
    assert observed[_syn(1)] == frozenset({"Slides", "Soundboard"})
    assert observed[_syn(3)] == frozenset({"Soundboard", "Slides"})
    assert used == ["2025"]
    assert evidence[AV_LEAD] == 2


def test_the_av_setup_column_never_grants_eligibility(tmp_path):
    # The workbook's long tabs carry an "AV setup" column that is not one of
    # the four staffing roles. V9 appears only there and must stay ineligible.
    path = tmp_path / "book.xlsx"
    _workbook_with_history(path)
    wb = Workbook(path)
    observed, _evidence, _used = build_observed_eligibility(
        wb, classify_workbook_tabs(wb), {_syn(9)}
    )
    assert observed == {}


def test_a_refused_tab_contributes_no_eligibility(tmp_path):
    path = tmp_path / "book.xlsx"
    _workbook_with_history(path)
    wb = Workbook(path)
    observed, _evidence, _used = build_observed_eligibility(
        wb, classify_workbook_tabs(wb), {_syn(7), _syn(8)}
    )
    assert observed == {}


def test_a_quarter_grid_tab_reads_the_same_as_its_csv(tmp_path):
    rows = _quarter_rows()
    csv_path = tmp_path / "quarter.csv"
    _write_csv(csv_path, rows)
    xlsx_path = tmp_path / "book.xlsx"
    serial_rows = [list(rows[0])] + [
        [_serial(datetime.date(2026, 10, d))] + list(r[1:])
        for d, r in zip((4, 11, 18), rows[1:4])
    ]
    _write_xlsx(xlsx_path, {"2026 Oct-Dec": serial_rows})

    from_csv = read_quarter_from_csv(csv_path, label="q")
    from_tab = read_quarter_from_sheet(Workbook(xlsx_path), "2026 Oct-Dec")
    assert from_csv.dates == from_tab.dates
    assert from_csv.assignments == from_tab.assignments
    assert from_csv.availability == from_tab.availability


# ==========================================================================
# 5: specialized-role scheduling through the real solver
# ==========================================================================


def _adapted_from(tmp_path, observed):
    path = tmp_path / "quarter.csv"
    _write_csv(path, _quarter_rows())
    q = read_quarter_from_csv(path, label="q")
    dataset = build_dataset(
        q, observed, period_label="synthetic", eligibility_source="test"
    )
    adapted = build_scheduling_input(
        dataset, allow_no_response=False,
        target_assignments_per_candidate=None, optimize_role_variety=False,
    )
    return dataset, adapted


def test_the_solver_only_places_people_into_observed_eligible_roles(tmp_path):
    observed = {
        _syn(1): frozenset({AV_LEAD}),
        _syn(2): frozenset({"Soundboard"}),
        _syn(3): frozenset({"Slides", "Video"}),
    }
    dataset, adapted = _adapted_from(tmp_path, observed)
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result, dataset)
    assert checks.ok, checks.all_violations

    role_by_id = adapted.role_name_by_id
    name_by_membership = adapted.display_name_by_membership
    for proposal in result.proposed_assignments:
        req = next(
            r for r in adapted.scheduling_input.requirements
            if r.requirement_id == proposal.requirement_id
        )
        assert (
            role_by_id[req.ministry_role_id]
            in observed[name_by_membership[proposal.membership_id]]
        )


def test_a_role_nobody_is_observed_eligible_for_comes_back_unfilled(tmp_path):
    # Scarcity must surface as an unresolved position, never as a placement
    # into a role the proxy does not support.
    observed = {
        _syn(1): frozenset({AV_LEAD}),
        _syn(2): frozenset({"Soundboard"}),
        _syn(3): frozenset({"Slides"}),
    }
    dataset, adapted = _adapted_from(tmp_path, observed)
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result, dataset)
    assert checks.ok, checks.all_violations
    unfilled = {
        (
            adapted.requirement_date[u.requirement_id],
            adapted.requirement_role_name[u.requirement_id],
        )
        for u in result.unfilled_requirements
    }
    # Video has no eligible volunteer at all, so it is unfilled on every date.
    assert {role for _d, role in unfilled if role == "Video"} == {"Video"}
    assert len([1 for _d, role in unfilled if role == "Video"]) == len(dataset.sundays)
    # Nobody was placed outside their single observed role to paper over it.
    role_by_id = adapted.role_name_by_id
    name_by_membership = adapted.display_name_by_membership
    for proposal in result.proposed_assignments:
        req = next(
            r for r in adapted.scheduling_input.requirements
            if r.requirement_id == proposal.requirement_id
        )
        assert (
            role_by_id[req.ministry_role_id]
            in observed[name_by_membership[proposal.membership_id]]
        )


def test_av_policy_carries_neither_of_setups_fairness_preferences(tmp_path):
    _dataset, adapted = _adapted_from(
        tmp_path, {_syn(1): frozenset(AV_STAFFING_ROLE_NAMES)}
    )
    assert adapted.policy.target_assignments_per_candidate is None
    assert adapted.policy.role_variety_role_ids is None
    assert adapted.variety_role_ids == frozenset()


def test_qualification_for_a_role_this_dataset_lacks_is_refused(tmp_path):
    with pytest.raises(ValueError, match="does not define"):
        _adapted_from(tmp_path, {_syn(1): frozenset({"Setup 2"})})


def test_conflict_free_result_is_not_claimed_without_conflict_data(tmp_path):
    dataset, adapted = _adapted_from(
        tmp_path, {_syn(1): frozenset(AV_STAFFING_ROLE_NAMES)}
    )
    assert dataset.conflict_data_available is False
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    checks = run_all_checks(adapted, result, dataset)
    # No candidate may carry blocked dates, or the run would be reporting a
    # cross-ministry check it never had the data to perform.
    assert not checks.conflict_claim_violations
    assert all(not c.blocked_dates for c in adapted.scheduling_input.candidates)


# ==========================================================================
# 6: the workbook reader itself
# ==========================================================================


def test_excel_date_serials_convert_and_plain_numbers_do_not():
    assert excel_serial_to_date("45662") == datetime.date(2025, 1, 5)
    assert excel_serial_to_date("45662.0") == datetime.date(2025, 1, 5)
    assert excel_serial_to_date("4") is None
    assert excel_serial_to_date("not a number") is None
    assert excel_serial_to_date("") is None


def test_a_file_that_is_not_a_workbook_is_refused(tmp_path):
    path = tmp_path / "nope.xlsx"
    path.write_text("this is not a zip", encoding="utf-8")
    with pytest.raises(WorkbookError, match="not a readable"):
        Workbook(path)


def test_asking_for_a_missing_sheet_is_refused(tmp_path):
    path = tmp_path / "book.xlsx"
    _workbook_with_history(path)
    with pytest.raises(WorkbookError, match="no sheet named"):
        Workbook(path).sheet("Does Not Exist")


# ==========================================================================
# 7: privacy
# ==========================================================================


def test_no_real_name_or_private_value_appears_in_this_file():
    # Every person in this module is built through ``_syn``, and nothing that
    # looks like a real contact detail may appear. The pattern is assembled at
    # run time so this check cannot trip over its own source.
    import re

    text = Path(__file__).read_text(encoding="utf-8")
    assert f'"{SYNTHETIC_PREFIX}' in text
    email = re.compile(r"[\w.+-]+" + "@" + r"[\w-]+\.[a-z]{2,}")
    assert not email.search(text)


def test_an_unstaffed_cell_stays_an_unfilled_position(tmp_path):
    # A blank assignment cell is a position the church could not fill. If
    # requirements were mined from the filled cells instead, the shortfall
    # would vanish from the report entirely.
    rows = _quarter_rows()
    rows[1][9] = ""  # 10/4 Video left blank
    path = tmp_path / "quarter.csv"
    _write_csv(path, rows)
    q = read_quarter_from_csv(path, label="q")

    assert (datetime.date(2026, 10, 4), "Video") not in q.assignments
    dataset = build_dataset(
        q, {_syn(1): frozenset(AV_STAFFING_ROLE_NAMES)},
        period_label="synthetic", eligibility_source="test",
    )
    # Still 3 dates x 4 staffing roles, the blank one included.
    assert len(dataset.requirements) == 12
    assert any(
        r.event_date == datetime.date(2026, 10, 4) and r.role_name == "Video"
        for r in dataset.requirements
    )
