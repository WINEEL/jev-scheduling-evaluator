"""The synthetic Jev demo endpoints: the guard, the contract, the failures.

**Offline, and provably so.** Every test that reaches the evaluation endpoint
replaces ``app.api.routes.evaluate_soft_constraints`` with a fake, and
an autouse fixture makes constructing a real ``TypeSafeClient`` an error. No
test here needs ``TYPESAFE_API_KEY``, no test opens a socket, and none would
pass if it tried.

Three things are pinned:

1. **The input surface.** A scenario name is the only thing either endpoint
   accepts, and an unknown one is refused before any handler runs. There is no
   request body to put a real person in.
2. **The contract.** The three parts of the response -- deterministic
   arithmetic, model probabilities, local policy status -- arrive separately,
   which is the distinction the whole project exists to show.
3. **What is never in a response**: an API key, the question instructions, the
   state sent upstream, or an SDK exception's own text.

The server's own refusal to run in production is :mod:`tests.test_main`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.soft_constraints.evaluator import SoftConstraintEvaluation
from app.soft_constraints.policy import (
    STATUS_ACCEPTABLE,
    STATUS_HUMAN_REVIEW,
    assess,
)
from app.soft_constraints.questions import NOUL_QUESTIONS, SCORE_QUESTIONS
from app.soft_constraints.results import SoftConstraintEvaluationError
from app.soft_constraints.scenarios import SCENARIO_NAMES, build_scenario
from tests.fixtures import response
from app.soft_constraints.evaluator import parse_judgments

SCENARIOS_URL = "/api/v1/jev-demo/scenarios"


def _evaluate_url(name: str) -> str:
    return f"/api/v1/jev-demo/scenarios/{name}/evaluate"


@pytest.fixture(autouse=True)
def no_real_client(monkeypatch):
    """Constructing a real TypeSafeClient anywhere in this file is a failure."""
    import typesafe_sdk

    def explode(*args, **kwargs):
        raise AssertionError("an API test tried to construct a real TypeSafeClient")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", explode)


def _demo_app() -> FastAPI:
    """A bare app carrying only the API router.

    These routes take no session and no actor, so nothing else is needed to
    exercise them -- which is itself worth stating: if this fixture ever has to
    grow a database or an identity to make the endpoints answer, the endpoints
    have acquired a dependency they are documented not to have.
    """
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_demo_app())


def _evaluation(name: str, **answers) -> SoftConstraintEvaluation:
    """A complete evaluation of a real scenario with a faked model answer."""
    state = build_scenario(name)
    judgments = parse_judgments(response(**answers))
    return SoftConstraintEvaluation(
        state=state, judgments=judgments, assessment=assess(judgments)
    )


# --------------------------------------------------------------------------
# 1. The input surface
# --------------------------------------------------------------------------


def test_the_scenario_list_needs_no_model_and_no_arguments(client, monkeypatch):
    """Listing is free: it asks nobody anything."""

    def explode(*args, **kwargs):
        raise AssertionError("listing scenarios must not call the evaluator")

    monkeypatch.setattr(routes, "evaluate_soft_constraints", explode)

    body = client.get(SCENARIOS_URL).json()

    assert [entry["name"] for entry in body["scenarios"]] == list(SCENARIO_NAMES)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_every_scenario_can_be_evaluated(client, monkeypatch, name):
    monkeypatch.setattr(
        routes, "evaluate_soft_constraints", lambda state, **kw: _evaluation(name)
    )

    result = client.post(_evaluate_url(name))

    assert result.status_code == 200
    assert result.json()["state"]["name"] == name


def test_an_unknown_scenario_is_refused_before_any_handler_runs(client, monkeypatch):
    """The path parameter is a closed enum, so there is no fourth scenario."""

    def explode(*args, **kwargs):
        raise AssertionError("an unknown scenario must not reach the evaluator")

    monkeypatch.setattr(routes, "evaluate_soft_constraints", explode)

    assert client.post(_evaluate_url("whatever-i-like")).status_code == 422


def test_neither_endpoint_accepts_a_request_body(client, monkeypatch):
    """The strongest statement this demo makes about private data.

    A body naming people is not validated and rejected -- it is ignored,
    because there is no field anywhere that reads one. The state sent upstream
    is built from the scenario name alone.
    """
    seen: list = []

    def record(state, **kwargs):
        seen.append(state)
        return _evaluation("balanced")

    monkeypatch.setattr(routes, "evaluate_soft_constraints", record)

    result = client.post(
        _evaluate_url("balanced"),
        json={"people": [{"reference": "A Real Name", "assignments": 9}]},
    )

    assert result.status_code == 200
    assert seen[0] == build_scenario("balanced")
    references = {person.reference for person in seen[0].people}
    assert references == {f"Volunteer {letter}" for letter in "ABCD"}


def test_the_openapi_schema_declares_exactly_three_scenarios():
    schema = _demo_app().openapi()
    enum = schema["components"]["schemas"]["DemoScenarioName"]["enum"]

    assert sorted(enum) == sorted(SCENARIO_NAMES)


# --------------------------------------------------------------------------
# 2. The contract: arithmetic, judgment and decision arrive separately
# --------------------------------------------------------------------------


def test_the_response_separates_state_judgments_and_policy(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "evaluate_soft_constraints",
        lambda state, **kw: _evaluation(
            "imbalanced",
            workload_fairness=0.2,
            preference_satisfaction=1.0,
            overall_quality=0.4,
            overuse_concern=0.95,
            human_review=0.93,
        ),
    )

    body = client.post(_evaluate_url("imbalanced")).json()

    # The arithmetic the deterministic layer already knew.
    assert body["state"]["workload_summary"]["assignment_spread"] == 4
    assert body["state"]["workload_summary"]["unused_available_people"] == 3

    # What the model said -- probabilities, and no decision.
    judgments = body["judgments"]
    assert set(judgments) == {
        "workload_fairness",
        "preference_satisfaction",
        "overall_quality",
        "overuse_concern",
        "human_review_warranted",
        "model_name",
    }
    assert judgments["overuse_concern"]["probability"] == 0.95
    assert "status" not in judgments

    # What the code decided.
    assert body["policy"]["status"] == STATUS_HUMAN_REVIEW
    assert "MODEL_REQUESTS_REVIEW" in body["policy"]["reasons"]


def test_a_good_draft_is_acceptable_with_no_reasons(client, monkeypatch):
    monkeypatch.setattr(
        routes,
        "evaluate_soft_constraints",
        lambda state, **kw: _evaluation(
            "balanced",
            workload_fairness=4.0,
            preference_satisfaction=4.0,
            overall_quality=4.0,
            overuse_concern=0.02,
            human_review=0.03,
        ),
    )

    body = client.post(_evaluate_url("balanced")).json()

    assert body["policy"]["status"] == STATUS_ACCEPTABLE
    assert body["policy"]["reasons"] == []


def test_scores_carry_both_scales_and_their_distribution(client, monkeypatch):
    """The screen shows a rubric position; the thresholds are on 0..1.

    Sending both means the browser never re-derives one from the other, which
    would put a second copy of the normalization rule where it could disagree.
    """
    monkeypatch.setattr(
        routes,
        "evaluate_soft_constraints",
        lambda state, **kw: _evaluation("balanced", workload_fairness=2.0),
    )

    fairness = client.post(_evaluate_url("balanced")).json()["judgments"][
        "workload_fairness"
    ]

    assert fairness["score"] == 2.0
    assert fairness["levels"] == 5
    assert fairness["normalized"] == pytest.approx(0.5)
    assert set(fairness["probabilities"]) == {"0", "1", "2", "3", "4"}


def test_the_thresholds_behind_the_status_are_reported(client, monkeypatch):
    """So a reader can check the arithmetic rather than take the status on
    trust -- which is the demo's central claim made verifiable."""
    monkeypatch.setattr(
        routes, "evaluate_soft_constraints", lambda state, **kw: _evaluation("balanced")
    )

    thresholds = client.post(_evaluate_url("balanced")).json()["policy"]["thresholds"]

    assert thresholds["review_probability"] == 0.60
    assert thresholds["target_quality"] == 0.75
    assert set(thresholds) == {
        "review_probability",
        "overuse_probability",
        "unacceptable_quality",
        "target_quality",
        "minimum_quality_confidence",
        "fair_workload",
        "satisfied_preferences",
    }


