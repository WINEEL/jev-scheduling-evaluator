"use client";

/**
 * A ministry's scheduling periods, and the one action that starts scheduling.
 *
 * Each period says three things a leader needs before doing anything: when it
 * runs, whether availability is ready, and whether scheduling has started. A
 * period that has been started offers to open its schedule; one that has not,
 * and is ready, offers to start it; one that is not ready yet offers the one
 * step that makes it ready -- closing availability collection.
 *
 * That last action is the only thing on this page that changes a period, and
 * it is here rather than on the availability screen because this is where its
 * absence was a dead end: "availability must be ready" was stated next to a
 * disabled button with nothing anywhere in the product that could make it so.
 * Individual answers are still edited on the availability screen, and staffing
 * and the scheduling rules on their own.
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import { getSchedulingPeriods, lockAvailability, startFirstSchedule } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MinistrySchedulingPeriods, SchedulingPeriodSummary } from "@/lib/api/types";
import { availabilityLabel, formatDateRange, isAvailabilityReady, statusLabel } from "@/lib/labels";
import { canStartSchedule, openableVersionId } from "@/lib/schedule";

export default function SchedulingPeriodsPage() {
  const params = useParams<{ ministryId: string }>();
  const ministryId = Number(params.ministryId);
  // Derived during render, never in an effect: whether the URL names a usable
  // id is a pure function of the URL, and starting `isLoading` at the right
  // value avoids a cascading render just to correct it.
  const hasValidId = Number.isInteger(ministryId) && ministryId > 0;

  const [data, setData] = useState<MinistrySchedulingPeriods | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidId);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getSchedulingPeriods(ministryId, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          // An abort is not an outcome: this component is unmounting, or a
          // newer request has replaced this one and is still in flight. The
          // loading flag is deliberately left alone -- clearing it here would
          // announce "finished, with no data and no error", which renders as
          // a permanent failure for a request that never got to answer.
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [ministryId],
  );

  useEffect(() => {
    if (!hasValidId) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidId]);

  if (!hasValidId) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Scheduling periods" }]} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That ministry address is not valid.</p>
        </div>
      </>
    );
  }

  const trail = [
    { label: "Home", href: "/" },
    { label: data?.ministry_name ?? "Scheduling periods" },
  ];

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <LoadingNotice what="scheduling periods" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Scheduling periods</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (data === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{data.ministry_name}</h1>
      <p className="page__subtitle">Scheduling periods</p>

      {!data.can_operate && <ReadOnlyNotice what="this ministry's schedules" />}

      {data.periods.length === 0 ? (
        <div className="notice notice--info">
          <p className="notice__title">No scheduling periods yet</p>
          <p>
            This ministry has no scheduling periods set up. Creating one is not available in
            this version.
          </p>
        </div>
      ) : (
        <ul className="list-reset">
          {data.periods.map((period) => (
            <PeriodCard
              key={period.scheduling_period_id}
              period={period}
              ministryId={ministryId}
              canOperate={data.can_operate}
              onChanged={() => load()}
            />
          ))}
        </ul>
      )}
    </>
  );
}

/**
 * The five configuration screens that feed one generation run, in the order a
 * ministry head works through them. Nothing here is ministry-specific: the
 * hints describe what each screen is for, and name no role, event or person.
 */
function workflowSteps(
  ministryId: number,
  periodId: number,
): readonly { href: string; label: string; hint: string }[] {
  const period = `/ministries/${ministryId}/periods/${periodId}`;
  return [
    {
      href: `${period}/staffing`,
      label: "Events and staffing",
      hint: "How many people each role needs at each event",
    },
    {
      href: `${period}/availability`,
      label: "Availability",
      hint: "Who can serve at each event, and who cannot",
    },
    {
      href: `/ministries/${ministryId}/roles`,
      label: "Roles and qualifications",
      hint: "Which roles exist, and who is approved for each",
    },
    {
      href: `${period}/serving-limits`,
      label: "Serving limits",
      hint: "The most turns any one person may take this period",
    },
    {
      href: `${period}/scheduling-rules`,
      label: "Scheduling rules",
      hint: "The hard rules this period's schedule must obey",
    },
  ];
}

