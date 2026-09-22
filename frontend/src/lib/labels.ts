/**
 * Turning backend vocabulary into words a Ministry Head would use.
 *
 * The rule throughout: **translate, never invent**. Where the backend sends a
 * short code with an obvious plain-English meaning, this file supplies it.
 * Where it sends a written message, that message is shown as-is -- it was
 * written by the domain and knows things this layer does not. An unrecognized
 * code falls back to the code itself rather than a guess, so a new backend
 * diagnostic shows up as something a person can search for instead of
 * silently disappearing.
 */

import type { IsoDate, IsoDateTime } from "./api/types";

// -- Schedule status -------------------------------------------------------

export const STATUS_DRAFT = "DRAFT";
export const STATUS_REVIEW = "REVIEW";
export const STATUS_FINALIZED = "FINALIZED";

/** A user-facing name for a version's status. */
export function statusLabel(status: string): string {
  switch (status) {
    case STATUS_DRAFT:
      return "Draft";
    case STATUS_REVIEW:
      return "In review";
    case STATUS_FINALIZED:
      return "Finalized";
    default:
      return status;
  }
}

// -- Availability ----------------------------------------------------------

/**
 * "availability_locked_at" is a column name, not something to put in front of
 * a person. What a head needs to know is whether the period is ready.
 */
export function availabilityLabel(availabilityLockedAt: IsoDateTime | null): string {
  return availabilityLockedAt === null ? "Availability not locked" : "Availability ready";
}

export function isAvailabilityReady(availabilityLockedAt: IsoDateTime | null): boolean {
  return availabilityLockedAt !== null;
}

// -- Ministry roles (Task 53) -----------------------------------------------

/**
 * "deactivated_at" is a column name, not something to put in front of a
 * person -- the same reasoning as `isAvailabilityReady` above.
 */
export function isRoleActive(deactivatedAt: IsoDateTime | null): boolean {
  return deactivatedAt === null;
}

export function roleStatusLabel(deactivatedAt: IsoDateTime | null): string {
  return isRoleActive(deactivatedAt) ? "Active" : "Inactive";
}

// -- Role qualifications (Task 55) -------------------------------------------

/**
 * `is_qualified` is `null` (never assessed), `true`, or `false` (backend
 * §8) -- this must never collapse `null` into either boolean, the same way
 * `qualified_role_ids` on the solver side never treats "never assessed" as
 * different from an explicit `false` for eligibility, while still keeping
 * the two visually distinguishable here for a head deciding which to change.
 */
export function qualificationLabel(isQualified: boolean | null): string {
  if (isQualified === null) return "Not yet assessed";
  return isQualified ? "Qualified" : "Not qualified";
}

// -- Availability (Task 56) --------------------------------------------------

/**
 * `availability_state` is `null` (no stored row) for "no response", or one of
 * the three stored answers. This must never collapse `null` into any of
 * them -- the same "translate, never invent" rule as `qualificationLabel`.
 *
 * The generic product wording throughout: "If need be" reads as a
 * lower-priority option, never as any one ministry's specific phrase for it
 * -- it is this product's own label for the stored `BACKUP` value, not a
 * borrowed one.
 */
export function availabilityStateLabel(state: string | null): string {
  switch (state) {
    case null:
      return "No response";
    case "AVAILABLE":
      return "Available";
    case "BACKUP":
      return "If need be";
    case "UNAVAILABLE":
      return "Unavailable";
    default:
      return state;
  }
}

/** A short line explaining what "If need be" means, for a head unfamiliar
 *  with the tier -- generic wording, not any one ministry's phrase for it,
 *  and deliberately not the label text itself, so the explanation reads as a
 *  definition rather than an echo. */
export const BACKUP_EXPLANATION =
  "Available if needed; the scheduler prefers eligible people marked Available first.";

// -- Serving limits (Task 57) ---------------------------------------------

/**
 * `max_assignments` is `null` (no hard maximum recorded) or a positive
 * integer. This must never collapse `null` into `0` or any number -- the same
 * "translate, never invent" rule as `qualificationLabel` and
 * `availabilityStateLabel`.
 */
export function servingLimitLabel(maxAssignments: number | null): string {
  return maxAssignments === null ? "No limit" : String(maxAssignments);
}

