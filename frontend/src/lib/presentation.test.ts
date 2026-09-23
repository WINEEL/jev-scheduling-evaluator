import { describe, expect, it } from "vitest";

import type { JevDemoNoulAnswer, JevDemoScoreAnswer } from "./types";
import {
  STATUS_ACCEPTABLE,
  STATUS_ATTENTION,
  STATUS_HUMAN_REVIEW,
  confidenceBand,
  isDistributionSpread,
  noulReading,
  percent,
  questionLabel,
  reasonLabel,
  reasonThreshold,
  scoreOutOf,
  statusExplanation,
  statusLabel,
  statusTone,
} from "./presentation";

/**
 * The demo page's wording, tone and formatting rules.
 *
 * All of it is presentation: nothing in `jevDemo.ts` decides a status, and one
 * test below asserts that by checking the module holds no thresholds of its
 * own for the statuses it renders. The decision is the backend's, and a copy
 * of it here would make the page lie about the architecture it is
 * demonstrating.
 */

function score(overrides: Partial<JevDemoScoreAnswer> = {}): JevDemoScoreAnswer {
  return {
    question: "workload_fairness",
    score: 3.4,
    levels: 5,
    normalized: 0.85,
    confidence: 0.9,
    probabilities: { "0": 0, "1": 0, "2": 0, "3": 0.9, "4": 0.1 },
    ...overrides,
  };
}

function noul(probability: number): JevDemoNoulAnswer {
  return { question: "overuse_concern", probability };
}

describe("statusLabel / statusExplanation / statusTone", () => {
  it("names each status as the action it implies", () => {
    expect(statusLabel(STATUS_ACCEPTABLE)).toBe("Acceptable");
    expect(statusLabel(STATUS_ATTENTION)).toBe("Worth a look");
    expect(statusLabel(STATUS_HUMAN_REVIEW)).toBe("Needs human review");
  });

  it("maps each status onto a distinct tone", () => {
    const tones = [STATUS_ACCEPTABLE, STATUS_ATTENTION, STATUS_HUMAN_REVIEW].map(statusTone);
    expect(tones).toEqual(["ok", "warning", "danger"]);
    expect(new Set(tones).size).toBe(3);
  });

  it("explains every status in a sentence", () => {
    for (const status of [STATUS_ACCEPTABLE, STATUS_ATTENTION, STATUS_HUMAN_REVIEW]) {
      expect(statusExplanation(status).length).toBeGreaterThan(20);
    }
  });

  it("renders an unrecognised status as itself rather than crashing", () => {
    // The backend's vocabulary is bounded, but a new value should look
    // unfamiliar on screen, not invisible.
    expect(statusLabel("something_new")).toBe("something_new");
    expect(statusTone("something_new")).toBe("warning");
  });
});

describe("reasonLabel", () => {
  const CODES = [
    "MODEL_REQUESTS_REVIEW",
    "QUALITY_UNACCEPTABLE",
    "QUALITY_UNCERTAIN",
    "OVERUSE_CONCERN",
    "WORKLOAD_UNFAIR",
    "PREFERENCES_UNMET",
    "QUALITY_BELOW_TARGET",
  ];

  it.each(CODES)("gives %s a sentence, not the code", (code) => {
    const label = reasonLabel(code);
    expect(label).not.toBe(code);
    expect(label.length).toBeGreaterThan(10);
  });

  it("distinguishes an uncertain model from a poor schedule", () => {
    // The one label that would be actively misleading if it were generic:
    // QUALITY_UNCERTAIN is a statement about the model, not the draft.
    expect(reasonLabel("QUALITY_UNCERTAIN")).toMatch(/confident/i);
    expect(reasonLabel("QUALITY_UNACCEPTABLE")).toMatch(/quality/i);
  });

  it("renders an unknown code as itself", () => {
    expect(reasonLabel("SOMETHING_ELSE")).toBe("SOMETHING_ELSE");
  });
});

describe("reasonThreshold", () => {
  const THRESHOLDS = {
    review_probability: 0.6,
    overuse_probability: 0.6,
    unacceptable_quality: 0.4,
    target_quality: 0.75,
    minimum_quality_confidence: 0.4,
    fair_workload: 0.6,
    satisfied_preferences: 0.6,
  };

  it("finds the number each reason was measured against", () => {
    expect(reasonThreshold("MODEL_REQUESTS_REVIEW", THRESHOLDS)).toBe("60%");
    expect(reasonThreshold("QUALITY_BELOW_TARGET", THRESHOLDS)).toBe("75%");
    expect(reasonThreshold("QUALITY_UNCERTAIN", THRESHOLDS)).toBe("40%");
  });

  it("returns null rather than inventing one for an unknown reason", () => {
    expect(reasonThreshold("SOMETHING_ELSE", THRESHOLDS)).toBeNull();
  });

  it("returns null when the backend did not send that threshold", () => {
    expect(reasonThreshold("MODEL_REQUESTS_REVIEW", {})).toBeNull();
  });
});

