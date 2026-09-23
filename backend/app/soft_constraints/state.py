"""The normalized input a soft-constraint evaluation is asked about.

Plain immutable values -- no ORM, no database session, no framework. That is
the point of this module rather than an accident of layering: the thing sent to
a third-party API should be a small, reviewable structure somebody can read in
full before it leaves the building, not an object graph whose contents depend
on what happened to be loaded.

**Nothing identifying belongs here.** ``PersonWorkload.reference`` is an opaque
label the caller chooses -- ``"Volunteer A"``, an internal id, a hash. This
module never sees a name, an email address, a team or a date, because none of
them is needed to judge whether four assignments out of four events is a fair
share, and every one of them would be sent to a vendor.

**Hard constraints are already satisfied by the time this state is built.**
In a real scheduling system a deterministic engine owns qualification,
availability, per-person limits, exclusions and gaps, and it owns them
absolutely. What is left over is the fuzzy residue -- *is this a reasonable way
to spread the work?* -- and that is the only question this package asks.
``assignments > available_events`` is therefore rejected as a caller bug rather
than reported as poor quality: it describes a schedule no correct engine could
have produced.

**The summary is computed here, in Python, and sent as part of the state.**
Counting, averaging and comparing against a stated preference are exact
operations; asking a model to redo them in its head would make a judgment that
should be about *fairness* partly about arithmetic. Jev is given the numbers
and asked what they mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

__all__ = [
    "EVALUATION_SCOPE",
    "PersonWorkload",
    "SoftConstraintState",
    "WorkloadSummary",
]

#: Sent with every request, verbatim. It is not decoration: without it the
#: model has no way to know that the things it would normally flag first --
#: somebody scheduled while unavailable, somebody past their agreed maximum --
#: are impossible by construction here, and a judgment that spends its
#: attention ruling them out is a judgment that spent less on the question
#: actually asked.
EVALUATION_SCOPE = (
    "Judge soft scheduling quality only. Every hard constraint is already "
    "satisfied: everyone listed is qualified, available for the events they "
    "were given, and within every limit the scheduling rules enforce. The "
    "open question is whether this is a considerate way to spread the work "
    "across the people who volunteered for it."
)


@dataclass(frozen=True, slots=True)
class PersonWorkload:
    """One person's share of a scheduling period, anonymized.

    ``reference`` is an opaque label, never a name. ``preferred_max_assignments``
    is the *soft* "I would rather not do more than about this many" -- distinct
    from a hard per-person cap, which a deterministic engine enforces and which
    nothing here may second-guess. ``None`` means the person expressed no
    preference, which is not the same as preferring zero.
    """

    reference: str
    available_events: int
    assignments: int
    preferred_max_assignments: int | None = None
    #: How many of this person's expressed scheduling preferences the draft
    #: honoured, and how many it did not. Counts, not the preferences
    #: themselves: "wanted three, got one" is what the judgment needs.
    preferences_granted: int = 0
    preferences_declined: int = 0
    #: One short, already-anonymized sentence of context a coordinator would
    #: want weighed -- "new to the team", "asked to be eased back in". Never a
    #: medical, family or relational circumstance: a schedule needs the number,
    #: not somebody's situation.
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.reference.strip():
            raise ValueError("reference must be a non-empty label")
        for name in ("available_events", "assignments", "preferences_granted", "preferences_declined"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must not be negative (got {value})")
        if self.assignments > self.available_events:
            raise ValueError(
                f"{self.reference}: assignments ({self.assignments}) exceeds "
                f"available_events ({self.available_events}); that is a hard-constraint "
                "violation, which this layer does not evaluate"
            )
        if self.preferred_max_assignments is not None and self.preferred_max_assignments < 0:
            raise ValueError("preferred_max_assignments must not be negative")

    @property
    def over_preferred_limit(self) -> bool:
        """Is this person past the number they said they would rather not exceed?"""
        if self.preferred_max_assignments is None:
            return False
        return self.assignments > self.preferred_max_assignments

    def to_state(self) -> dict[str, Any]:
        """The JSON object this person contributes to the request state."""
        payload: dict[str, Any] = {
            "person": self.reference,
            "available_events": self.available_events,
            "assignments": self.assignments,
            "preferred_max_assignments": self.preferred_max_assignments,
            "over_preferred_limit": self.over_preferred_limit,
            "preferences_granted": self.preferences_granted,
            "preferences_declined": self.preferences_declined,
        }
        if self.note is not None:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True, slots=True)
class WorkloadSummary:
    """The exact arithmetic, computed once and handed to the model.

    Derived entirely from the people in a :class:`SoftConstraintState`; it
    holds no independent truth and is never passed in by a caller.
    """

    people_count: int
    total_assignments: int
    mean_assignments: float
    minimum_assignments: int
    maximum_assignments: int
    #: ``maximum - minimum``. The single number that most often carries the
    #: fairness question, which is why it is spelled out rather than left for
    #: the model to subtract.
    assignment_spread: int
    #: People who were available for at least one event and received none.
    unused_available_people: int
    #: People scheduled past their own stated soft preference.
    people_over_preferred_limit: int
    #: ``granted / (granted + declined)`` across everybody, or ``None`` when
    #: nobody expressed a preference -- which is materially different from
    #: "every preference was refused" and must not be reported as 0.0.
    preference_grant_rate: float | None

    def to_state(self) -> dict[str, Any]:
        return {
            "people_count": self.people_count,
            "total_assignments": self.total_assignments,
            "mean_assignments_per_person": round(self.mean_assignments, 2),
            "minimum_assignments": self.minimum_assignments,
            "maximum_assignments": self.maximum_assignments,
            "assignment_spread": self.assignment_spread,
            "unused_available_people": self.unused_available_people,
            "people_over_preferred_limit": self.people_over_preferred_limit,
            "preference_grant_rate": (
                None
                if self.preference_grant_rate is None
                else round(self.preference_grant_rate, 2)
            ),
        }


@dataclass(frozen=True, slots=True)
class SoftConstraintState:
    """A whole draft, reduced to what a soft-constraint judgment needs.

    Built by a caller from whatever it has -- a solver result, a stored
    version, a synthetic scenario in the demo -- and never by this package
    from a database.
    """

    scenario: str
    event_count: int
    people: tuple[PersonWorkload, ...]
    #: Free-text context about the period as a whole: "two of the five events
    #: are at short notice". Anonymized, like everything else here.
    notes: tuple[str, ...] = ()

    def __init__(
        self,
        *,
        scenario: str,
        event_count: int,
        people: Sequence[PersonWorkload],
        notes: Sequence[str] = (),
    ) -> None:
        if not scenario.strip():
            raise ValueError("scenario must be a non-empty description")
        if event_count <= 0:
            raise ValueError(f"event_count must be positive (got {event_count})")
        if not people:
            raise ValueError("a soft-constraint state needs at least one person")
        references = [person.reference for person in people]
        duplicates = {ref for ref in references if references.count(ref) > 1}
        if duplicates:
            raise ValueError(f"duplicate person references: {sorted(duplicates)}")
        for person in people:
            if person.available_events > event_count:
                raise ValueError(
                    f"{person.reference}: available_events ({person.available_events}) "
                    f"exceeds event_count ({event_count})"
                )
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "event_count", event_count)
        object.__setattr__(self, "people", tuple(people))
        object.__setattr__(self, "notes", tuple(notes))

    @property
    def summary(self) -> WorkloadSummary:
        """The exact counts behind the fuzzy question."""
        assignments = [person.assignments for person in self.people]
        granted = sum(person.preferences_granted for person in self.people)
        declined = sum(person.preferences_declined for person in self.people)
        expressed = granted + declined
        return WorkloadSummary(
            people_count=len(self.people),
            total_assignments=sum(assignments),
            mean_assignments=sum(assignments) / len(assignments),
            minimum_assignments=min(assignments),
            maximum_assignments=max(assignments),
            assignment_spread=max(assignments) - min(assignments),
            unused_available_people=sum(
                1
                for person in self.people
                if person.assignments == 0 and person.available_events > 0
            ),
            people_over_preferred_limit=sum(
                1 for person in self.people if person.over_preferred_limit
            ),
            preference_grant_rate=(granted / expressed) if expressed else None,
        )

    def to_state(self) -> dict[str, Any]:
        """The JSON object sent as the request's ``state``.

        Named fields rather than one prose blob, as the state guidance asks:
        the questions reference ``summary.assignment_spread`` and
        ``people[].assignments`` by name, and a flat sentence would leave the
        model to find them.
        """
        payload: dict[str, Any] = {
            "scenario": self.scenario,
            "evaluation_scope": EVALUATION_SCOPE,
            "event_count": self.event_count,
            "summary": self.summary.to_state(),
            "people": [person.to_state() for person in self.people],
        }
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload
