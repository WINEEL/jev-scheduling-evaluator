"""Three invented drafts, and nothing real.

The synthetic corpus this project demonstrates itself with, and the only input
its API accepts.

**Every person here is invented.** ``Volunteer A`` through ``Volunteer F``,
plain integers, no dates, no team, no organization, no database. That is not
incidental to a demo -- it is the rule the evaluator enforces, written
somewhere a reader can check it: this file and
:mod:`app.soft_constraints.state` are what somebody reads to find out what
leaves the building.

**The scenario name is the whole input.** A caller picks one of three
identifiers and the state is constructed here, server-side. Nothing accepts a
name, a count or a note from outside, so there is no request that can put a
real person into a request to a third party.

The three exist to show the policy layer doing different things with one
rubric:

- ``imbalanced`` -- one person carries a whole period while three willing
  people carry nothing. The obvious bad case.
- ``balanced`` -- the same period spread evenly, preferences honoured. The
  obvious good case.
- ``ambiguous`` -- even totals, reached by overriding what two people asked
  for. The interesting one, and the reason a model is here at all: arithmetic
  alone calls it a fine schedule, and whether it is depends on things only a
  judgment weighs.
"""

from __future__ import annotations

from typing import Callable, Mapping

from app.soft_constraints.state import PersonWorkload, SoftConstraintState

__all__ = [
    "SCENARIOS",
    "SCENARIO_AMBIGUOUS",
    "SCENARIO_BALANCED",
    "SCENARIO_IMBALANCED",
    "SCENARIO_NAMES",
    "build_scenario",
    "describe_scenarios",
]

SCENARIO_IMBALANCED = "imbalanced"
SCENARIO_BALANCED = "balanced"
SCENARIO_AMBIGUOUS = "ambiguous"


def _imbalanced() -> SoftConstraintState:
    """One person does everything; three willing people do nothing."""
    return SoftConstraintState(
        scenario="Four weekly events; one volunteer carries all of them",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=4,
                preferred_max_assignments=2,
                preferences_declined=1,
            ),
            PersonWorkload(
                reference="Volunteer B",
                available_events=4,
                assignments=0,
                preferred_max_assignments=2,
                preferences_declined=1,
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
                preferred_max_assignments=2,
            ),
        ],
        notes=(
            "Every person listed is qualified and available for all four "
            "events; the distribution is the only thing in question.",
        ),
    )


def _balanced() -> SoftConstraintState:
    """The same period, spread evenly, with preferences honoured."""
    return SoftConstraintState(
        scenario="Four weekly events shared evenly across four volunteers",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
                preferences_granted=1,
            ),
            PersonWorkload(
                reference="Volunteer B",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
                preferences_granted=1,
            ),
            PersonWorkload(
                reference="Volunteer C",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
            ),
            PersonWorkload(
                reference="Volunteer D",
                available_events=4,
                assignments=1,
                preferred_max_assignments=1,
                preferences_granted=1,
            ),
        ],
    )


def _ambiguous() -> SoftConstraintState:
    """Even totals, reached by overriding what two people asked for.

    The case worth having a model for. Arithmetic alone says this is a fine
    schedule -- everybody has two of six events, spread zero. Two of the six
    are past the maximum they stated, one of them is new, and whether that is
    acceptable is a judgment rather than a calculation.
    """
    return SoftConstraintState(
        scenario=(
            "Six events across six volunteers with differing availability; "
            "totals are even but two people are past what they asked for"
        ),
        event_count=6,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=6,
                assignments=2,
                preferred_max_assignments=3,
                preferences_granted=2,
            ),
            PersonWorkload(
                reference="Volunteer B",
                available_events=6,
                assignments=2,
                preferred_max_assignments=1,
                preferences_declined=2,
                note="Joined the team this term and asked to start slowly.",
            ),
            PersonWorkload(
                reference="Volunteer C",
                available_events=4,
                assignments=2,
                preferred_max_assignments=2,
                preferences_granted=1,
            ),
            PersonWorkload(
                reference="Volunteer D",
                available_events=3,
                assignments=2,
                preferred_max_assignments=1,
                preferences_declined=1,
                note="Available for half the events and asked for one.",
            ),
            PersonWorkload(
                reference="Volunteer E",
                available_events=6,
                assignments=2,
            ),
            PersonWorkload(
                reference="Volunteer F",
                available_events=6,
                assignments=2,
                preferred_max_assignments=4,
                preferences_granted=1,
            ),
        ],
        notes=(
            "Total assignments are identical for everybody; the question is "
            "whether reaching that took more from some people than they "
            "offered.",
        ),
    )


#: The builders, keyed by identifier. A mapping of *functions*, not of built
#: states: every caller gets its own immutable state rather than a shared one
#: that a future caller could be tempted to adapt in place.
SCENARIOS: Mapping[str, Callable[[], SoftConstraintState]] = {
    SCENARIO_IMBALANCED: _imbalanced,
    SCENARIO_BALANCED: _balanced,
    SCENARIO_AMBIGUOUS: _ambiguous,
}

#: In the order they are meant to be read: bad, good, then the interesting one.
SCENARIO_NAMES: tuple[str, ...] = (
    SCENARIO_IMBALANCED,
    SCENARIO_BALANCED,
    SCENARIO_AMBIGUOUS,
)

#: One line per scenario, for a menu. Written for somebody choosing between
#: them, so each says what makes it different rather than restating its name.
SCENARIO_SUMMARIES: Mapping[str, str] = {
    SCENARIO_IMBALANCED: (
        "One volunteer takes every event while three who were available take "
        "none. Every hard rule is still satisfied."
    ),
    SCENARIO_BALANCED: (
        "The same period shared evenly, with everybody inside the maximum "
        "they asked for."
    ),
    SCENARIO_AMBIGUOUS: (
        "Totals are identical for everybody, but two people are past the "
        "maximum they asked for. Arithmetic cannot settle this one."
    ),
}


def build_scenario(name: str) -> SoftConstraintState:
    """The synthetic state for ``name``.

    Raises:
        KeyError: ``name`` is not one of :data:`SCENARIO_NAMES`. Deliberately
            not a fallback to a default scenario: a caller that asked for
            something that does not exist should be told, not quietly given
            something else and shown a judgment about it.
    """
    try:
        build = SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"unknown scenario {name!r}; expected one of {list(SCENARIO_NAMES)}"
        ) from None
    return build()


def describe_scenarios() -> tuple[tuple[str, str, SoftConstraintState], ...]:
    """Every scenario as ``(name, summary, state)``, in reading order."""
    return tuple(
        (name, SCENARIO_SUMMARIES[name], build_scenario(name))
        for name in SCENARIO_NAMES
    )
