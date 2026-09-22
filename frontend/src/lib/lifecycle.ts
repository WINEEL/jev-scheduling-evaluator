/**
 * Which lifecycle action a schedule offers, and to whom.
 *
 * Extracted as plain functions rather than left as conditionals inside the
 * review page, for the same reason `homeView.ts` and `peopleAccess.ts` are:
 * the rules worth protecting are decisions about a schedule and an actor, not
 * about markup, and written this way they can be tested directly,
 * exhaustively, and without a browser.
 *
 * **None of this is a security boundary, and saying so is not a disclaimer.**
 * Every rule below is enforced again in FastAPI, in
 * `app.services.schedule_lifecycle`, on every request: an administrator who
 * does not head the ministry gets 403 from the API whatever this file returns,
 * a draft cannot be finalized however many buttons are rendered, and readiness
 * is re-computed server-side against current facts at the moment of the
 * transition. What these functions buy is a screen that tells the truth — no
 * control that leads only to a refusal, and no control that implies a
 * finalized schedule can be casually changed.
 *
 * The three states, and what each offers the ministry's head:
 *
 * - **Draft** — generate and regenerate freely; submit for review once the
 *   draft is built and its inputs have not drifted.
 * - **In review** — no more generation; finalize once the checks pass.
 * - **Finalized** — nothing. It is the authoritative schedule, volunteers are
 *   relying on it, and correcting it means a newer version, which this release
 *   does not create.
 *
 * Anybody who is not that head — an administrator overseeing the ministry, or
 * a volunteer who guessed the URL — gets none of the three, whatever the
 * status.
 */

import type { ScheduleVersionDetail } from "./api/types";
import { STATUS_DRAFT, STATUS_FINALIZED, STATUS_REVIEW } from "./labels";

/** The fields every decision here reads, and deliberately no more.
 *
 * Taking a narrow shape rather than the whole payload means a change to any
 * other part of `ScheduleVersionDetail` cannot silently change which actions
 * appear — the same reasoning `homeSectionsFor` applies. */
export interface LifecycleState {
  /** `DRAFT`, `REVIEW` or `FINALIZED`, exactly as the backend reported it. */
  readonly status: string;
  /** Whether this reader is an active head of the schedule's ministry. */
  readonly canOperate: boolean;
  /** Whether the events or staffing numbers have changed since the draft was
   *  built. The backend refuses to submit a stale draft. */
  readonly isStale: boolean;
  /** Whether the schedule's *contents* would block finalization. Not a
   *  statement that finalizing is allowed — that also needs `REVIEW`. */
  readonly isReady: boolean;
}

/** Read the four fields out of a detail payload, in one place. */
export function lifecycleStateOf(detail: ScheduleVersionDetail): LifecycleState {
  return {
    status: detail.schedule_version.status,
    canOperate: detail.can_operate,
    isStale: detail.staleness.is_stale,
    isReady: detail.finalization_readiness.is_ready,
  };
}

/**
 * Whether the generate / regenerate controls are shown.
 *
 * Only a draft may be generated into, and only its head may do it. A version
 * in review is deliberately excluded even though it is still a working
 * version: generation rewrites many rows at once and must not happen
 * underneath the people reviewing it — which is the backend's rule, mirrored
 * here so the button matches.
 */
export function canGenerateInto(state: LifecycleState): boolean {
  return state.canOperate && state.status === STATUS_DRAFT;
}

/**
 * Whether the "Submit for review" action is shown.
 *
 * A draft, its head, and inputs that have not drifted. The staleness test is
 * the one that is easy to leave out: the backend refuses a stale draft with a
 * 409 and tells the head to start a fresh version, so offering the button
 * would promise something that cannot happen.
 */
export function canSubmitForReview(state: LifecycleState): boolean {
  return state.canOperate && state.status === STATUS_DRAFT && !state.isStale;
}

/**
 * Whether the "Finalize" action is shown.
 *
 * In review, its head, and every check passing. Readiness is not advisory
 * here: the backend re-runs it at the moment of the transition and refuses
 * outright if anything fails, so a Finalize button on a schedule with open
 * issues would be a button that only ever produces an error.
 *
 * **`isReady` is necessary but never sufficient**, which is why the status
 * test is not dropped as redundant: a draft can be perfectly ready and still
 * must pass through review first.
 */
export function canFinalize(state: LifecycleState): boolean {
  return state.canOperate && state.status === STATUS_REVIEW && state.isReady;
}

