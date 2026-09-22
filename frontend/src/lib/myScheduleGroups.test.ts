/**
 * Grouping the signed-in person's commitments for display (Task 77).
 *
 * The rule worth protecting is that a volunteer serving twice on one morning
 * reads it as *one day with two things in it*, not as two unrelated rows -- and
 * that the server's ordering is preserved rather than re-derived here.
 */

import { describe, expect, it } from "vitest";

import type { MyScheduleAssignment } from "./api/types";
import { groupByDay, hasUnconfirmed } from "./myScheduleGroups";

function commitment(
  overrides: Partial<MyScheduleAssignment> & { assignment_id: number; event_date: string },
): MyScheduleAssignment {
  return {
    event_id: overrides.assignment_id,
    event_kind: "SUNDAY",
    event_name: null,
    ministry_id: 1,
    ministry_name: "SetupMin",
    ministry_role_id: 1,
    ministry_role_name: "SetupRole",
    is_confirmed: true,
    schedule_version_status: "FINALIZED",
    ...overrides,
  };
}

describe("groupByDay", () => {
  it("returns nothing for an empty schedule", () => {
    expect(groupByDay([])).toEqual([]);
  });

  it("puts one commitment in one day", () => {
    const days = groupByDay([commitment({ assignment_id: 1, event_date: "2026-10-11" })]);

    expect(days).toHaveLength(1);
    expect(days[0].date).toBe("2026-10-11");
    expect(days[0].assignments).toHaveLength(1);
  });

  it("groups two ministries on the same morning into one day", () => {
    // The case the grouping exists for: serving in Setup and AV on one Sunday
    // is one entry in your week, not two rows to cross-reference.
    const days = groupByDay([
      commitment({ assignment_id: 1, event_date: "2026-10-11", ministry_name: "AvMin" }),
      commitment({ assignment_id: 2, event_date: "2026-10-11", ministry_name: "SetupMin" }),
    ]);

    expect(days).toHaveLength(1);
    expect(days[0].assignments.map((a) => a.ministry_name)).toEqual(["AvMin", "SetupMin"]);
  });

  it("keeps different dates apart", () => {
    const days = groupByDay([
      commitment({ assignment_id: 1, event_date: "2026-10-11" }),
      commitment({ assignment_id: 2, event_date: "2026-10-18" }),
    ]);

    expect(days.map((d) => d.date)).toEqual(["2026-10-11", "2026-10-18"]);
  });

  it("preserves the server's order rather than re-sorting", () => {
    // The API sorts by date, then ministry, then the role's own display order
    // -- which is only available in SQL. Re-sorting here would be a second copy
    // of that rule, free to drift.
    const days = groupByDay([
      commitment({ assignment_id: 1, event_date: "2026-10-18" }),
      commitment({ assignment_id: 2, event_date: "2026-10-11" }),
    ]);

    expect(days.map((d) => d.date)).toEqual(["2026-10-18", "2026-10-11"]);
  });

  it("marks a day confirmed only when every commitment on it is", () => {
    const days = groupByDay([
      commitment({ assignment_id: 1, event_date: "2026-10-11" }),
      commitment({
        assignment_id: 2, event_date: "2026-10-11",
        is_confirmed: false, schedule_version_status: "DRAFT",
      }),
    ]);

    expect(days[0].allConfirmed).toBe(false);
  });

  it("marks an all-finalized day confirmed", () => {
    const days = groupByDay([
      commitment({ assignment_id: 1, event_date: "2026-10-11" }),
      commitment({ assignment_id: 2, event_date: "2026-10-11" }),
    ]);

    expect(days[0].allConfirmed).toBe(true);
  });
});

describe("hasUnconfirmed", () => {
  it("is false for a fully finalized schedule", () => {
    // The ordinary volunteer case -- they must not be shown an explanation of
    // what a draft is when none is on screen.
    expect(hasUnconfirmed([commitment({ assignment_id: 1, event_date: "2026-10-11" })])).toBe(false);
  });

  it("is true when any commitment is still a proposal", () => {
    expect(
      hasUnconfirmed([
        commitment({ assignment_id: 1, event_date: "2026-10-11" }),
        commitment({
          assignment_id: 2, event_date: "2026-10-18",
          is_confirmed: false, schedule_version_status: "DRAFT",
        }),
      ]),
    ).toBe(true);
  });

  it("is false for an empty schedule", () => {
    expect(hasUnconfirmed([])).toBe(false);
  });
});
