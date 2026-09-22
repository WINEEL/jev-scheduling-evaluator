"use client";

/**
 * Availability for one scheduling period, as one compact matrix: every
 * ministry member down the side, every event across the top, and each cell a
 * single compact select carrying that person's answer -- Available, If need
 * be, Unavailable, or no response (Task 56; Task 72 continuation reshapes
 * this from one card per event with four buttons per member into one table
 * with one control per cell).
 *
 * The backend still answers per event -- `GET /events/{id}/availability` --
 * so building the matrix means fetching every event's availability once,
 * fanned out in parallel, and reshaping the result with
 * {@link buildAvailabilityMatrix}. No bulk endpoint was added for this.
 *
 * Each cell saves itself the moment it changes, exactly as the four buttons
 * it replaces did -- a `<select>`'s `onChange` only fires on an actual
 * change, so "only changed cells call the API" holds without a separate
 * dirty-tracking layer the way the staffing and serving-limit matrices need.
 * A successful save patches this page's own copy of that one event's
 * response in place, the same way the original per-card design did; it does
 * not refetch all 14+ events over again for a change to one cell.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import { clearAvailability, getEventAvailability, getPeriodEvents, setAvailability } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type {
  AvailabilityState,
  EventAvailabilityResponse,
  EventSummary,
  MembershipAvailability,
} from "@/lib/api/types";
import {
  anyCanOperate,
  anyLockedAt,
  availabilityCellClass,
  availabilitySelectValue,
  availabilityStateFromSelectValue,
  buildAvailabilityMatrix,
  type AvailabilityMatrixRow,
} from "@/lib/availabilityMatrix";
import { BACKUP_EXPLANATION, availabilityStateLabel, formatCompactDate } from "@/lib/labels";

const STATES: readonly AvailabilityState[] = ["AVAILABLE", "BACKUP", "UNAVAILABLE"];

export default function AvailabilityPage() {
  const params = useParams<{ ministryId: string; periodId: string }>();
  const ministryId = Number(params.ministryId);
  const periodId = Number(params.periodId);
  const hasValidIds =
    Number.isInteger(ministryId) && ministryId > 0 && Number.isInteger(periodId) && periodId > 0;

  const [events, setEvents] = useState<EventSummary[] | null>(null);
  const [responsesByEvent, setResponsesByEvent] = useState<Map<number, EventAvailabilityResponse> | null>(
    null,
  );
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidIds);

  const load = useCallback(
    (signal?: AbortSignal): Promise<void> =>
      getPeriodEvents(periodId, signal)
        .then(async (result) => {
          setEvents(result.events);
          if (result.events.length === 0) {
            setResponsesByEvent(new Map());
            setError(null);
            setIsLoading(false);
            return;
          }
          const responses = await Promise.all(
            result.events.map((event) => getEventAvailability(event.event_id, false, signal)),
          );
          setResponsesByEvent(new Map(responses.map((r) => [r.event_id, r])));
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
    { label: "Availability" },
  ];

  if (!hasValidIds) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Availability" }]} />
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
        <LoadingNotice what="availability" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Availability</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (events === null || responsesByEvent === null) return <UnexpectedErrorNotice />;

  /** Patch one event's response in place after a successful save -- no
   *  refetch. `updated` is `null` for a cleared answer, since
   *  {@link clearAvailability} returns nothing to patch in with. */
  function patchCell(eventId: number, membershipId: number, updated: MembershipAvailability | null) {
    setResponsesByEvent((current) => {
      if (current === null) return current;
      const response = current.get(eventId);
      if (response === undefined) return current;
      const nextMemberships = response.memberships.map((m) =>
        m.ministry_membership_id === membershipId
          ? (updated ?? { ...m, availability_state: null })
          : m,
      );
      const next = new Map(current);
      next.set(eventId, { ...response, memberships: nextMemberships });
      return next;
    });
  }

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">Availability</h1>
      <p className="page__subtitle">Who can serve at each event this period.</p>

      {responsesByEvent.size > 0 && !anyCanOperate(responsesByEvent) && (
        <ReadOnlyNotice what="who can serve at each event" />
      )}

      {events.length === 0 ? (
        <div className="notice notice--info">
          <p className="notice__title">No events yet</p>
          <p>This period has no events to record availability for.</p>
        </div>
      ) : (
        <AvailabilityMatrixEditor
          events={events}
          responsesByEvent={responsesByEvent}
          periodsHref={`/ministries/${ministryId}/periods`}
          onCellSaved={patchCell}
        />
      )}
    </>
  );
}