/**
 * Why the finalize action is unavailable to somebody who could otherwise use
 * it — or `null` when it is available, or when the reader is not the head at
 * all and no explanation is owed.
 *
 * Shown beside the disabled state so "In review, and nothing happens here" is
 * never the whole story a head gets.
 */
export function finalizeBlockedReason(state: LifecycleState): string | null {
  if (!state.canOperate || state.status !== STATUS_REVIEW) return null;
  if (state.isStale) {
    return "The events or staffing numbers changed after this schedule was built. It cannot be finalized as it stands.";
  }
  if (!state.isReady) {
    return "The schedule checks below must pass before this can be finalized.";
  }
  return null;
}

/** Why "Submit for review" is unavailable to a head looking at a draft. */
export function submitBlockedReason(state: LifecycleState): string | null {
  if (!state.canOperate || state.status !== STATUS_DRAFT) return null;
  if (state.isStale) {
    return "The events or staffing numbers changed after this draft was built. Start a fresh schedule to pick up the current setup.";
  }
  return null;
}

/**
 * Whether to tell the reader they are looking at a ministry they do not run.
 *
 * True only for somebody who can see the schedule and cannot act on it. It is
 * not derived from `is_admin`: the backend's own answer is what says whether
 * this reader may operate the ministry, and an administrator who *also* heads
 * it gets the head's screen, not this notice.
 */
export function isReadOnlyView(state: LifecycleState): boolean {
  return !state.canOperate;
}

// -- Wording ---------------------------------------------------------------

/** The badge tone for a status: draft and review are in progress, finalized
 *  is settled. */
export function statusBadgeClass(status: string): string {
  switch (status) {
    case STATUS_DRAFT:
      return "badge badge--info";
    case STATUS_REVIEW:
      return "badge badge--warning";
    case STATUS_FINALIZED:
      return "badge badge--ok";
    default:
      return "badge";
  }
}

/** The headline of the "what happens next" panel, per status. */
export function stageHeadline(status: string): string {
  switch (status) {
    case STATUS_DRAFT:
      return "This is a draft. Nobody has been told about it.";
    case STATUS_REVIEW:
      return "This schedule is in review. Volunteers still cannot see it.";
    case STATUS_FINALIZED:
      return "This schedule is final. Volunteers can rely on it.";
    default:
      return "Where this schedule stands";
  }
}

/** The body of that panel, per status. Says what is true now and what the one
 *  next step is — never more actions than the status actually offers. */
export function stageExplanation(status: string): readonly string[] {
  switch (status) {
    case STATUS_DRAFT:
      return [
        "Nothing here has been sent to anyone, and no volunteer can see it. Generate again, fix what the checks flag, and adjust the staffing, availability or scheduling rules it was built from.",
        "When it looks right, submit it for review. That still tells nobody — it marks the schedule as ready to be checked before it goes out.",
      ];
    case STATUS_REVIEW:
      return [
        "A schedule in review is still private: no volunteer sees these assignments, and no other ministry treats them as settled.",
        "Check it over. When the checks pass and you are happy with it, finalize it — that is the step that makes it real.",
      ];
    case STATUS_FINALIZED:
      return [
        "These assignments are now authoritative. Everyone scheduled sees their own turns in their personal schedule, and other ministries will not schedule the same people on the same Sunday.",
        "A finalized schedule cannot be edited or regenerated. Correcting it means creating a newer version of this schedule, which is not available in this release.",
      ];
    default:
      return [];
  }
}

/** The confirmation a head must give before finalizing. Stated in terms of
 *  the consequence — volunteers start relying on it — rather than the state
 *  change, because the consequence is what they are agreeing to. */
export const FINALIZE_CONFIRM_TITLE = "Finalize this schedule?";

export const FINALIZE_CONFIRM_BODY =
  "Finalizing makes these assignments authoritative and visible in volunteers'" +
  " personal schedules. Other ministries will schedule around them. This" +
  " cannot be undone.";

export const FINALIZE_CONFIRM_ACTION = "Finalize schedule";

/** The note shown to a reader who may see the schedule but not act on it. */
export const READ_ONLY_TITLE = "You are viewing this schedule, not running it";

export const READ_ONLY_BODY =
  "Scheduling for a ministry is done by its ministry head. You can see" +
  " everything here — the schedule, the checks, and where it has got to — and" +
  " the actions that change it are theirs.";
