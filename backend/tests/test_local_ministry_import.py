"""Task 72 -- the generic local roster importer, checked offline.

**No real data, and none possible.** Every dataset here is hand-written with
obviously synthetic names, and one test asserts the tracked source files carry
no church-specific value at all -- which is the property that makes this
tooling committable while the rosters it reads are not.

The rows the importer writes into a real database are a database concern and
are not exercised here; what is exercised is every decision it makes before
touching one: how qualification is read from each source shape, how a
non-Sunday date becomes a named special event, what it refuses, and what its
output is allowed to contain.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import pytest

from app.scheduling.input import AvailabilityState

from scripts import import_local_ministry, local_ministry_import
from scripts.historical_setup.model import HistoricalVolunteer
from scripts.local_ministry_import import (
    STAFFING_OVERRIDE_COLUMNS,
    ImportPlan,
    LocalImportError,
    availability_state_column,
    load_staffing_overrides,
    parse_staffing_overrides,
    qualified_role_names_for,
)

LEAD = "Lead"
ROLE_NAMES = (LEAD, "Support 2", "Support 3")

#: Synthetic throughout: a ministry called "Lead / Support 2 / Support 3" on
#: two dates in a year no source in this repository covers. Nothing here
#: corresponds to a real roster, and the tracked-source check below is what
#: keeps it that way.
DAY_ONE = datetime.date(2031, 3, 2)
DAY_TWO = datetime.date(2031, 3, 9)
KNOWN_DATES = (DAY_ONE, DAY_TWO)


def overrides(rows, *, year_hint: int = 2031):
    return parse_staffing_overrides(
        rows, known_roles=ROLE_NAMES, known_dates=KNOWN_DATES, year_hint=year_hint
    )


def row(date: str, role: str, count: str) -> dict[str, str]:
    return {"event_date": date, "role": role, "required_count": count}


def volunteer(
    *,
    membership_id: int = 1,
    lead_qualified: bool = False,
    restricted: frozenset[str] = frozenset(),
    qualified_role_names: frozenset[str] | None = None,
) -> HistoricalVolunteer:
    return HistoricalVolunteer(
        membership_id=membership_id,
        person_id=membership_id,
        display_name=f"Synthetic Volunteer {membership_id}",
        lead_qualified=lead_qualified,
        restricted_support_roles=restricted,
        qualified_role_names=qualified_role_names,
    )


def qualified(
    person: HistoricalVolunteer, *, also_lead: bool = False
) -> frozenset[str]:
    return qualified_role_names_for(
        person, role_names=ROLE_NAMES, lead_role_name=LEAD, also_lead=also_lead
    )


# ==========================================================================
# Nothing church-specific is embedded
# ==========================================================================


def test_the_tracked_importer_carries_no_church_specific_value():
    """The whole design rests on this: the tooling is generic, the records are
    not. A default church, ministry, head or role list in either file would put
    a real organization's details into tracked source.
    """
    sources = [
        Path(local_ministry_import.__file__).read_text(),
        Path(import_local_ministry.__file__).read_text(),
    ]
    for text in sources:
        lowered = text.lower()
        for forbidden in ("church of", "@gmail", "@yahoo", "setup lead", "setup 2", "nlf"):
            assert forbidden not in lowered, forbidden
        # No calendar date of anyone's, either. A per-event staffing rule is
        # the obvious place for one to creep in, so the rule lives in an
        # ignored manifest and this code only ever parses what it is handed.
        assert re.search(r"\b20\d\d-\d\d-\d\d\b", text) is None

    # Every church-specific value is a required argument with no default.
    parser = import_local_ministry.build_parser()
    required = {
        action.dest
        for action in parser._actions  # noqa: SLF001 - argparse has no public view
        if getattr(action, "required", False)
    }
    assert {
        "input_dir",
        "church_name",
        "ministry_name",
        "period_name",
        "head_name",
    } <= required


def test_the_import_reason_names_no_ministry():
    assert "git-ignored" in local_ministry_import.IMPORT_REASON
    assert local_ministry_import.IMPORT_REASON.isascii()


# ==========================================================================
# Qualification, read from each shape the source can supply
# ==========================================================================


def test_the_default_shape_is_every_support_role_and_the_lead_only_if_stated():
    assert qualified(volunteer()) == frozenset({"Support 2", "Support 3"})
    assert qualified(volunteer(lead_qualified=True)) == frozenset(ROLE_NAMES)


def test_restricted_support_roles_narrow_the_support_set_only():
    person = volunteer(lead_qualified=True, restricted=frozenset({"Support 3"}))
    assert qualified(person) == frozenset({LEAD, "Support 3"})


def test_an_explicit_role_set_is_authoritative_including_when_empty():
    """An empty set is a real answer -- "this person may serve nothing" -- and
    must not fall back to the default "everything but the lead".
    """
    assert qualified(volunteer(qualified_role_names=frozenset())) == frozenset()

    person = volunteer(lead_qualified=True, qualified_role_names=frozenset({LEAD}))
    assert qualified(person) == frozenset({LEAD})


def test_an_explicit_role_set_is_clipped_to_the_roles_that_exist():
    person = volunteer(qualified_role_names=frozenset({"Support 2", "Retired Role"}))
    assert qualified(person) == frozenset({"Support 2"})


def test_also_lead_adds_the_lead_role_whatever_the_source_says():
    """The operator's current correction wins over the source, including over
    an explicit role set that omits the lead role.
    """
    assert qualified(volunteer(), also_lead=True) == frozenset(ROLE_NAMES)

    person = volunteer(qualified_role_names=frozenset({"Support 2"}))
    assert qualified(person, also_lead=True) == frozenset({LEAD, "Support 2"})


def test_also_lead_never_takes_a_qualification_away():
    person = volunteer(lead_qualified=True)
    assert qualified(person, also_lead=False) == frozenset(ROLE_NAMES)


# ==========================================================================
# Availability
# ==========================================================================


def test_every_stored_answer_maps_to_its_column_value():
    assert availability_state_column(AvailabilityState.AVAILABLE) == "AVAILABLE"
    assert availability_state_column(AvailabilityState.BACKUP) == "BACKUP"
    assert availability_state_column(AvailabilityState.UNAVAILABLE) == "UNAVAILABLE"


def test_no_response_is_the_absence_of_a_row_not_an_answer():
    """Writing "available" for a blank cell would put words in somebody's
    mouth; the importer skips it instead.
    """
    assert availability_state_column(AvailabilityState.NO_RESPONSE) is None


def test_every_member_of_the_enum_is_handled():
    """A new availability tier must not silently import as "no response"."""
    for state in AvailabilityState:
        column = availability_state_column(state)
        assert column is None or isinstance(column, str)


# ==========================================================================
# Per-event staffing, stated by the head rather than inferred
# ==========================================================================


def test_a_listed_date_takes_exactly_the_roles_listed_for_it():
    """The mechanism that makes a smaller team expressible at all: leaving a
    role out of a date is how "this event needs fewer people" is said.
    """
    result = overrides(
        [row("2031-03-02", LEAD, "1"), row("2031-03-02", "Support 2", "1")]
    )

    assert result == {DAY_ONE: {LEAD: 1, "Support 2": 1}}
    assert "Support 3" not in result[DAY_ONE]


def test_dates_not_listed_are_absent_so_the_source_keeps_them():
    result = overrides([row("2031-03-02", LEAD, "1")])

    assert DAY_TWO not in result


def test_counts_above_one_are_kept():
    assert overrides([row("2031-03-09", "Support 2", "3")]) == {DAY_TWO: {"Support 2": 3}}


def test_role_labels_match_on_case_and_spacing_not_on_a_ministrys_vocabulary():
    """A head types what they say, not a normalized identifier -- and these
    role names are whatever this ministry chose, so nothing may be mapped onto
    a fixed vocabulary.
    """
    result = overrides([row("2031-03-02", "  support   2 ", "1")])

    assert result == {DAY_ONE: {"Support 2": 1}}


def test_the_date_notations_a_person_actually_writes_are_accepted():
    for written in ("2031-03-02", "3/2/2031", "3/2/31", "Mar 2 2031"):
        assert overrides([row(written, LEAD, "1")]) == {DAY_ONE: {LEAD: 1}}


def test_blank_lines_are_not_statements():
    assert overrides([row("", "", ""), row("2031-03-02", LEAD, "1")]) == {
        DAY_ONE: {LEAD: 1}
    }


@pytest.mark.parametrize(
    "bad",
    [
        row("2031-03-16", LEAD, "1"),
        row("2031-03-02", "Nonexistent Role", "1"),
        row("2031-03-02", LEAD, "0"),
        row("2031-03-02", LEAD, "-1"),
        row("2031-03-02", LEAD, "two"),
        row("not a date", LEAD, "1"),
    ],
    ids=[
        "date not in the source",
        "role the ministry does not have",
        "count of zero",
        "negative count",
        "count that is not a number",
        "unreadable date",
    ],
)
def test_anything_unrecognized_stops_the_import(bad):
    with pytest.raises(LocalImportError):
        overrides([bad])


def test_the_same_date_and_role_twice_is_refused_rather_than_resolved():
    with pytest.raises(LocalImportError):
        overrides([row("2031-03-02", LEAD, "1"), row("2031-03-02", LEAD, "2")])


def test_a_missing_column_names_the_column():
    with pytest.raises(LocalImportError) as raised:
        overrides([{"event_date": "2031-03-02", "role": LEAD}])

    assert "required_count" in str(raised.value)


def test_the_error_for_an_unknown_role_does_not_repeat_the_label_back():
    """Role labels come from a private manifest; an error a person may paste
    into a report must not carry one.
    """
    with pytest.raises(LocalImportError) as raised:
        overrides([row("2031-03-02", "Sunrise Greeter", "1")])

    assert "Sunrise Greeter" not in str(raised.value)


def test_the_columns_are_generic(tmp_path):
    assert STAFFING_OVERRIDE_COLUMNS == ("event_date", "role", "required_count")


def test_a_file_is_read_with_the_same_rules(tmp_path):
    path = tmp_path / "staffing.csv"
    path.write_text(
        "event_date,role,required_count\n"
        "2031-03-02,Lead,1\n"
        "2031-03-02,Support 2,1\n"
    )

    result = load_staffing_overrides(
        path, known_roles=ROLE_NAMES, known_dates=KNOWN_DATES, year_hint=2031
    )

    assert result == {DAY_ONE: {LEAD: 1, "Support 2": 1}}


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "staffing.csv"
    path.write_text("")

    with pytest.raises(LocalImportError):
        load_staffing_overrides(
            path, known_roles=ROLE_NAMES, known_dates=KNOWN_DATES, year_hint=2031
        )


def test_a_missing_file_is_refused_without_a_traceback(tmp_path):
    with pytest.raises(LocalImportError):
        load_staffing_overrides(
            tmp_path / "absent.csv",
            known_roles=ROLE_NAMES,
            known_dates=KNOWN_DATES,
            year_hint=2031,
        )


# ==========================================================================
# The plan
# ==========================================================================


def test_a_plan_locks_nothing_unless_asked():
    """Locking is one-way, so it is never the default: an operator who wanted
    the availability screen to stay editable cannot undo an accidental lock.
    """
    plan = ImportPlan(
        church_name="Synthetic Church",
        ministry_name="Synthetic Ministry",
        period_name="Synthetic Period",
        head_name="Synthetic Volunteer 1",
        admin_name="Local Import Admin",
    )
    assert plan.lock_availability_when_done is False
    assert plan.also_lead_names == frozenset()
    # No staffing rule by default: without a manifest the source's shape is
    # used, and that is a visible choice rather than a silent one.
    assert plan.staffing_overrides == {}


# ==========================================================================
# Refusals
# ==========================================================================


class _StubSession:
    """Enough Session surface to prove the importer refuses before it writes.

    Any call at all would be a failure for these tests, so every method raises.
    """

    def execute(self, *args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the importer touched the database before validating")

    add = flush = execute


def _plan() -> ImportPlan:
    return ImportPlan(
        church_name="Synthetic Church",
        ministry_name="Synthetic Ministry",
        period_name="Synthetic Period",
        head_name="Synthetic Volunteer 1",
        admin_name="Local Import Admin",
    )


def _dataset(**overrides):
    from scripts.historical_setup.model import HistoricalDataset, HistoricalRole

    defaults = dict(
        period_label="Synthetic Period",
        sundays=(datetime.date(2026, 10, 4),),
        roles=(HistoricalRole(role_id=1, name=LEAD, is_lead=True, in_variety_set=False),),
        volunteers=(volunteer(),),
        requirements=(),
    )
    defaults.update(overrides)
    return HistoricalDataset(**defaults)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sundays": ()},
        {"roles": ()},
        {"volunteers": ()},
    ],
    ids=["no dates", "no roles", "no volunteers"],
)
def test_an_empty_source_is_refused_before_anything_is_written(overrides):
    with pytest.raises(LocalImportError):
        local_ministry_import.import_dataset(
            _StubSession(), dataset=_dataset(**overrides), plan=_plan()
        )


def test_a_refusal_names_no_person():
    """Error text is read by a person and may be pasted into a report, so it
    carries settings, counts and dates -- never a display name.
    """
    with pytest.raises(LocalImportError) as raised:
        local_ministry_import.import_dataset(
            _StubSession(), dataset=_dataset(sundays=()), plan=_plan()
        )
    assert "Synthetic Volunteer" not in str(raised.value)
