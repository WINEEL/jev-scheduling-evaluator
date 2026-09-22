/**
 * Synthetic throughout -- invented roles ("Lead", "Support 2"/"Support 3"),
 * invented people, invented dates. Nothing here corresponds to a real
 * ministry or schedule.
 */

import { describe, expect, it } from "vitest";

import type { AssignmentDetail } from "./api/types";
import type { EventGroup, RoleRow, Slot } from "./schedule";
import { buildScheduleMatrix, cellHasUnfilled, cellIsNotRequired } from "./scheduleMatrix";

function assignment(name: string, overrides: Partial<AssignmentDetail> = {}): AssignmentDetail {
  return {
    assignment_id: 1,
    requirement_id: 1,
    event_id: 1,
    membership_id: 1,
    person_id: 1,
    person_display_name: name,
    role_id: 1,
    role_name: "Lead",
    is_override: false,
    override_reason: null,
    ...overrides,
  };
}

function filled(name: string): Slot {
  return { kind: "filled", assignment: assignment(name) };
}

const UNFILLED: Slot = { kind: "unfilled" };

function role(
  requirementId: number,
  roleLabel: string,
  requiredCount: number,
  slots: Slot[],
  overrides: Partial<RoleRow> = {},
): RoleRow {
  return {
    requirementId,
    roleLabel,
    requiredCount,
    assignedCount: slots.filter((s) => s.kind === "filled").length,
    slots,
    unfilledCount: slots.filter((s) => s.kind === "unfilled").length,
    isOverfilled: false,
    diagnosticCodes: [],
    ...overrides,
  };
}

function group(eventId: number, rows: RoleRow[], overrides: Partial<EventGroup> = {}): EventGroup {
  return {
    eventId,
    eventDate: "2031-03-02",
    eventLabel: null,
    rows,
    ...overrides,
  };
}

describe("buildScheduleMatrix", () => {
  it("derives role columns generically, from whichever roles the version's own events use", () => {
    const groups = [
      group(1, [role(1, "Lead", 1, [filled("Ada")]), role(2, "Support 2", 1, [filled("Ben")])]),
    ];

    const matrix = buildScheduleMatrix(groups);

    expect(matrix.roleLabels).toEqual(["Lead", "Support 2"]);
  });

  it("one row per event, in the order given", () => {
    const groups = [group(1, [role(1, "Lead", 1, [filled("Ada")])]), group(2, [])];

    const matrix = buildScheduleMatrix(groups);

    expect(matrix.rows.map((r) => r.eventId)).toEqual([1, 2]);
  });

  it("a role a smaller event does not need is not-required, never a blank fill", () => {
    const ordinary = group(1, [role(1, "Lead", 1, [filled("Ada")]), role(2, "Support 2", 1, [filled("Ben")])]);
    const smallerEvent = group(2, [role(3, "Lead", 1, [filled("Casey")])]); // no Support 2 here

    const matrix = buildScheduleMatrix([ordinary, smallerEvent]);

    const support2Index = matrix.roleLabels.indexOf("Support 2");
    const smallerRow = matrix.rows[1];
    expect(cellIsNotRequired(smallerRow.cells[support2Index])).toBe(true);
    expect(smallerRow.cells[support2Index].slots).toEqual([]);
  });

  it("a filled position carries its assignment through untouched", () => {
    const groups = [group(1, [role(1, "Lead", 1, [filled("Ada")])])];

    const matrix = buildScheduleMatrix(groups);

    expect(matrix.rows[0].cells[0].slots).toEqual([filled("Ada")]);
    expect(cellIsNotRequired(matrix.rows[0].cells[0])).toBe(false);
    expect(cellHasUnfilled(matrix.rows[0].cells[0])).toBe(false);
  });

  it("an unfilled required position is flagged, distinctly from not-required", () => {
    const groups = [group(1, [role(1, "Lead", 1, [UNFILLED])])];

    const matrix = buildScheduleMatrix(groups);
    const cell = matrix.rows[0].cells[0];

    expect(cellHasUnfilled(cell)).toBe(true);
    expect(cellIsNotRequired(cell)).toBe(false);
  });

  it("a partially filled position keeps both the person and the gap", () => {
    const groups = [group(1, [role(1, "Lead", 2, [filled("Ada"), UNFILLED])])];

    const matrix = buildScheduleMatrix(groups);
    const cell = matrix.rows[0].cells[0];

    expect(cell.slots).toHaveLength(2);
    expect(cellHasUnfilled(cell)).toBe(true);
  });

  it("preserves the overfill flag and diagnostic codes exactly as given", () => {
    const groups = [
      group(1, [
        role(1, "Lead", 1, [filled("Ada"), filled("Ben")], {
          isOverfilled: true,
          diagnosticCodes: ["SOME_CODE"],
        }),
      ]),
    ];

    const matrix = buildScheduleMatrix(groups);
    const cell = matrix.rows[0].cells[0];

    expect(cell.isOverfilled).toBe(true);
    expect(cell.diagnosticCodes).toEqual(["SOME_CODE"]);
  });

  it("carries the event's own special-event label through unchanged", () => {
    const groups = [group(1, [role(1, "Lead", 1, [filled("Ada")])], { eventLabel: "Holiday Service" })];

    const matrix = buildScheduleMatrix(groups);

    expect(matrix.rows[0].eventLabel).toBe("Holiday Service");
  });

  it("no events at all is an empty matrix, not an error", () => {
    expect(buildScheduleMatrix([])).toEqual({ roleLabels: [], rows: [] });
  });
});
