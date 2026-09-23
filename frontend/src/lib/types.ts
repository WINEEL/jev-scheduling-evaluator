/**
 * Response shapes of the evaluation API.
 *
 * These mirror `backend/app/api/schemas.py`, hand-written rather than
 * generated so the compiler catches a drift the day it happens.
 *
 * The three-part shape is the point rather than an artefact: `state` is what
 * deterministic code already knew, `judgments` is what the model added, and
 * `policy` is what deterministic code decided. A flatter response could not
 * show the distinction the page exists to make.
 *
 * A nullable field is `T | null`, never optional-and-absent, because the
 * backend always sends the key.
 */

/** One invented volunteer. `reference` is a label — never a name. */
export interface JevDemoWorkloadPerson {
  reference: string;
  available_events: number;
  assignments: number;
  /** `null` when this person stated no soft maximum, which is not zero. */
  preferred_max_assignments: number | null;
  over_preferred_limit: boolean;
  preferences_granted: number;
  preferences_declined: number;
  note: string | null;
}

/** The exact arithmetic, computed in Python before any model was asked. */
export interface JevDemoWorkloadSummary {
  people_count: number;
  total_assignments: number;
  mean_assignments_per_person: number;
  minimum_assignments: number;
  maximum_assignments: number;
  assignment_spread: number;
  unused_available_people: number;
  people_over_preferred_limit: number;
  /** `null` when nobody expressed a preference — never `0`. */
  preference_grant_rate: number | null;
}

export interface JevDemoScenario {
  /** `imbalanced` | `balanced` | `ambiguous`. A plain string for the same
   *  reason `status` is: an unrecognised value should render, not crash. */
  name: string;
  summary_line: string;
  scenario: string;
  event_count: number;
  notes: string[];
  workload_summary: JevDemoWorkloadSummary;
  people: JevDemoWorkloadPerson[];
}

export interface JevDemoScenarioList {
  scenarios: JevDemoScenario[];
}

/**
 * One Jev `Score`.
 *
 * `score` is on the rubric's own `0..levels - 1` scale; `normalized` is the
 * same value on `0..1`, which is the scale the policy thresholds are stated
 * on. Both are sent by the backend so this app never re-derives one from the
 * other.
 */
export interface JevDemoScoreAnswer {
  question: string;
  score: number;
  levels: number;
  normalized: number;
  confidence: number;
  /** Keyed by rubric level. JSON object keys are strings even though the
   *  levels are integers. */
  probabilities: Record<string, number>;
}

/** One Jev `Noul`: the probability of yes. No confidence — TypeSafe sends none. */
export interface JevDemoNoulAnswer {
  question: string;
  probability: number;
}

/** What the model said. Probabilities only; no decision anywhere in here. */
export interface JevDemoJudgments {
  workload_fairness: JevDemoScoreAnswer;
  preference_satisfaction: JevDemoScoreAnswer;
  overall_quality: JevDemoScoreAnswer;
  overuse_concern: JevDemoNoulAnswer;
  human_review_warranted: JevDemoNoulAnswer;
  model_name: string;
}

/** What the application decided. Ordinary Python; no model involved. */
export interface JevDemoPolicy {
  /** `acceptable` | `attention` | `human_review`. */
  status: string;
  /** The bounded reason codes, most severe first. Empty iff `acceptable`. */
  reasons: string[];
  /** The number each rule was measured against, so the page can show the
   *  comparison rather than assert the conclusion. */
  thresholds: Record<string, number>;
}

export interface JevDemoEvaluation {
  state: JevDemoScenario;
  judgments: JevDemoJudgments;
  policy: JevDemoPolicy;
}
