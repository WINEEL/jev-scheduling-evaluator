"""Typed answers, so a caller never parses model output.

Everything Jev returns for a soft-constraint evaluation arrives here first and
is checked on the way in: the right questions answered, the right primitive for
each, every probability inside ``0..1``, every score inside its rubric. A
caller that reads :class:`SoftConstraintJudgments` is reading values that have
already been proven well-formed, which is the whole reason this layer exists
between the SDK and :mod:`app.soft_constraints.policy`.

**Typed output guarantees the interface, not the truth.** A ``ScoreJudgment``
of 3.4 is a well-formed number whether or not it is the right one; nothing in
this module claims the judgment is correct, only that it is the shape the rest
of the code was written against.

``normalized`` is the one derived value, and it exists so thresholds do not
have to know rubric sizes. A five-level Score returns ``0.0..4.0``;
``normalized`` divides by ``levels - 1`` to give ``0.0..1.0``, where 1.0 is the
top level. Policy is written against ``normalized`` alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "NoulJudgment",
    "ScoreJudgment",
    "SoftConstraintEvaluationError",
    "SoftConstraintJudgments",
]


class SoftConstraintEvaluationError(RuntimeError):
    """A soft-constraint evaluation could not be completed or believed.

    Raised for a malformed or incomplete answer set -- a question missing, a
    Noul where a Score was asked, a probability outside ``0..1``. Transport and
    authentication failures keep the SDK's own exception types
    (``TypeSafeAPIError`` and friends); wrapping them here would only hide
    which of the two went wrong.
    """


@dataclass(frozen=True, slots=True)
class ScoreJudgment:
    """One Score answer: where on a described rubric, and how sure.

    ``probabilities`` is the full distribution across the rubric's levels, kept
    because ``confidence`` is a summary of it and a caller diagnosing an odd
    result needs the shape, not the summary (TypeSafe's confidence guidance).
    """

    question: str
    score: float
    levels: int
    confidence: float
    probabilities: Mapping[int, float]

    @property
    def normalized(self) -> float:
        """``0.0..1.0``, where 1.0 is the top rubric level."""
        return self.score / (self.levels - 1)


@dataclass(frozen=True, slots=True)
class NoulJudgment:
    """One Noul answer: the probability that the answer is yes.

    No ``confidence`` field, deliberately -- TypeSafe does not return one for a
    Noul, and inventing one would invite ``0.5`` to be read as "moderately
    yes" when it means "as likely as not".
    """

    question: str
    probability: float


@dataclass(frozen=True, slots=True)
class SoftConstraintJudgments:
    """Every judgment from one evaluation, plus what produced it.

    ``model`` and the token counts are carried because a stored judgment is
    only interpretable next to the model that made it: rubric wording and model
    version both move, and a result that cannot say which produced it cannot be
    compared with a later one.
    """

    workload_fairness: ScoreJudgment
    preference_satisfaction: ScoreJudgment
    overall_quality: ScoreJudgment
    overuse_concern: NoulJudgment
    human_review_warranted: NoulJudgment
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def scores(self) -> Mapping[str, ScoreJudgment]:
        """The Score answers keyed by question id, for reporting."""
        return MappingProxyType(
            {
                judgment.question: judgment
                for judgment in (
                    self.workload_fairness,
                    self.preference_satisfaction,
                    self.overall_quality,
                )
            }
        )

    @property
    def nouls(self) -> Mapping[str, NoulJudgment]:
        """The Noul answers keyed by question id, for reporting."""
        return MappingProxyType(
            {
                judgment.question: judgment
                for judgment in (self.overuse_concern, self.human_review_warranted)
            }
        )
