"use client";

/**
 * Staffing requirements for one scheduling period, as one compact matrix:
 * every event down the side, every active role across the top, and each
 * cell the number of people that role needs at that event (Task 54; Task 72
 * continuation reshapes this from one card per event into one table).
 *
 * The backend still answers per event -- `GET /events/{id}/staffing-requirements`
 * -- so building the matrix means fetching every event's staffing once,
 * fanned out in parallel, and reshaping the result with
 * {@link buildStaffingMatrix}. No bulk endpoint was added for this: a
 * period-sized period has a handful of events, and the existing per-event
 * calls answer this in one round of requests.
 *
 * Editing is direct: type in a cell, and it is dirty until "Save changes"
 * sends exactly the changed cells through the existing per-cell PUT/DELETE
 * endpoints -- never a Set/Clear button pair repeated on every cell. Role
 * creation has its own screen (Task 53); this page only sets and clears the
 * *count* for roles that already exist.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import { SaveChangesBar } from "@/components/SaveChangesBar";
import {
  clearStaffingRequirement,
  getEventStaffing,
  getPeriodEvents,
  setStaffingRequirement,
} from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { EventSummary, RoleStaffing } from "@/lib/api/types";
import { formatCompactDate } from "@/lib/labels";
import { parsePositiveIntegerField, positiveIntegerFieldText } from "@/lib/positiveIntegerField";
import {
  buildStaffingMatrix,
  staffingCellChange,
  staffingCellKey,
  type StaffingChange,
  type StaffingMatrix,
} from "@/lib/staffingMatrix";

export default function StaffingPage() {
  const params = useParams<{ ministryId: string; periodId: string }>();
  const ministryId = Number(params.ministryId);
  const periodId = Number(params.periodId);
  const hasValidIds =
    Number.isInteger(ministryId) && ministryId > 0 && Number.isInteger(periodId) && periodId > 0;

  const [events, setEvents] = useState<EventSummary[] | null>(null);
  const [staffingByEvent, setStaffingByEvent] = useState<Map<number, RoleStaffing[]> | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidIds);
  /** Whether this reader may change this ministry's staffing. The server's
   *  answer, carried on the period read; never derived from `is_admin`. */
  const [canOperate, setCanOperate] = useState(false);

  const load = useCallback(
    (signal?: AbortSignal): Promise<void> =>
      getPeriodEvents(periodId, signal)
        .then(async (result) => {
          setEvents(result.events);
          setCanOperate(result.can_operate);
          if (result.events.length === 0) {
            setStaffingByEvent(new Map());
            setError(null);
            setIsLoading(false);
            return;
          }
          const responses = await Promise.all(
            result.events.map((event) => getEventStaffing(event.event_id, signal)),
          );
          setStaffingByEvent(new Map(responses.map((r) => [r.event_id, r.roles])));
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [periodId],
  );

  useEffect(() => {
    if (!hasValidIds) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidIds]);

  const trail = [
    { label: "Home", href: "/" },
    { label: "Scheduling periods", href: `/ministries/${ministryId}/periods` },
    { label: "Staffing" },
  ];

  if (!hasValidIds) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Staffing" }]} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That address is not valid.</p>
        </div>
      </>
    );
  }

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <LoadingNotice what="staffing" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Staffing</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (events === null || staffingByEvent === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">Staffing</h1>
      <p className="page__subtitle">
        How many people each role needs at each event. Role creation is on the{" "}
        <Link href={`/ministries/${ministryId}/roles`}>roles page</Link>.
      </p>

      {!canOperate && <ReadOnlyNotice what="how many people each role needs" />}

      {events.length === 0 ? (
        <div className="notice notice--info">
          <p className="notice__title">No events yet</p>
          <p>This period has no events to staff.</p>
        </div>
      ) : (
        <StaffingMatrixEditor
          events={events}
          staffingByEvent={staffingByEvent}
          canOperate={canOperate}
          onReload={load}
        />
      )}
    </>
  );
}

function initialDrafts(matrix: StaffingMatrix): Map<string, string> {
  const drafts = new Map<string, string>();
  for (const row of matrix.rows) {
    for (const cell of row.cells) {
      drafts.set(staffingCellKey(row.event.event_id, cell.roleId), positiveIntegerFieldText(cell.requiredCount));
    }
  }
  return drafts;
}

