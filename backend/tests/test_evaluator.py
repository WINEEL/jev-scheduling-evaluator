"""The TypeSafe boundary: what is sent, what is parsed, what is refused.

**Offline, and provably so.** Every test here passes a fake client, and two
tests actively sabotage ``typesafe_sdk.TypeSafeClient`` so that constructing a
real one would fail loudly rather than quietly reach the network. No test in
this file needs ``TYPESAFE_API_KEY``, and none would pass if it did.

What is proven, and what is not:

- **Proven:** the five questions are asked in one call over the state the
  caller built; every answer is validated on the way in; a malformed,
  incomplete or wrong-primitive response raises rather than producing a
  judgment; a caller's client is used and never closed by us.
- **Not proven, honestly:** that Jev's answers are any good. That is a
  question about the model in this domain, and the only thing that settles it
  is real drafts and a person's opinion of them -- never a unit test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.soft_constraints.evaluator import (
    SoftConstraintEvaluation,
    evaluate_soft_constraints,
    parse_judgments,
)
from app.soft_constraints.policy import (
    STATUS_ACCEPTABLE,
    STATUS_HUMAN_REVIEW,
    SoftConstraintPolicy,
)
from app.soft_constraints.questions import (
    NOUL_QUESTIONS,
    QUESTION_HUMAN_REVIEW,
    QUESTION_OVERALL_QUALITY,
    QUESTION_OVERUSE_CONCERN,
    QUESTION_PREFERENCE_SATISFACTION,
    QUESTION_WORKLOAD_FAIRNESS,
    SCORE_LEVELS,
    SCORE_QUESTIONS,
    build_questions,
)
from app.soft_constraints.results import SoftConstraintEvaluationError
from tests.fixtures import (
    FakeSystemOneClient,
    balanced_state,
    imbalanced_state,
    noul_answer,
    response,
    score_answer,
)


@pytest.fixture
def no_real_client(monkeypatch):
    """Make constructing a real TypeSafeClient an error, for this test.

    A stronger guard than "we passed a fake": it catches a future change that
    ignores the ``client`` argument on some path and falls back to the real
    one.
    """
    import typesafe_sdk

    def explode(*args, **kwargs):
        raise AssertionError("a unit test tried to construct a real TypeSafeClient")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", explode)
    return explode


# --------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------


def test_one_call_asks_all_five_questions_over_the_caller_state(no_real_client):
    state = imbalanced_state()
    client = FakeSystemOneClient(response())

    evaluate_soft_constraints(state, client=client)

    assert len(client.calls) == 1, "the five questions are independent: one round trip"
    call = client.calls[0]
    assert set(call["questions"]) == set(SCORE_QUESTIONS) | set(NOUL_QUESTIONS)
    assert call["state"] == state.to_state()


def test_the_model_override_is_passed_through_only_when_given(no_real_client):
    default_client = FakeSystemOneClient(response())
    evaluate_soft_constraints(balanced_state(), client=default_client)
    assert "model" not in default_client.calls[0]

    pinned_client = FakeSystemOneClient(response())
    evaluate_soft_constraints(balanced_state(), client=pinned_client, model="jev-1")
    assert pinned_client.calls[0]["model"] == "jev-1"


def test_a_caller_supplied_client_is_not_closed(no_real_client):
    """We did not open it; closing it would break the caller's next call."""
    client = FakeSystemOneClient(response())

    evaluate_soft_constraints(balanced_state(), client=client)

    assert client.closed is False


def test_build_questions_returns_a_fresh_mapping_each_time():
    first = build_questions()
    first["extra"] = object()

    assert "extra" not in build_questions()


def test_every_score_question_has_the_declared_number_of_levels():
    """Thresholds normalize by ``SCORE_LEVELS``; a rubric of a different size
    would silently move every threshold."""
    questions = build_questions()

    for question_id in SCORE_QUESTIONS:
        assert len(questions[question_id].criteria) == SCORE_LEVELS

    for question_id in NOUL_QUESTIONS:
        assert questions[question_id].type == "noul"


