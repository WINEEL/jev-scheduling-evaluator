"""The synthetic evaluation API. Two endpoints, three fixed scenarios.

``GET  /api/v1/jev-demo/scenarios``
``POST /api/v1/jev-demo/scenarios/{scenario}/evaluate``

A small read-only surface over :mod:`app.soft_constraints`, so the judgment
that package produces can be seen on a screen instead of in a terminal. It
exists to demonstrate one distinction and is shaped entirely around it:

    hard constraints  -> a deterministic engine (already satisfied)
    soft constraints  -> Jev's probabilistic judgment
    final action      -> deterministic Python policy

**The only input is a scenario name, and it names one of three fictions.**
There is no request body on either endpoint and no field anywhere that accepts
a person, a count, a note or a roster. The state is built server-side by
:mod:`app.soft_constraints.scenarios`, so what this service sends to TypeSafe
does not depend on what any caller typed. That is a stronger guarantee than
validating a submitted roster would be: there is no submitted roster.

**No database, no session, no actor.** Nothing on this path opens a
connection or consults an identity, and there is nothing here that would
benefit from either: every value in a response is a constant from this
repository or a probability TypeSafe just returned.

**Evaluation is a POST, and that is not pedantry.** It costs money, takes a
second and calls a third party. GET invites a browser, a proxy or a link
prefetcher to do it unasked; the scenario menu, which costs nothing, is the
GET.

Never in a response: the API key, the question instructions and rubric text,
or the request body sent to TypeSafe. The prompt text is internal -- readable
in :mod:`app.soft_constraints.questions` by anybody with the source, which is a
different thing from an endpoint handing it out.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status

from app.api.schemas import (
    DemoScenarioName,
    JevDemoEvaluationResponse,
    JevDemoJudgmentsResponse,
    JevDemoNoulAnswer,
    JevDemoPolicyResponse,
    JevDemoScenarioDetail,
    JevDemoScenarioListResponse,
    JevDemoScoreAnswer,
    JevDemoStateResponse,
    JevDemoWorkloadPerson,
    JevDemoWorkloadSummary,
)
from app.soft_constraints.evaluator import (
    SoftConstraintEvaluation,
    evaluate_soft_constraints,
)
from app.soft_constraints.policy import SoftConstraintPolicy
from app.soft_constraints.results import (
    NoulJudgment,
    ScoreJudgment,
    SoftConstraintEvaluationError,
)
from app.soft_constraints.scenarios import (
    SCENARIO_SUMMARIES,
    build_scenario,
    describe_scenarios,
)
from app.soft_constraints.state import SoftConstraintState

__all__ = ["router"]

router = APIRouter(tags=["jev demo"], prefix="/jev-demo")

#: Said when TypeSafe answered with something this application will not
#: interpret. A sentence for a person, naming no internals -- the same
#: contract every other error message in this API keeps.
_UNUSABLE_JUDGMENT = (
    "The evaluation service answered with something this application could "
    "not read. Nothing was decided."
)

#: Said when TypeSafe could not be reached, refused the key, or timed out.
#: Deliberately does not distinguish those: the caller's next step is the same
#: for all three, and which one it was belongs in the server's log.
_EVALUATION_UNAVAILABLE = (
    "The evaluation service could not be reached. This demo needs a live "
    "TypeSafe API key on the server."
)


@router.get(
    "/scenarios",
    response_model=JevDemoScenarioListResponse,
    summary="The three synthetic scenarios this demo can evaluate",
)
def list_scenarios() -> JevDemoScenarioListResponse:
    """Every scenario, with the arithmetic already computed for each.

    Free: no model is asked anything here. A screen can show all three drafts
    and their exact workload figures before anybody decides to spend a call,
    which is also what makes the "before" of the demo visible.
    """
    return JevDemoScenarioListResponse(
        scenarios=[
            JevDemoScenarioDetail(**_state_fields(name, summary, state))
            for name, summary, state in describe_scenarios()
        ]
    )


@router.post(
    "/scenarios/{scenario}/evaluate",
    response_model=JevDemoEvaluationResponse,
    summary="Run one synthetic scenario through Jev and the local policy",
    responses={
        502: {"description": "TypeSafe was unreachable or answered unusably."},
    },
)
def evaluate_scenario(
    scenario: DemoScenarioName = Path(description="Which synthetic draft to judge."),
) -> JevDemoEvaluationResponse:
    """One live TypeSafe call, then one deterministic decision.

    The scenario is built here from its name; nothing from the request reaches
    the model. ``evaluate_soft_constraints`` opens and closes its own client
    for this call, reading ``TYPESAFE_API_KEY`` from the process environment --
    the key never passes through this module, this application's settings, or
    any response.

    Both failure modes become 502, because both mean "the upstream service did
    not give us an answer we can use" and neither is the caller's mistake:
    there was nothing in the request to get wrong.
    """
    state = build_scenario(scenario.value)
    try:
        evaluation = evaluate_soft_constraints(state)
    except SoftConstraintEvaluationError:
        # The parser refused the answer. That is the evaluator's guarantee
        # working: no judgment is better than one nobody can interpret.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_UNUSABLE_JUDGMENT
        ) from None
    except Exception:
        # The SDK's transport, authentication and rate-limit errors. Caught as
        # a group and re-raised as one status because the message must not
        # carry the SDK's own text: an authentication failure's string is
        # exactly the kind of thing that ends up quoting a key.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_EVALUATION_UNAVAILABLE
        ) from None

    return _evaluation_response(scenario.value, evaluation)


def _evaluation_response(
    name: str, evaluation: SoftConstraintEvaluation
) -> JevDemoEvaluationResponse:
    """Map the evaluator's result onto the wire, field by field.

    Built explicitly rather than serialized from the dataclasses, for the same
    reason :func:`app.api.v1.read_current_actor` builds its response by hand:
    nothing a result object happens to carry can escape by accident.
    """
    judgments = evaluation.judgments
    assessment = evaluation.assessment
    return JevDemoEvaluationResponse(
        state=JevDemoStateResponse(
            **_state_fields(name, SCENARIO_SUMMARIES[name], evaluation.state)
        ),
        judgments=JevDemoJudgmentsResponse(
            workload_fairness=_score(judgments.workload_fairness),
            preference_satisfaction=_score(judgments.preference_satisfaction),
            overall_quality=_score(judgments.overall_quality),
            overuse_concern=_noul(judgments.overuse_concern),
            human_review_warranted=_noul(judgments.human_review_warranted),
            model_name=judgments.model,
        ),
        policy=JevDemoPolicyResponse(
            status=assessment.status,
            reasons=list(assessment.reasons),
            thresholds=_thresholds(assessment.policy),
        ),
    )


def _state_fields(name: str, summary_line: str, state: SoftConstraintState) -> dict:
    """The response fields for one synthetic draft."""
    workload = state.summary
    return {
        "name": name,
        "summary_line": summary_line,
        "scenario": state.scenario,
        "event_count": state.event_count,
        "notes": list(state.notes),
        "workload_summary": JevDemoWorkloadSummary(
            people_count=workload.people_count,
            total_assignments=workload.total_assignments,
            mean_assignments_per_person=round(workload.mean_assignments, 2),
            minimum_assignments=workload.minimum_assignments,
            maximum_assignments=workload.maximum_assignments,
            assignment_spread=workload.assignment_spread,
            unused_available_people=workload.unused_available_people,
            people_over_preferred_limit=workload.people_over_preferred_limit,
            preference_grant_rate=(
                None
                if workload.preference_grant_rate is None
                else round(workload.preference_grant_rate, 2)
            ),
        ),
        "people": [
            JevDemoWorkloadPerson(
                reference=person.reference,
                available_events=person.available_events,
                assignments=person.assignments,
                preferred_max_assignments=person.preferred_max_assignments,
                over_preferred_limit=person.over_preferred_limit,
                preferences_granted=person.preferences_granted,
                preferences_declined=person.preferences_declined,
                note=person.note,
            )
            for person in state.people
        ],
    }


def _score(judgment: ScoreJudgment) -> JevDemoScoreAnswer:
    return JevDemoScoreAnswer(
        question=judgment.question,
        score=judgment.score,
        levels=judgment.levels,
        normalized=judgment.normalized,
        confidence=judgment.confidence,
        probabilities=dict(judgment.probabilities),
    )


def _noul(judgment: NoulJudgment) -> JevDemoNoulAnswer:
    return JevDemoNoulAnswer(
        question=judgment.question, probability=judgment.probability
    )


def _thresholds(policy: SoftConstraintPolicy) -> dict[str, float]:
    """The numbers each policy rule was measured against.

    Read off the assessment's own policy object rather than the module
    default, so a response always reports the thresholds that actually
    produced its status even if a caller one day passes a different set.
    """
    return {
        "review_probability": policy.review_probability,
        "overuse_probability": policy.overuse_probability,
        "unacceptable_quality": policy.unacceptable_quality,
        "target_quality": policy.target_quality,
        "minimum_quality_confidence": policy.minimum_quality_confidence,
        "fair_workload": policy.fair_workload,
        "satisfied_preferences": policy.satisfied_preferences,
    }
