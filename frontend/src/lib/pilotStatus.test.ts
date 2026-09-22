/**
 * The pilot-status configuration: what it does with what a person typed into
 * an environment file, and -- more importantly -- what it refuses to claim
 * when nothing was typed at all.
 */

import { describe, expect, it } from "vitest";

import {
  EXAMPLE_MINISTRY_LABEL,
  VALIDATED_MINISTRY_LABEL,
  isValidatedMinistry,
  parseValidatedMinistries,
  pilotStatusNote,
} from "./pilotStatus";

describe("parseValidatedMinistries", () => {
  it("reads nothing out of an unset variable", () => {
    expect(parseValidatedMinistries(undefined)).toEqual([]);
  });

  it("reads nothing out of an empty or whitespace-only value", () => {
    expect(parseValidatedMinistries("")).toEqual([]);
    expect(parseValidatedMinistries("   ")).toEqual([]);
    // A trailing comma is not a ministry called "".
    expect(parseValidatedMinistries("Alpha,")).toEqual(["Alpha"]);
  });

  it("splits on commas and trims what a person typed", () => {
    expect(parseValidatedMinistries(" Alpha , Beta Team ")).toEqual(["Alpha", "Beta Team"]);
  });
});

describe("isValidatedMinistry", () => {
  it("matches regardless of case or surrounding space", () => {
    const configured = parseValidatedMinistries(" alpha ");
    expect(isValidatedMinistry("Alpha", configured)).toBe(true);
    expect(isValidatedMinistry("  ALPHA  ", configured)).toBe(true);
  });

  it("does not match a different ministry", () => {
    expect(isValidatedMinistry("Beta", ["Alpha"])).toBe(false);
  });

  it("matches nothing at all when nothing is configured", () => {
    // The honest default: with no configuration, no ministry is described as
    // validated *or* as an unvalidated example -- the screen says neither.
    expect(isValidatedMinistry("Alpha", [])).toBe(false);
  });
});

describe("pilotStatusNote", () => {
  it("explains the other ministries only when there are any", () => {
    expect(pilotStatusNote(true)).toContain("The others");
    // A head who leads only validated ministries is told what validated means
    // and nothing about ministries that are not on their screen.
    expect(pilotStatusNote(false)).not.toContain("The others");
    expect(pilotStatusNote(false)).toContain("ministry head");
  });
});

describe("the wording", () => {
  it("claims a conversation, not an endorsement", () => {
    const all =
      `${VALIDATED_MINISTRY_LABEL} ${EXAMPLE_MINISTRY_LABEL} ${pilotStatusNote(true)}`.toLowerCase();
    for (const claim of ["production ready", "approved", "certified", "fully validated"]) {
      expect(all).not.toContain(claim);
    }
    expect(VALIDATED_MINISTRY_LABEL).toContain("ministry head");
  });
});
