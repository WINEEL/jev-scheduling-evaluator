"""The deterministic application policy. No model, no network, no state.

This is where "Jev returns probabilities, code decides the status" is actually
proven. Every test builds judgments directly -- no client, fake or otherwise --
because that is the whole claim: given the same numbers, :func:`assess` returns
the same answer, and it does so without asking anybody.

Four properties are tested, and they are the four the design rests on:

1. **Every rule is evaluated.** A draft with four problems reports four
   reasons, not the first one. A coordinator told only the first fixes it and
   comes straight back.
2. **Status is the most severe reason present**, and severity does not
   accumulate: three ``attention`` reasons never become ``human_review``,
   because nothing says three mild problems are one severe one.
3. **Each threshold is inclusive or exclusive on purpose**, and the boundary
   cases say which.
4. **Low confidence routes to a human** rather than being read as a middling
   rating -- confidence is a second axis, not a score.
"""

from __future__ import annotations

import pytest

from app.soft_constraints.policy import (
    DEFAULT_POLICY,
    REASON_MODEL_REQUESTS_REVIEW,
    REASON_OVERUSE_CONCERN,
    REASON_PREFERENCES_UNMET,
    REASON_QUALITY_BELOW_TARGET,
    REASON_QUALITY_UNACCEPTABLE,
    REASON_QUALITY_UNCERTAIN,
    REASON_WORKLOAD_UNFAIR,
    STATUS_ACCEPTABLE,
    STATUS_ATTENTION,
    STATUS_HUMAN_REVIEW,
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
)
from app.soft_constraints.results import (
    NoulJudgment,
    ScoreJudgment,
    SoftConstraintJudgments,
)

TOP_LEVEL = SCORE_LEVELS - 1


def _score(question: str, normalized: float, confidence: float = 0.9) -> ScoreJudgment:
    """A Score judgment stated on the normalized scale the policy reads."""
    return ScoreJudgment(
        question=question,
        score=normalized * TOP_LEVEL,
        levels=SCORE_LEVELS,
        confidence=confidence,
        probabilities={level: 0.0 for level in range(SCORE_LEVELS)},
    )


def judgments(
    *,
    fairness: float = 1.0,
    preferences: float = 1.0,
    quality: float = 1.0,
    quality_confidence: float = 0.9,
    overuse: float = 0.0,
    review: float = 0.0,
) -> SoftConstraintJudgments:
    """Judgments stated in normalized terms. Defaults describe a perfect draft."""
    return SoftConstraintJudgments(
        workload_fairness=_score(QUESTION_WORKLOAD_FAIRNESS, fairness),
        preference_satisfaction=_score(QUESTION_PREFERENCE_SATISFACTION, preferences),
        overall_quality=_score(
            QUESTION_OVERALL_QUALITY, quality, confidence=quality_confidence
        ),
        overuse_concern=NoulJudgment(question=QUESTION_OVERUSE_CONCERN, probability=overuse),
        human_review_warranted=NoulJudgment(
            question=QUESTION_HUMAN_REVIEW, probability=review
        ),
        model="jev-test",
    )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_a_clean_draft_is_acceptable_with_no_reasons():
    assessment = assess(judgments())

    assert assessment.status == STATUS_ACCEPTABLE
    assert assessment.reasons == ()
    assert assessment.needs_human_review is False


def test_the_policy_used_is_reported_back():
    """A stored assessment that cannot say which thresholds produced it cannot
    be compared with one produced after they moved."""
    assessment = assess(judgments())

    assert assessment.policy is DEFAULT_POLICY


def test_assess_is_deterministic():
    first = assess(judgments(fairness=0.3, quality=0.5, overuse=0.8))
    second = assess(judgments(fairness=0.3, quality=0.5, overuse=0.8))

    assert first == second


# --------------------------------------------------------------------------
# Individual rules
# --------------------------------------------------------------------------


def test_the_model_asking_for_review_reaches_human_review():
    assessment = assess(judgments(review=0.7))

    assert assessment.status == STATUS_HUMAN_REVIEW
    assert REASON_MODEL_REQUESTS_REVIEW in assessment.reasons


def test_unacceptable_quality_reaches_human_review():
    assessment = assess(judgments(quality=0.2))

    assert assessment.status == STATUS_HUMAN_REVIEW
    assert REASON_QUALITY_UNACCEPTABLE in assessment.reasons


def test_low_confidence_reaches_human_review_even_on_a_good_rating():
    """The distinguishing test for the confidence axis.

    Quality is at the top of the rubric, so every quality rule passes. The
    model saying it cannot read this draft is still a reason for a person to.
    """
    assessment = assess(judgments(quality=1.0, quality_confidence=0.2))

    assert assessment.status == STATUS_HUMAN_REVIEW
    assert assessment.reasons == (REASON_QUALITY_UNCERTAIN,)


