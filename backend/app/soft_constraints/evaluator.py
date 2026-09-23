"""The boundary: state in, typed judgments and a local status out.

The only module in this package that talks to TypeSafe. Everything either side
of it is ordinary code -- :mod:`app.soft_constraints.state` builds the input,
:mod:`app.soft_constraints.policy` decides what to do -- which is what keeps
the vendor at one seam rather than spread through the application.

**The client is a parameter, and that is what makes this testable offline.**
:func:`evaluate_soft_constraints` takes any object with a ``system_one``
method (:class:`SystemOneCaller`); the unit suite passes a fake and never
opens a socket. When no client is given, one real
:class:`~typesafe_sdk.TypeSafeClient` is created and closed around the call,
reading ``TYPESAFE_API_KEY`` from the environment as the SDK does -- the key
never passes through this package, or through any application setting, because
nothing here needs to know it.

**Answers are parsed structurally, not trusted.** The response is read through
attributes rather than isinstance checks against SDK classes, so the parsing
this module actually performs is the parsing the tests exercise. Every answer
is checked: present, the right primitive, probabilities within ``0..1``, score
within its rubric. A response that fails any of those raises
:class:`~app.soft_constraints.results.SoftConstraintEvaluationError` rather
than producing a judgment nobody can interpret.

**One request, not five.** The five questions are independent, so they go
together and return together; a second call would be warranted only if one
answer were needed to build the next question's state, and none is.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Protocol

from app.soft_constraints.policy import (
    DEFAULT_POLICY,
    SoftConstraintAssessment,
    SoftConstraintPolicy,
    assess,
)
from app.soft_constraints.questions import (
    QUESTION_HUMAN_REVIEW,
    QUESTION_OVERALL_QUALITY,
    QUESTION_OVERUSE_CONCERN,
    QUESTION_PREFERENCE_SATISFACTION,
    QUESTION_WORKLOAD_FAIRNESS,
    SCORE_LEVELS,
    build_questions,
)
from app.soft_constraints.results import (
    NoulJudgment,
    ScoreJudgment,
    SoftConstraintEvaluationError,
    SoftConstraintJudgments,
)
from app.soft_constraints.state import SoftConstraintState

__all__ = [
    "SoftConstraintEvaluation",
    "SystemOneCaller",
    "evaluate_soft_constraints",
    "parse_judgments",
]


class SystemOneCaller(Protocol):
    """Anything that can answer a batch of System One questions.

    Structural, not a base class: ``typesafe_sdk.TypeSafeClient`` satisfies it
    without knowing this package exists, and so does a fake in a test.
    """

    def system_one(
        self,
        state: Any,
        questions: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class SoftConstraintEvaluation:
    """One evaluated draft: what was asked, what the model said, what we do.

    The state is carried alongside the judgments because a judgment without
    the numbers it was made about cannot be reviewed, and a coordinator asking
    "why does it say that?" is asking about the state.
    """

    state: SoftConstraintState
    judgments: SoftConstraintJudgments
    assessment: SoftConstraintAssessment

    @property
    def status(self) -> str:
        """Shorthand for ``assessment.status``."""
        return self.assessment.status


def evaluate_soft_constraints(
    state: SoftConstraintState,
    *,
    client: SystemOneCaller | None = None,
    policy: SoftConstraintPolicy = DEFAULT_POLICY,
    model: str | None = None,
) -> SoftConstraintEvaluation:
    """Ask Jev the five questions about ``state`` and apply local policy.

    Args:
        state: The normalized, anonymized draft to judge.
        client: Anything satisfying :class:`SystemOneCaller`. ``None`` creates
            a real ``TypeSafeClient`` for this call and closes it afterwards,
            which is the only path that touches the network.
        policy: Thresholds for the local status decision.
        model: A TypeSafe model override; ``None`` uses the SDK default.

    Raises:
        SoftConstraintEvaluationError: The answers are missing, incomplete or
            outside their declared ranges.
        typesafe_sdk.TypeSafeError: The SDK's own transport, authentication and
            rate-limit failures, deliberately not wrapped -- "the vendor is
            down" and "the vendor answered nonsense" are different problems
            with different remedies.
    """
    questions = build_questions()
    with _client(client) as caller:
        kwargs: dict[str, Any] = {"model": model} if model is not None else {}
        response = caller.system_one(state=state.to_state(), questions=questions, **kwargs)

    judgments = parse_judgments(response)
    return SoftConstraintEvaluation(
        state=state,
        judgments=judgments,
        assessment=assess(judgments, policy=policy),
    )


@contextlib.contextmanager
def _client(client: SystemOneCaller | None) -> Iterator[SystemOneCaller]:
    """Yield the caller's client untouched, or own a real one for one call.

    A client the caller supplied is never closed here: this function did not
    open it, and closing somebody else's connection pool is how a second
    evaluation in the same request fails.
    """
    if client is not None:
        yield client
        return
    # Imported at call time, not at module import: the import itself is cheap,
    # but keeping it here means a test that never evaluates live never needs
    # the SDK's HTTP stack constructed, and the failure mode of a missing
    # dependency lands on the call that needs it rather than on importing the
    # application.
    from typesafe_sdk import TypeSafeClient

    with TypeSafeClient() as owned:
        yield owned


def parse_judgments(response: Any) -> SoftConstraintJudgments:
    """Validate one ``system_one`` response into typed judgments.

    Separate from :func:`evaluate_soft_constraints` so parsing can be tested
    against fixed responses without any notion of a client, and so a caller
    holding a stored response can re-derive judgments from it.
    """
    answers = getattr(response, "answers", None)
    if not isinstance(answers, Mapping):
        raise SoftConstraintEvaluationError(
            "response has no `answers` mapping; got "
            f"{type(answers).__name__} from {type(response).__name__}"
        )

    judgments = SoftConstraintJudgments(
        workload_fairness=_score(answers, QUESTION_WORKLOAD_FAIRNESS),
        preference_satisfaction=_score(answers, QUESTION_PREFERENCE_SATISFACTION),
        overall_quality=_score(answers, QUESTION_OVERALL_QUALITY),
        overuse_concern=_noul(answers, QUESTION_OVERUSE_CONCERN),
        human_review_warranted=_noul(answers, QUESTION_HUMAN_REVIEW),
        model=str(getattr(response, "model", "") or "unknown"),
        input_tokens=_usage(response, "input_tokens"),
        output_tokens=_usage(response, "output_tokens"),
    )
    return judgments


def _answer(answers: Mapping[str, Any], question: str) -> Any:
    try:
        return answers[question]
    except KeyError:
        raise SoftConstraintEvaluationError(
            f"no answer for question {question!r}; "
            f"got {sorted(answers)}"
        ) from None


def _score(answers: Mapping[str, Any], question: str) -> ScoreJudgment:
    answer = _answer(answers, question)
    score = _number(answer, "score", question)
    confidence = _probability(answer, "confidence", question)
    top_level = SCORE_LEVELS - 1
    if not 0.0 <= score <= top_level:
        raise SoftConstraintEvaluationError(
            f"{question}: score {score} is outside the rubric's 0..{top_level}"
        )
    raw = getattr(answer, "probabilities", None)
    if not isinstance(raw, Mapping):
        raise SoftConstraintEvaluationError(
            f"{question}: expected a Score answer with `probabilities`; "
            f"got {type(answer).__name__}"
        )
    probabilities: dict[int, float] = {}
    for level, value in raw.items():
        probability = float(value)
        if not 0.0 <= probability <= 1.0:
            raise SoftConstraintEvaluationError(
                f"{question}: probability {probability} for level {level!r} "
                "is outside 0..1"
            )
        probabilities[int(level)] = probability

    return ScoreJudgment(
        question=question,
        score=score,
        levels=SCORE_LEVELS,
        confidence=confidence,
        probabilities=probabilities,
    )


def _noul(answers: Mapping[str, Any], question: str) -> NoulJudgment:
    answer = _answer(answers, question)
    if hasattr(answer, "score") or hasattr(answer, "choice"):
        raise SoftConstraintEvaluationError(
            f"{question}: expected a Noul answer; got {type(answer).__name__}"
        )
    return NoulJudgment(
        question=question,
        probability=_probability(answer, "noul", question),
    )


def _number(answer: Any, attribute: str, question: str) -> float:
    value = getattr(answer, attribute, None)
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SoftConstraintEvaluationError(
            f"{question}: expected a numeric `{attribute}`; "
            f"got {value!r} on {type(answer).__name__}"
        )
    return float(value)


def _probability(answer: Any, attribute: str, question: str) -> float:
    value = _number(answer, attribute, question)
    if not 0.0 <= value <= 1.0:
        raise SoftConstraintEvaluationError(
            f"{question}: `{attribute}` {value} is outside 0..1"
        )
    return value


def _usage(response: Any, attribute: str) -> int | None:
    """Token counts, when the API reported them. Never load-bearing."""
    usage = getattr(response, "usage", None)
    value = getattr(usage, attribute, None)
    return int(value) if isinstance(value, int) else None
