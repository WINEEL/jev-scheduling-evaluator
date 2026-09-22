/**
 * Which lifecycle action a schedule offers, and to whom.
 *
 * The decisions in `lifecycle.ts` decide what a ministry head is shown at the
 * moment they are about to publish a rota, so every combination that matters
 * is enumerated rather than sampled: three statuses, two kinds of reader, and
 * the two conditions (staleness, readiness) that gate each action.
 *
 * **None of this is the security boundary**, and these tests do not pretend
 * otherwise — the backend refuses the same things again, and has its own
 * tests saying so. What is asserted here is that the *screen* never offers an
 * action the backend would refuse, and never withholds one it would allow.
 */

import { describe, expect, it } from "vitest";

import type { ScheduleVersionDetail } from "./api/types";
import { STATUS_DRAFT, STATUS_FINALIZED, STATUS_REVIEW } from "./labels";
import {
  type LifecycleState,
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
} from "./lifecycle";

const ALL_STATUSES = [STATUS_DRAFT, STATUS_REVIEW, STATUS_FINALIZED];

function state(overrides: Partial<LifecycleState> = {}): LifecycleState {
  return {
    status: STATUS_DRAFT,
    canOperate: true,
    isStale: false,
    isReady: true,
    ...overrides,
  };
}

// -- lifecycleStateOf -------------------------------------------------------

describe("lifecycleStateOf", () => {
  it("reads the four fields out of a detail payload", () => {
    const detail = {
      schedule_version: { status: STATUS_REVIEW },
      can_operate: true,
      staleness: { is_stale: true },
      finalization_readiness: { is_ready: false },
    } as unknown as ScheduleVersionDetail;

    expect(lifecycleStateOf(detail)).toEqual({
      status: STATUS_REVIEW,
      canOperate: true,
      isStale: true,
      isReady: false,
    });
  });

  it("takes `can_operate` from the server and derives it from nothing else", () => {
    // Deliberately an administrator-shaped payload: the field is false, and
    // there is nothing else in it the function could mistake for authority.
    const detail = {
      schedule_version: { status: STATUS_DRAFT },
      can_operate: false,
      staleness: { is_stale: false },
      finalization_readiness: { is_ready: true },
    } as unknown as ScheduleVersionDetail;

    expect(lifecycleStateOf(detail).canOperate).toBe(false);
  });
});

// -- Who gets nothing -------------------------------------------------------

describe("a reader who does not run this ministry", () => {
  it.each(ALL_STATUSES)("is offered no action in %s", (status) => {
    const viewer = state({ status, canOperate: false });

    expect(canGenerateInto(viewer)).toBe(false);
    expect(canSubmitForReview(viewer)).toBe(false);
    expect(canFinalize(viewer)).toBe(false);
  });

  it.each(ALL_STATUSES)("is told the view is read-only in %s", (status) => {
    expect(isReadOnlyView(state({ status, canOperate: false }))).toBe(true);
  });

  it("is given no explanation of a blocked action, because none is owed", () => {
    // The actions are not blocked for them; the actions are not theirs. A
    // "fix the checks and try again" line would be addressed to the wrong
    // person.
    const viewer = state({ status: STATUS_REVIEW, canOperate: false, isReady: false });

    expect(finalizeBlockedReason(viewer)).toBeNull();
    expect(submitBlockedReason(viewer)).toBeNull();
  });

  it("is not told the view is read-only when they do run it", () => {
    expect(isReadOnlyView(state({ canOperate: true }))).toBe(false);
  });
});

// -- Generation -------------------------------------------------------------

describe("canGenerateInto", () => {
  it("is true only for a head looking at a draft", () => {
    expect(canGenerateInto(state({ status: STATUS_DRAFT }))).toBe(true);
  });

  it.each([STATUS_REVIEW, STATUS_FINALIZED])("is false in %s", (status) => {
    expect(canGenerateInto(state({ status }))).toBe(false);
  });

  it("is true for a stale draft", () => {
    // Regenerating is one of the things a head does *about* staleness, and
    // the backend allows it; only submitting is refused.
    expect(canGenerateInto(state({ isStale: true }))).toBe(true);
  });
});

// -- Submit for review ------------------------------------------------------

describe("canSubmitForReview", () => {
  it("is true for a head looking at a fresh draft", () => {
    expect(canSubmitForReview(state())).toBe(true);
  });

  it.each([STATUS_REVIEW, STATUS_FINALIZED])("is false in %s", (status) => {
    expect(canSubmitForReview(state({ status }))).toBe(false);
  });

  it("is false for a stale draft, because the backend refuses one", () => {
    expect(canSubmitForReview(state({ isStale: true }))).toBe(false);
  });

  it("does not require the checks to pass", () => {
    // A head submits a schedule *so that* its gaps get looked at; refusing an
    // incomplete one would make the review state useless. This mirrors the
    // backend, which gates completeness at finalization and not before.
    expect(canSubmitForReview(state({ isReady: false }))).toBe(true);
  });

  it("explains a stale draft, and says nothing otherwise", () => {
    expect(submitBlockedReason(state({ isStale: true }))).toMatch(/fresh schedule/i);
    expect(submitBlockedReason(state())).toBeNull();
  });
});

