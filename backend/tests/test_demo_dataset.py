"""The demo dataset itself: fictional, and shaped for a legible demo.

Offline checks on the data definition -- that every name is invented, that the
dates really are Sundays, that Lead qualification is restricted. The rows this
produces in a real database are covered by the integration suite.
"""

from __future__ import annotations

import datetime

from scripts.demo_dataset import (
    ADMIN_NAME,
    CHURCH_NAME,
    EXPECTED_REQUIRED_POSITIONS,
    EXPECTED_SUNDAYS,
    HEAD_NAME,
    LEAD_ROLE,
    MINISTRY_NAME,
    PERIOD_END,
    PERIOD_NAME,
    PERIOD_START,
    ROLE_NAMES,
    VOLUNTEERS,
)

# ==========================================================================
# 18: nothing real is embedded
# ==========================================================================


def test_18_no_real_church_or_person_data_is_embedded():
    """Every name is invented and every address is unreachable by
    construction. `.invalid` is reserved by RFC 2606 and can never resolve, so
    none of these could reach a real person even by accident.
    """
    assert CHURCH_NAME == "Demo Church"
    assert "Demo" in MINISTRY_NAME
    assert "Demo" in PERIOD_NAME

    names = [volunteer.display_name for volunteer in VOLUNTEERS] + [ADMIN_NAME]
    for name in names:
        # Ordinary two-part fictional names, not initials or handles.
        assert len(name.split()) == 2, name

    for volunteer in VOLUNTEERS:
        assert volunteer.email.endswith("@demo.invalid"), volunteer.email

    # No phone numbers at all: a field nobody needs for this demo.
    assert not hasattr(VOLUNTEERS[0], "phone")


def test_18b_the_dataset_names_no_real_organisation():
    """Guards against the specific mistake of reaching for the actual church's
    name or a real ministry's roster.
    """
    haystack = " ".join(
        [CHURCH_NAME, MINISTRY_NAME, PERIOD_NAME, ADMIN_NAME, *ROLE_NAMES]
        + [volunteer.display_name for volunteer in VOLUNTEERS]
        + [volunteer.email for volunteer in VOLUNTEERS]
    ).lower()

    for forbidden in ("real-church-name", "real-person-name"):
        assert forbidden not in haystack, forbidden


# ==========================================================================
# 11-13: the shape of the demo
# ==========================================================================


def test_11_every_event_date_is_a_sunday():
    for sunday in EXPECTED_SUNDAYS:
        assert sunday.weekday() == 6, sunday
        assert sunday.strftime("%A") == "Sunday"

    assert len(EXPECTED_SUNDAYS) == 4
    assert EXPECTED_SUNDAYS[0] == PERIOD_START
    assert EXPECTED_SUNDAYS[-1] == PERIOD_END
    assert EXPECTED_SUNDAYS[0].year == 2026 and EXPECTED_SUNDAYS[0].month == 10


def test_11b_the_period_covers_exactly_those_sundays():
    day = PERIOD_START
    sundays = []
    while day <= PERIOD_END:
        if day.weekday() == 6:
            sundays.append(day)
        day += datetime.timedelta(days=1)

    assert tuple(sundays) == EXPECTED_SUNDAYS


def test_12_staffing_is_one_person_per_role_per_sunday():
    assert ROLE_NAMES == ("Setup Lead", "Setup 2", "Setup 3", "Setup 4", "Setup 5")
    assert EXPECTED_REQUIRED_POSITIONS == len(EXPECTED_SUNDAYS) * len(ROLE_NAMES) == 20


def test_13_lead_qualification_is_restricted():
    """Not everyone can lead. A ministry where they could would hide the
    scarcity rule the scheduler exists to respect.
    """
    lead_qualified = [v for v in VOLUNTEERS if v.lead_qualified]

    assert 1 <= len(lead_qualified) < len(VOLUNTEERS)
    assert len(lead_qualified) == 2
    assert HEAD_NAME in {v.display_name for v in lead_qualified}


def test_13b_a_lead_is_available_on_every_sunday():
    """Otherwise a Sunday would be short for a reason that looks like a bug."""
    for sunday in EXPECTED_SUNDAYS:
        available_leads = [
            v for v in VOLUNTEERS if v.lead_qualified and sunday not in v.unavailable_on
        ]
        assert available_leads, sunday


def test_the_demo_has_enough_people_to_be_interesting():
    assert 7 <= len(VOLUNTEERS) <= 9
    assert len({v.display_name for v in VOLUNTEERS}) == len(VOLUNTEERS)
    assert len({v.email for v in VOLUNTEERS}) == len(VOLUNTEERS)


def test_availability_makes_the_demo_legible():
    """Mostly available, with a few real "no" answers so the generated
    schedule visibly respects them -- and exactly one Sunday short-staffed, so
    the review screen shows an unresolved position without anyone having to
    break something to see it.
    """
    answers = len(VOLUNTEERS) * len(EXPECTED_SUNDAYS)
    unavailable = sum(len(v.unavailable_on) for v in VOLUNTEERS)

    assert unavailable > 0, "no 'unavailable' answer means availability is never visibly used"
    assert unavailable < answers / 3, "too many absences to read as a normal month"

    short_sundays = []
    for sunday in EXPECTED_SUNDAYS:
        available = [v for v in VOLUNTEERS if sunday not in v.unavailable_on]
        if len(available) < len(ROLE_NAMES):
            short_sundays.append(sunday)

    assert len(short_sundays) == 1, "exactly one Sunday should be short, for the demo"
    assert short_sundays[0] == EXPECTED_SUNDAYS[-1]


def test_no_availability_relies_on_a_missing_answer():
    """This first demo does not depend on NO_RESPONSE: every member answers
    for every Sunday, so the frontend checkbox changes nothing here.
    """
    for volunteer in VOLUNTEERS:
        for sunday in volunteer.unavailable_on:
            assert sunday in EXPECTED_SUNDAYS, (volunteer.display_name, sunday)
