/**
 * The decisions each screen makes, tested as plain functions.
 *
 * These are the rules a person actually experiences -- whether the Start
 * button appears, how many gaps a Sunday shows, whether an overfilled role
 * quietly cancels out an empty one -- so they live outside React and are
 * checked directly rather than through a rendered tree.
 */

import { describe, expect, it } from "vitest";

import type {
  AssignmentDetail,
  RequirementDetail,
  ScheduleVersionDetail,
  SchedulingPeriodSummary,
} from "./api/types";
import { availabilityLabel, statusLabel } from "./labels";
import { canStartSchedule, groupByEvent, openableVersionId, unfilledCountFor } from "./schedule";

function period(overrides: Partial<SchedulingPeriodSummary> = {}): SchedulingPeriodSummary {
  return {
    scheduling_period_id: 12,
    name: "Q4 2026",
    start_date: "2026-10-04",
    end_date: "2026-12-27",
    availability_locked_at: "2026-09-30T12:00:00Z",
    schedule: null,
    ...overrides,
  };
}

function requirement(overrides: Partial<RequirementDetail> = {}): RequirementDetail {
  return {
    requirement_id: 1,
    event_id: 31,
    event_date: "2026-10-04",
    event_name: "Sunday Service",
    event_kind: "SUNDAY_SERVICE",
    role_id: 21,
    role_name: "Setup Lead",
    required_count: 1,
    assigned_count: 0,
    ...overrides,
  };
}

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

function detail(
  requirements: RequirementDetail[],
  assignments: AssignmentDetail[] = [],
): ScheduleVersionDetail {
  return {
    // Task 80. Irrelevant to grouping, which reshapes the schedule and has no
    // opinion about who may change it -- present because the payload carries
    // it.
    can_operate: true,
    schedule_version: {
      id: 21,
      schedule_id: 8,
      scheduling_period_id: 12,
      version_number: 1,
      status: "DRAFT",
      finalized_at: null,
      amends_version_id: null,
      amendment_reason: null,
      notes: null,
    },
    period: {
      id: 12,
      name: "Q4 2026",
      ministry_id: 5,
      ministry_name: "Setup",
      start_date: "2026-10-04",
      end_date: "2026-12-27",
      availability_locked_at: "2026-09-30T12:00:00Z",
    },
    requirements,
    assignments,
    summary: {
      required_positions: 0,
      assigned_positions: 0,
      unfilled_positions: 0,
      is_fully_staffed: true,
    },
    staleness: { is_stale: false, current_only: [], snapshot_only: [] },
    finalization_readiness: { is_ready: true, issues: [] },
  };
}

// -- 18-20: starting and opening -------------------------------------------

describe("starting a schedule", () => {
  it("18. a period whose availability is not ready cannot be started", () => {
    const notReady = period({ availability_locked_at: null });

    expect(canStartSchedule(notReady)).toBe(false);
    expect(availabilityLabel(notReady.availability_locked_at)).toBe("Availability not locked");
  });

  it("19. a ready, unscheduled period can be started", () => {
    const ready = period();

    expect(canStartSchedule(ready)).toBe(true);
    expect(availabilityLabel(ready.availability_locked_at)).toBe("Availability ready");
    expect(openableVersionId(ready)).toBeNull();
  });

  it("20. a period that already has a schedule opens its latest version instead", () => {
    const started = period({
      schedule: {
        schedule_id: 8,
        latest_version_id: 77,
        latest_version_number: 3,
        latest_version_status: "REVIEW",
      },
    });

    expect(canStartSchedule(started)).toBe(false);
    expect(openableVersionId(started)).toBe(77);
  });

  it("20b. a schedule row with no version yet offers nothing to open, and no restart", () => {
    const odd = period({
      schedule: {
        schedule_id: 8,
        latest_version_id: null,
        latest_version_number: null,
        latest_version_status: null,
      },
    });

    expect(openableVersionId(odd)).toBeNull();
    // Still not startable: a schedule exists, so the backend would refuse.
    expect(canStartSchedule(odd)).toBe(false);
  });
});

// -- 21-22, 25: status -----------------------------------------------------

