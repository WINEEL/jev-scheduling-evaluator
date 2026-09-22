/**
 * The abort-during-load defect, and the rule that fixes it (Task 48A).
 *
 * A page's loader is a promise chain: resolve -> show data, reject -> show an
 * error. An **aborted** request is neither, and the bug was treating it as a
 * settled outcome. `AbortController.abort()` runs from an effect's cleanup,
 * which means the component is unmounting or a newer request has replaced
 * this one -- so clearing the loading flag announces "finished, with no data
 * and no error", and every one of these pages renders that combination as the
 * permanent "Something went wrong" notice.
 *
 * React's development StrictMode mounts effects twice, so the first request is
 * always aborted; the slower the endpoint, the longer that false failure sits
 * on screen before the second request answers. These tests pin the promise
 * semantics that made it possible, with no React and no network.
 */

import { describe, expect, it } from "vitest";

import { isAbortError } from "./errors";

function abortError(): DOMException {
  return new DOMException("The operation was aborted.", "AbortError");
}

/** The three flags every one of these pages renders from. */
interface PageState {
  data: string | null;
  error: unknown;
  isLoading: boolean;
}

/** What the pages do now: the loading flag is cleared per outcome. */
function load(promise: Promise<string>, state: PageState): Promise<void> {
  return promise
    .then((result) => {
      state.data = result;
      state.error = null;
      state.isLoading = false;
    })
    .catch((cause: unknown) => {
      if (isAbortError(cause)) return;
      state.error = cause;
      state.isLoading = false;
    });
}

/** What they used to do: `.finally` cleared it on *every* settlement. */
function loadWithFinally(promise: Promise<string>, state: PageState): Promise<void> {
  return promise
    .then((result) => {
      state.data = result;
      state.error = null;
    })
    .catch((cause: unknown) => {
      if (isAbortError(cause)) return;
      state.error = cause;
    })
    .finally(() => {
      state.isLoading = false;
    });
}

function freshState(): PageState {
  return { data: null, error: null, isLoading: true };
}

/** The combination all three pages render as `UnexpectedErrorNotice`. */
function rendersUnexpectedError(state: PageState): boolean {
  return !state.isLoading && state.error === null && state.data === null;
}

describe("isAbortError", () => {
  it("recognises the rejection fetch produces for an aborted request", () => {
    expect(isAbortError(abortError())).toBe(true);
  });

  it("does not classify ordinary failures as aborts", () => {
    expect(isAbortError(new Error("boom"))).toBe(false);
    expect(isAbortError(new DOMException("nope", "NotFoundError"))).toBe(false);
    expect(isAbortError(null)).toBe(false);
    expect(isAbortError(undefined)).toBe(false);
    expect(isAbortError("AbortError")).toBe(false);
  });
});

describe("the regression: an abort must not end the loading state", () => {
  it("reproduces the old bug -- .finally rendered a permanent error", async () => {
    const state = freshState();
    await loadWithFinally(Promise.reject(abortError()), state);

    // No data, no error, and no longer loading: the exact combination that
    // put "Something went wrong" on screen for a request nobody had answered.
    expect(rendersUnexpectedError(state)).toBe(true);
  });

  it("keeps the page in its loading state when a request is aborted", async () => {
    const state = freshState();
    await load(Promise.reject(abortError()), state);

    expect(state.isLoading).toBe(true);
    expect(state.error).toBeNull();
    expect(state.data).toBeNull();
    expect(rendersUnexpectedError(state)).toBe(false);
  });

  it("still shows real failures as errors", async () => {
    const state = freshState();
    const failure = new Error("network");
    await load(Promise.reject(failure), state);

    expect(state.isLoading).toBe(false);
    expect(state.error).toBe(failure);
  });

  it("still shows data on success", async () => {
    const state = freshState();
    await load(Promise.resolve("detail"), state);

    expect(state.isLoading).toBe(false);
    expect(state.error).toBeNull();
    expect(state.data).toBe("detail");
  });

  it("survives the StrictMode sequence: first request aborted, second answers", async () => {
    const state = freshState();

    // Effect 1 starts, cleanup aborts it; effect 2 starts and is still in
    // flight. The page must be showing "loading", not a failure.
    const first = load(Promise.reject(abortError()), state);
    let resolveSecond: (value: string) => void = () => {};
    const second = load(
      new Promise<string>((resolve) => {
        resolveSecond = resolve;
      }),
      state,
    );

    await first;
    expect(state.isLoading).toBe(true);
    expect(rendersUnexpectedError(state)).toBe(false);

    resolveSecond("detail");
    await second;
    expect(state.isLoading).toBe(false);
    expect(state.data).toBe("detail");
    expect(state.error).toBeNull();
  });

  it("a slow second request does not shorten the loading state", async () => {
    // The schedule-version endpoint answers in seconds, which is why this
    // page showed the false failure most obviously: the whole wait was spent
    // in the wrongly-cleared state.
    const state = freshState();
    await load(Promise.reject(abortError()), state);

    for (let tick = 0; tick < 5; tick += 1) {
      await Promise.resolve();
      expect(state.isLoading).toBe(true);
    }
  });
});
