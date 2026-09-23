"""Transport models for the synthetic Jev demo.

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes`` -- and one extra rule that matters more than the rest:

**Nothing in this module carries a request field naming a person.** The only
input the demo accepts is a scenario identifier, and it arrives in the path as
an enum of three values. There is deliberately no request body at all: a demo
that accepted a roster would be a demo that could be handed a real one, and
what is sent to TypeSafe would then depend on what a caller typed rather than
on what :mod:`app.soft_constraints.scenarios` contains.

**The responses mirror the evaluator's own result types**
(:mod:`app.soft_constraints.results`, :mod:`app.soft_constraints.policy`)
field for field, rather than inventing a flatter shape for the screen. Two
reasons: the distinction the demo exists to show -- model probabilities on one
side, a deterministic status on the other -- is carried by that structure, and
a screen that received a single blended number could not show it. And the
frontend's types then describe the evaluator, so a change to the judgment
shape surfaces as a type error rather than as a silently missing field.

**What is deliberately absent from every response**: the API key, the
questions' instructions and rubric text, the request body sent to TypeSafe,
and anything read from a database. The prompt text is internal -- interesting
to a developer reading the source, not something an HTTP surface should hand
out -- and there is no database read on this path to have anything to leak.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.soft_constraints.scenarios import (
    SCENARIO_AMBIGUOUS,
    SCENARIO_BALANCED,
    SCENARIO_IMBALANCED,
)

__all__ = [
    "DemoScenarioName",
    "JevDemoEvaluationResponse",
    "JevDemoJudgmentsResponse",
    "JevDemoNoulAnswer",
    "JevDemoPolicyResponse",
    "JevDemoScoreAnswer",
    "JevDemoScenarioDetail",
    "JevDemoScenarioListResponse",
    "JevDemoStateResponse",
    "JevDemoWorkloadPerson",
    "JevDemoWorkloadSummary",
]


class DemoScenarioName(str, Enum):
    """The three scenarios, as a closed path parameter.

    An enum rather than a free string so FastAPI refuses an unknown name with
    422 before any handler runs, and so the OpenAPI schema states the whole
    set. There is no fourth value and no way to add one from a request.
    """

    imbalanced = SCENARIO_IMBALANCED
    balanced = SCENARIO_BALANCED
    ambiguous = SCENARIO_AMBIGUOUS


class JevDemoWorkloadPerson(BaseModel):
    """One invented volunteer's share of an invented period.

    ``reference`` is a label, never a name: the scenarios use ``Volunteer A``
    through ``Volunteer F``.
    """

    model_config = ConfigDict(extra="forbid")

    reference: str
    available_events: int
    assignments: int
    #: ``null`` when this person expressed no soft maximum -- which is not the
    #: same as a maximum of zero, and is never rendered as one.
    preferred_max_assignments: int | None
    over_preferred_limit: bool
    preferences_granted: int
    preferences_declined: int
    note: str | None


class JevDemoWorkloadSummary(BaseModel):
    """The exact arithmetic, computed in Python before any model is asked.

    Carried into the response as its own object rather than folded into the
    judgments, because the demo's point is that these numbers are *not* the
    judgment: they are what a deterministic system already knows, and what the
    model is asked to interpret.
    """

    model_config = ConfigDict(extra="forbid")

    people_count: int
    total_assignments: int
    mean_assignments_per_person: float
    minimum_assignments: int
    maximum_assignments: int
    assignment_spread: int
    unused_available_people: int
    people_over_preferred_limit: int
    #: ``null`` when nobody expressed a preference, never ``0.0``.
    preference_grant_rate: float | None


class JevDemoStateResponse(BaseModel):
    """A whole synthetic draft, exactly as it is handed to the evaluator."""

    model_config = ConfigDict(extra="forbid")

    name: str
    summary_line: str
    scenario: str
    event_count: int
    notes: list[str]
    workload_summary: JevDemoWorkloadSummary
    people: list[JevDemoWorkloadPerson]


class JevDemoScenarioDetail(JevDemoStateResponse):
    """A scenario in the menu. Identical to the state, and named for the menu."""


class JevDemoScenarioListResponse(BaseModel):
    """Every scenario the demo can run, in reading order."""

    model_config = ConfigDict(extra="forbid")

    scenarios: list[JevDemoScenarioDetail]


class JevDemoScoreAnswer(BaseModel):
    """One Jev ``Score``: a position on a rubric, and how concentrated it is.

    ``score`` is on the rubric's own 0..``levels - 1`` scale and ``normalized``
    is the same value on 0..1. Both are sent because the screen shows the
    rubric position and the thresholds are stated on the normalized scale --
    deriving one from the other in the browser would put a second copy of that
    rule somewhere it could disagree.
    """

    model_config = ConfigDict(extra="forbid")

    question: str
    score: float
    levels: int
    normalized: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    #: The full distribution across rubric levels, keyed by level. Sent because
    #: confidence is a summary of it, and a flat distribution is worth being
    #: able to show.
    probabilities: dict[int, float]


class JevDemoNoulAnswer(BaseModel):
    """One Jev ``Noul``: the probability that the answer is yes.

    No confidence field, because TypeSafe does not return one for a Noul and
    inventing one would invite ``0.5`` to be read as "moderately yes" when it
    means "as likely as not".
    """

    model_config = ConfigDict(extra="forbid")

    question: str
    probability: float = Field(ge=0.0, le=1.0)


class JevDemoJudgmentsResponse(BaseModel):
    """What the model said. Probabilities only; no decision anywhere in here."""

    model_config = ConfigDict(extra="forbid")

    workload_fairness: JevDemoScoreAnswer
    preference_satisfaction: JevDemoScoreAnswer
    overall_quality: JevDemoScoreAnswer
    overuse_concern: JevDemoNoulAnswer
    human_review_warranted: JevDemoNoulAnswer
    #: Which model answered. A judgment is only interpretable next to the model
    #: that made it.
    model_name: str


class JevDemoPolicyResponse(BaseModel):
    """What the application decided. Ordinary Python; no model involved.

    ``thresholds`` is included so the screen can show the number each reason
    was measured against. That is the demo's central claim made checkable: the
    status is arithmetic over the judgments above, and a reader can do the
    comparison themselves.
    """

    model_config = ConfigDict(extra="forbid")

    #: ``acceptable`` | ``attention`` | ``human_review``.
    status: str
    #: The bounded reason vocabulary from :mod:`app.soft_constraints.policy`,
    #: most severe first. Empty exactly when the status is ``acceptable``.
    reasons: list[str]
    thresholds: dict[str, float]


class JevDemoEvaluationResponse(BaseModel):
    """One evaluated scenario: the input, the judgment, and the decision.

    All three in one response, in that order, because the demo's subject is
    the relationship between them -- what was already known, what the model
    added, and what the code did about it.
    """

    model_config = ConfigDict(extra="forbid")

    state: JevDemoStateResponse
    judgments: JevDemoJudgmentsResponse
    policy: JevDemoPolicyResponse
