"""Soft-constraint evaluation state: validation, arithmetic, and what it sends.

Offline: no PostgreSQL, no network, no TypeSafe.

Three claims are worth testing here, and they are the three this module is
responsible for:

- **The summary is exact.** It is computed in Python precisely so a model
  never has to derive it, which only helps if it is right.
- **Hard-constraint violations are caller bugs, not poor quality.** The layer
  above (:mod:`app.scheduling`) has already enforced them; a state that
  contradicts that describes a schedule the solver could not have produced, so
  it is rejected rather than scored.
- **Nothing identifying is in the payload, and the payload is JSON.** The
  second because it is sent to a vendor; the first because of what it is.
"""

from __future__ import annotations

import json

import pytest

from app.soft_constraints.state import (
    EVALUATION_SCOPE,
    PersonWorkload,
    SoftConstraintState,
)


# --------------------------------------------------------------------------
# PersonWorkload
# --------------------------------------------------------------------------


def test_over_preferred_limit_is_false_without_a_stated_preference():
    """No stated maximum is not a maximum of zero.

    The distinction matters: somebody who never answered the question has not
    been overworked by being given four events, and reporting them as over
    their limit would put a rule in the state that nobody agreed to.
    """
    person = PersonWorkload(reference="Volunteer A", available_events=4, assignments=4)

    assert person.preferred_max_assignments is None
    assert person.over_preferred_limit is False


def test_over_preferred_limit_compares_against_the_stated_maximum():
    at_limit = PersonWorkload(
        reference="Volunteer A",
        available_events=4,
        assignments=2,
        preferred_max_assignments=2,
    )
    over_limit = PersonWorkload(
        reference="Volunteer B",
        available_events=4,
        assignments=3,
        preferred_max_assignments=2,
    )

    assert at_limit.over_preferred_limit is False
    assert over_limit.over_preferred_limit is True


def test_more_assignments_than_available_events_is_rejected():
    """A hard-constraint violation is not a soft-constraint judgment."""
    with pytest.raises(ValueError, match="hard-constraint violation"):
        PersonWorkload(reference="Volunteer A", available_events=2, assignments=3)


@pytest.mark.parametrize(
    "field",
    ["available_events", "assignments", "preferences_granted", "preferences_declined"],
)
def test_negative_counts_are_rejected(field):
    kwargs = {"reference": "Volunteer A", "available_events": 4, "assignments": 1}
    kwargs[field] = -1

    with pytest.raises(ValueError, match="must not be negative"):
        PersonWorkload(**kwargs)


def test_a_blank_reference_is_rejected():
    with pytest.raises(ValueError, match="non-empty label"):
        PersonWorkload(reference="   ", available_events=4, assignments=1)


def test_a_person_is_immutable():
    person = PersonWorkload(reference="Volunteer A", available_events=4, assignments=1)

    with pytest.raises(Exception):
        person.assignments = 2  # type: ignore[misc]


# --------------------------------------------------------------------------
# SoftConstraintState validation
# --------------------------------------------------------------------------


def test_an_empty_people_list_is_rejected():
    with pytest.raises(ValueError, match="at least one person"):
        SoftConstraintState(scenario="empty", event_count=4, people=[])


def test_duplicate_references_are_rejected():
    """Two rows for one person would double-count them in every statistic."""
    person = PersonWorkload(reference="Volunteer A", available_events=4, assignments=1)

    with pytest.raises(ValueError, match="duplicate person references"):
        SoftConstraintState(scenario="dupe", event_count=4, people=[person, person])


def test_availability_beyond_the_period_is_rejected():
    with pytest.raises(ValueError, match="exceeds event_count"):
        SoftConstraintState(
            scenario="impossible",
            event_count=2,
            people=[
                PersonWorkload(
                    reference="Volunteer A", available_events=4, assignments=1
                )
            ],
        )


def test_a_non_positive_event_count_is_rejected():
    with pytest.raises(ValueError, match="event_count must be positive"):
        SoftConstraintState(
            scenario="no events",
            event_count=0,
            people=[
                PersonWorkload(
                    reference="Volunteer A", available_events=0, assignments=0
                )
            ],
        )


# --------------------------------------------------------------------------
# The summary: the exact arithmetic handed to the model
# --------------------------------------------------------------------------


