/**
 * Wording. The rule under test throughout is "translate, never invent".
 */

import { describe, expect, it } from "vitest";

import {
  BACKUP_EXPLANATION,
  EVENT_GAP_EXPLANATION,
  EVENT_GAP_HELP,
  EVENT_GAP_QUESTION,
  EVENT_GAP_RULE_NAME,
  LINKED_MEMBER_RULE_NAME,
  LINKED_MEMBER_RULE_STATUS,
  MEMBER_GROUP_LIMITS_EXPLANATION,
  RULES_OVERVIEW_NOTE,
  RULE_SOURCE_EXTERNAL,
  SAME_EVENT_SUPPORT_EXPLANATION,
  SERVING_LIMITS_RULE_NAME,
  SERVING_LIMIT_EXPLANATION,
  availabilityStateLabel,
  memberGroupLimitsStatus,
  supportRequirementsStatus,
  diagnosticText,
  eventGapDescription,
  eventGapLabel,
  formatCompactDate,
  formatDateRange,
  formatEventDate,
  isAvailabilityReady,
  isRoleActive,
  memberGroupLimitLabel,
  supportRequirementLabel,
  qualificationLabel,
  readinessLabel,
  roleStatusLabel,
  servingLimitLabel,
} from "./labels";

describe("solver diagnostics", () => {
  it("gives a known code a plain-English sentence", () => {
    expect(diagnosticText("NO_QUALIFIED_CANDIDATES")).toBe("Nobody is approved for this role.");
    expect(diagnosticText("ALL_UNAVAILABLE")).toContain("unavailable");
  });

  it("shows an unknown code as itself rather than guessing", () => {
    expect(diagnosticText("SOME_FUTURE_CODE")).toBe("SOME_FUTURE_CODE");
  });

  it("covers every diagnostic the backend can currently emit", () => {
    const backendCodes = [
      "ROLE_INACTIVE",
      "NO_QUALIFIED_CANDIDATES",
      "ALL_UNAVAILABLE",
      "NO_RESPONSE_DISALLOWED",
      "ALL_CHURCH_CONFLICTED",
      "SAME_EVENT_CONTENTION",
      "INSUFFICIENT_FEASIBLE_CANDIDATES",
      "ALL_AT_PERIOD_LIMIT",
      "LINKED_DATE_CONFLICT",
      "ALL_WITHIN_EVENT_GAP",
    ];

    for (const code of backendCodes) {
      // Translated, not echoed back as a shouty constant.
      expect(diagnosticText(code)).not.toBe(code);
      expect(diagnosticText(code)).toMatch(/[a-z]/);
    }
  });

  it("explains a period limit as a limit, not as unavailability", () => {
    const text = diagnosticText("ALL_AT_PERIOD_LIMIT").toLowerCase();
    expect(text).toContain("maximum");
    expect(text).not.toContain("unavailable");
  });

  it("explains a linked-date conflict without naming who is linked to whom", () => {
    const text = diagnosticText("LINKED_DATE_CONFLICT");
    expect(text.toLowerCase()).toContain("linked");
    expect(text.toLowerCase()).toContain("same-date");
    // Never a specific person's name -- the backend itself never says who.
    expect(text).not.toMatch(/[A-Z][a-z]+ [A-Z][a-z]+/);
  });
});

describe("readiness issues", () => {
  it("labels a known issue code", () => {
    expect(readinessLabel("UNFILLED_REQUIREMENT")).toBe("Position not filled");
    expect(readinessLabel("STALE_REQUIREMENT_SNAPSHOT")).toBe("Scheduling information changed");
  });

  it("shows an unknown code as itself", () => {
    expect(readinessLabel("BRAND_NEW_ISSUE")).toBe("BRAND_NEW_ISSUE");
  });

  it("never uses the word 'stale' at a reader", () => {
    for (const code of ["STALE_REQUIREMENT_SNAPSHOT", "UNFILLED_REQUIREMENT"]) {
      expect(readinessLabel(code).toLowerCase()).not.toContain("stale");
    }
  });

  it("labels a serving-limit overrun without the internal code name", () => {
    const label = readinessLabel("EXCEEDS_SERVING_LIMIT");
    expect(label).not.toBe("EXCEEDS_SERVING_LIMIT");
    expect(label.toLowerCase()).toContain("serving limit");
  });

  it("labels a linked-member same-date conflict without the internal code name", () => {
    const label = readinessLabel("SAME_DATE_LINKED_MEMBER_CONFLICT");
    expect(label).not.toBe("SAME_DATE_LINKED_MEMBER_CONFLICT");
    expect(label.toLowerCase()).toContain("linked-member");
  });

  it("labels an event-gap conflict in plain words, never in days", () => {
    const label = readinessLabel("MIN_EVENT_GAP_CONFLICT");
    expect(label).not.toBe("MIN_EVENT_GAP_CONFLICT");
    expect(label.toLowerCase()).toContain("too soon");
    expect(label.toLowerCase()).not.toContain("day");
  });
});

