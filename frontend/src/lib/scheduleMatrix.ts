/**
 * Pivoting `groupByEvent`'s per-event role rows into one event-by-role
 * matrix -- the same reshaping `staffingMatrix.ts` and `availabilityMatrix.ts`
 * do for their own screens, so the schedule review page reads as the same
 * kind of table instead of the one screen still built from per-event cards.
 *
 * This adds no new fact: every cell here is exactly one of `groupByEvent`'s
 * own `RoleRow`s, restated as a column instead of a nested list. Nothing
 * about who is assigned, what is unfilled, or why is computed again -- see
 * `unfilledCountFor` and `groupByEvent` in `schedule.ts` for the one place
 * that logic lives.
 */

import type { EventGroup, RoleRow, Slot } from "./schedule";

export interface ScheduleMatrixCell {
  /** `null` when this role is not required at this event at all -- not the
   *  same as a required role with nobody in it, which is `slots` holding an
   *  `"unfilled"` entry instead. */
  readonly requirementId: number | null;
  readonly requiredCount: number;
  readonly slots: readonly Slot[];
  readonly isOverfilled: boolean;
  readonly diagnosticCodes: readonly string[];
}

export interface ScheduleMatrixRow {
  readonly eventId: number;
  readonly eventDate: string;
  readonly eventLabel: string | null;
  /** One cell per entry of {@link ScheduleMatrix.roleLabels}, in that order. */
  readonly cells: readonly ScheduleMatrixCell[];
}

export interface ScheduleMatrix {
  /** Every role label that appears on at least one event, in first-seen
   *  order -- which is already the backend's own role display order, since
   *  `groupByEvent` preserves the order its requirements arrived in. Derived
   *  entirely from this version's own data: no ministry's roles are named
   *  here or assumed to exist. */
  readonly roleLabels: readonly string[];
  /** One row per event, in the order `groups` was given. */
  readonly rows: readonly ScheduleMatrixRow[];
}

const NOT_REQUIRED_CELL: ScheduleMatrixCell = {
  requirementId: null,
  requiredCount: 0,
  slots: [],
  isOverfilled: false,
  diagnosticCodes: [],
};

export function buildScheduleMatrix(groups: readonly EventGroup[]): ScheduleMatrix {
  const roleLabels: string[] = [];
  const seen = new Set<string>();
  for (const group of groups) {
    for (const row of group.rows) {
      if (!seen.has(row.roleLabel)) {
        seen.add(row.roleLabel);
        roleLabels.push(row.roleLabel);
      }
    }
  }

  const rows = groups.map((group) => {
    const rowByLabel = new Map(group.rows.map((row) => [row.roleLabel, row]));
    const cells = roleLabels.map((label) => cellFromRow(rowByLabel.get(label)));
    return {
      eventId: group.eventId,
      eventDate: group.eventDate,
      eventLabel: group.eventLabel,
      cells,
    };
  });

  return { roleLabels, rows };
}

function cellFromRow(row: RoleRow | undefined): ScheduleMatrixCell {
  if (row === undefined) return NOT_REQUIRED_CELL;
  return {
    requirementId: row.requirementId,
    requiredCount: row.requiredCount,
    slots: row.slots,
    isOverfilled: row.isOverfilled,
    diagnosticCodes: row.diagnosticCodes,
  };
}

/** A required position with nobody in it -- what the review screen must
 *  keep impossible to miss, however the cell is styled. */
export function cellHasUnfilled(cell: ScheduleMatrixCell): boolean {
  return cell.slots.some((slot) => slot.kind === "unfilled");
}

/** This event simply does not need this role -- distinct from a required
 *  position nobody has been put in yet, which is {@link cellHasUnfilled}. */
export function cellIsNotRequired(cell: ScheduleMatrixCell): boolean {
  return cell.requiredCount === 0;
}