/** The one sentence shown on the serving-limits page, stated once so the
 *  scope of the number is never ambiguous: this ministry, this period, a hard
 *  cap -- not church-wide, and not carried into any future period. */
export const SERVING_LIMIT_EXPLANATION =
  "The maximum number of assignments this person may receive in this ministry" +
  " for this scheduling period. It is a hard limit, and it applies only to" +
  " this period \u2014 it is not a church-wide limit and does not carry into" +
  " future periods.";

// -- Period scheduling rules (Task 71) --------------------------------------

/**
 * The user-facing name of the rule stored as `min_intervening_events`. The
 * field name is a backend/API detail (Task 72 continuation): the product
 * only ever shows this phrase, never the column name.
 */
export const EVENT_GAP_RULE_NAME = "Events to skip after serving";

/**
 * `min_intervening_events` is `null` (no rule) or a positive integer. This
 * must never collapse `null` into `0` -- the same "translate, never invent"
 * rule as `servingLimitLabel`. "No rule" and "skip zero events" would be two
 * spellings of one fact, and the backend deliberately stores only one of them.
 */
export function eventGapLabel(minInterveningEvents: number | null): string {
  if (minInterveningEvents === null) return "No rule";
  if (minInterveningEvents === 1) return "Skip 1 event";
  return `Skip ${minInterveningEvents} events`;
}

/**
 * The "Meaning" column of the compact rule table: what a configured value
 * actually does, in the fewest words that stay accurate. `1` gets the exact
 * phrasing a head would recognise ("no consecutive ministry events"); larger
 * numbers get the general form rather than a strained plural of the special
 * case.
 */
export function eventGapDescription(minInterveningEvents: number | null): string {
  if (minInterveningEvents === null) {
    return "No rule — the same person may serve two ministry events in a row.";
  }
  if (minInterveningEvents === 1) {
    return "No consecutive ministry events.";
  }
  return `${minInterveningEvents} ministry events must pass before the same person serves again.`;
}

/** The short question shown above the rule, stated once so the scope of the
 *  number is never ambiguous: this ministry's own events, counted as events
 *  rather than as days. Deliberately mentions no ministry and no weekday. */
export const EVENT_GAP_QUESTION =
  "How many of this ministry's events should someone sit out after serving?";

/** The fuller scope statement, condensed (Task 72 continuation) from a
 *  paragraph into the handful of facts a head actually needs: what unit it
 *  counts in, that enforcement reaches just past the period's own edges, and
 *  that the rule belongs to this ministry and this period alone. */
export const EVENT_GAP_EXPLANATION =
  "Counted in events, not in days — every event this ministry holds counts," +
  " so two events with nothing between them are in a row however far apart" +
  " their dates are. It also looks just outside this period: someone who" +
  " served the event before it, or already serves the event after it, is not" +
  " put on two in a row across the join. Set for this ministry and this" +
  " period only — not a church-wide rule, and does not carry into future" +
  " periods.";

/** Explains the one value a head is most likely to set, in the words they
 *  would actually use. Plain language, no ministry named. */
export const EVENT_GAP_HELP =
  "Set to 1 to prevent someone from serving two consecutive ministry events.";

// -- The two generic rules configured outside the product (Task 74) --------

/**
 * A member group's per-event limit, as a sentence a head reads.
 *
 * `null` is "no limit", never `0` -- the same "translate, never invent" rule
 * `eventGapLabel` keeps. The backend cannot store a zero, and showing one
 * would be a second spelling of a fact that already has one.
 */
export function memberGroupLimitLabel(maxPerEvent: number | null): string {
  if (maxPerEvent === null) return "No limit";
  if (maxPerEvent === 1) return "1 per service";
  return `${maxPerEvent} per service`;
}

/**
 * A same-event support requirement, as a sentence a head reads.
 *
 * It says what the rule requires and never why the ministry agreed it -- the
 * system does not know, and must not imply that it does.
 */
export function supportRequirementLabel(minSupporters: number): string {
  if (minSupporters === 1) {
    return "Must serve with at least 1 of their approved supporting members";
  }
  return `Must serve with at least ${minSupporters} of their approved supporting members`;
}

export const MEMBER_GROUP_LIMITS_HEADING = "Member group limits";