describe("the event-gap rule", () => {
  it("has a plain-English name, and never shows the stored field name", () => {
    expect(EVENT_GAP_RULE_NAME).toBe("Events to skip after serving");
    expect(EVENT_GAP_RULE_NAME.toLowerCase()).not.toContain("min_intervening_events");
    expect(EVENT_GAP_RULE_NAME.toLowerCase()).not.toContain("intervening");
  });

  it("labels absence as 'No rule' and a number as a count of events, never zero", () => {
    expect(eventGapLabel(null)).toBe("No rule");
    expect(eventGapLabel(1)).toBe("Skip 1 event");
    expect(eventGapLabel(3)).toBe("Skip 3 events");
  });

  it("the Meaning column says exactly what a configured value does", () => {
    expect(eventGapDescription(null).toLowerCase()).toContain("may serve");
    expect(eventGapDescription(1).toLowerCase()).toBe("no consecutive ministry events.");
    expect(eventGapDescription(4)).toContain("4");
  });

  it("stays accurate above one rather than pluralising the special case", () => {
    // "no consecutive ministry events" is only true of a gap of one; a larger
    // gap must not borrow its wording.
    expect(eventGapDescription(2).toLowerCase()).not.toContain("consecutive");
  });

  it("scopes the explanation to this ministry and this period, counted in events", () => {
    const text = EVENT_GAP_EXPLANATION.toLowerCase();
    expect(text).toContain("this ministry");
    expect(text).toContain("this period");
    expect(text).toContain("counted in events, not in");
    expect(text).toContain("not a church-wide rule");
    expect(text).toContain("does not carry");
  });

  it("says the rule looks just outside the period, in both directions", () => {
    // The setting is period-scoped; enforcement is not, and a head whose first
    // Sunday will not fill needs to know why without reading the backend.
    const text = EVENT_GAP_EXPLANATION.toLowerCase();
    expect(text).toContain("just outside this period");
    expect(text).toContain("before it");
    expect(text).toContain("after it");
  });

  it("keeps the product copy generic -- no ministry and no weekday", () => {
    for (const text of [EVENT_GAP_EXPLANATION, EVENT_GAP_HELP, EVENT_GAP_QUESTION, EVENT_GAP_RULE_NAME]) {
      const lower = text.toLowerCase();
      expect(lower).not.toContain("setup");
      expect(lower).not.toContain("sunday");
      expect(lower).not.toContain("nlf");
    }
  });

  it("asks the concise question, event-based rather than calendar-day-based", () => {
    expect(EVENT_GAP_QUESTION.toLowerCase()).toContain("events");
    expect(EVENT_GAP_QUESTION.toLowerCase()).not.toContain("day");
  });

  it("tells a head exactly what 1 means, in the help text -- no consecutive events", () => {
    expect(EVENT_GAP_HELP.toLowerCase()).toContain("set to 1");
    expect(EVENT_GAP_HELP.toLowerCase()).toContain("consecutive");
    expect(EVENT_GAP_HELP.toLowerCase()).not.toContain("day");
  });
});

describe("availability", () => {
  it("is ready only once it has been locked", () => {
    expect(isAvailabilityReady(null)).toBe(false);
    expect(isAvailabilityReady("2026-09-30T12:00:00Z")).toBe(true);
  });
});

