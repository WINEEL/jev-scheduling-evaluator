/**
 * How the Jev demo's numbers are read on screen. Pure functions, no React.
 *
 * Everything the `/jev-demo` page decides about wording, tone and formatting
 * lives here rather than inside the component, for the reason this app's
 * `vitest.config.ts` gives: the rules worth protecting are written as plain
 * functions so they can be tested directly, with no browser and no renderer.
 *
 * **Nothing here decides anything.** The status is the backend's — computed in
 * `app/soft_constraints/policy.py` by ordinary Python, from probabilities the
 * model returned. This file chooses a colour and a sentence for a decision
 * that has already been made somewhere else, and that separation is exactly
 * what the page is trying to show. If a threshold ever appeared in this file,
 * the demo would be lying about its own architecture.
 *
 * The reason and status vocabularies are bounded by the backend, so they are
 * translated through lookup tables. An unrecognised code is rendered as
 * itself, never dropped: a new reason the backend starts sending should look
 * unfamiliar on screen, not invisible.
 */

import type { JevDemoNoulAnswer, JevDemoScoreAnswer } from "./types";

/** The three statuses `app/soft_constraints/policy.py` can return. */
export const STATUS_ACCEPTABLE = "acceptable";
export const STATUS_ATTENTION = "attention";
export const STATUS_HUMAN_REVIEW = "human_review";

/** Maps onto this app's existing `--ok` / `--warning` / `--danger` tokens. */
export type Tone = "ok" | "warning" | "danger";

/**
 * What the status is called on screen.
 *
 * Written as what the application will *do*, not as a grade for the schedule.
 * "Attention" describes a draft; "Needs a person to look" describes the next
 * step, which is the thing a coordinator is actually reading for.
 */
export function statusLabel(status: string): string {
  switch (status) {
    case STATUS_ACCEPTABLE:
      return "Acceptable";
    case STATUS_ATTENTION:
      return "Worth a look";
    case STATUS_HUMAN_REVIEW:
      return "Needs human review";
    default:
      return status;
  }
}

/** One sentence saying what the application does about this status. */
export function statusExplanation(status: string): string {
  switch (status) {
    case STATUS_ACCEPTABLE:
      return "Nothing in how the work was shared calls for anybody's time. The draft can go out as it stands.";
    case STATUS_ATTENTION:
      return "Something here is worth a coordinator's eye, but the draft stands on its own.";
    case STATUS_HUMAN_REVIEW:
      return "A person should look at this draft before it is sent to the people on it.";
    default:
      return "The application returned a status this page does not recognise.";
  }
}

export function statusTone(status: string): Tone {
  switch (status) {
    case STATUS_ACCEPTABLE:
      return "ok";
    case STATUS_ATTENTION:
      return "warning";
    case STATUS_HUMAN_REVIEW:
      return "danger";
    default:
      return "warning";
  }
}

/**
 * The bounded reason vocabulary from `app/soft_constraints/policy.py`.
 *
 * Each entry says which rule fired in the terms a person would use, because
 * `QUALITY_UNCERTAIN` on its own reads like a fault in the schedule when it is
 * a statement about the model.
 */
const REASON_LABELS: Record<string, string> = {
  MODEL_REQUESTS_REVIEW: "Jev judged this draft worth a person's look",
  QUALITY_UNACCEPTABLE: "Overall quality is below the acceptable floor",
  QUALITY_UNCERTAIN: "Jev was not confident enough in its overall rating",
  OVERUSE_CONCERN: "At least one person looks leaned on too heavily",
  WORKLOAD_UNFAIR: "The work is not spread fairly enough",
  PREFERENCES_UNMET: "Stated preferences were not well enough respected",
  QUALITY_BELOW_TARGET: "Overall quality is below target",
};

/** Unrecognised codes render as themselves rather than vanishing. */
export function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? code;
}