export const MEMBER_GROUP_LIMITS_EXPLANATION =
  "A member group is a category this ministry defines for its own members." +
  " A limit caps how many of that group may serve one service, counted once" +
  " per person whatever role they fill. Set for this ministry and this period" +
  " only, and hard: a service that would exceed it is left short rather than" +
  " filled.";

export const SAME_EVENT_SUPPORT_HEADING = "Same-event support requirements";

export const SAME_EVENT_SUPPORT_EXPLANATION =
  "Some members are scheduled only alongside particular others. Each rule" +
  " names one member and the members approved to serve with them, and applies" +
  " to the same service — not merely the same day. Set for this ministry and" +
  " this period only, and hard: a service without enough of the approved" +
  " members is left short rather than filled.";

export const RULES_CONFIGURED_ELSEWHERE_NOTE =
  "Shown here for reference. These two rules are configured through the API" +
  " or the local import tool, not on this screen.";

// -- The rules overview (Task 75) -------------------------------------------

/**
 * One table at the top of the scheduling-rules screen, answering the question
 * a head has before generating: *which rules is this period actually going to
 * apply, and where is each one set?*
 *
 * Every status below is derived from what the API reported, and says only what
 * can be known from it. Two rule families are named but not listed: serving
 * limits, which are per person and have their own screen; and the
 * linked-member same-date restriction, which the domain enforces but this
 * period's rules endpoint does not report at all. Both are stated as such
 * rather than shown as "none", which would be a claim the data does not make.
 */
export const RULES_OVERVIEW_HEADING = "Rules in force";

export const SERVING_LIMITS_RULE_NAME = "Serving limits";
export const LINKED_MEMBER_RULE_NAME = "Linked-member same-date restrictions";
export const MEMBER_GROUP_LIMITS_RULE_NAME = "Member group limits";
export const SAME_EVENT_SUPPORT_RULE_NAME = "Same-event support requirements";

/** Where a rule is configured, as three fixed answers. */
export const RULE_SOURCE_THIS_PAGE = "On this page";
export const RULE_SOURCE_SERVING_LIMITS = "Serving limits screen";
export const RULE_SOURCE_EXTERNAL = "API or local import tool";

export const SERVING_LIMITS_RULE_STATUS = "Set per person";
export const LINKED_MEMBER_RULE_STATUS = "Not listed here";

/** Said once, under the overview: the two things the table above cannot tell
 *  a head, so neither is mistaken for "no rule". */
export const RULES_OVERVIEW_NOTE =
  "Serving limits are per person and are shown on their own screen." +
  " Linked-member same-date restrictions are applied when they are configured," +
  " but this screen cannot list them — the rules this page reads do not include" +
  " them. Neither being absent from this table means no rule is in force.";

/**
 * How many member groups have a cap for this period.
 *
 * `undefined` means the backend did not report the field at all -- an older
 * deployment -- which is a different fact from "no group has a cap", and is
 * not collapsed into it.
 */
export function memberGroupLimitsStatus(
  groups: readonly { max_per_event: number | null }[] | undefined,
): string {
  if (groups === undefined) return "Not reported";
  if (groups.length === 0) return "No groups defined";
  const limited = groups.filter((group) => group.max_per_event !== null).length;
  if (limited === 0) return `No limits set · ${groups.length} group${groups.length === 1 ? "" : "s"}`;
  return `${limited} of ${groups.length} group${groups.length === 1 ? "" : "s"} limited`;
}

/** How many same-event support requirements this period carries. */
export function supportRequirementsStatus(
  requirements: readonly unknown[] | undefined,
): string {
  if (requirements === undefined) return "Not reported";
  if (requirements.length === 0) return "None configured";
  return `${requirements.length} configured`;
}

// -- Solver diagnostics ----------------------------------------------------

/**
 * Why a position could not be filled. These mirror the bounded vocabulary in
 * the backend's scheduling result; anything not listed is shown verbatim.
 */
