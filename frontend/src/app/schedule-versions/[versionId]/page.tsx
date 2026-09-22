"use client";

/**
 * The review screen: what this schedule contains, what still needs work, and
 * the one action that moves it forward.
 *
 * **It is the whole lifecycle, on one page (Task 80).** A schedule is a draft,
 * then in review, then final, and each of those states offers exactly one next
 * step to exactly one person. Putting that on a second screen would mean a
 * head deciding whether to publish somewhere other than where they can see
 * what they would be publishing, so the status, the schedule, the checks and
 * the action live together and the page re-reads itself after each transition.
 *
 * **The order of this page is its most important property (Task 75).** A
 * ministry head opens it asking "what is the schedule?", not "what is each
 * person's statistical breakdown?", so the sections run:
 *
 *   1. a short summary of where the schedule stands;
 *   2. the generate / regenerate controls, while it is still a draft;
 *   3. the draft schedule matrix -- the one centrepiece of the page;
 *   4. assignments by person, below the schedule it summarises;
 *   5. the schedule checks;
 *   6. what happens next, and the action that does it.
 *
 * Per-person analytics used to sit above the schedule itself. It does not
 * any more, and `structure.test.ts` asserts the order so it cannot drift back.
 *
 * **Which controls appear is the backend's answer, not this page's guess.**
 * `can_operate` says whether the reader is an active head of this ministry;
 * an administrator overseeing a ministry they do not run sees every one of the
 * sections above and none of the actions. Nothing here is a security boundary
 * -- `lib/lifecycle.ts` says why -- and every rule it renders is enforced
 * again on every request.
 */

import { useCallback, useEffect, useState } from "react";

import { AssignmentsByPersonList } from "@/components/AssignmentsByPersonList";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ScheduleTable } from "@/components/ScheduleTable";
import {
  finalizeScheduleVersion,
  generateSchedule,
  getScheduleVersion,
  submitScheduleVersionForReview,
} from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { GenerationResult, ScheduleVersionDetail } from "@/lib/api/types";
import { summarizeAssignmentsByPerson } from "@/lib/assignmentsByPerson";
import {
  STATUS_DRAFT,
  STATUS_FINALIZED,
  STATUS_REVIEW,
  formatDateRange,
  readinessLabel,
  statusLabel,
} from "@/lib/labels";
import type { LifecycleState } from "@/lib/lifecycle";
import {
  FINALIZE_CONFIRM_ACTION,
  FINALIZE_CONFIRM_BODY,
  FINALIZE_CONFIRM_TITLE,
  READ_ONLY_BODY,
  READ_ONLY_TITLE,
  canFinalize,
  canGenerateInto,
  canSubmitForReview,
  finalizeBlockedReason,
  isReadOnlyView,
  lifecycleStateOf,
  stageExplanation,
  stageHeadline,
  statusBadgeClass,
  submitBlockedReason,
} from "@/lib/lifecycle";
import { groupByEvent } from "@/lib/schedule";
import { useParams } from "next/navigation";

