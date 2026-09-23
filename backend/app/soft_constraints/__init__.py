"""Soft-constraint evaluation: the fuzzy judgment a solver deliberately omits.

A deterministic scheduling engine decides who serves. It is exact, it is the
authority, and nothing in this package may change what it produces. What it
cannot do is answer the question a coordinator asks after reading a draft --
*is this a considerate way to have spread the work?* -- because that question
has no threshold in it. Fairness is not a number a solver can be given;
"leaned on too heavily" depends on how many events there were, what people
asked for, and what a coordinator knows about the season.

So this package asks a System One model (TypeSafe's Jev) for exactly that
judgment and nothing else, in five narrow questions, and gets probabilities
back. It is an advisory layer over a finished draft, never a scheduling rule:

- **It cannot place or move anybody.** There is no assignment here, no
  database, and no write path of any kind.
- **It never sees a hard constraint.** Qualification, availability, per-person
  limits, exclusions and gaps belong to the deterministic engine, absolutely.
  The state this package builds asserts they are already satisfied.
- **It never sees a person.** No name, email address, team or date leaves the
  building; :mod:`app.soft_constraints.state` takes opaque references and
  counts, and that is all there is to send.

Five modules, in the order data moves through them:

1. :mod:`~app.soft_constraints.state` -- the normalized, anonymized input, plus
   the exact arithmetic (spread, means, over-preference counts) computed in
   Python so the model judges numbers rather than deriving them.
2. :mod:`~app.soft_constraints.scenarios` -- three invented drafts, the only
   input the API accepts. A caller names one and the state is built here, so
   no request can put a real person into a call to a third party.
3. :mod:`~app.soft_constraints.questions` -- the five typed Jev questions.
   Three Scores (workload fairness, preference satisfaction, overall quality)
   and two Nouls (overuse concern, human review warranted). **No Choice**, and
   that omission is the design.
4. :mod:`~app.soft_constraints.evaluator` -- the one seam that talks to
   TypeSafe, and the parsing that turns a response into checked, typed values.
5. :mod:`~app.soft_constraints.policy` -- ordinary deterministic Python that
   turns those probabilities into ``acceptable`` / ``attention`` /
   ``human_review``. It imports no SDK and asks no model.

    from app.soft_constraints import build_scenario, evaluate_soft_constraints

    evaluation = evaluate_soft_constraints(build_scenario("ambiguous"))
    evaluation.status                               # "human_review"
    evaluation.assessment.reasons                   # ("PREFERENCES_UNMET", ...)

The test suite fakes this package's one seam and opens no socket, so nothing
automated ever calls TypeSafe.
"""

from app.soft_constraints.evaluator import (
    SoftConstraintEvaluation,
    SystemOneCaller,
    evaluate_soft_constraints,
    parse_judgments,
)
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
    SoftConstraintAssessment,
    SoftConstraintPolicy,
    assess,
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
from app.soft_constraints.scenarios import (
    SCENARIO_AMBIGUOUS,
    SCENARIO_BALANCED,
    SCENARIO_IMBALANCED,
    SCENARIO_NAMES,
    SCENARIO_SUMMARIES,
    SCENARIOS,
    build_scenario,
    describe_scenarios,
)
from app.soft_constraints.results import (
    NoulJudgment,
    ScoreJudgment,
    SoftConstraintEvaluationError,
    SoftConstraintJudgments,
)
from app.soft_constraints.state import (
    EVALUATION_SCOPE,
    PersonWorkload,
    SoftConstraintState,
    WorkloadSummary,
)

__all__ = [
    # scenarios (the synthetic demo corpus)
    "SCENARIOS",
    "SCENARIO_AMBIGUOUS",
    "SCENARIO_BALANCED",
    "SCENARIO_IMBALANCED",
    "SCENARIO_NAMES",
    "SCENARIO_SUMMARIES",
    "build_scenario",
    "describe_scenarios",
    # state
    "EVALUATION_SCOPE",
    "PersonWorkload",
    "SoftConstraintState",
    "WorkloadSummary",
    # questions
    "NOUL_QUESTIONS",
    "QUESTION_HUMAN_REVIEW",
    "QUESTION_OVERALL_QUALITY",
    "QUESTION_OVERUSE_CONCERN",
    "QUESTION_PREFERENCE_SATISFACTION",
    "QUESTION_WORKLOAD_FAIRNESS",
    "SCORE_LEVELS",
    "SCORE_QUESTIONS",
    "build_questions",
    # results
    "NoulJudgment",
    "ScoreJudgment",
    "SoftConstraintEvaluationError",
    "SoftConstraintJudgments",
    # evaluator
    "SoftConstraintEvaluation",
    "SystemOneCaller",
    "evaluate_soft_constraints",
    "parse_judgments",
    # policy
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
