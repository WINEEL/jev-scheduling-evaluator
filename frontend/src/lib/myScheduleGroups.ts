/**
 * Grouping the signed-in person's commitments for display (Task 77).
 *
 * A pure function, in the idiom `vitest.config.ts` states for this project:
 * the rule worth protecting is *how a list of commitments becomes a list of
 * days*, and that is a decision about data rather than markup, so it is
 * written where it can be tested directly.
 *
 * **Grouped by date, not by ministry.** Somebody reading their own schedule is
 * asking "what am I doing, and when" -- they think in Sundays, not in
 * departments. A volunteer serving in two ministries on one morning needs to
 * see that as one entry in their week with two things in it, not as two
 * separate rows in two sections they have to cross-reference.
 */

import type { MyScheduleAssignment } from "./api/types";

export interface ScheduleDay {
  /** ISO date; the key the group was formed on. */
  date: string;
  /** Every commitment on that day, in the order the API returned them. */
  assignments: MyScheduleAssignment[];
  /** True when every commitment that day is from a finalized schedule. */
  allConfirmed: boolean;
}

/**
 * Group commitments into days, preserving the server's ordering.
 *
 * The API already sorts by date and then by ministry, so this walks the list
 * once and starts a new group whenever the date changes. It deliberately does
 * **not** re-sort: the order is the server's decision, made in SQL where the
 * role's own display order is available, and re-deriving it here would be a
 * second copy of that rule that could drift.
 */
export function groupByDay(assignments: readonly MyScheduleAssignment[]): ScheduleDay[] {
  const days: ScheduleDay[] = [];

  for (const assignment of assignments) {
    const current = days[days.length - 1];
    if (current !== undefined && current.date === assignment.event_date) {
      current.assignments.push(assignment);
      current.allConfirmed = current.allConfirmed && assignment.is_confirmed;
      continue;
    }
    days.push({
      date: assignment.event_date,
      assignments: [assignment],
      allConfirmed: assignment.is_confirmed,
    });
  }

  return days;
}

/**
 * Whether any commitment in the list is still only a proposal.
 *
 * Used to decide whether the screen needs to explain what "not confirmed"
 * means at all. Somebody whose schedule is entirely finalized -- the ordinary
 * case for a volunteer -- should not be shown an explanation of drafts.
 */
export function hasUnconfirmed(assignments: readonly MyScheduleAssignment[]): boolean {
  return assignments.some((assignment) => !assignment.is_confirmed);
}
