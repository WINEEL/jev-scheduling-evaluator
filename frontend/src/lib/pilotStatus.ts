/**
 * Which ministries a ministry head has actually sat down and validated, and
 * which are examples that have not been through that yet.
 *
 * **Why this is configuration and not code.** The backend stores no such fact:
 * a Ministry row knows its name and its members, not whether a human has
 * signed off on its scheduling configuration. Writing a ministry's name into
 * the source would be this app inventing domain knowledge -- exactly what the
 * rest of it refuses to do -- so the list is read from the environment on the
 * server and passed down to the one screen that shows it.
 *
 * **Nothing configured means nothing claimed.** With the variable unset every
 * ministry is shown plainly, with no badge and no note. That is the honest
 * default: silence, rather than an unearned claim in either direction.
 *
 * The wording is deliberately restrained. "Validated with its ministry head"
 * is a statement about one conversation that happened; it is not "production
 * ready", not "church approved", and says nothing about any other ministry.
 */

/** The environment variable naming the validated ministries, comma separated.
 *  Read on the server only -- it is not secret, but it is also not something
 *  the browser needs to be told twice. */
export const VALIDATED_MINISTRIES_ENV_VAR =
  "CHURCH_SCHEDULING_FRONTEND_PILOT_VALIDATED_MINISTRIES";

/**
 * Parse the configured list into names that can be compared to a ministry.
 *
 * Case and surrounding space are ignored, because a value typed into a `.env`
 * file is typed by a person; empty entries are dropped, so a trailing comma is
 * not a ministry called "".
 */
export function parseValidatedMinistries(raw: string | undefined): readonly string[] {
  if (raw === undefined) return [];
  return raw
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
}

/** Whether this ministry is one of the configured, validated ones. */
export function isValidatedMinistry(
  ministryName: string,
  validated: readonly string[],
): boolean {
  const wanted = ministryName.trim().toLocaleLowerCase();
  return validated.some((entry) => entry.trim().toLocaleLowerCase() === wanted);
}

/** The badge on a ministry whose configuration has been validated. */
export const VALIDATED_MINISTRY_LABEL = "Validated with its ministry head";

/** The badge on every other ministry, when any validation is configured at
 *  all. It claims nothing about the ministry except that this step has not
 *  happened for it yet. */
export const EXAMPLE_MINISTRY_LABEL = "Example · not yet validated";

/** What "validated" means, said once under the list. */
const VALIDATION_MEANING =
  "A validated ministry is one whose roles, staffing, qualifications and rules" +
  " have been checked against how that ministry actually schedules, with its" +
  " own ministry head.";

/** And what the ministries beside it are -- only said when there are any. */
const EXAMPLE_MEANING =
  " The others are set up as working examples and are waiting for the same" +
  " review.";

/**
 * The explanation under the list, matched to what is actually in it.
 *
 * A head who leads only validated ministries is not told about "the others":
 * there are none on their screen, and describing ministries they cannot see
 * would be noise at best and a claim about somebody else's ministry at worst.
 */
export function pilotStatusNote(hasUnvalidatedMinistry: boolean): string {
  return hasUnvalidatedMinistry ? VALIDATION_MEANING + EXAMPLE_MEANING : VALIDATION_MEANING;
}