def test_question_ids_match_the_evaluator(client, monkeypatch):
    """A renamed question must not leave the screen labelling a stale answer."""
    monkeypatch.setattr(
        routes, "evaluate_soft_constraints", lambda state, **kw: _evaluation("balanced")
    )

    judgments = client.post(_evaluate_url("balanced")).json()["judgments"]
    asked = {
        judgments[key]["question"]
        for key in judgments
        if isinstance(judgments[key], dict)
    }

    assert asked == set(SCORE_QUESTIONS) | set(NOUL_QUESTIONS)


# --------------------------------------------------------------------------
# 3. Failures, and what a response never carries
# --------------------------------------------------------------------------


def test_an_unusable_judgment_becomes_a_502_naming_no_internals(client, monkeypatch):
    def refuse(state, **kwargs):
        raise SoftConstraintEvaluationError(
            "workload_fairness: score 9.0 is outside the rubric's 0..4"
        )

    monkeypatch.setattr(routes, "evaluate_soft_constraints", refuse)

    result = client.post(_evaluate_url("balanced"))

    assert result.status_code == 502
    detail = result.json()["detail"]
    assert "workload_fairness" not in detail
    assert "rubric" not in detail


def test_an_sdk_failure_becomes_a_502_that_does_not_quote_the_sdk(client, monkeypatch):
    """An authentication error's own message is exactly the kind of string
    that ends up carrying a key. None of it reaches the response."""

    def fail(state, **kwargs):
        raise RuntimeError("401 Unauthorized: bad api key apikey_super_secret_value")

    monkeypatch.setattr(routes, "evaluate_soft_constraints", fail)

    result = client.post(_evaluate_url("balanced"))

    assert result.status_code == 502
    body = result.text
    assert "apikey" not in body
    assert "Unauthorized" not in body


