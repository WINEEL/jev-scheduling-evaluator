/**
 * Who is scheduled this version, and in which roles -- one compact table
 * (Task 72 final continuation replaces one large card per person), in the
 * same visual language as staffing, availability and the schedule matrix.
 *
 * A quick per-person read for a Ministry Head to scan before finalizing: how
 * often someone is serving this period, and where that load falls. This
 * reports the version being viewed only -- it says nothing about
 * qualification or experience from earlier periods. The data and its sort
 * order come entirely from `summarizeAssignmentsByPerson`; this component
 * only changes how that same information is laid out.
 */

import type { PersonAssignmentSummary } from "@/lib/assignmentsByPerson";

export function AssignmentsByPersonList({
  summaries,
}: {
  summaries: readonly PersonAssignmentSummary[];
}) {
  return (
    <div className="matrix-scroll">
      <table className="matrix matrix--wrap">
        <caption className="small">Assignments by person, most turns first</caption>
        <thead>
          <tr>
            <th scope="col">Person</th>
            <th scope="col">Total</th>
            <th scope="col">Role breakdown</th>
          </tr>
        </thead>
        <tbody>
          {summaries.map((summary, index) => (
            <tr key={`${summary.displayName}-${index}`}>
              <th scope="row" className="matrix__row-head">
                {summary.displayName}
              </th>
              <td className="matrix__total-col">{summary.total}</td>
              <td>{summary.roles.map((role) => `${role.roleLabel} ×${role.count}`).join(" · ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