describe("ministry roles", () => {
  it("is active only while deactivated_at is null", () => {
    expect(isRoleActive(null)).toBe(true);
    expect(isRoleActive("2026-09-30T12:00:00Z")).toBe(false);
  });

  it("labels active and inactive plainly", () => {
    expect(roleStatusLabel(null)).toBe("Active");
    expect(roleStatusLabel("2026-09-30T12:00:00Z")).toBe("Inactive");
  });
});

describe("role qualifications", () => {
  it("labels all three states distinctly, never collapsing null into a boolean", () => {
    expect(qualificationLabel(null)).toBe("Not yet assessed");
    expect(qualificationLabel(true)).toBe("Qualified");
    expect(qualificationLabel(false)).toBe("Not qualified");
    const labels = new Set([qualificationLabel(null), qualificationLabel(true), qualificationLabel(false)]);
    expect(labels.size).toBe(3);
  });
});

describe("availability", () => {
  it("labels no response and all three stored states distinctly", () => {
    expect(availabilityStateLabel(null)).toBe("No response");
    expect(availabilityStateLabel("AVAILABLE")).toBe("Available");
    expect(availabilityStateLabel("BACKUP")).toBe("If need be");
    expect(availabilityStateLabel("UNAVAILABLE")).toBe("Unavailable");
    const labels = [null, "AVAILABLE", "BACKUP", "UNAVAILABLE"].map(availabilityStateLabel);
    expect(new Set(labels).size).toBe(4);
  });

  it("shows an unrecognized value as itself rather than guessing", () => {
    expect(availabilityStateLabel("SOME_FUTURE_STATE")).toBe("SOME_FUTURE_STATE");
  });

  it("the explanation defines the tier rather than echoing its own label", () => {
    // The label is "If need be"; the explanation is not required to repeat
    // those words verbatim, and doing so would read as circular ("if need be
    // means if need be") rather than as a definition.
    expect(BACKUP_EXPLANATION.toLowerCase()).not.toContain("kids");
  });
});

describe("serving limits", () => {
  it("labels absence as 'No limit' and a number as itself, never zero", () => {
    expect(servingLimitLabel(null)).toBe("No limit");
    expect(servingLimitLabel(4)).toBe("4");
    expect(servingLimitLabel(1)).toBe("1");
  });

  it("scopes the explanation to this ministry and this period, hard, not church-wide", () => {
    const text = SERVING_LIMIT_EXPLANATION.toLowerCase();
    expect(text).toContain("this ministry");
    expect(text).toContain("this scheduling period");
    expect(text).toContain("hard");
    expect(text).toContain("not a church-wide limit");
    expect(text).toContain("does not carry");
  });
});

describe("dates", () => {
  it("renders a schedule date as the calendar day it names", () => {
    // Parsed as a local calendar date, not a UTC instant: `new Date(iso)`
    // would render 4 October as the 3rd anywhere west of Greenwich.
    const formatted = formatEventDate("2026-10-04");
    expect(formatted).toContain("2026");
    expect(formatted).toContain("4");
    expect(formatted).toContain("Sunday");
  });

  it("renders a range", () => {
    const range = formatDateRange("2026-10-04", "2026-12-27");
    expect(range).toContain("2026");
    expect(range).toContain("–");
  });

  it("shows an unparseable date as given rather than as 'Invalid Date'", () => {
    expect(formatEventDate("not-a-date")).toBe("not-a-date");
  });

  it("the compact matrix-header form names the day and month, not the year", () => {
    const compact = formatCompactDate("2031-03-02");
    expect(compact).toContain("2");
    expect(compact).not.toContain("2031");
  });

  it("the compact form is unparseable-safe too", () => {
    expect(formatCompactDate("not-a-date")).toBe("not-a-date");
  });
});


describe("the member group limit (Task 74)", () => {
  it("shows no limit as words, never as zero", () => {
    // The backend cannot store a zero, and showing one would be a second
    // spelling of a fact that already has one.
    expect(memberGroupLimitLabel(null)).toBe("No limit");
    expect(memberGroupLimitLabel(null)).not.toContain("0");
  });

  it("reads naturally at one and above", () => {
    expect(memberGroupLimitLabel(1)).toBe("1 per service");
    expect(memberGroupLimitLabel(3)).toBe("3 per service");
  });

  it("the explanation names no ministry and no category", () => {
    const text = MEMBER_GROUP_LIMITS_EXPLANATION.toLowerCase();
    for (const specific of ["setup", "student", "kids", "av"]) {
      expect(text).not.toContain(specific);
    }
  });
});

