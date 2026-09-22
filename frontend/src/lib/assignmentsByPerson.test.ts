/**
 * Per-person assignment totals, tested as a plain function.
 *
 * This is the "who is carrying the load, and in which roles" reading of a
 * schedule version -- separate from `groupByEvent`'s "what does each Sunday
 * need" reading, but built from the same `AssignmentDetail[]`.
 */

import { describe, expect, it } from "vitest";

import type { AssignmentDetail } from "./api/types";
import { summarizeAssignmentsByPerson } from "./assignmentsByPerson";

function assignment(overrides: Partial<AssignmentDetail> = {}): AssignmentDetail {
  return {
    assignment_id: 501,
    requirement_id: 1,
    event_id: 31,
    membership_id: 41,
    person_id: 51,
    person_display_name: "John",
    role_id: 21,
    role_name: "Setup Lead",
    is_override: false,
    override_reason: null,
    ...overrides,
  };
}

describe("summarizeAssignmentsByPerson", () => {
  it("an empty assignment set summarizes to nothing", () => {
    expect(summarizeAssignmentsByPerson([])).toEqual([]);
  });

  it("counts one person in multiple different roles", () => {
    const [summary] = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, role_id: 21, role_name: "Setup Lead" }),
      assignment({ assignment_id: 2, role_id: 22, role_name: "Setup 4" }),
    ]);

    expect(summary.displayName).toBe("John");
    expect(summary.total).toBe(2);
    expect(summary.roles).toEqual([
      { roleLabel: "Setup 4", count: 1 },
      { roleLabel: "Setup Lead", count: 1 },
    ]);
  });

  it("counts repeated assignments in the same role once each", () => {
    const [summary] = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, role_id: 22, role_name: "Setup 4" }),
      assignment({ assignment_id: 2, role_id: 22, role_name: "Setup 4" }),
    ]);

    expect(summary.total).toBe(2);
    expect(summary.roles).toEqual([{ roleLabel: "Setup 4", count: 2 }]);
  });

  it("keeps separate people separate", () => {
    const summaries = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, person_id: 51, person_display_name: "John" }),
      assignment({ assignment_id: 2, person_id: 52, person_display_name: "Mary" }),
    ]);

    expect(summaries).toHaveLength(2);
    expect(summaries.map((entry) => entry.displayName).sort()).toEqual(["John", "Mary"]);
  });

  it("sorts people by total assignments descending, then name ascending", () => {
    const summaries = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, person_id: 51, person_display_name: "Zoe" }),
      assignment({ assignment_id: 2, person_id: 52, person_display_name: "Ann", role_id: 22, role_name: "Setup 4" }),
      assignment({ assignment_id: 3, person_id: 52, person_display_name: "Ann", role_id: 23, role_name: "Setup 5" }),
      assignment({ assignment_id: 4, person_id: 53, person_display_name: "Bob", role_id: 22, role_name: "Setup 4" }),
      assignment({ assignment_id: 5, person_id: 53, person_display_name: "Bob", role_id: 23, role_name: "Setup 5" }),
    ]);

    expect(summaries.map((entry) => entry.displayName)).toEqual(["Ann", "Bob", "Zoe"]);
  });

  it("breaks a tie in role counts by role label ascending", () => {
    const [summary] = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, role_id: 23, role_name: "Setup 5" }),
      assignment({ assignment_id: 2, role_id: 22, role_name: "Setup 4" }),
    ]);

    expect(summary.roles.map((role) => role.roleLabel)).toEqual(["Setup 4", "Setup 5"]);
  });

  it("orders roles by count descending before falling back to the label", () => {
    const [summary] = summarizeAssignmentsByPerson([
      assignment({ assignment_id: 1, role_id: 21, role_name: "Setup Lead" }),
      assignment({ assignment_id: 2, role_id: 22, role_name: "Setup 4" }),
      assignment({ assignment_id: 3, role_id: 22, role_name: "Setup 4" }),
    ]);

    expect(summary.roles).toEqual([
      { roleLabel: "Setup 4", count: 2 },
      { roleLabel: "Setup Lead", count: 1 },
    ]);
  });

  it("keeps a role visible when its name has been deleted", () => {
    const [summary] = summarizeAssignmentsByPerson([assignment({ role_name: null })]);

    expect(summary.roles).toEqual([{ roleLabel: "Unnamed role", count: 1 }]);
  });

  it("never shows a person with zero assignments -- there is nothing to build one from", () => {
    const summaries = summarizeAssignmentsByPerson([assignment({ person_id: 51 })]);

    expect(summaries.every((entry) => entry.total > 0)).toBe(true);
  });
});
