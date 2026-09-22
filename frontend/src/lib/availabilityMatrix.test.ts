/**
 * Synthetic throughout -- invented membership ids and display names, no real
 * roster.
 */

import { describe, expect, it } from "vitest";

import type { EventAvailabilityResponse, MembershipAvailability } from "./api/types";
import {
  anyCanOperate,
  anyLockedAt,
  availabilityCellClass,
  availabilitySelectValue,
  availabilityStateFromSelectValue,
  buildAvailabilityMatrix,
} from "./availabilityMatrix";

function membership(
  id: number,
  name: string,
  state: MembershipAvailability["availability_state"],
  overrides: Partial<MembershipAvailability> = {},
): MembershipAvailability {
  return {
    ministry_membership_id: id,
    person_id: id,
    person_display_name: name,
    membership_deactivated_at: null,
    person_deactivated_at: null,
    availability_state: state,
    ...overrides,
  };
}

function response(
  eventId: number,
  memberships: MembershipAvailability[],
  lockedAt: string | null = null,
  canOperate = true,
): EventAvailabilityResponse {
  return {
    event_id: eventId,
    event_date: "2031-03-02",
    event_name: null,
    event_kind: "SUNDAY_SERVICE",
    ministry_id: 1,
    availability_locked_at: lockedAt,
    memberships,
    // Task 80. Irrelevant to the matrix builder, which reshapes rows and has
    // no opinion about who may edit them; read by `anyCanOperate` below.
    can_operate: canOperate,
  };
}

describe("buildAvailabilityMatrix", () => {
  it("builds one row per person, sorted by display name", () => {
    const responses = new Map([
      [1, response(1, [membership(2, "Blair", "AVAILABLE"), membership(1, "Amir", "UNAVAILABLE")])],
    ]);

    const matrix = buildAvailabilityMatrix(responses);

    expect(matrix.rows.map((r) => r.displayName)).toEqual(["Amir", "Blair"]);
  });

  it("carries each person's answer for each event under that event's id", () => {
    const responses = new Map([
      [10, response(10, [membership(1, "Amir", "AVAILABLE")])],
      [20, response(20, [membership(1, "Amir", "BACKUP")])],
    ]);

    const matrix = buildAvailabilityMatrix(responses);

    const amir = matrix.rows[0];
    expect(amir.cellsByEvent.get(10)).toBe("AVAILABLE");
    expect(amir.cellsByEvent.get(20)).toBe("BACKUP");
  });

  it("no response is a stored null, distinct from an event the person has no row for at all", () => {
    const responses = new Map([
      [10, response(10, [membership(1, "Amir", null)])],
    ]);

    const matrix = buildAvailabilityMatrix(responses);

    expect(matrix.rows[0].cellsByEvent.get(10)).toBeNull();
    expect(matrix.rows[0].cellsByEvent.has(99)).toBe(false);
  });

  it("carries the inactive flag from either the membership or the person", () => {
    const responses = new Map([
      [
        1,
        response(1, [
          membership(1, "Amir", "AVAILABLE", { membership_deactivated_at: "2031-01-01T00:00:00Z" }),
          membership(2, "Blair", "AVAILABLE", { person_deactivated_at: "2031-01-01T00:00:00Z" }),
          membership(3, "Casey", "AVAILABLE"),
        ]),
      ],
    ]);

    const matrix = buildAvailabilityMatrix(responses);
    const byName = new Map(matrix.rows.map((r) => [r.displayName, r.isInactive]));
    expect(byName.get("Amir")).toBe(true);
    expect(byName.get("Blair")).toBe(true);
    expect(byName.get("Casey")).toBe(false);
  });
});

describe("anyLockedAt", () => {
  it("is null when nothing has loaded", () => {
    expect(anyLockedAt(new Map())).toBeNull();
  });

  it("is null while every loaded event is unlocked", () => {
    const responses = new Map([[1, response(1, [], null)], [2, response(2, [], null)]]);
    expect(anyLockedAt(responses)).toBeNull();
  });

  it("reports the lock instant found on any loaded event", () => {
    const responses = new Map([
      [1, response(1, [], null)],
      [2, response(2, [], "2031-01-01T00:00:00Z")],
    ]);
    expect(anyLockedAt(responses)).toBe("2031-01-01T00:00:00Z");
  });
});

describe("anyCanOperate", () => {
  it("is false when nothing has loaded", () => {
    // An empty map is not evidence of authority. Defaulting the other way
    // would flash editable controls at a reader who has none.
    expect(anyCanOperate(new Map())).toBe(false);
  });

  it("reads the answer off whichever response arrived", () => {
    const responses = new Map([[1, response(1, [], null, true)]]);
    expect(anyCanOperate(responses)).toBe(true);
  });

  it("is false for a reader the server says may not write", () => {
    const responses = new Map([
      [1, response(1, [], null, false)],
      [2, response(2, [], null, false)],
    ]);
    expect(anyCanOperate(responses)).toBe(false);
  });
});

describe("the compact select control", () => {
  it("round-trips every stored state through the select value and back", () => {
    for (const state of ["AVAILABLE", "BACKUP", "UNAVAILABLE"] as const) {
      const value = availabilitySelectValue(state);
      expect(availabilityStateFromSelectValue(value)).toBe(state);
    }
  });

  it("no response is the empty select value in both directions", () => {
    expect(availabilitySelectValue(null)).toBe("");
    expect(availabilityStateFromSelectValue("")).toBeNull();
  });
});

describe("availabilityCellClass", () => {
  it("gives every one of the four states its own, distinguishable class", () => {
    const classes = new Set([
      availabilityCellClass("AVAILABLE"),
      availabilityCellClass("BACKUP"),
      availabilityCellClass("UNAVAILABLE"),
      availabilityCellClass(null),
    ]);
    expect(classes.size).toBe(4);
  });
});