def test_overuse_concern_is_attention():
    assessment = assess(judgments(overuse=0.8))

    assert assessment.status == STATUS_ATTENTION
    assert REASON_OVERUSE_CONCERN in assessment.reasons


def test_unfair_workload_is_attention():
    assessment = assess(judgments(fairness=0.3))

    assert assessment.status == STATUS_ATTENTION
    assert REASON_WORKLOAD_UNFAIR in assessment.reasons


def test_unmet_preferences_are_attention():
    assessment = assess(judgments(preferences=0.3))

    assert assessment.status == STATUS_ATTENTION
    assert REASON_PREFERENCES_UNMET in assessment.reasons


def test_quality_below_target_but_above_unacceptable_is_attention_only():
    assessment = assess(judgments(quality=0.6))

    assert assessment.status == STATUS_ATTENTION
    assert assessment.reasons == (REASON_QUALITY_BELOW_TARGET,)


# --------------------------------------------------------------------------
# Boundaries -- each threshold is inclusive or exclusive deliberately
# --------------------------------------------------------------------------


def test_a_noul_exactly_at_its_threshold_fires():
    """``>=``: the threshold is the point at which the rule applies."""
    assert REASON_MODEL_REQUESTS_REVIEW in assess(
        judgments(review=DEFAULT_POLICY.review_probability)
    ).reasons
    assert REASON_OVERUSE_CONCERN in assess(
        judgments(overuse=DEFAULT_POLICY.overuse_probability)
    ).reasons


def test_a_score_exactly_at_its_threshold_does_not_fire():
    """``<``: a draft that *meets* the target has met it."""
    assert assess(judgments(quality=DEFAULT_POLICY.target_quality)).reasons == ()
    assert assess(judgments(fairness=DEFAULT_POLICY.fair_workload)).reasons == ()
    assert (
        assess(judgments(preferences=DEFAULT_POLICY.satisfied_preferences)).reasons == ()
    )


def test_confidence_exactly_at_the_floor_does_not_fire():
    assessment = assess(
        judgments(quality_confidence=DEFAULT_POLICY.minimum_quality_confidence)
    )

    assert assessment.reasons == ()


# --------------------------------------------------------------------------
# Combination: all rules evaluated, most severe wins
# --------------------------------------------------------------------------


def test_every_failing_rule_is_reported_not_just_the_first():
    assessment = assess(
        judgments(
            fairness=0.1,
            preferences=0.1,
            quality=0.1,
            quality_confidence=0.1,
            overuse=0.99,
            review=0.99,
        )
    )

    assert set(assessment.reasons) == {
        REASON_MODEL_REQUESTS_REVIEW,
        REASON_QUALITY_UNACCEPTABLE,
        REASON_QUALITY_UNCERTAIN,
        REASON_OVERUSE_CONCERN,
        REASON_WORKLOAD_UNFAIR,
        REASON_PREFERENCES_UNMET,
        REASON_QUALITY_BELOW_TARGET,
    }
    assert assessment.status == STATUS_HUMAN_REVIEW


def test_reasons_are_ordered_most_severe_first():
    assessment = assess(judgments(quality=0.1, fairness=0.1))

    assert assessment.reasons[0] == REASON_QUALITY_UNACCEPTABLE
    assert REASON_WORKLOAD_UNFAIR in assessment.reasons[1:]


def test_several_attention_reasons_do_not_add_up_to_human_review():
    """No accumulation rule, deliberately: nothing says three mild problems
    are one severe one, so the policy does not pretend otherwise."""
    assessment = assess(judgments(fairness=0.3, preferences=0.3, overuse=0.9, quality=0.6))

    assert len(assessment.reasons) == 4
    assert assessment.status == STATUS_ATTENTION


# --------------------------------------------------------------------------
# The policy object itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["review_probability", "target_quality", "minimum_quality_confidence"]
)
def test_a_threshold_outside_zero_to_one_is_rejected(field):
    with pytest.raises(ValueError, match="within 0.0..1.0"):
        SoftConstraintPolicy(**{field: 1.5})


def test_an_unacceptable_threshold_above_the_target_is_rejected():
    """Otherwise a draft could be too bad to send without being below target,
    and the two escalation levels would cross over."""
    with pytest.raises(ValueError, match="must not exceed target_quality"):
        SoftConstraintPolicy(unacceptable_quality=0.9, target_quality=0.5)


def test_the_policy_module_asks_no_model():
    """The load-bearing separation, asserted rather than trusted.

    Read from the module's own syntax tree, not from its text: a docstring
    that *mentions* the SDK is fine and a comment about it is fine, and only
    an actual import is not. This is what stops the policy layer quietly
    acquiring a model call after somebody edits it.
    """
    import ast
    import inspect

    import app.soft_constraints.policy as policy_module

    tree = ast.parse(inspect.getsource(policy_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(name.split(".")[0] == "typesafe_sdk" for name in imported), imported