export default function ScheduleVersionPage() {
  const params = useParams<{ versionId: string }>();
  const versionId = Number(params.versionId);
  // Derived during render for the same reason as the periods page.
  const hasValidId = Number.isInteger(versionId) && versionId > 0;

  const [detail, setDetail] = useState<ScheduleVersionDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidId);

  /** The most recent generation run, kept only to explain the gaps it found. */
  const [lastRun, setLastRun] = useState<GenerationResult | null>(null);

  // State is updated inside promise callbacks rather than after an `await`,
  // so nothing is set synchronously while the effect below is running.
  const load = useCallback(
    (signal?: AbortSignal): Promise<void> =>
      getScheduleVersion(versionId, signal)
        .then((result) => {
          setDetail(result);
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
    [versionId],
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
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Schedule" }]} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That schedule address is not valid.</p>
        </div>
      </>
    );
  }

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Schedule" }]} />
        <LoadingNotice what="this schedule" />
      </>
    );
  }

  if (error !== null && detail === null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Schedule" }]} />
        <h1 className="page__title">Schedule</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (detail === null) return <UnexpectedErrorNotice />;

  const { period, schedule_version: version, summary, staleness, finalization_readiness } = detail;
  const groups = groupByEvent(detail, lastRun?.unfilled_requirements ?? []);
  const personSummaries = summarizeAssignmentsByPerson(detail.assignments);
  const isDraft = version.status === STATUS_DRAFT;
  // One value, read once: every control below asks `lib/lifecycle` rather
  // than testing the status and `can_operate` inline, so the rules stay in
  // one testable place.
  const lifecycle = lifecycleStateOf(detail);

  return (
    <>
      <Breadcrumbs
        trail={[
          { label: "Home", href: "/" },
          { label: period.ministry_name, href: `/ministries/${period.ministry_id}/periods` },
          { label: period.name },
        ]}
      />

      <h1 className="page__title">{period.name}</h1>
      <p className="page__subtitle">
        {period.ministry_name} · {formatDateRange(period.start_date, period.end_date)} ·{" "}
        <span className={statusBadgeClass(version.status)}>{statusLabel(version.status)}</span>
      </p>

      {/* Said once, at the top, to somebody who can see all of this and
          change none of it. Everything below simply has no actions for
          them -- this is what explains why. */}
      {isReadOnlyView(lifecycle) && (
        <div className="notice notice--info notice--compact" role="status">
          <p className="notice__title">{READ_ONLY_TITLE}</p>
          <p>{READ_ONLY_BODY}</p>
        </div>
      )}

      {staleness.is_stale && (
        <div className="notice notice--warning" role="status">
          <p className="notice__title">Scheduling information changed after this draft was created</p>
          <p>
            The events or staffing numbers for this period are no longer the same as when this
            draft was started. What you see below is what the draft was built from.
          </p>
        </div>
      )}

      {/* 1. Where this stands, in four numbers and nothing more. */}
      <section className="section" aria-labelledby="summary-heading">
        <h2 className="section__title" id="summary-heading">
          Where this schedule stands
        </h2>
        <ul className="stats">
          <li className="stat">
            <p className="stat__value">{summary.required_positions}</p>
            <p className="stat__label">Positions needed</p>
          </li>
          <li className="stat">
            <p className="stat__value">{summary.assigned_positions}</p>
            <p className="stat__label">Positions filled</p>
          </li>
          <li className={summary.unfilled_positions > 0 ? "stat stat--alert" : "stat"}>
            <p className="stat__value">{summary.unfilled_positions}</p>
            <p className="stat__label">Still unfilled</p>
          </li>
          {/* The word, not the colour, is what says whether the checks passed:
              "Pass" / "N to review" reads the same to someone who cannot use
              the tint at all. */}
          <li className={finalization_readiness.is_ready ? "stat stat--ok" : "stat stat--alert"}>
            <p className="stat__value">
              {finalization_readiness.is_ready ? "Pass" : finalization_readiness.issues.length}
            </p>
            <p className="stat__label">
              {finalization_readiness.is_ready ? "Schedule checks" : "Checks to review"}
            </p>
          </li>
        </ul>
      </section>

      {/* 2. Generate / regenerate, above the schedule it produces. */}
      {canGenerateInto(lifecycle) && (
        <GenerateSection
          versionId={versionId}
          hasAssignments={detail.assignments.length > 0}
          onGenerated={async (result) => {
            setLastRun(result);
            await load();
          }}
        />
      )}

      {/* 3. The schedule itself -- the centrepiece, and the reason for the
             whole page. Everything below it explains or qualifies it. */}
      <section className="section section--feature" aria-labelledby="schedule-heading">
        <div className="section__heading">
          <h2 className="section__title" id="schedule-heading">
            {isDraft ? "Draft schedule" : "Schedule"}
          </h2>
          {/* Said on the schedule itself, not only in the status line above
              it: "nobody has seen this" is the fact a head most needs while
              they are looking at names and dates. A finalized schedule gets
              the opposite reassurance for the same reason. */}
          {isDraft && <span className="badge badge--info">Draft — not sent to anyone</span>}
          {version.status === STATUS_REVIEW && (
            <span className="badge badge--warning">In review — not sent to anyone</span>
          )}
          {version.status === STATUS_FINALIZED && (
            <span className="badge badge--ok">Final — volunteers can see this</span>
          )}
          {summary.unfilled_positions > 0 && (
            <span className="section__note">
              Unfilled positions are highlighted and labelled “Unfilled”.
            </span>
          )}
        </div>
        {groups.length === 0 ? (
          <div className="notice notice--info">
            <p>This schedule has no positions to fill.</p>
          </div>
        ) : (
          <ScheduleTable groups={groups} />
        )}
      </section>

      {/* 4. Per-person load, below the schedule and visually quieter than it. */}
      {personSummaries.length > 0 && (
        <section className="section" aria-labelledby="by-person-heading">
          <div className="section__heading">
            <h2 className="section__title" id="by-person-heading">
              Assignments by person
            </h2>
            <span className="section__note">
              How often each person serves this period, and in which roles.
            </span>
          </div>
          <AssignmentsByPersonList summaries={personSummaries} />
        </section>
      )}

      {/* 5. What the checks say about what is above. */}
      <section className="section" aria-labelledby="checks-heading">
        <h2 className="section__title" id="checks-heading">
          Schedule checks
        </h2>
        {finalization_readiness.is_ready ? (
          <div className="notice notice--ok">
            <p>Current assignments pass the scheduling checks.</p>
          </div>
        ) : (
          <ul className="issue-list">
            {finalization_readiness.issues.map((issue, index) => (
              <li className="issue" key={`${issue.code}-${index}`}>
                <span className="badge badge--warning">{readinessLabel(issue.code)}</span>
                <p className="issue__message">{issue.message}</p>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 6. Where this stands in its life, and the one step that moves it. */}
      <section className="section" aria-labelledby="next-heading">
        <h2 className="section__title" id="next-heading">
          What happens next
        </h2>
        <div
          className={`notice notice--compact ${
            version.status === STATUS_FINALIZED ? "notice--ok" : "notice--info"
          }`}
        >
          <p className="notice__title">{stageHeadline(version.status)}</p>
          {stageExplanation(version.status).map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </div>

        <LifecycleAction
          versionId={versionId}
          lifecycle={lifecycle}
          onTransitioned={(updated) => {
            setDetail(updated);
            // The last generation run described a draft that no longer
            // exists in that state; keeping its gaps on screen would
            // annotate a schedule it is no longer about.
            setLastRun(null);
          }}
        />
      </section>
    </>
  );
}

/**
 * The one lifecycle control, whichever one this schedule currently offers.
 *
 * A single component rather than a `SubmitSection` and a `FinalizeSection`,
 * because the two are the same shape — an explanation, a button, and an error
 * if the backend refuses — and the whole point is that a schedule offers
 * exactly one of them at a time. Which one is `lib/lifecycle`'s decision, not
 * this component's.
 *
 * **Finalizing asks first, and submitting does not.** Submitting for review is
 * reversible in the only sense that matters — it tells nobody, and a head who
 * submits too early can keep working on the inputs and start a fresh version.
 * Finalizing is not: volunteers begin relying on the schedule, other
 * ministries schedule around it, and nothing un-finalizes it. So the second
 * click is asked for there and nowhere else, in the same two-step shape the
 * availability lock uses on the periods screen.
 */
function LifecycleAction({
  versionId,
  lifecycle,
  onTransitioned,
}: {
  versionId: number;
  lifecycle: LifecycleState;
  /** The fresh detail the backend returned, so the page re-renders in its
   *  new state without a second read. */
  onTransitioned: (detail: ScheduleVersionDetail) => void;
}) {
  const [isConfirming, setIsConfirming] = useState(false);
  const [isWorking, setIsWorking] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const showSubmit = canSubmitForReview(lifecycle);
  const showFinalize = canFinalize(lifecycle);
  const blocked = submitBlockedReason(lifecycle) ?? finalizeBlockedReason(lifecycle);

  async function run(action: () => Promise<ScheduleVersionDetail>) {
    setIsWorking(true);
    setError(null);
    try {
      const updated = await action();
      setIsConfirming(false);
      onTransitioned(updated);
    } catch (cause: unknown) {
      setError(cause);
    } finally {
      setIsWorking(false);
    }
  }

  const apiError = asApiError(error);
  const failure =
    error === null ? null : apiError !== null ? (
      <ErrorNotice error={apiError} title="That could not be done" />
    ) : (
      <UnexpectedErrorNotice />
    );

  // Nothing to offer: a finalized schedule, or a reader who does not run this
  // ministry. The explanation above has already said so.
  if (!showSubmit && !showFinalize) {
    return blocked === null ? null : (
      <p className="small muted" style={{ marginTop: "0.85rem" }}>
        {blocked}
      </p>
    );
  }

  if (showSubmit) {
    return (
      <div className="card__actions" style={{ marginTop: "0.85rem" }}>
        <button
          className="button button--primary"
          type="button"
          onClick={() => void run(() => submitScheduleVersionForReview(versionId))}
          disabled={isWorking}
        >
          {isWorking ? "Submitting…" : "Submit for review"}
        </button>
        <span className="small muted">
          This still tells nobody. It marks the schedule as ready to be checked.
        </span>
        {failure}
      </div>
    );
  }

  if (!isConfirming) {
    return (
      <div className="card__actions" style={{ marginTop: "0.85rem" }}>
        <button
          className="button button--primary"
          type="button"
          onClick={() => setIsConfirming(true)}
        >
          Finalize schedule
        </button>
        <span className="small muted">
          Volunteers will see their assignments once you do.
        </span>
        {failure}
      </div>
    );
  }

  return (
    <div className="notice notice--warning" role="status" style={{ marginTop: "0.85rem" }}>
      <p className="notice__title">{FINALIZE_CONFIRM_TITLE}</p>
      <p>{FINALIZE_CONFIRM_BODY}</p>
      <div className="card__actions">
        <button
          className="button"
          type="button"
          onClick={() => setIsConfirming(false)}
          disabled={isWorking}
        >
          Cancel
        </button>
        <button
          className="button button--primary"
          type="button"
          onClick={() => void run(() => finalizeScheduleVersion(versionId))}
          disabled={isWorking}
        >
          {isWorking ? "Finalizing…" : FINALIZE_CONFIRM_ACTION}
        </button>
      </div>
      {failure}
    </div>
  );
}

/**
 * The generate control, and the two preferences this release exposes.
 *
 * Only two, on purpose. Both are generic settings any ministry might use, and
 * neither carries a default this app invented -- the target box starts empty,
 * and a leader types the number their ministry actually aims for.
 */
function GenerateSection({
  versionId,
  hasAssignments,
  onGenerated,
}: {
  versionId: number;
  /** Whether this draft already holds assignments -- the button then offers
   *  to fill what is still open rather than to build the schedule. */
  hasAssignments: boolean;
  onGenerated: (result: GenerationResult) => Promise<void>;
}) {
  const [allowNoResponse, setAllowNoResponse] = useState(false);
  const [target, setTarget] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<unknown>(null);
  const [lastResult, setLastResult] = useState<GenerationResult | null>(null);

  async function handleGenerate() {
    setIsGenerating(true);
    setGenerateError(null);
    try {
      const trimmed = target.trim();
      const result = await generateSchedule(versionId, {
        allow_no_response: allowNoResponse,
        target_assignments_per_candidate: trimmed === "" ? null : Number(trimmed),
        // Choosing variety roles would mean typing role ids; see the note in
        // the API types.
        role_variety_role_ids: null,
      });
      setLastResult(result);
      await onGenerated(result);
    } catch (cause: unknown) {
      setGenerateError(cause);
    } finally {
      setIsGenerating(false);
    }
  }

  const apiError = asApiError(generateError);

  return (
    <section className="section" aria-labelledby="generate-heading">
      <h2 className="section__title" id="generate-heading">
        {hasAssignments ? "Fill the rest of this schedule" : "Fill this schedule"}
      </h2>

      <div className="matrix-toolbar">
        <label className="field field--inline" style={{ margin: 0 }}>
          <input
            type="checkbox"
            checked={allowNoResponse}
            onChange={(event) => setAllowNoResponse(event.target.checked)}
          />
          Include people who have not answered
        </label>

        <label htmlFor="target-per-person" className="small muted" style={{ marginLeft: "0.5rem" }}>
          Turns per person
        </label>
        <input
          id="target-per-person"
          className="matrix__cell-input matrix__cell-input--wide"
          type="number"
          min={0}
          inputMode="numeric"
          value={target}
          placeholder="No target"
          onChange={(event) => setTarget(event.target.value)}
        />

        <button
          className="button button--primary"
          type="button"
          onClick={() => void handleGenerate()}
          disabled={isGenerating}
        >
          {isGenerating ? "Generating…" : "Generate schedule"}
        </button>
      </div>

      <p className="small muted">
        By default only people who said they are available are scheduled. The target above is a
        goal, not a limit — someone may still serve more often if nobody else can fill a position;
        leave it empty for no target.
      </p>

      {apiError !== null && (
        <div style={{ marginTop: "0.85rem" }}>
          <ErrorNotice error={apiError} title="The schedule could not be generated" />
        </div>
      )}
      {generateError !== null && apiError === null && (
        <div style={{ marginTop: "0.85rem" }}>
          <UnexpectedErrorNotice />
        </div>
      )}

      {lastResult !== null && generateError === null && (
        <div className={`notice ${lastResult.is_complete ? "notice--ok" : "notice--info"}`} role="status">
          <p className="notice__title">
            {lastResult.created_count === 0
              ? "Nothing new to fill"
              : `Added ${lastResult.created_count} ${lastResult.created_count === 1 ? "person" : "people"}`}
          </p>
          <p>
            {lastResult.is_complete
              ? "Every position is filled."
              : "Some positions could not be filled. They are marked below, with the reason."}
          </p>
        </div>
      )}
    </section>
  );
}