def test_no_choice_question_is_ever_asked():
    """The application policy decision is code's, not the model's."""
    assert all(
        question.type in {"score", "noul"} for question in build_questions().values()
    )


# --------------------------------------------------------------------------
# Parsing a well-formed response
# --------------------------------------------------------------------------


def test_answers_become_typed_judgments_with_normalized_scores():
    judgments = parse_judgments(
        response(
            workload_fairness=0.0,
            preference_satisfaction=2.0,
            overall_quality=4.0,
            overuse_concern=0.91,
            human_review=0.07,
            confidence=0.8,
            model="jev-test",
        )
    )

    assert judgments.workload_fairness.score == 0.0
    assert judgments.workload_fairness.normalized == 0.0
    assert judgments.preference_satisfaction.normalized == pytest.approx(0.5)
    assert judgments.overall_quality.normalized == 1.0
    assert judgments.overall_quality.confidence == 0.8
    assert judgments.overuse_concern.probability == 0.91
    assert judgments.human_review_warranted.probability == 0.07
    assert judgments.model == "jev-test"
    assert judgments.input_tokens == 321
    assert judgments.output_tokens == 12


def test_judgments_expose_their_answers_by_question_id():
    judgments = parse_judgments(response())

    assert set(judgments.scores) == set(SCORE_QUESTIONS)
    assert set(judgments.nouls) == set(NOUL_QUESTIONS)
    assert judgments.scores[QUESTION_OVERALL_QUALITY] is judgments.overall_quality


def test_the_score_distribution_is_kept_not_just_the_confidence():
    """Confidence summarizes the distribution; diagnosing an odd answer needs
    the shape, so the probabilities are carried through."""
    judgments = parse_judgments(response(workload_fairness=3.0))

    assert judgments.workload_fairness.probabilities[3] == 1.0
    assert set(judgments.workload_fairness.probabilities) == set(range(SCORE_LEVELS))


def test_missing_token_counts_are_none_not_zero():
    """Absent usage is unknown usage; reporting it as zero would be a lie a
    cost report would believe."""
    reply = response()
    reply.usage = SimpleNamespace(input_tokens=None, output_tokens=None)

    judgments = parse_judgments(reply)

    assert judgments.input_tokens is None
    assert judgments.output_tokens is None


def test_an_evaluation_carries_the_state_it_judged(no_real_client):
    state = imbalanced_state()

    evaluation = evaluate_soft_constraints(
        state, client=FakeSystemOneClient(response())
    )

    assert isinstance(evaluation, SoftConstraintEvaluation)
    assert evaluation.state is state
    assert evaluation.status == evaluation.assessment.status


# --------------------------------------------------------------------------
# Refusing a response that cannot be believed
# --------------------------------------------------------------------------


def test_a_missing_question_is_refused():
    answers = dict(response().answers)
    del answers[QUESTION_OVERALL_QUALITY]

    with pytest.raises(SoftConstraintEvaluationError, match=QUESTION_OVERALL_QUALITY):
        parse_judgments(response(answers=answers))


def test_a_noul_where_a_score_was_asked_is_refused():
    answers = dict(response().answers)
    answers[QUESTION_WORKLOAD_FAIRNESS] = noul_answer(0.5)

    with pytest.raises(SoftConstraintEvaluationError, match="numeric `score`"):
        parse_judgments(response(answers=answers))


def test_a_score_where_a_noul_was_asked_is_refused():
    answers = dict(response().answers)
    answers[QUESTION_OVERUSE_CONCERN] = score_answer(3.0)

    with pytest.raises(SoftConstraintEvaluationError, match="expected a Noul answer"):
        parse_judgments(response(answers=answers))


def test_a_score_outside_its_rubric_is_refused():
    answers = dict(response().answers)
    answers[QUESTION_PREFERENCE_SATISFACTION] = score_answer(9.0)

    with pytest.raises(SoftConstraintEvaluationError, match="outside the rubric"):
        parse_judgments(response(answers=answers))


