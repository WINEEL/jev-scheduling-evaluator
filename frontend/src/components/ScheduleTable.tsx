/**
 * The schedule itself: one compact matrix, an event per row and a role per
 * column, in the same visual language as staffing and availability (Task 72
 * final continuation reshapes this from one card and table per event).
 *
 * Role columns come from {@link buildScheduleMatrix}, derived generically
 * from whichever roles this version's own events use -- nothing here names
 * a ministry's roles, so a different ministry's different roles produce a
 * different set of columns with no code change.
 *
 * A gap is shown as an explicit "Unfilled" line and a highlighted cell,
 * rather than an absence, because the thing a leader is looking for on this
 * screen is what still needs a person. Where the last generation run said
 * *why* a position could not be filled, that reason is shown under it.
 */

import { buildScheduleMatrix, cellHasUnfilled, cellIsNotRequired, type ScheduleMatrixCell } from "@/lib/scheduleMatrix";
import type { EventGroup } from "@/lib/schedule";
import { diagnosticText, formatCompactDate } from "@/lib/labels";

export function ScheduleTable({ groups }: { groups: readonly EventGroup[] }) {
  const matrix = buildScheduleMatrix(groups);

  const unfilledCells = matrix.rows.reduce(
    (count, row) => count + row.cells.filter((cell) => cellHasUnfilled(cell)).length,
    0,
  );

  return (
    <>
      {/* One line, above the matrix rather than inside a cell, so the tint on
          an unfilled cell is explained before it is met. Shown only when there
          is something to explain. */}
      {unfilledCells > 0 && (
        <ul className="matrix-legend">
          <li className="matrix-legend__item">
            <span className="matrix-legend__swatch schedule-cell--unfilled" />
            A position nobody could be scheduled for — labelled “Unfilled”, with the reason where
            the last run gave one
          </li>
        </ul>
      )}
      <div className="matrix-scroll matrix-scroll--feature">
        <table className="matrix matrix--wrap">
          <caption className="small">
            Every event in this period, in date order, and who is assigned to each role
          </caption>
          <thead>
            <tr>
              <th scope="col">Event</th>
              {matrix.roleLabels.map((label) => (
                <th key={label} scope="col">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matrix.rows.map((row) => (
              <tr key={row.eventId}>
                <th scope="row" className="matrix__row-head">
                  {formatCompactDate(row.eventDate)}
                  {row.eventLabel !== null && (
                    <span className="matrix__row-sub">
                      <span className="matrix__row-sub--tag">{row.eventLabel}</span>
                    </span>
                  )}
                </th>
                {row.cells.map((cell, index) => (
                  <td
                    key={`${row.eventId}-${index}`}
                    className={cellHasUnfilled(cell) ? "schedule-cell--unfilled" : undefined}
                  >
                    <ScheduleCell cell={cell} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function ScheduleCell({ cell }: { cell: ScheduleMatrixCell }) {
  if (cellIsNotRequired(cell)) {
    return (
      <span className="schedule-cell__blank" aria-hidden="true">
        —
      </span>
    );
  }

  return (
    <>
      {cell.slots.map((slot, index) => (
        <div className="schedule-cell__slot" key={index}>
          {slot.kind === "filled" ? (
            <>
              <span>{slot.assignment.person_display_name}</span>
              {slot.assignment.is_override && (
                <>
                  {" "}
                  <span className="badge badge--warning">Override</span>
                  {slot.assignment.override_reason !== null && (
                    <p className="override-reason">{slot.assignment.override_reason}</p>
                  )}
                </>
              )}
            </>
          ) : (
            <span className="unfilled">Unfilled</span>
          )}
        </div>
      ))}
      {cell.isOverfilled && (
        <span className="badge badge--warning" style={{ marginTop: "0.2rem", display: "inline-block" }}>
          Extra person
        </span>
      )}
      {cellHasUnfilled(cell) && cell.diagnosticCodes.length > 0 && (
        <p className="diagnostic">{cell.diagnosticCodes.map((code) => diagnosticText(code)).join(" ")}</p>
      )}
    </>
  );
}