describe("the same-event support requirement (Task 74)", () => {
  it("says what the rule requires", () => {
    expect(supportRequirementLabel(1)).toContain("at least 1");
    expect(supportRequirementLabel(2)).toContain("at least 2");
    expect(supportRequirementLabel(1)).toContain("approved supporting members");
  });

  it("never says why the support is needed", () => {
    // The system does not store a reason, does not know one, and must not
    // imply that it does.
    const wording = [
      supportRequirementLabel(1),
      supportRequirementLabel(2),
      SAME_EVENT_SUPPORT_EXPLANATION,
    ].join(" ").toLowerCase();
    for (const forbidden of [
      "transport", "lift", "ride", "drive", "family", "spouse", "partner",
      "household", "parent",
    ]) {
      expect(wording).not.toContain(forbidden);
    }
  });

  it("says same service, not same day", () => {
    expect(SAME_EVENT_SUPPORT_EXPLANATION).toContain("same service");
    expect(SAME_EVENT_SUPPORT_EXPLANATION).toContain("not merely the same day");
  });
});

describe("the new readiness and diagnostic codes", () => {
  it("names the member group cap in both vocabularies", () => {
    expect(readinessLabel("MEMBER_GROUP_EVENT_LIMIT_CONFLICT")).toBe(
      "Too many from one member group",
    );
    expect(diagnosticText("ALL_AT_GROUP_EVENT_LIMIT")).toContain("member group");
  });

  it("names the support rule in both vocabularies", () => {
    expect(readinessLabel("SAME_EVENT_SUPPORT_CONFLICT")).toBe(
      "Missing required supporting member",
    );
    expect(diagnosticText("ALL_WITHOUT_EVENT_SUPPORT")).toContain(
      "approved supporting members",
    );
  });

  it("still shows an unknown code verbatim rather than swallowing it", () => {
    expect(readinessLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(diagnosticText("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

// -- The rules overview (Task 75) -------------------------------------------

describe("the rules overview statuses", () => {
  it("distinguishes a backend that reported nothing from one that reported none", () => {
    // "Not reported" and "none configured" are different facts, and collapsing
    // them would tell a head there is no rule when nobody actually said so.
    expect(memberGroupLimitsStatus(undefined)).toBe("Not reported");
    expect(memberGroupLimitsStatus([])).toBe("No groups defined");
    expect(supportRequirementsStatus(undefined)).toBe("Not reported");
    expect(supportRequirementsStatus([])).toBe("None configured");
  });

  it("counts only the groups that actually carry a cap", () => {
    expect(memberGroupLimitsStatus([{ max_per_event: null }])).toContain("No limits set");
    expect(memberGroupLimitsStatus([{ max_per_event: 2 }, { max_per_event: null }])).toBe(
      "1 of 2 groups limited",
    );
    expect(memberGroupLimitsStatus([{ max_per_event: 1 }])).toBe("1 of 1 group limited");
  });

  it("counts the support requirements that exist", () => {
    expect(supportRequirementsStatus([{}, {}])).toBe("2 configured");
  });

  it("says where each rule family is set, in generic terms", () => {
    const wording = [
      SERVING_LIMITS_RULE_NAME,
      LINKED_MEMBER_RULE_NAME,
      LINKED_MEMBER_RULE_STATUS,
      RULE_SOURCE_EXTERNAL,
      RULES_OVERVIEW_NOTE,
    ]
      .join(" ")
      .toLowerCase();

    // Generic vocabulary only: no relationship, reason or category a ministry
    // might have had for a rule the system stores no reason for.
    for (const word of ["ride", "spouse", "family", "student", "parent", "child"]) {
      expect(wording).not.toContain(word);
    }
    expect(RULES_OVERVIEW_NOTE).toContain("Serving limits");
  });
});