describe("status", () => {
  // 21-22, "which statuses may be generated into", moved to
  // `lifecycle.test.ts` with the rule itself: since Task 80 the answer also
  // depends on whether the reader runs the ministry, so a status-only
  // predicate could no longer state it.

  it("25. statuses are shown in user-facing words", () => {
    expect(statusLabel("DRAFT")).toBe("Draft");
    expect(statusLabel("REVIEW")).toBe("In review");
    expect(statusLabel("FINALIZED")).toBe("Finalized");
  });

  it("25b. an unrecognized status is shown as itself rather than hidden", () => {
    expect(statusLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
  });
});

// -- 23-24: unfilled positions ---------------------------------------------

describe("unfilled positions", () => {
  it("23. a partly staffed role shows one gap per missing position", () => {
    const groups = groupByEvent(
      detail([requirement({ required_count: 3, assigned_count: 1 })], [assignment()]),
    );

    const row = groups[0].rows[0];
    expect(row.unfilledCount).toBe(2);
    expect(row.slots).toHaveLength(3);
    expect(row.slots.filter((slot) => slot.kind === "unfilled")).toHaveLength(2);
    expect(row.slots[0]).toMatchObject({ kind: "filled" });
  });

  it("23b. a fully staffed role shows no gaps", () => {
    const groups = groupByEvent(
      detail([requirement({ required_count: 1, assigned_count: 1 })], [assignment()]),
    );

    expect(groups[0].rows[0].unfilledCount).toBe(0);
    expect(groups[0].rows[0].slots).toHaveLength(1);
  });

  it("23c. an entirely unstaffed role is all gaps", () => {
    const groups = groupByEvent(detail([requirement({ required_count: 2, assigned_count: 0 })]));

    expect(groups[0].rows[0].unfilledCount).toBe(2);
    expect(groups[0].rows[0].slots.every((slot) => slot.kind === "unfilled")).toBe(true);
  });

  it("24. an overfilled role never produces a negative gap", () => {
    expect(unfilledCountFor({ required_count: 1, assigned_count: 3 })).toBe(0);

    const groups = groupByEvent(
      detail(
        [requirement({ required_count: 1, assigned_count: 2 })],
        [assignment({ assignment_id: 1, person_display_name: "Ann" }), assignment({ assignment_id: 2, person_display_name: "Bob" })],
      ),
    );

    const row = groups[0].rows[0];
    expect(row.unfilledCount).toBe(0);
    expect(row.isOverfilled).toBe(true);
    expect(row.slots).toHaveLength(2);
    expect(row.slots.every((slot) => slot.kind === "filled")).toBe(true);
  });

  it("24b. an overfill on one role cannot hide a gap on another", () => {
    const groups = groupByEvent(
      detail(
        [
          requirement({ requirement_id: 1, role_id: 21, required_count: 1, assigned_count: 3 }),
          requirement({ requirement_id: 2, role_id: 22, role_name: "Setup 2", required_count: 2, assigned_count: 0 }),
        ],
        [
          assignment({ assignment_id: 1, requirement_id: 1, person_display_name: "Ann" }),
          assignment({ assignment_id: 2, requirement_id: 1, person_display_name: "Bob" }),
          assignment({ assignment_id: 3, requirement_id: 1, person_display_name: "Cal" }),
        ],
      ),
    );

    const totalUnfilled = groups[0].rows.reduce((sum, row) => sum + row.unfilledCount, 0);
    expect(totalUnfilled).toBe(2);
  });
});

// -- grouping --------------------------------------------------------------

describe("grouping", () => {
  it("puts each Sunday's roles together, in the order the backend sent them", () => {
    const groups = groupByEvent(
      detail([
        requirement({ requirement_id: 1, event_id: 31, event_date: "2026-10-04", role_name: "Setup Lead" }),
        requirement({ requirement_id: 2, event_id: 31, event_date: "2026-10-04", role_name: "Setup 2" }),
        requirement({ requirement_id: 3, event_id: 32, event_date: "2026-10-11", role_name: "Setup Lead" }),
      ]),
    );

    expect(groups).toHaveLength(2);
    expect(groups[0].eventDate).toBe("2026-10-04");
    expect(groups[0].rows.map((row) => row.roleLabel)).toEqual(["Setup Lead", "Setup 2"]);
    expect(groups[1].eventDate).toBe("2026-10-11");
  });

  it("matches each assignment to its own requirement", () => {
    const groups = groupByEvent(
      detail(
        [
          requirement({ requirement_id: 1, role_name: "Setup Lead" }),
          requirement({ requirement_id: 2, role_name: "Setup 2" }),
        ],
        [
          assignment({ assignment_id: 1, requirement_id: 2, person_display_name: "Mary" }),
          assignment({ assignment_id: 2, requirement_id: 1, person_display_name: "John" }),
        ],
      ),
    );

    const [lead, second] = groups[0].rows;
    expect(lead.slots[0]).toMatchObject({ kind: "filled", assignment: { person_display_name: "John" } });
    expect(second.slots[0]).toMatchObject({ kind: "filled", assignment: { person_display_name: "Mary" } });
  });

  it("keeps a role visible when its name has been deleted", () => {
    const groups = groupByEvent(detail([requirement({ role_name: null })]));

    expect(groups[0].rows).toHaveLength(1);
    expect(groups[0].rows[0].roleLabel).toBe("Unnamed role");
  });

  it("attaches generation diagnostics to the requirement they belong to", () => {
    const groups = groupByEvent(
      detail([
        requirement({ requirement_id: 1, required_count: 1, assigned_count: 0 }),
        requirement({ requirement_id: 2, role_name: "Setup 2", required_count: 1, assigned_count: 0 }),
      ]),
      [{ requirement_id: 2, missing_count: 1, diagnostic_codes: ["NO_QUALIFIED_CANDIDATES"] }],
    );

    expect(groups[0].rows[0].diagnosticCodes).toEqual([]);
    expect(groups[0].rows[1].diagnosticCodes).toEqual(["NO_QUALIFIED_CANDIDATES"]);
  });

  it("an empty schedule groups into nothing rather than failing", () => {
    expect(groupByEvent(detail([]))).toEqual([]);
  });
});
