/**
 * The current schedule's assignments, reshaped per person.
 *
 * A Ministry Head reviewing a draft wants to know at a glance who is
 * carrying the load this period, and in which roles -- not just a raw
 * headcount. This is a pure re-aggregation of the assignments already on
 * `ScheduleVersionDetail`; nothing here is fetched or computed on the
 * backend, and nothing here says anything about qualification, cross-role
 * experience, or history beyond the version being viewed.
 */

import type { AssignmentDetail } from "./api/types";

export interface PersonRoleCount {
  readonly roleLabel: string;
  readonly count: number;
}

export interface PersonAssignmentSummary {
  readonly displayName: string;
  readonly total: number;
  /** Highest count first, then role label ascending. */
  readonly roles: readonly PersonRoleCount[];
}

/**
 * One entry per person with at least one assignment in `assignments`,
 * sorted by total assignments descending, then display name ascending so
 * two reads of unchanged data look identical.
 */
export function summarizeAssignmentsByPerson(
  assignments: readonly AssignmentDetail[],
): PersonAssignmentSummary[] {
  const byPerson = new Map<number, { displayName: string; roleCounts: Map<string, number> }>();

  for (const assignment of assignments) {
    let entry = byPerson.get(assignment.person_id);
    if (entry === undefined) {
      entry = { displayName: assignment.person_display_name, roleCounts: new Map() };
      byPerson.set(assignment.person_id, entry);
    }
    // A role whose row was deleted still counts as a turn served.
    const roleLabel = assignment.role_name ?? "Unnamed role";
    entry.roleCounts.set(roleLabel, (entry.roleCounts.get(roleLabel) ?? 0) + 1);
  }

  const summaries = [...byPerson.values()].map(({ displayName, roleCounts }) => {
    const roles = [...roleCounts.entries()]
      .map(([roleLabel, count]) => ({ roleLabel, count }))
      .sort((left, right) => right.count - left.count || left.roleLabel.localeCompare(right.roleLabel));
    const total = roles.reduce((sum, role) => sum + role.count, 0);
    return { displayName, total, roles };
  });

  return summaries.sort(
    (left, right) => right.total - left.total || left.displayName.localeCompare(right.displayName),
  );
}