function AvailabilityMatrixEditor({
  events,
  responsesByEvent,
  periodsHref,
  onCellSaved,
}: {
  events: EventSummary[];
  responsesByEvent: Map<number, EventAvailabilityResponse>;
  /** Where "Finish collecting availability" actually lives -- the period card
   *  that owns the lock, which is also the only screen that can undo nothing
   *  and so is the only one that asks for confirmation. */
  periodsHref: string;
  onCellSaved: (eventId: number, membershipId: number, updated: MembershipAvailability | null) => void;
}) {
  const matrix = useMemo(() => buildAvailabilityMatrix(responsesByEvent), [responsesByEvent]);
  const isLocked = anyLockedAt(responsesByEvent) !== null;
  // Two independent reasons a cell cannot be changed, collapsed into the one
  // flag the cells already take: the period has closed, or this reader does
  // not run the ministry. The wording above each says which.
  const isReadOnly = isLocked || !anyCanOperate(responsesByEvent);

  return (
    <>
      {/* Which of the two states this period is in, said in words on both
          sides -- "still open" was previously shown as the *absence* of the
          locked notice, which is not something a person can read. */}
      {isLocked ? (
        <div className="notice notice--ok" style={{ marginBottom: "0.85rem" }} role="status">
          <p className="notice__title">Collecting availability is finished</p>
          <p>
            Every answer below is the one this period will be scheduled from. Nothing here can be
            changed now, and there is no way to reopen collection.
          </p>
        </div>
      ) : (
        <div className="notice notice--info" style={{ marginBottom: "0.85rem" }} role="status">
          <p className="notice__title">Still collecting availability</p>
          <p>
            Answers can be changed here, and each one saves as you set it. When everyone has
            answered, use <strong>Finish collecting availability</strong> on the{" "}
            <Link href={periodsHref}>scheduling periods page</Link> — a schedule cannot be started
            until then, and it cannot be undone afterwards.
          </p>
        </div>
      )}

      <ul className="matrix-legend">
        <li className="matrix-legend__item">
          <span className="matrix-legend__swatch availability-cell--available" />
          {availabilityStateLabel("AVAILABLE")}
        </li>
        <li className="matrix-legend__item">
          <span className="matrix-legend__swatch availability-cell--backup" />
          {availabilityStateLabel("BACKUP")} — {BACKUP_EXPLANATION.toLowerCase()}
        </li>
        <li className="matrix-legend__item">
          <span className="matrix-legend__swatch availability-cell--unavailable" />
          {availabilityStateLabel("UNAVAILABLE")}
        </li>
        <li className="matrix-legend__item">
          <span className="matrix-legend__swatch availability-cell--none" />
          {availabilityStateLabel(null)}
        </li>
      </ul>

      {matrix.rows.length === 0 ? (
        <p className="small muted">This ministry has no active members yet.</p>
      ) : (
        <div className="matrix-scroll">
          <table className="matrix">
            <caption className="small">Availability by person and event</caption>
            <thead>
              <tr>
                <th scope="col">Member</th>
                {events.map((event) => (
                  <th key={event.event_id} scope="col">
                    <span className="matrix__col-name">{formatCompactDate(event.event_date)}</span>
                    {event.event_name !== null && (
                      <span className="matrix__col-sub matrix__col-sub--tag">{event.event_name}</span>
                    )}
                    {event.cancelled_at !== null && <span className="matrix__col-sub">Cancelled</span>}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.rows.map((row) => (
                <AvailabilityMatrixRowView
                  key={row.membershipId}
                  row={row}
                  events={events}
                  disabled={isReadOnly}
                  onCellSaved={onCellSaved}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function AvailabilityMatrixRowView({
  row,
  events,
  disabled,
  onCellSaved,
}: {
  row: AvailabilityMatrixRow;
  events: EventSummary[];
  disabled: boolean;
  onCellSaved: (eventId: number, membershipId: number, updated: MembershipAvailability | null) => void;
}) {
  return (
    <tr>
      <th scope="row" className="matrix__row-head">
        {row.displayName}
        {row.isInactive && <span className="matrix__row-sub">Inactive</span>}
      </th>
      {events.map((event) => (
        <AvailabilityCell
          key={event.event_id}
          eventId={event.event_id}
          personLabel={row.displayName}
          eventLabel={formatCompactDate(event.event_date)}
          membershipId={row.membershipId}
          state={row.cellsByEvent.get(event.event_id) ?? null}
          disabled={disabled}
          onSaved={onCellSaved}
        />
      ))}
    </tr>
  );
}

function AvailabilityCell({
  eventId,
  membershipId,
  personLabel,
  eventLabel,
  state,
  disabled,
  onSaved,
}: {
  eventId: number;
  membershipId: number;
  personLabel: string;
  eventLabel: string;
  state: AvailabilityState | null;
  disabled: boolean;
  onSaved: (eventId: number, membershipId: number, updated: MembershipAvailability | null) => void;
}) {
  const [isSaving, setIsSaving] = useState(false);
  const [hasError, setHasError] = useState(false);

  async function handleChange(value: string) {
    const nextState = availabilityStateFromSelectValue(value);
    setIsSaving(true);
    setHasError(false);
    try {
      if (nextState === null) {
        await clearAvailability(eventId, membershipId);
        onSaved(eventId, membershipId, null);
      } else {
        const updated = await setAvailability(eventId, membershipId, nextState);
        onSaved(eventId, membershipId, updated);
      }
    } catch {
      setHasError(true);
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <td className={availabilityCellClass(state)}>
      <select
        className="matrix__cell-select"
        aria-label={`${personLabel}, ${eventLabel}: availability`}
        title={state === "BACKUP" ? BACKUP_EXPLANATION : undefined}
        value={availabilitySelectValue(state)}
        onChange={(event) => void handleChange(event.target.value)}
        disabled={isSaving || disabled}
      >
        <option value="">{availabilityStateLabel(null)}</option>
        {STATES.map((option) => (
          <option key={option} value={option}>
            {availabilityStateLabel(option)}
          </option>
        ))}
      </select>
      {hasError && (
        <span className="matrix__cell-error" title="Could not be saved">
          !
        </span>
      )}
    </td>
  );
}
