"""What the application does about a judgment. Ordinary Python, no model.

This module imports nothing from ``typesafe_sdk`` and never will. It takes the
probabilities in :class:`~app.soft_constraints.results.SoftConstraintJudgments`
and decides one local status: ``acceptable``, ``attention`` or
``human_review``. That separation is the point of the package.

**Why the model does not decide this.** Jev could be asked to pick the status
directly with a Choice. Three reasons not to:

- **The thresholds are the operator's, not the model's.** "Interrupt a
  coordinator" is a cost somebody local pays, and the point at which it becomes
  worth paying is a number they will want to move after seeing a season's worth
  of drafts. Moving it here is a one-line edit with a test; moving it inside a
  model's judgment is a rubric rewrite and a re-evaluation.
- **The same judgment must produce the same status.** A stored set of
  probabilities re-assessed next month yields exactly the same answer, because
  this is arithmetic. That is what makes a status auditable.
- **Re-deciding must not cost an inference call.** Changing a threshold or a
  display filter re-runs :func:`assess` over judgments already in hand; nothing
  is sent anywhere.

**Every rule is evaluated; none short-circuits.** One call reports everything
that is wrong at once, because a coordinator told only the first problem fixes
it and comes straight back.
``status`` is then the most severe reason present.

**Confidence is a second axis, not a score.** A quality rating the model is
genuinely unsure of routes to a human rather than being treated as a middling
rating -- TypeSafe's confidence-gated routing pattern. A Noul carries no
confidence, so no rule here pretends to read one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.soft_constraints.results import SoftConstraintJudgments

__all__ = [
    "DEFAULT_POLICY",
    "REASON_MODEL_REQUESTS_REVIEW",
    "REASON_OVERUSE_CONCERN",
    "REASON_PREFERENCES_UNMET",
    "REASON_QUALITY_BELOW_TARGET",
    "REASON_QUALITY_UNACCEPTABLE",
    "REASON_QUALITY_UNCERTAIN",
    "REASON_WORKLOAD_UNFAIR",
    "STATUS_ACCEPTABLE",
    "STATUS_ATTENTION",
    "STATUS_HUMAN_REVIEW",
    "SoftConstraintAssessment",
    "SoftConstraintPolicy",
    "assess",
]

#: Nothing in the draft's soft handling calls for anybody's time.
STATUS_ACCEPTABLE = "acceptable"
#: Something is worth a coordinator's eye, but the draft stands on its own.
STATUS_ATTENTION = "attention"
#: A person should look at this before it is sent.
STATUS_HUMAN_REVIEW = "human_review"

#: The bounded reason vocabulary. Short codes: each names one rule that fired,
#: and an assessment may carry several because several may genuinely apply.
REASON_MODEL_REQUESTS_REVIEW = "MODEL_REQUESTS_REVIEW"
REASON_QUALITY_UNACCEPTABLE = "QUALITY_UNACCEPTABLE"
REASON_QUALITY_UNCERTAIN = "QUALITY_UNCERTAIN"
REASON_OVERUSE_CONCERN = "OVERUSE_CONCERN"
REASON_WORKLOAD_UNFAIR = "WORKLOAD_UNFAIR"
REASON_PREFERENCES_UNMET = "PREFERENCES_UNMET"
REASON_QUALITY_BELOW_TARGET = "QUALITY_BELOW_TARGET"

#: Which status each reason forces. A reason that maps to ``human_review``
#: outranks any number of ``attention`` reasons; there is no accumulation rule
#: by which three mild problems become a severe one, because there is no
#: evidence that they do.
_REASON_STATUS = {
    REASON_MODEL_REQUESTS_REVIEW: STATUS_HUMAN_REVIEW,
    REASON_QUALITY_UNACCEPTABLE: STATUS_HUMAN_REVIEW,
    REASON_QUALITY_UNCERTAIN: STATUS_HUMAN_REVIEW,
    REASON_OVERUSE_CONCERN: STATUS_ATTENTION,
    REASON_WORKLOAD_UNFAIR: STATUS_ATTENTION,
    REASON_PREFERENCES_UNMET: STATUS_ATTENTION,
    REASON_QUALITY_BELOW_TARGET: STATUS_ATTENTION,
}

_STATUS_SEVERITY = {
    STATUS_ACCEPTABLE: 0,
    STATUS_ATTENTION: 1,
    STATUS_HUMAN_REVIEW: 2,
}


@dataclass(frozen=True, slots=True)
class SoftConstraintPolicy:
    """The thresholds, in one place, so they can be tuned and tested.

    Starting values, not discovered ones. TypeSafe's guidance is explicit that
    thresholds must be evaluated against the operator's own data and the
    consequences of being wrong; these are deliberately conservative -- they
    err towards asking for attention -- and are expected to move once there is
    a term of real drafts to calibrate against.

    Score thresholds are stated on the normalized ``0.0..1.0`` scale
    (:attr:`~app.soft_constraints.results.ScoreJudgment.normalized`), so they
    do not depend on how many levels a rubric has.
    """

    #: ``human_review_warranted`` at or above this asks for a person. 0.6
    #: rather than 0.5 because a Noul at 0.5 is "as likely as not", and
    #: interrupting somebody on a coin flip is how an alert stops being read.
    review_probability: float = 0.60
    #: ``overuse_concern`` at or above this flags a person being leaned on.
    overuse_probability: float = 0.60
    #: Overall quality below this is not a middling draft, it is a bad one, and
    #: a bad draft is a person's problem rather than a note on a screen.
    unacceptable_quality: float = 0.40
    #: Overall quality below this is worth mentioning but not escalating.
    target_quality: float = 0.75
    #: Confidence in the overall-quality Score below this means the model is
    #: telling us it cannot read this draft. That is a routing signal, not a
    #: low rating: a human gets it.
    minimum_quality_confidence: float = 0.40
    #: Fairness below this is worth a coordinator's eye.
    fair_workload: float = 0.60
    #: Preference satisfaction below this is worth a coordinator's eye.
    satisfied_preferences: float = 0.60

    def __post_init__(self) -> None:
        for name in (
            "review_probability",
            "overuse_probability",
            "unacceptable_quality",
            "target_quality",
            "minimum_quality_confidence",
            "fair_workload",
            "satisfied_preferences",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within 0.0..1.0 (got {value})")
        if self.unacceptable_quality > self.target_quality:
            raise ValueError(
                "unacceptable_quality must not exceed target_quality: a draft "
                "cannot be too bad to send without also being below target"
            )


#: The thresholds every caller gets unless it says otherwise.
DEFAULT_POLICY = SoftConstraintPolicy()


@dataclass(frozen=True, slots=True)
class SoftConstraintAssessment:
    """What the application decided, and every rule that says why.

    ``reasons`` is ordered most severe first, and is empty exactly when
    ``status`` is :data:`STATUS_ACCEPTABLE`.
    """

    status: str
    reasons: tuple[str, ...]
    policy: SoftConstraintPolicy

    @property
    def needs_human_review(self) -> bool:
        return self.status == STATUS_HUMAN_REVIEW


def assess(
    judgments: SoftConstraintJudgments,
    *,
    policy: SoftConstraintPolicy = DEFAULT_POLICY,
) -> SoftConstraintAssessment:
    """Turn probabilities into one local status. Pure; no I/O, no model.

    Deterministic in the strong sense: the same judgments and the same policy
    give the same assessment, today and in six months.
    """
    reasons: list[str] = []

    # -- review-level rules ------------------------------------------------
    if judgments.human_review_warranted.probability >= policy.review_probability:
        reasons.append(REASON_MODEL_REQUESTS_REVIEW)
    if judgments.overall_quality.normalized < policy.unacceptable_quality:
        reasons.append(REASON_QUALITY_UNACCEPTABLE)
    if judgments.overall_quality.confidence < policy.minimum_quality_confidence:
        reasons.append(REASON_QUALITY_UNCERTAIN)

    # -- attention-level rules ---------------------------------------------
    if judgments.overuse_concern.probability >= policy.overuse_probability:
        reasons.append(REASON_OVERUSE_CONCERN)
    if judgments.workload_fairness.normalized < policy.fair_workload:
        reasons.append(REASON_WORKLOAD_UNFAIR)
    if judgments.preference_satisfaction.normalized < policy.satisfied_preferences:
        reasons.append(REASON_PREFERENCES_UNMET)
    if judgments.overall_quality.normalized < policy.target_quality:
        reasons.append(REASON_QUALITY_BELOW_TARGET)

    status = STATUS_ACCEPTABLE
    for reason in reasons:
        candidate = _REASON_STATUS[reason]
        if _STATUS_SEVERITY[candidate] > _STATUS_SEVERITY[status]:
            status = candidate

    return SoftConstraintAssessment(
        status=status,
        reasons=tuple(reasons),
        policy=policy,
    )