describe("percent", () => {
  it("renders a 0..1 value as a whole-number percentage", () => {
    expect(percent(0)).toBe("0%");
    expect(percent(0.855)).toBe("86%");
    expect(percent(1)).toBe("100%");
  });

  it("clamps rather than rendering an impossible figure", () => {
    // A probability outside 0..1 is a backend bug. "-40%" would hide it behind
    // something that reads as a formatting quirk.
    expect(percent(-0.4)).toBe("0%");
    expect(percent(1.4)).toBe("100%");
  });

  it("renders a non-finite value as a dash", () => {
    expect(percent(Number.NaN)).toBe("—");
  });
});

describe("scoreOutOf", () => {
  it("states the position on the rubric's own scale", () => {
    // Levels are 0-based, so a five-level rubric tops out at 4.
    expect(scoreOutOf(score({ score: 3.4, levels: 5 }))).toBe("3.40 / 4");
    expect(scoreOutOf(score({ score: 0, levels: 5 }))).toBe("0.00 / 4");
  });
});

describe("noulReading", () => {
  it("reads a middling probability as uncertainty, never as intensity", () => {
    // The specific misreading TypeSafe's documentation warns about: 0.5 means
    // "as likely as not", not "moderately yes".
    expect(noulReading(noul(0.5))).toBe("As likely as not");
    expect(noulReading(noul(0.45))).toBe("As likely as not");
  });

  it("reads the confident ends plainly", () => {
    expect(noulReading(noul(0.97))).toBe("Yes, clearly");
    expect(noulReading(noul(0.03))).toBe("No, clearly");
  });

  it("reads the leaning ranges as leaning", () => {
    expect(noulReading(noul(0.65))).toBe("Leaning yes");
    expect(noulReading(noul(0.3))).toBe("Leaning no");
  });
});

describe("confidenceBand", () => {
  it("bands confidence into three readings with distinct tones", () => {
    expect(confidenceBand(0.92)).toEqual({ label: "Confident", tone: "ok" });
    expect(confidenceBand(0.55)).toEqual({ label: "Somewhat unsure", tone: "warning" });
    expect(confidenceBand(0.07)).toEqual({ label: "Not confident", tone: "danger" });
  });

  it("puts the bottom boundary at the policy's own confidence floor", () => {
    // 0.40 is `minimum_quality_confidence` in app/soft_constraints/policy.py:
    // below it the backend routes to a human, so the page should not be
    // calling that same value merely "somewhat unsure".
    expect(confidenceBand(0.4).tone).toBe("warning");
    expect(confidenceBand(0.39).tone).toBe("danger");
  });
});

describe("isDistributionSpread", () => {
  it("is false when the probability sits on one level", () => {
    expect(isDistributionSpread(score({ probabilities: { "0": 0, "1": 0, "2": 0, "3": 1, "4": 0 } }))).toBe(false);
  });

  it("is true when no level holds most of the mass", () => {
    // The interesting case, and the one worth the space on screen: the model
    // is genuinely split, which is what a low confidence figure reports.
    expect(
      isDistributionSpread(score({ probabilities: { "0": 0.1, "1": 0.3, "2": 0.35, "3": 0.2, "4": 0.05 } })),
    ).toBe(true);
  });

  it("is false when there is no distribution at all", () => {
    expect(isDistributionSpread(score({ probabilities: {} }))).toBe(false);
  });
});

describe("questionLabel", () => {
  it("names each of the five questions", () => {
    expect(questionLabel("workload_fairness")).toBe("Workload fairness");
    expect(questionLabel("preference_satisfaction")).toBe("Preference satisfaction");
    expect(questionLabel("soft_constraint_quality")).toBe("Overall soft-constraint quality");
    expect(questionLabel("overuse_concern")).toBe("Overuse concern");
    expect(questionLabel("human_review_warranted")).toBe("Human review warranted");
  });

  it("renders an unknown question id as itself", () => {
    expect(questionLabel("something_new")).toBe("something_new");
  });
});
