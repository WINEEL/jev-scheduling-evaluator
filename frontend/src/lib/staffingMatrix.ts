/**
 * Reshaping staffing requirements into one event-by-role matrix, and
 * deciding which edited cells are actually a change worth saving.
 *
 * The backend answers per event (`GET /events/{id}/staffing-requirements`):
 * one call, one event's roles. A person configuring a period wants the other
 * orientation -- every event down the side, every role across the top, so a
 * smaller-team week is visible as a gap in one row rather than something to
 * discover by opening events one at a time. This module does that reshaping
 * as plain functions, so the rule that matters -- blank means "not required
 * here", never zero -- is testable without a page to render it in.
 */

import type { EventSummary, RoleStaffing } from "./api/types";
import { parsePositiveIntegerField } from "./positiveIntegerField";

export interface StaffingRoleColumn {
  readonly roleId: number;
  readonly name: string;
  readonly displayOrder: number;
}

export interface StaffingCell {
  readonly roleId: number;
  /** `null` means no requirement is set for this role at this event --
   *  never `0`, which would be a different, unused fact. */
  readonly requiredCount: number | null;
}

export interface StaffingMatrixRow {
  readonly event: EventSummary;
  /** One cell per column of {@link StaffingMatrix.columns}, in that order. */
  readonly cells: readonly StaffingCell[];
  /** The sum of every non-null cell in this row. */
  readonly total: number;
}

export interface StaffingMatrix {
  /** Every active role that appears on at least one fetched event, ordered
   *  by the role's own display order. A ministry's roles are the same across
   *  its events, so this is normally just "the ministry's active roles" --
   *  but it is derived from what was actually returned, never assumed. */
  readonly columns: readonly StaffingRoleColumn[];
  /** One row per event, in the order `events` was given. */
  readonly rows: readonly StaffingMatrixRow[];
}

/**
 * Build the matrix from one staffing response per event.
 *
 * An event missing from `staffingByEvent` (still loading, or its fetch
 * failed) gets a row of every column blank -- visibly incomplete rather than
 * silently dropped from the table.
 */
export function buildStaffingMatrix(
  events: readonly EventSummary[],
  staffingByEvent: ReadonlyMap<number, readonly RoleStaffing[]>,
): StaffingMatrix {
  const columnsById = new Map<number, StaffingRoleColumn>();
  for (const roles of staffingByEvent.values()) {
    for (const role of roles) {
      if (!columnsById.has(role.ministry_role_id)) {
        columnsById.set(role.ministry_role_id, {
          roleId: role.ministry_role_id,
          name: role.name,
          displayOrder: role.display_order,
        });
      }
    }
  }
  const columns = [...columnsById.values()].sort(
    (a, b) => a.displayOrder - b.displayOrder || a.name.localeCompare(b.name),
  );

  const rows = events.map((event) => {
    const roles = staffingByEvent.get(event.event_id) ?? [];
    const requiredByRole = new Map(roles.map((r) => [r.ministry_role_id, r.required_count]));
    const cells = columns.map((column) => ({
      roleId: column.roleId,
      requiredCount: requiredByRole.get(column.roleId) ?? null,
    }));
    const total = cells.reduce((sum, cell) => sum + (cell.requiredCount ?? 0), 0);
    return { event, cells, total };
  });

  return { columns, rows };
}

/** One cell's key in a drafts map: stable, and safe to use in a `Map` or a
 *  React list key. */
export function staffingCellKey(eventId: number, roleId: number): string {
  return `${eventId}:${roleId}`;
}

export type StaffingChange =
  | { readonly kind: "set"; readonly eventId: number; readonly roleId: number; readonly requiredCount: number }
  | { readonly kind: "clear"; readonly eventId: number; readonly roleId: number };

/**
 * What one edited cell means to save, or `null` if it is not a change.
 *
 * `null` covers three cases that must never trigger a network call: the
 * draft is invalid (the caller is expected to check `parsePositiveIntegerField`
 * separately and block Save while any cell is invalid), the draft is blank
 * and there was never a requirement, or the draft matches the saved value
 * exactly.
 */
export function staffingCellChange(
  eventId: number,
  roleId: number,
  draft: string,
  savedCount: number | null,
): StaffingChange | null {
  const { isValid, value } = parsePositiveIntegerField(draft);
  if (!isValid || value === savedCount) return null;
  return value === null
    ? { kind: "clear", eventId, roleId }
    : { kind: "set", eventId, roleId, requiredCount: value };
}