const DIAGNOSTIC_TEXT: Readonly<Record<string, string>> = {
  ROLE_INACTIVE: "This role is no longer active.",
  NO_QUALIFIED_CANDIDATES: "Nobody is approved for this role.",
  ALL_UNAVAILABLE: "Everyone approved for this role said they are unavailable.",
  NO_RESPONSE_DISALLOWED:
    "The only people left have not answered, and this run did not schedule people who have not answered.",
  ALL_CHURCH_CONFLICTED: "Everyone approved is already serving elsewhere that day.",
  SAME_EVENT_CONTENTION: "The people available are already filling another role at this service.",
  INSUFFICIENT_FEASIBLE_CANDIDATES: "There are not enough eligible people to fill every position.",
  ALL_AT_PERIOD_LIMIT:
    "Everyone eligible for this role has already reached their configured maximum assignments for this scheduling period.",
  LINKED_DATE_CONFLICT:
    "Everyone otherwise eligible for this role is linked, by a configured same-date rule, to someone already scheduled that day.",
  ALL_WITHIN_EVENT_GAP:
    "Everyone otherwise eligible for this role served too recently in this ministry, or is already scheduled again too soon after it, under the rule this period sets for how many events must pass between assignments.",
  ALL_AT_GROUP_EVENT_LIMIT:
    "Everyone otherwise eligible for this role belongs to a member group that has already reached the number this period allows on one service.",
  ALL_WITHOUT_EVENT_SUPPORT:
    "Everyone otherwise eligible for this role may only serve alongside their approved supporting members, and too few of them are on this service.",
};

export function diagnosticText(code: string): string {
  return DIAGNOSTIC_TEXT[code] ?? code;
}

// -- Readiness issues ------------------------------------------------------

/**
 * A short category for a readiness issue. The backend's own `message` is
 * always shown alongside this -- the label groups, it does not replace.
 */
const READINESS_LABEL: Readonly<Record<string, string>> = {
  STALE_REQUIREMENT_SNAPSHOT: "Scheduling information changed",
  UNFILLED_REQUIREMENT: "Position not filled",
  INACTIVE_MEMBERSHIP: "Person no longer in this ministry",
  INACTIVE_PERSON: "Person no longer active",
  CANCELLED_EVENT: "Service cancelled",
  UNAUTHORIZED_BLOCKER: "Needs review",
  MISSING_OVERRIDE_AUDIT: "Needs review",
  AMBIGUOUS_OVERRIDE_AUDIT: "Needs review",
  INVALID_OVERRIDE_PAYLOAD: "Needs review",
  UNAUTHORIZED_OVERFILL: "More people than positions",
  EXCEEDS_SERVING_LIMIT: "Over serving limit",
  SAME_DATE_LINKED_MEMBER_CONFLICT: "Linked-member date conflict",
  MIN_EVENT_GAP_CONFLICT: "Scheduled again too soon",
  MEMBER_GROUP_EVENT_LIMIT_CONFLICT: "Too many from one member group",
  SAME_EVENT_SUPPORT_CONFLICT: "Missing required supporting member",
};

export function readinessLabel(code: string): string {
  return READINESS_LABEL[code] ?? code;
}

// -- Dates -----------------------------------------------------------------

/**
 * "Sunday, 4 October 2026".
 *
 * Parsed as a plain calendar date, not a timestamp: `new Date("2026-10-04")`
 * is midnight **UTC**, which in a negative-offset timezone renders as the 3rd.
 * A schedule that says the wrong Sunday is worse than one that is not
 * formatted at all.
 */
export function formatEventDate(isoDate: IsoDate): string {
  const parsed = parseCalendarDate(isoDate);
  if (parsed === null) return isoDate;
  return parsed.toLocaleDateString(undefined, {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

/** "4 Oct 2026" -- for compact places such as a period's date range. */
export function formatShortDate(isoDate: IsoDate): string {
  const parsed = parseCalendarDate(isoDate);
  if (parsed === null) return isoDate;
  return parsed.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function formatDateRange(start: IsoDate, end: IsoDate): string {
  return `${formatShortDate(start)} – ${formatShortDate(end)}`;
}

/** "Oct 11" -- a matrix column or row header needs a date that stays a fixed
 *  width across a dozen-plus columns; the year is dropped because it rarely
 *  changes within one scheduling period, and the full weekday/month form
 *  ({@link formatEventDate}) would make a wide matrix unusable. */
export function formatCompactDate(isoDate: IsoDate): string {
  const parsed = parseCalendarDate(isoDate);
  if (parsed === null) return isoDate;
  return parsed.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function parseCalendarDate(isoDate: IsoDate): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate);
  if (match === null) return null;
  const [, year, month, day] = match;
  const parsed = new Date(Number(year), Number(month) - 1, Number(day));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
