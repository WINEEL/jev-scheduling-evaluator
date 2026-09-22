/**
 * Synthetic throughout -- a ministry with roles "Lead" and "Support 2"/"Support
 * 3", on invented dates. Nothing here corresponds to any real roster.
 */

import { describe, expect, it } from "vitest";

import type { EventSummary, RoleStaffing } from "./api/types";
import { buildStaffingMatrix, staffingCellChange, staffingCellKey } from "./staffingMatrix";

function event(id: number, overrides: Partial<EventSummary> = {}): EventSummary {
  return {
    event_id: id,
    event_date: "2031-03-02",
    event_name: null,
    event_kind: "SUNDAY_SERVICE",
    cancelled_at: null,
    ...overrides,
  };
}

function role(
  id: number,
  name: string,
  displayOrder: number,
  requiredCount: number | null,
): RoleStaffing {
  return { ministry_role_id: id, name, description: null, display_order: displayOrder, required_count: requiredCount };
}

describe("buildStaffingMatrix", () => {
  it("builds one column per role, ordered by display order", () => {
    const events = [event(1)];
    const byEvent = new Map([[1, [role(2, "Support 2", 1, 5), role(1, "Lead", 0, 5)]]]);

    const matrix = buildStaffingMatrix(events, byEvent);

    expect(matrix.columns.map((c) => c.roleId)).toEqual([1, 2]);
    expect(matrix.columns.map((c) => c.name)).toEqual(["Lead", "Support 2"]);
  });

  it("builds one row per event, in the order given", () => {
    const events = [event(1), event(2)];
    const byEvent = new Map([
      [1, [role(1, "Lead", 0, 5)]],
      [2, [role(1, "Lead", 0, 5)]],
    ]);

    const matrix = buildStaffingMatrix(events, byEvent);

    expect(matrix.rows.map((r) => r.event.event_id)).toEqual([1, 2]);
  });

  it("a role a smaller event does not need shows as a blank cell, never a zero", () => {
    // The scenario this whole redesign exists for: an ordinary event needs
    // every role, a smaller-team event needs one fewer -- and the missing
    // role must render as absent, not as a staffed-with-zero-people row.
    const ordinary = event(1);
    const smallerTeam = event(2, { event_name: "Reduced crew" });
    const byEvent = new Map([
      [1, [role(1, "Lead", 0, 1), role(2, "Support 2", 1, 1), role(3, "Support 3", 2, 1)]],
      [2, [role(1, "Lead", 0, 1), role(2, "Support 2", 1, 1)]], // Support 3 absent
    ]);

    const matrix = buildStaffingMatrix([ordinary, smallerTeam], byEvent);

    const ordinaryRow = matrix.rows[0];
    const smallerRow = matrix.rows[1];
    expect(ordinaryRow.cells.every((c) => c.requiredCount === 1)).toBe(true);
    expect(ordinaryRow.total).toBe(3);

    const support3Column = matrix.columns.findIndex((c) => c.name === "Support 3");
    expect(smallerRow.cells[support3Column].requiredCount).toBeNull();
    expect(smallerRow.total).toBe(2);
  });

  it("an event with no fetched staffing yet gets an all-blank row rather than being dropped", () => {
    const matrix = buildStaffingMatrix([event(1), event(2)], new Map([[1, [role(1, "Lead", 0, 1)]]]));

    const missingRow = matrix.rows[1];
    expect(missingRow.cells).toHaveLength(matrix.columns.length);
    expect(missingRow.cells.every((c) => c.requiredCount === null)).toBe(true);
    expect(missingRow.total).toBe(0);
  });

  it("computes each row's total as the sum of its non-null cells", () => {
    const byEvent = new Map([[1, [role(1, "Lead", 0, 1), role(2, "Support 2", 1, 3)]]]);
    const matrix = buildStaffingMatrix([event(1)], byEvent);

    expect(matrix.rows[0].total).toBe(4);
  });
});

describe("staffingCellKey", () => {
  it("is stable and distinguishes event/role pairs", () => {
    expect(staffingCellKey(1, 2)).toBe(staffingCellKey(1, 2));
    expect(staffingCellKey(1, 2)).not.toBe(staffingCellKey(2, 1));
  });
});

describe("staffingCellChange", () => {
  it("a blank draft where nothing was ever required is not a change", () => {
    expect(staffingCellChange(1, 1, "", null)).toBeNull();
  });

  it("a blank draft that clears an existing requirement is a clear", () => {
    expect(staffingCellChange(1, 1, "", 5)).toEqual({ kind: "clear", eventId: 1, roleId: 1 });
  });

  it("a number matching the saved value is not a change", () => {
    expect(staffingCellChange(1, 1, "5", 5)).toBeNull();
  });

  it("a new positive number is a set", () => {
    expect(staffingCellChange(1, 1, "4", null)).toEqual({
      kind: "set", eventId: 1, roleId: 1, requiredCount: 4,
    });
    expect(staffingCellChange(1, 1, "4", 5)).toEqual({
      kind: "set", eventId: 1, roleId: 1, requiredCount: 4,
    });
  });

  it("an invalid draft is never a change -- the caller must block saving it instead", () => {
    expect(staffingCellChange(1, 1, "0", 5)).toBeNull();
    expect(staffingCellChange(1, 1, "-1", 5)).toBeNull();
    expect(staffingCellChange(1, 1, "abc", 5)).toBeNull();
  });
});
