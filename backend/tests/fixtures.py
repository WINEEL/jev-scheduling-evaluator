"""A fake TypeSafe boundary, and answers to feed it. No network, ever.

Shared by the soft-constraint tests. Two things live here:

- :class:`FakeSystemOneClient`, which satisfies
  :class:`~app.soft_constraints.evaluator.SystemOneCaller` and records the
  state and questions it was handed, so a test can assert what *would* have
  been sent without sending it.
- Builders for well-formed Score and Noul answers, so a test that is about one
  malformed field does not have to hand-write four correct ones around it.

The fake answers are deliberately duck-typed ``SimpleNamespace`` objects
rather than SDK models. That is the point: the evaluator parses responses
structurally, so the parsing the tests exercise is the parsing that runs in
production. Building these from ``typesafe_sdk`` classes instead would let a
parser that only works on SDK models pass.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

from app.soft_constraints.questions import (
    QUESTION_HUMAN_REVIEW,
    QUESTION_OVERALL_QUALITY,
    QUESTION_OVERUSE_CONCERN,
    QUESTION_PREFERENCE_SATISFACTION,
    QUESTION_WORKLOAD_FAIRNESS,
    SCORE_LEVELS,
)
from app.soft_constraints.state import PersonWorkload, SoftConstraintState

__all__ = [
    "FakeSystemOneClient",
    "balanced_state",
    "imbalanced_state",
    "noul_answer",
    "response",
    "score_answer",
]


class FakeSystemOneClient:
    """Records every call and returns a canned response. Opens no socket."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def system_one(
        self,
        state: Any,
        questions: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"state": state, "questions": dict(questions), **kwargs})
        return self._response

    # The evaluator must *not* call these on a client it was given; a test
    # asserts ``closed`` stays False.
    def close(self) -> None:  # pragma: no cover - asserted-against, not used
        self.closed = True

    def __exit__(self, *exc: object) -> None:  # pragma: no cover - same
        self.closed = True


def score_answer(score: float, confidence: float = 0.9) -> SimpleNamespace:
    """A well-formed Score answer, with a distribution that is merely plausible.

    ``probabilities`` is not derived from ``score``; nothing in the evaluator
    reads one from the other, and tying them here would test the fixture.
    """
    probabilities = {level: 0.0 for level in range(SCORE_LEVELS)}
    nearest = min(SCORE_LEVELS - 1, max(0, round(score)))
    probabilities[nearest] = 1.0
    return SimpleNamespace(
        score=score,
        confidence=confidence,
        probabilities=probabilities,
        legend={level: f"level {level}" for level in range(SCORE_LEVELS)},
    )


def noul_answer(probability: float) -> SimpleNamespace:
    """A well-formed Noul answer: a probability of yes and nothing else."""
    return SimpleNamespace(noul=probability)


def response(
    *,
    workload_fairness: float = 3.5,
    preference_satisfaction: float = 3.5,
    overall_quality: float = 3.5,
    overuse_concern: float = 0.05,
    human_review: float = 0.05,
    confidence: float = 0.9,
    model: str = "jev-test",
    answers: Mapping[str, Any] | None = None,
) -> SimpleNamespace:
    """A complete, well-formed response. Defaults describe a good draft.

    ``answers`` replaces the whole mapping, for the malformed-response tests.
    """
    if answers is None:
        answers = {
            QUESTION_WORKLOAD_FAIRNESS: score_answer(workload_fairness, confidence),
            QUESTION_PREFERENCE_SATISFACTION: score_answer(
                preference_satisfaction, confidence
            ),
            QUESTION_OVERALL_QUALITY: score_answer(overall_quality, confidence),
            QUESTION_OVERUSE_CONCERN: noul_answer(overuse_concern),
            QUESTION_HUMAN_REVIEW: noul_answer(human_review),
        }
    return SimpleNamespace(
        answers=dict(answers),
        model=model,
        usage=SimpleNamespace(input_tokens=321, output_tokens=12),
    )


def balanced_state() -> SoftConstraintState:
    """Synthetic: four events, four people, one each."""
    return SoftConstraintState(
        scenario="Four events shared evenly",
        event_count=4,
        people=[
            PersonWorkload(
                reference=f"Volunteer {letter}",
                available_events=4,
                assignments=1,
                preferred_max_assignments=2,
            )
            for letter in "ABCD"
        ],
    )


def imbalanced_state() -> SoftConstraintState:
    """Synthetic: four events, one person takes all of them."""
    return SoftConstraintState(
        scenario="Four events, one volunteer takes all",
        event_count=4,
        people=[
            PersonWorkload(
                reference="Volunteer A",
                available_events=4,
                assignments=4,
                preferred_max_assignments=2,
            ),
            *(
                PersonWorkload(
                    reference=f"Volunteer {letter}",
                    available_events=4,
                    assignments=0,
                    preferred_max_assignments=2,
                )
                for letter in "BCD"
            ),
        ],
    )
