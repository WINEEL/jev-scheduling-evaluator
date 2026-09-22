/**
 * The one validation rule shared by every "blank means no value, otherwise a
 * positive whole number" field in this product -- a staffing requirement, a
 * serving limit, the event-gap rule. Blank is a real state (no requirement,
 * no limit, no rule) and is never a stand-in for zero; anything else must be
 * a whole number of 1 or more.
 *
 * Extracted once so the three screens that share this exact shape cannot
 * drift into three slightly different definitions of "valid".
 */

export interface PositiveIntegerFieldResult {
  readonly isValid: boolean;
  /** `null` for a blank (valid) field; the parsed value for a valid number.
   *  Meaningless when `isValid` is `false`. */
  readonly value: number | null;
}

export function parsePositiveIntegerField(draft: string): PositiveIntegerFieldResult {
  const trimmed = draft.trim();
  if (trimmed === "") return { isValid: true, value: null };
  const parsed = Number(trimmed);
  if (Number.isInteger(parsed) && parsed >= 1) return { isValid: true, value: parsed };
  return { isValid: false, value: null };
}

/** The text a field should start with for a given saved value -- the inverse
 *  of what a person types, used to seed a draft from loaded data. */
export function positiveIntegerFieldText(value: number | null): string {
  return value === null ? "" : String(value);
}
