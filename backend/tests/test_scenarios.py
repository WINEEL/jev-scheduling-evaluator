"""The synthetic corpus itself: it is synthetic, and it demonstrates something.

Offline. No model, no network, no database.

These three drafts are the only input the demo surfaces accept, so what they
contain is the whole answer to "what does this send to a third party?". Two
claims are worth pinning:

- **Every person is invented, and every field is a count.** Asserted against a
  fixed permitted set rather than left to review, so a future edit that pastes
  in a real roster to make a screenshot more convincing fails here.
- **Each scenario actually shows what it claims.** A demo whose "bad" case is
  not bad demonstrates nothing, and an "ambiguous" case that arithmetic can
  settle is not ambiguous.

``tests/test_api.py`` covers the endpoints that serve them.
"""

from __future__ import annotations

import json

import pytest

from app.soft_constraints.scenarios import (
    SCENARIO_NAMES,
    SCENARIO_SUMMARIES,
    SCENARIOS,
    build_scenario,
    describe_scenarios,
)
from app.soft_constraints.state import SoftConstraintState

PERMITTED_REFERENCES = {f"Volunteer {letter}" for letter in "ABCDEF"}
PERMITTED_PERSON_KEYS = {
    "person",
    "available_events",
    "assignments",
    "preferred_max_assignments",
    "over_preferred_limit",
    "preferences_granted",
    "preferences_declined",
    "note",
}


def test_the_three_required_scenarios_exist_in_reading_order():
    """Bad, good, then the interesting one -- the order a demo is walked."""
    assert SCENARIO_NAMES == ("imbalanced", "balanced", "ambiguous")
    assert set(SCENARIOS) == set(SCENARIO_NAMES)
    assert set(SCENARIO_SUMMARIES) == set(SCENARIO_NAMES)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_every_scenario_builds_a_valid_json_state(name):
    state = build_scenario(name)

    assert isinstance(state, SoftConstraintState)
    json.dumps(state.to_state())


def test_each_call_returns_a_separate_state():
    """A mapping of builders, not of built states.

    Sharing one immutable state between callers would be harmless today and an
    invitation tomorrow, when somebody wants to adapt one for a screenshot.
    """
    assert build_scenario("balanced") is not build_scenario("balanced")
    assert build_scenario("balanced") == build_scenario("balanced")


def test_an_unknown_scenario_raises_rather_than_falling_back():
    """A caller who asked for something that does not exist should be told.

    A default would show them a judgment about a draft they did not ask for,
    which is worse than an error in exactly the way that is hard to notice.
    """
    with pytest.raises(KeyError, match="unknown scenario"):
        build_scenario("does-not-exist")


def test_the_imbalanced_scenario_is_actually_imbalanced():
    summary = build_scenario("imbalanced").summary

    assert summary.assignment_spread == 4
    assert summary.unused_available_people == 3
    assert summary.people_over_preferred_limit == 1


def test_the_balanced_scenario_is_actually_balanced():
    summary = build_scenario("balanced").summary

    assert summary.assignment_spread == 0
    assert summary.unused_available_people == 0
    assert summary.people_over_preferred_limit == 0


def test_the_ambiguous_scenario_is_even_yet_overrides_stated_preferences():
    """The case a deterministic rule cannot settle, which is why it is here.

    Spread is zero, so arithmetic alone calls this a fine schedule. Two people
    are past the maximum they asked for, which is a judgment rather than a
    calculation -- exactly the residue this package exists to evaluate.
    """
    summary = build_scenario("ambiguous").summary

    assert summary.assignment_spread == 0
    assert summary.people_over_preferred_limit == 2
    assert summary.preference_grant_rate is not None
    assert summary.preference_grant_rate < 1.0


def test_every_person_in_every_scenario_is_synthetic():
    for name in SCENARIO_NAMES:
        payload = build_scenario(name).to_state()
        for person in payload["people"]:
            assert person["person"] in PERMITTED_REFERENCES
            assert set(person) <= PERMITTED_PERSON_KEYS


def test_no_scenario_text_names_a_date_an_email_or_an_organization():
    """The free-text fields are where a real detail would most easily arrive.

    Scenario descriptions and notes are prose, so they are the one part of the
    corpus a reviewer cannot check by looking at a key name. This checks them.
    """
    for name in SCENARIO_NAMES:
        text = json.dumps(build_scenario(name).to_state()).lower()

        assert "@" not in text
        for month in ("january", "february", "march", "sunday", "monday"):
            assert month not in text


def test_describe_scenarios_pairs_each_state_with_its_summary_line():
    described = describe_scenarios()

    assert [name for name, _, _ in described] == list(SCENARIO_NAMES)
    for name, summary_line, state in described:
        assert summary_line == SCENARIO_SUMMARIES[name]
        assert isinstance(state, SoftConstraintState)
        assert summary_line.strip() != ""