/** Which policy threshold a reason was measured against, where there is one. */
const REASON_THRESHOLDS: Record<string, string> = {
  MODEL_REQUESTS_REVIEW: "review_probability",
  QUALITY_UNACCEPTABLE: "unacceptable_quality",
  QUALITY_UNCERTAIN: "minimum_quality_confidence",
  OVERUSE_CONCERN: "overuse_probability",
  WORKLOAD_UNFAIR: "fair_workload",
  PREFERENCES_UNMET: "satisfied_preferences",
  QUALITY_BELOW_TARGET: "target_quality",
};

/**
 * The threshold behind one reason, formatted, or `null` when there is none.
 *
 * Shown beside each reason so a reader can check the arithmetic instead of
 * taking the status on trust — which is the demo's central claim made
 * verifiable rather than merely asserted.
 */
export function reasonThreshold(
  code: string,
  thresholds: Record<string, number>,
): string | null {
  const key = REASON_THRESHOLDS[code];
  if (key === undefined) return null;
  const value = thresholds[key];
  return typeof value === "number" ? percent(value) : null;
}

/** The five question ids, as headings. */
const QUESTION_LABELS: Record<string, string> = {
  workload_fairness: "Workload fairness",
  preference_satisfaction: "Preference satisfaction",
  soft_constraint_quality: "Overall soft-constraint quality",
  overuse_concern: "Overuse concern",
  human_review_warranted: "Human review warranted",
};

export function questionLabel(question: string): string {
  return QUESTION_LABELS[question] ?? question;
}

/**
 * A `0..1` value as a whole-number percentage.
 *
 * Rounded, not truncated, and clamped: a probability outside the range is a
 * backend bug, and rendering "-4%" would hide it behind something that looks
 * like a formatting quirk.
 */
export function percent(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const clamped = Math.min(1, Math.max(0, value));
  return `${Math.round(clamped * 100)}%`;
}

/** A Score's position on its own rubric, e.g. `3.4 / 4`. */
export function scoreOutOf(answer: JevDemoScoreAnswer): string {
  return `${answer.score.toFixed(2)} / ${answer.levels - 1}`;
}

/**
 * How to read a Noul, in words.
 *
 * **0.5 means "as likely as not", never "moderately".** A Noul carries no
 * confidence, so the probability is the whole answer; describing a middling
 * one as a middling *intensity* is the specific misreading TypeSafe's
 * documentation warns about, and a demo that made it would be teaching the
 * wrong thing.
 */
export function noulReading(answer: JevDemoNoulAnswer): string {
  const p = answer.probability;
  if (p >= 0.8) return "Yes, clearly";
  if (p >= 0.6) return "Leaning yes";
  if (p > 0.4) return "As likely as not";
  if (p > 0.2) return "Leaning no";
  return "No, clearly";
}

/**
 * How much weight a Score's confidence will bear.
 *
 * Bands, not a number alone, because confidence is a second axis rather than a
 * second score: the page uses it to say whether the rating can be acted on,
 * which is what `confidence` is for. The boundaries match the policy's own
 * `minimum_quality_confidence` floor of 0.40 at the bottom; the upper one is
 * presentational and decides nothing.
 */
export function confidenceBand(confidence: number): {
  label: string;
  tone: Tone;
} {
  if (confidence >= 0.7) return { label: "Confident", tone: "ok" };
  if (confidence >= 0.4) return { label: "Somewhat unsure", tone: "warning" };
  return { label: "Not confident", tone: "danger" };
}

/**
 * Whether a Score's probability mass is spread rather than concentrated.
 *
 * Used to decide whether showing the distribution is worth the space. A
 * concentrated answer's distribution says nothing the score did not; a flat
 * one is the interesting case, and is exactly when a reader wants to see it.
 */
export function isDistributionSpread(answer: JevDemoScoreAnswer): boolean {
  const values = Object.values(answer.probabilities);
  if (values.length === 0) return false;
  return Math.max(...values) < 0.75;
}