function liveCellValue(eventId: number, roleId: number, drafts: Map<string, string>): number {
  const { isValid, value } = parsePositiveIntegerField(drafts.get(staffingCellKey(eventId, roleId)) ?? "");
  return isValid && value !== null ? value : 0;
}

function liveRowTotal(cells: readonly { roleId: number }[], eventId: number, drafts: Map<string, string>): number {
  return cells.reduce((sum, cell) => sum + liveCellValue(eventId, cell.roleId, drafts), 0);
}

/** Every position one role is asked for across the whole period -- the figure
 *  a head is actually budgeting when they wonder how many people they need. */
function liveColumnTotal(
  rows: readonly { event: EventSummary }[],
  roleId: number,
  drafts: Map<string, string>,
): number {
  return rows.reduce((sum, row) => sum + liveCellValue(row.event.event_id, roleId, drafts), 0);
}

function StaffingMatrixEditor({
  events,
  staffingByEvent,
  canOperate,
  onReload,
}: {
  events: EventSummary[];
  staffingByEvent: Map<number, RoleStaffing[]>;
  /** False turns the matrix into a table: the numbers are still worth
   *  reading, so the cells stay -- they simply cannot be typed into, and the
   *  save bar that would follow an edit is not rendered at all. */
  canOperate: boolean;
  onReload: () => Promise<void>;
}) {
  const matrix = useMemo(() => buildStaffingMatrix(events, staffingByEvent), [events, staffingByEvent]);

  // Owned entirely by this component from the moment it first renders --
  // never reset wholesale when `matrix` changes, so a reload after a partial
  // save can update only the cells that actually succeeded (see handleSave)
  // without discarding a still-unsaved edit sitting in a cell that failed.
  const [drafts, setDrafts] = useState<Map<string, string>>(() => initialDrafts(matrix));
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [failedKeys, setFailedKeys] = useState<ReadonlySet<string>>(new Set());

  const changes = useMemo(() => {
    const list: StaffingChange[] = [];
    for (const row of matrix.rows) {
      for (const cell of row.cells) {
        const key = staffingCellKey(row.event.event_id, cell.roleId);
        const change = staffingCellChange(row.event.event_id, cell.roleId, drafts.get(key) ?? "", cell.requiredCount);
        if (change !== null) list.push(change);
      }
    }
    return list;
  }, [matrix, drafts]);

  const dirtyKeys = useMemo(() => new Set(changes.map((c) => staffingCellKey(c.eventId, c.roleId))), [changes]);

  const hasInvalidDraft = useMemo(
    () => [...drafts.values()].some((draft) => !parsePositiveIntegerField(draft).isValid),
    [drafts],
  );

  function setCellDraft(eventId: number, roleId: number, value: string) {
    setDrafts((current) => {
      const next = new Map(current);
      next.set(staffingCellKey(eventId, roleId), value);
      return next;
    });
  }

  function handleDiscard() {
    setDrafts(initialDrafts(matrix));
    setFailedKeys(new Set());
    setSaveError(null);
  }

  async function handleSave() {
    setIsSaving(true);
    setSaveError(null);
    const results = await Promise.allSettled(
      changes.map((change) =>
        change.kind === "set"
          ? setStaffingRequirement(change.eventId, change.roleId, change.requiredCount)
          : clearStaffingRequirement(change.eventId, change.roleId),
      ),
    );

    // Only the cells that actually saved move their draft to the new saved
    // text; a cell that failed keeps exactly what the person typed, so nothing
    // they entered is lost and Save can be tried again for just that cell.
    setDrafts((current) => {
      const next = new Map(current);
      results.forEach((result, index) => {
        if (result.status !== "fulfilled") return;
        const change = changes[index];
        const savedValue = change.kind === "set" ? change.requiredCount : null;
        next.set(staffingCellKey(change.eventId, change.roleId), positiveIntegerFieldText(savedValue));
      });
      return next;
    });

    const failed = new Set<string>();
    let firstError: unknown = null;
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        const change = changes[index];
        failed.add(staffingCellKey(change.eventId, change.roleId));
        if (firstError === null) firstError = result.reason;
      }
    });
    setFailedKeys(failed);
    setSaveError(firstError);
    setIsSaving(false);

    if (failed.size < changes.length) {
      await onReload();
    }
  }

  const saveApiError = asApiError(saveError);

  return (
    <>
      {canOperate && (
      <SaveChangesBar
        dirtyCount={changes.length}
        isSaving={isSaving}
        canSave={!hasInvalidDraft}
        onSave={() => void handleSave()}
        onDiscard={handleDiscard}
        error={
          saveError !== null ? (
            <div style={{ marginTop: "0.5rem" }}>
              {saveApiError !== null ? (
                <ErrorNotice
                  error={saveApiError}
                  title={
                    failedKeys.size === changes.length
                      ? "Nothing could be saved"
                      : `${failedKeys.size} of ${changes.length} change(s) could not be saved`
                  }
                />
              ) : (
                <UnexpectedErrorNotice />
              )}
            </div>
          ) : undefined
        }
      />
      )}

      <div className="matrix-scroll">
        <table className="matrix">
          <caption className="small">Required positions by event and role</caption>
          <thead>
            <tr>
              <th scope="col">Event</th>
              {matrix.columns.map((column) => (
                <th key={column.roleId} scope="col">
                  {column.name}
                </th>
              ))}
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {matrix.rows.map((row) => {
              const isCancelled = row.event.cancelled_at !== null;
              return (
                <tr key={row.event.event_id}>
                  <th scope="row" className="matrix__row-head">
                    {formatCompactDate(row.event.event_date)}
                    {row.event.event_name !== null && (
                      <span className="matrix__row-sub">
                        <span className="matrix__row-sub--tag">{row.event.event_name}</span>
                      </span>
                    )}
                    {isCancelled && (
                      <span className="matrix__row-sub">
                        <span className="badge badge--warning">Cancelled</span>
                      </span>
                    )}
                  </th>
                  {matrix.columns.map((column) => {
                    const key = staffingCellKey(row.event.event_id, column.roleId);
                    const draft = drafts.get(key) ?? "";
                    const { isValid } = parsePositiveIntegerField(draft);
                    const isDirty = dirtyKeys.has(key);
                    return (
                      <td key={column.roleId}>
                        <input
                          className={[
                            "matrix__cell-input",
                            isDirty ? "matrix__cell-input--dirty" : "",
                            !isValid ? "matrix__cell-input--invalid" : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          type="number"
                          min={1}
                          inputMode="numeric"
                          placeholder="—"
                          value={draft}
                          disabled={isSaving}
                          readOnly={!canOperate}
                          onChange={(event) => setCellDraft(row.event.event_id, column.roleId, event.target.value)}
                          aria-label={`${column.name} required on ${formatCompactDate(row.event.event_date)}`}
                        />
                        {failedKeys.has(key) && (
                          <span className="matrix__cell-error" title="Could not be saved">
                            !
                          </span>
                        )}
                      </td>
                    );
                  })}
                  <td className="matrix__total-col">{liveRowTotal(row.cells, row.event.event_id, drafts)}</td>
                </tr>
              );
            })}
          </tbody>
          {/* The period's own totals: one per role, and the whole staffing
              figure for the period in the corner. Both follow the cells as
              they are edited, so a change to one event shows its effect on
              the period immediately. */}
          <tfoot>
            <tr>
              <th scope="row" className="matrix__row-head">
                All events
              </th>
              {matrix.columns.map((column) => (
                <td className="matrix__total-col" key={column.roleId}>
                  {liveColumnTotal(matrix.rows, column.roleId, drafts)}
                </td>
              ))}
              <td className="matrix__total-col">
                {matrix.rows.reduce(
                  (sum, row) => sum + liveRowTotal(row.cells, row.event.event_id, drafts),
                  0,
                )}
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      <p className="small muted" style={{ marginTop: "0.6rem" }}>
        A blank cell means that role is not required at that event — not zero, and an event asking for
        fewer roles than the others is exactly what a special event usually looks like. Cancelled events
        keep any existing requirement visible but cannot take a new one.
      </p>
    </>
  );
}