def test_summary_counts_spread_unused_people_and_over_limit_people():
    state = SoftConstraintState(
        scenario="Four events, one volunteer takes all",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=4,
                preferred_max_assignments=2,
            ),
            PersonWorkload(
                reference="Volunteer B",
                available_events=4,
                assignments=0,
                preferred_max_assignments=2,
            ),
            PersonWorkload(
                reference="Volunteer C",
                available_events=4,
                assignments=0,
                preferred_max_assignments=2,
            ),
            PersonWorkload(
                reference="Volunteer D",
                available_events=4,
                assignments=0,
            ),
        ],
    )

    summary = state.summary

    assert summary.people_count == 4
    assert summary.total_assignments == 4
    assert summary.mean_assignments == 1.0
    assert summary.minimum_assignments == 0
    assert summary.maximum_assignments == 4
    assert summary.assignment_spread == 4
    assert summary.unused_available_people == 3
    # Volunteer D stated no maximum, so they are not over one.
    assert summary.people_over_preferred_limit == 1


def test_an_unavailable_person_with_no_assignments_is_not_counted_as_unused():
    """"Unused" means willing and not used, not merely absent from the roster."""
    state = SoftConstraintState(
        scenario="One volunteer unavailable throughout",
        event_count=4,
        people=[
            PersonWorkload(reference="Volunteer A", available_events=4, assignments=4),
            PersonWorkload(reference="Volunteer B", available_events=0, assignments=0),
        ],
    )

    assert state.summary.unused_available_people == 0


def test_preference_grant_rate_is_none_when_nobody_expressed_a_preference():
    """None, never 0.0.

    "Nobody asked for anything" and "every request was refused" are opposite
    situations, and reporting the first as a zero grant rate would invite the
    model to judge a draft harshly for something that never happened.
    """
    state = SoftConstraintState(
        scenario="No preferences expressed",
        event_count=4,
        people=[
            PersonWorkload(reference="Volunteer A", available_events=4, assignments=2),
            PersonWorkload(reference="Volunteer B", available_events=4, assignments=2),
        ],
    )

    assert state.summary.preference_grant_rate is None


def test_preference_grant_rate_is_granted_over_expressed():
    state = SoftConstraintState(
        scenario="Mixed preference outcomes",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=2,
                preferences_granted=3,
                preferences_declined=1,
            ),
            PersonWorkload(
                reference="Volunteer B",
                available_events=4,
                assignments=2,
                preferences_declined=1,
            ),
        ],
    )

    assert state.summary.preference_grant_rate == pytest.approx(3 / 5)


# --------------------------------------------------------------------------
# The payload: what actually leaves the building
# --------------------------------------------------------------------------


def test_the_payload_is_json_serializable_and_names_its_parts():
    state = SoftConstraintState(
        scenario="Four events shared evenly",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
                note="Asked to be eased back in.",
            )
        ],
        notes=("Two events are at short notice.",),
    )

    payload = state.to_state()
    json.dumps(payload)  # raises if anything is not JSON

    assert payload["scenario"] == "Four events shared evenly"
    assert payload["evaluation_scope"] == EVALUATION_SCOPE
    assert payload["event_count"] == 4
    assert payload["notes"] == ["Two events are at short notice."]
    assert payload["summary"]["assignment_spread"] == 0
    assert payload["people"][0]["person"] == "Volunteer A"
    assert payload["people"][0]["note"] == "Asked to be eased back in."


def test_the_payload_omits_notes_that_were_never_given():
    """An absent note is absent, not an empty string the model must interpret."""
    state = SoftConstraintState(
        scenario="No notes",
        event_count=4,
        people=[
            PersonWorkload(reference="Volunteer A", available_events=4, assignments=1)
        ],
    )

    payload = state.to_state()

    assert "notes" not in payload
    assert "note" not in payload["people"][0]


def test_the_payload_carries_only_the_reference_and_counts():
    """The anonymity guarantee, asserted rather than asserted-in-prose.

    Every key in a person's payload is one of a fixed, reviewable set. A field
    added later that carried a name, an email address or a date would fail
    here, which is the point.
    """
    state = SoftConstraintState(
        scenario="Four events shared evenly",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
                note="New this term.",
            )
        ],
    )

    permitted = {
        "person",
        "available_events",
        "assignments",
        "preferred_max_assignments",
        "over_preferred_limit",
        "preferences_granted",
        "preferences_declined",
        "note",
    }

    for person in state.to_state()["people"]:
        assert set(person) <= permitted
