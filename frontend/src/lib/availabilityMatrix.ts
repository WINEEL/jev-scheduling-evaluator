/**
 * Reshaping per-event availability into one person-by-event matrix.
 *
 * The backend answers per event (`GET /events/{id}/availability`): one call,
 * one event's members and their answers. A person recording availability for
 * a whole period wants the other orientation -- every member down the side,
 * every event across the top -- so one person's pattern for the quarter reads
 * as one row instead of one cell on each of a dozen separate cards. This
 * module does that reshaping as plain functions, so the mapping between a
 * stored answer, a select's value, and a cell's colour is testable without a
 * page to render it in.
 */

import type { AvailabilityState, EventAvailabilityResponse, IsoDateTime } from "./api/types";

export interface AvailabilityMatrixRow {
  readonly membershipId: number;
  readonly personId: number;
  readonly displayName: string;
  readonly isInactive: boolean;
  /** This person's answer for each event, keyed by event id. An event this
   *  person has no row for at all (should not happen for an active member of
   *  the ministry, but the source is per-event responses) is simply absent
   *  from the map -- the caller reads that the same as "no response". */
  readonly cellsByEvent: ReadonlyMap<number, AvailabilityState | null>;
}

export interface AvailabilityMatrix {
  /** One row per membership that appears in at least one fetched event,
   *  ordered by display name so a roster of dozens reads as an alphabetical
   *  list rather than in whatever order the database happens to return. */
  readonly rows: readonly AvailabilityMatrixRow[];
}

export function buildAvailabilityMatrix(
  responsesByEvent: ReadonlyMap<number, EventAvailabilityResponse>,
): AvailabilityMatrix {
  interface Building {
    displayName: string;
    personId: number;
    isInactive: boolean;
    cells: Map<number, AvailabilityState | null>;
  }
  const rowsByMembership = new Map<number, Building>();

  for (const [eventId, response] of responsesByEvent) {
    for (const membership of response.memberships) {
      let row = rowsByMembership.get(membership.ministry_membership_id);
      if (row === undefined) {
        row = {
          displayName: membership.person_display_name,
          personId: membership.person_id,
          isInactive:
            membership.membership_deactivated_at !== null || membership.person_deactivated_at !== null,
          cells: new Map(),
        };
        rowsByMembership.set(membership.ministry_membership_id, row);
      }
      row.cells.set(eventId, membership.availability_state);
    }
  }

  const rows = [...rowsByMembership.entries()]
    .map(([membershipId, row]) => ({
      membershipId,
      personId: row.personId,
      displayName: row.displayName,
      isInactive: row.isInactive,
      cellsByEvent: row.cells,
    }))
    .sort((a, b) => a.displayName.localeCompare(b.displayName));

  return { rows };
}

/** The one lock instant governing every event in a period is the same value
 *  on every event's own response -- this reads it from whichever response
 *  arrived, so the page does not need a second, period-level call just to
 *  know whether editing is allowed. `null` when nothing has loaded yet. */
export function anyLockedAt(
  responsesByEvent: ReadonlyMap<number, EventAvailabilityResponse>,
): IsoDateTime | null {
  for (const response of responsesByEvent.values()) {
    if (response.availability_locked_at !== null) return response.availability_locked_at;
  }
  return null;
}

/** Whether this reader may record availability for the period these responses
 *  describe.
 *
 *  Every event in a period belongs to one ministry, so the server's answer is
 *  the same on every response -- this reads it off whichever arrived, exactly
 *  as {@link anyLockedAt} does, so the page needs no second call to know
 *  whether its controls are real.
 *
 *  **`false` when nothing has loaded**, deliberately: an empty map is not
 *  evidence of authority, and defaulting the other way would flash editable
 *  controls at a reader who has none. */
export function anyCanOperate(
  responsesByEvent: ReadonlyMap<number, EventAvailabilityResponse>,
): boolean {
  for (const response of responsesByEvent.values()) {
    return response.can_operate;
  }
  return false;
}

// -- The compact cell control -----------------------------------------------

/** A `<select>` has one string value; `""` is this matrix's spelling of "no
 *  response" (`null`), because a native `<select>` cannot carry `null`
 *  itself. Never confuse this with a *stored* fourth state -- there is not
 *  one; see `AvailabilityState` in `api/types`. */
export type AvailabilitySelectValue = "" | AvailabilityState;

export function availabilitySelectValue(state: AvailabilityState | null): AvailabilitySelectValue {
  return state ?? "";
}

/** The inverse of {@link availabilitySelectValue}: what a person chose, as
 *  the value to send the API -- `null` means "clear it back to no response". */
export function availabilityStateFromSelectValue(value: string): AvailabilityState | null {
  return value === "" ? null : (value as AvailabilityState);
}

/**
 * The CSS class that makes a cell's current state visually distinguishable
 * at a glance -- the whole point of showing 29 people and 14 events on one
 * screen instead of one card per event. Reuses this app's existing status
 * colours (ok/warning/danger) rather than inventing a fourth palette: this is
 * status, and status already has a colour language here.
 */
export function availabilityCellClass(state: AvailabilityState | null): string {
  switch (state) {
    case "AVAILABLE":
      return "availability-cell--available";
    case "BACKUP":
      return "availability-cell--backup";
    case "UNAVAILABLE":
      return "availability-cell--unavailable";
    default:
      return "availability-cell--none";
  }
}