def test_no_response_carries_a_key_a_prompt_or_the_upstream_request(
    client, monkeypatch
):
    """The single assertion that most of this module exists to make.

    Checked against the raw response text of both endpoints, because a leak
    would arrive as a field nobody thought to look at rather than as one of
    the fields tested above.
    """
    monkeypatch.setattr(
        routes, "evaluate_soft_constraints", lambda state, **kw: _evaluation("ambiguous")
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_should_never_be_echoed")

    bodies = [
        client.get(SCENARIOS_URL).text,
        client.post(_evaluate_url("ambiguous")).text,
    ]

    for body in bodies:
        assert "apikey_should_never_be_echoed" not in body
        assert "TYPESAFE_API_KEY" not in body
        # The question rubric and instructions are internal.
        assert "instructions" not in body
        assert "criteria" not in body
        assert "evaluation_scope" not in body


def test_every_person_in_every_response_is_synthetic(client, monkeypatch):
    """The scenarios are fictions; this asserts the endpoint keeps them so."""
    monkeypatch.setattr(
        routes, "evaluate_soft_constraints", lambda state, **kw: _evaluation("ambiguous")
    )
    permitted = {f"Volunteer {letter}" for letter in "ABCDEF"}

    listed = client.get(SCENARIOS_URL).json()["scenarios"]
    evaluated = client.post(_evaluate_url("ambiguous")).json()["state"]

    for scenario in [*listed, evaluated]:
        for person in scenario["people"]:
            assert person["reference"] in permitted


def test_evaluation_is_a_post_and_a_get_is_refused(client, monkeypatch):
    """A GET would invite a browser, a proxy or a prefetcher to spend a call."""

    def explode(*args, **kwargs):
        raise AssertionError("a GET must not reach the evaluator")

    monkeypatch.setattr(routes, "evaluate_soft_constraints", explode)

    assert client.get(_evaluate_url("balanced")).status_code == 405