// -- Finalize ---------------------------------------------------------------

describe("canFinalize", () => {
  it("is true for a head looking at a ready schedule in review", () => {
    expect(canFinalize(state({ status: STATUS_REVIEW }))).toBe(true);
  });

  it("is false for a ready draft", () => {
    // Readiness is necessary and never sufficient: a clean draft still has to
    // pass through review, and the backend refuses DRAFT -> FINALIZED.
    expect(canFinalize(state({ status: STATUS_DRAFT, isReady: true }))).toBe(false);
  });

  it("is false when the checks have not passed", () => {
    expect(canFinalize(state({ status: STATUS_REVIEW, isReady: false }))).toBe(false);
  });

  it("is false for a schedule already finalized", () => {
    // Not because finalizing twice would break anything — the backend treats
    // it as a no-op — but because a Finalize button on a published schedule
    // implies it is still being decided.
    expect(canFinalize(state({ status: STATUS_FINALIZED }))).toBe(false);
  });

  it("explains an unready schedule in review", () => {
    expect(finalizeBlockedReason(state({ status: STATUS_REVIEW, isReady: false }))).toMatch(
      /checks below/i,
    );
  });

  it("explains a stale schedule in review before it mentions the checks", () => {
    const reason = finalizeBlockedReason(
      state({ status: STATUS_REVIEW, isStale: true, isReady: false }),
    );

    expect(reason).toMatch(/changed after/i);
  });

  it("says nothing when finalizing is available", () => {
    expect(finalizeBlockedReason(state({ status: STATUS_REVIEW }))).toBeNull();
  });
});

// -- Exactly one action at a time -------------------------------------------

describe("a schedule never offers two lifecycle actions at once", () => {
  const combinations: LifecycleState[] = ALL_STATUSES.flatMap((status) =>
    [true, false].flatMap((isStale) =>
      [true, false].flatMap((isReady) =>
        [true, false].map((canOperate) => ({ status, isStale, isReady, canOperate })),
      ),
    ),
  );

  it.each(combinations)(
    "offers at most one of submit/finalize for %o",
    (candidate) => {
      const offered = [canSubmitForReview(candidate), canFinalize(candidate)].filter(
        Boolean,
      );

      expect(offered.length).toBeLessThanOrEqual(1);
    },
  );

  it.each(combinations)("never offers generation alongside finalize for %o", (candidate) => {
    expect(canGenerateInto(candidate) && canFinalize(candidate)).toBe(false);
  });
});

// -- Wording ----------------------------------------------------------------

describe("wording", () => {
  it("gives a finalized schedule the settled badge, and the others a working one", () => {
    expect(statusBadgeClass(STATUS_FINALIZED)).toContain("badge--ok");
    expect(statusBadgeClass(STATUS_DRAFT)).toContain("badge--info");
    expect(statusBadgeClass(STATUS_REVIEW)).toContain("badge--warning");
  });

  it("falls back to a plain badge for a status it does not know", () => {
    // Translate, never invent — the same rule `labels.ts` keeps.
    expect(statusBadgeClass("SOMETHING_NEW")).toBe("badge");
  });

  it("tells a draft and a schedule in review that volunteers cannot see them", () => {
    expect(stageHeadline(STATUS_DRAFT)).toMatch(/nobody/i);
    expect(stageHeadline(STATUS_REVIEW)).toMatch(/still cannot see/i);
  });

  it("tells a finalized schedule that volunteers can rely on it", () => {
    expect(stageHeadline(STATUS_FINALIZED)).toMatch(/rely on it/i);
  });

  it("says a finalized schedule cannot be edited, and how it is corrected", () => {
    const text = stageExplanation(STATUS_FINALIZED).join(" ");

    expect(text).toMatch(/cannot be edited or regenerated/i);
    expect(text).toMatch(/newer version/i);
  });

  it("never tells a draft or a schedule in review that volunteers can see it", () => {
    for (const status of [STATUS_DRAFT, STATUS_REVIEW]) {
      const text = `${stageHeadline(status)} ${stageExplanation(status).join(" ")}`;
      expect(text).toMatch(/no volunteer|nobody|private/i);
    }
  });

  it("has nothing to say about a status it does not know", () => {
    expect(stageExplanation("SOMETHING_NEW")).toEqual([]);
  });
});
