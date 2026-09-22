/**
 * Turning the schedule detail payload into something a page can render.
 *
 * The backend answers in two flat lists -- requirements, and assignments --
 * because that is what it stores. A person reads a schedule the other way
 * round: one Sunday at a time, each role under it, each position either
 * filled by somebody or visibly empty. This module does that reshaping, as
 * plain functions with no React in them, so the rules that matter are
 * testable on their own.
 */

import type {
  AssignmentDetail,
  RequirementDetail,
  ScheduleVersionDetail,
  SchedulingPeriodSummary,
  UnfilledRequirementReport,
} from "./api/types";
import { isAvailabilityReady } from "./labels";

/** One position within a role: either a person, or a gap. */
export type Slot =
  | { readonly kind: "filled"; readonly assignment: AssignmentDetail }
  | { readonly kind: "unfilled" };

export interface RoleRow {
  readonly requirementId: number;
  readonly roleLabel: string;
  readonly requiredCount: number;
  readonly assignedCount: number;
  /** Filled positions first, then one entry per gap. */
  readonly slots: readonly Slot[];
  /** `required - assigned`, never below zero. */
  readonly unfilledCount: number;
  /** True when more people are assigned than the role asked for. */
  readonly isOverfilled: boolean;
  /** Reasons from the most recent generation run, if any apply here. */
  readonly diagnosticCodes: readonly string[];
}

export interface EventGroup {
  readonly eventId: number;
  readonly eventDate: string;
  readonly eventLabel: string | null;
  readonly rows: readonly RoleRow[];
}

/**
 * How many positions are still empty for one requirement.
 *
 * **Floored at zero.** An authorized overfill means more assignments than the
 * role asked for; subtracting would produce a negative "gap" that, summed
 * across a schedule, would cancel out a genuinely empty Sunday and report the
 * whole thing as staffed.
 */
export function unfilledCountFor(requirement: {
  required_count: number;
  assigned_count: number;
}): number {
  return Math.max(requirement.required_count - requirement.assigned_count, 0);
}

/**
 * Group a schedule into Sundays, each listing its roles.
 *
 * Ordering follows the backend's, which is already deterministic (snapshot
 * date, then event, then the role's own display order). Assignments are
 * matched to their requirement by id, and sorted by person name so two reads
 * of unchanged data look identical.
 *
 * `generationReports` is optional: after a generation run the caller can pass
 * that run's unfilled reports so each gap can say *why*. Without it the gaps
 * are still shown -- just without an explanation the API did not provide.
 */
export function groupByEvent(
  detail: ScheduleVersionDetail,
  generationReports: readonly UnfilledRequirementReport[] = [],
): EventGroup[] {
  const assignmentsByRequirement = new Map<number, AssignmentDetail[]>();
  for (const assignment of detail.assignments) {
    const existing = assignmentsByRequirement.get(assignment.requirement_id);
    if (existing === undefined) {
      assignmentsByRequirement.set(assignment.requirement_id, [assignment]);
    } else {
      existing.push(assignment);
    }
  }

  const diagnosticsByRequirement = new Map<number, readonly string[]>();
  for (const report of generationReports) {
    diagnosticsByRequirement.set(report.requirement_id, report.diagnostic_codes);
  }

  const groups = new Map<number, { group: EventGroup; rows: RoleRow[] }>();

  for (const requirement of detail.requirements) {
    const assigned = [...(assignmentsByRequirement.get(requirement.requirement_id) ?? [])].sort(
      (left, right) =>
        left.person_display_name.localeCompare(right.person_display_name) ||
        left.assignment_id - right.assignment_id,
    );

    const row = buildRow(
      requirement,
      assigned,
      diagnosticsByRequirement.get(requirement.requirement_id) ?? [],
    );

    const existing = groups.get(requirement.event_id);
    if (existing === undefined) {
      const rows: RoleRow[] = [row];
      groups.set(requirement.event_id, {
        rows,
        group: {
          eventId: requirement.event_id,
          eventDate: requirement.event_date,
          eventLabel: requirement.event_name,
          rows,
        },
      });
    } else {
      existing.rows.push(row);
    }
  }

  return [...groups.values()].map((entry) => entry.group);
}

function buildRow(
  requirement: RequirementDetail,
  assigned: readonly AssignmentDetail[],
  diagnosticCodes: readonly string[],
): RoleRow {
  const unfilledCount = unfilledCountFor(requirement);
  const slots: Slot[] = assigned.map((assignment) => ({ kind: "filled", assignment }));
  for (let index = 0; index < unfilledCount; index += 1) {
    slots.push({ kind: "unfilled" });
  }

  return {
    requirementId: requirement.requirement_id,
    // A role whose row was deleted still has positions to show.
    roleLabel: requirement.role_name ?? "Unnamed role",
    requiredCount: requirement.required_count,
    assignedCount: requirement.assigned_count,
    slots,
    unfilledCount,
    isOverfilled: requirement.assigned_count > requirement.required_count,
    diagnosticCodes,
  };
}

// -- Scheduling-period decisions -------------------------------------------

/**
 * A period can be started only when nothing has been started for it yet and
 * availability is ready. Both halves matter: the first because this release
 * starts first schedules only, the second because the backend refuses
 * otherwise and a button that always fails is worse than no button.
 */
export function canStartSchedule(period: SchedulingPeriodSummary): boolean {
  return period.schedule === null && isAvailabilityReady(period.availability_locked_at);
}

/** The version to open for a period that has been started, if there is one. */
export function openableVersionId(period: SchedulingPeriodSummary): number | null {
  return period.schedule?.latest_version_id ?? null;
}