def test_a_probability_outside_zero_to_one_is_refused():
    answers = dict(response().answers)
    answers[QUESTION_HUMAN_REVIEW] = noul_answer(1.4)

    with pytest.raises(SoftConstraintEvaluationError, match="outside 0..1"):
        parse_judgments(response(answers=answers))


def test_a_confidence_outside_zero_to_one_is_refused():
    answers = dict(response().answers)
    answers[QUESTION_OVERALL_QUALITY] = score_answer(3.0, confidence=1.2)

    with pytest.raises(SoftConstraintEvaluationError, match="`confidence`"):
        parse_judgments(response(answers=answers))


def test_a_level_probability_outside_zero_to_one_is_refused():
    broken = score_answer(3.0)
    broken.probabilities = {0: 0.0, 1: 0.0, 2: 0.0, 3: 1.4, 4: 0.0}
    answers = dict(response().answers)
    answers[QUESTION_WORKLOAD_FAIRNESS] = broken

    with pytest.raises(SoftConstraintEvaluationError, match="for level"):
        parse_judgments(response(answers=answers))


def test_a_response_without_an_answers_mapping_is_refused():
    with pytest.raises(SoftConstraintEvaluationError, match="no `answers` mapping"):
        parse_judgments(SimpleNamespace(model="jev-test"))


def test_a_boolean_is_not_accepted_as_a_probability():
    """``True`` is an ``int`` in Python and would sail through a naive range
    check as 1.0; a Noul that answered ``True`` is a broken response."""
    answers = dict(response().answers)
    answers[QUESTION_OVERUSE_CONCERN] = SimpleNamespace(noul=True)

    with pytest.raises(SoftConstraintEvaluationError, match="numeric `noul`"):
        parse_judgments(response(answers=answers))


# --------------------------------------------------------------------------
# End to end, with the boundary faked
# --------------------------------------------------------------------------


def test_a_good_draft_is_acceptable_without_a_model_deciding_that(no_real_client):
    evaluation = evaluate_soft_constraints(
        balanced_state(),
        client=FakeSystemOneClient(
            response(
                workload_fairness=4.0,
                preference_satisfaction=4.0,
                overall_quality=4.0,
                overuse_concern=0.02,
                human_review=0.03,
            )
        ),
    )

    assert evaluation.status == STATUS_ACCEPTABLE
    assert evaluation.assessment.reasons == ()


def test_a_bad_draft_reaches_human_review(no_real_client):
    evaluation = evaluate_soft_constraints(
        imbalanced_state(),
        client=FakeSystemOneClient(
            response(
                workload_fairness=0.0,
                preference_satisfaction=1.0,
                overall_quality=0.5,
                overuse_concern=0.95,
                human_review=0.92,
            )
        ),
    )

    assert evaluation.status == STATUS_HUMAN_REVIEW


def test_a_caller_policy_overrides_the_default_thresholds(no_real_client):
    """Re-deciding is a code change, not another inference call."""
    lenient = SoftConstraintPolicy(
        review_probability=0.99,
        overuse_probability=0.99,
        unacceptable_quality=0.0,
        target_quality=0.1,
        minimum_quality_confidence=0.0,
        fair_workload=0.0,
        satisfied_preferences=0.0,
    )
    reply = response(
        workload_fairness=1.0,
        preference_satisfaction=1.0,
        overall_quality=1.0,
        overuse_concern=0.9,
        human_review=0.9,
    )

    strict = evaluate_soft_constraints(
        balanced_state(), client=FakeSystemOneClient(reply)
    )
    relaxed = evaluate_soft_constraints(
        balanced_state(), client=FakeSystemOneClient(reply), policy=lenient
    )

    assert strict.status == STATUS_HUMAN_REVIEW
    assert relaxed.status == STATUS_ACCEPTABLE