function PeriodCard({
  period,
  ministryId,
  canOperate,
  onChanged,
}: {
  period: SchedulingPeriodSummary;
  ministryId: number;
  /** Whether this reader may run this ministry. The server's answer, and the
   *  same one it will apply to every request the buttons below would make. */
  canOperate: boolean;
  /** Re-read the list. Locking availability changes what this card may do. */
  onChanged: () => Promise<void> | void;
}) {
  const router = useRouter();
  const [isStarting, setIsStarting] = useState(false);
  const [startError, setStartError] = useState<unknown>(null);

  const versionId = openableVersionId(period);
  const availabilityReady = isAvailabilityReady(period.availability_locked_at);
  const canStart = canStartSchedule(period);

  async function handleStart() {
    setIsStarting(true);
    setStartError(null);
    try {
      const started = await startFirstSchedule(period.scheduling_period_id);
      router.push(`/schedule-versions/${started.schedule_version_id}`);
    } catch (cause: unknown) {
      setStartError(cause);
      setIsStarting(false);
    }
  }

  const startApiError = asApiError(startError);

  return (
    <li className="card">
      <div className="card__header">
        <div>
          <h2 className="card__title">{period.name}</h2>
          <p className="card__meta">{formatDateRange(period.start_date, period.end_date)}</p>
        </div>
        <div>
          <span className={`badge ${availabilityReady ? "badge--ok" : "badge--warning"}`}>
            {availabilityLabel(period.availability_locked_at)}
          </span>{" "}
          {period.schedule?.latest_version_status != null && (
            <span className="badge">{statusLabel(period.schedule.latest_version_status)}</span>
          )}
        </div>
      </div>

      {/* The same five screens this card always linked to, in the order the
          work is actually done and numbered so that order is visible rather
          than implied -- staffing, then availability, then the qualifications,
          limits and rules the generator will obey. The sixth step, generating
          and reviewing, is the outcome of the other five and sits apart from
          them below. */}
      <ol className="workflow">
        {workflowSteps(ministryId, period.scheduling_period_id).map((step, index) => (
          <li className="workflow__step" key={step.href}>
            <span className="workflow__number" aria-hidden="true">
              {index + 1}
            </span>
            <span>
              <Link className="workflow__link" href={step.href}>
                {step.label}
              </Link>
              <span className="workflow__hint">{step.hint}</span>
            </span>
          </li>
        ))}
      </ol>

      <div className="workflow-finish">
        <span className="workflow-finish__label">
          {versionId !== null ? "Review the schedule" : "Generate the schedule"}
        </span>
        {versionId !== null ? (
          // Opening a started schedule is a read, so it is offered to
          // anybody who can see this card; the review screen decides for
          // itself which actions that reader gets.
          <Link className="button button--primary link-button" href={`/schedule-versions/${versionId}`}>
            Open schedule
          </Link>
        ) : !canOperate ? (
          <span className="small muted">
            Scheduling for this period has not started yet.
          </span>
        ) : canStart ? (
          <button
            className="button button--primary"
            type="button"
            onClick={() => void handleStart()}
            disabled={isStarting}
          >
            {isStarting ? "Starting…" : "Start schedule"}
          </button>
        ) : (
          <>
            <button className="button" type="button" disabled>
              Start schedule
            </button>
            <LockAvailabilityAction period={period} onLocked={onChanged} />
          </>
        )}
      </div>

      {startError !== null &&
        (startApiError !== null ? (
          <div style={{ marginTop: "0.85rem" }}>
            <ErrorNotice error={startApiError} title="The schedule could not be started" />
          </div>
        ) : (
          <div style={{ marginTop: "0.85rem" }}>
            <UnexpectedErrorNotice />
          </div>
        ))}
    </li>
  );
}

/**
 * The one action that turns "availability is still open" from a dead end into
 * a step.
 *
 * Before this existed, a period whose availability was never closed could not
 * be scheduled from the product at all: starting a schedule requires the lock,
 * and nothing in the UI could set it. The card said so and stopped there.
 *
 * **Confirmed before it happens, because it cannot be undone.** There is no
 * unlock in the domain, so a single misplaced click would leave a period that
 * can never accept another availability answer. The first click explains what
 * is about to happen; the second does it.
 */
function LockAvailabilityAction({
  period,
  onLocked,
}: {
  period: SchedulingPeriodSummary;
  onLocked: () => Promise<void> | void;
}) {
  const [isConfirming, setIsConfirming] = useState(false);
  const [isLocking, setIsLocking] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function handleLock() {
    setIsLocking(true);
    setError(null);
    try {
      await lockAvailability(period.scheduling_period_id);
      setIsConfirming(false);
      await onLocked();
    } catch (cause: unknown) {
      setError(cause);
    } finally {
      setIsLocking(false);
    }
  }

  const apiError = asApiError(error);

  if (!isConfirming) {
    return (
      <>
        <button className="button" type="button" onClick={() => setIsConfirming(true)}>
          Finish collecting availability
        </button>
        <span className="small muted">
          Availability must be closed before starting the schedule.
        </span>
        {error !== null &&
          (apiError !== null ? (
            <ErrorNotice error={apiError} title="Availability could not be closed" />
          ) : (
            <UnexpectedErrorNotice />
          ))}
      </>
    );
  }

  return (
    <div className="notice notice--warning" role="status">
      <p className="notice__title">Close availability for {period.name}?</p>
      <p>
        Answers already given are kept, and anyone who has not answered stays unanswered. No
        further availability can be recorded for this period afterwards, and this cannot be
        undone.
      </p>
      <div className="card__actions">
        <button
          className="button button--primary"
          type="button"
          onClick={() => void handleLock()}
          disabled={isLocking}
        >
          {isLocking ? "Closing…" : "Yes, close availability"}
        </button>
        <button
          className="button"
          type="button"
          onClick={() => setIsConfirming(false)}
          disabled={isLocking}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
