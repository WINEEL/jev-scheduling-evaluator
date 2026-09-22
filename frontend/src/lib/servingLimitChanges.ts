/**
 * Deciding which edited serving-limit rows are an actual change, so a page
 * can offer one "Save changes" action over a whole table instead of a
 * Save/Clear pair repeated on every row.
 *
 * The validation itself is the shared rule in `positiveIntegerField` --
 * this module only adds what "a change" means for one person's row: blank
 * clears an existing limit, a new number sets one, and anything matching
 * what is already saved is not a change at all.
 */

import { parsePositiveIntegerField } from "./positiveIntegerField";

export type ServingLimitChange =
  | { readonly kind: "set"; readonly membershipId: number; readonly maxAssignments: number }
  | { readonly kind: "clear"; readonly membershipId: number };

/**
 * What one edited row means to save, or `null` if it is not a change.
 *
 * `null` covers three cases that must never trigger a network call: the
 * draft is invalid (the caller blocks Save separately while any row is
 * invalid), the draft is blank and there was never a limit, or the draft
 * matches the saved maximum exactly.
 */
export function servingLimitChange(
  membershipId: number,
  draft: string,
  savedMaxAssignments: number | null,
): ServingLimitChange | null {
  const { isValid, value } = parsePositiveIntegerField(draft);
  if (!isValid || value === savedMaxAssignments) return null;
  return value === null
    ? { kind: "clear", membershipId }
    : { kind: "set", membershipId, maxAssignments: value };
}
