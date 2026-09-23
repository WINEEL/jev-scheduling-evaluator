/**
 * The one place a network response becomes either data or a typed error.
 *
 * Deliberately narrow, and what it lacks is the design:
 *
 * - **No notion of a user.** The API takes no actor and there is no `/me`.
 * - **No cookies and no credentials.** `fetch` is called with the default
 *   `credentials: "same-origin"`, and the proxy forwards no cookie upstream,
 *   so nothing in this path can read or write a session.
 * - **No `unauthenticated` failure kind**, because no response can be a 401 —
 *   there is nothing to authenticate against. A client that could represent
 *   one would invite a component to ask somebody to log in.
 * - **No retries.** An evaluation costs money and calls a third party; a
 *   client deciding to spend twice is different from a person pressing the
 *   button again.
 *
 * Two calls, because the API has two. The only thing either one sends is a
 * scenario name from a fixed list of three.
 */

import type { JevDemoEvaluation, JevDemoScenarioList } from "./types";

/**
 * This app's own origin. The proxy in `app/api/jev-demo/[...path]` forwards to
 * the backend, so the browser never learns its address, no CORS policy is
 * involved, and `TYPESAFE_API_KEY` stays server-side by construction.
 */
export const JEV_DEMO_BASE_PATH = "/api/jev-demo";

/** How a demo request failed, in the demo's own terms. */
export type JevDemoErrorKind =
  /** The demo server is not running, or the browser is offline. */
  | "unreachable"
  /** The scenario name is not one of the three. A wiring bug, not a user's. */
  | "unknown_scenario"
  /** The demo server reached TypeSafe and could not use the answer, or could
   *  not reach it at all — most often a missing `TYPESAFE_API_KEY`. */
  | "evaluation_failed"
  /** Anything else the server said. */
  | "failed";

export class JevDemoError extends Error {
  readonly kind: JevDemoErrorKind;
  /** `null` when the request never reached the server. */
  readonly status: number | null;
  /** The server's own `detail`, preserved verbatim where it sent one. */
  readonly detail: string | null;

  constructor(kind: JevDemoErrorKind, status: number | null, detail: string | null) {
    super(detail ?? defaultMessage(kind));
    this.name = "JevDemoError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

function defaultMessage(kind: JevDemoErrorKind): string {
  switch (kind) {
    case "unreachable":
      return "Could not reach the evaluation backend.";
    case "unknown_scenario":
      return "That is not one of the three demo scenarios.";
    case "evaluation_failed":
      return "The evaluation could not be completed.";
    case "failed":
      return "The demo server could not answer.";
  }
}

/** Whether a rejection is an aborted request rather than a failure. */
export function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

/**
 * The three synthetic scenarios, with their workload arithmetic.
 *
 * Costs nothing: no model is asked anything, so the page shows all three
 * drafts and their exact figures before anybody decides to spend a call.
 */
export async function getScenarios(signal?: AbortSignal): Promise<JevDemoScenarioList> {
  return demoRequest<JevDemoScenarioList>("GET", "/api/v1/jev-demo/scenarios", signal);
}

/**
 * Run one synthetic scenario through Jev, then through the local policy.
 *
 * **A POST, and deliberately so**: it costs money, takes a second and calls a
 * third party, none of which should happen because a browser or a link
 * prefetcher followed a URL.
 *
 * Takes a scenario name and nothing else. There is no request body and no
 * field that accepts a person — the draft is built on the server, so nothing
 * typed in this app can reach the model.
 */
export async function evaluateScenario(
  scenario: string,
  signal?: AbortSignal,
): Promise<JevDemoEvaluation> {
  return demoRequest<JevDemoEvaluation>(
    "POST",
    `/api/v1/jev-demo/scenarios/${encodeURIComponent(scenario)}/evaluate`,
    signal,
  );
}

/** The one place a demo response becomes either data or a `JevDemoError`. */
async function demoRequest<T>(
  method: "GET" | "POST",
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${JEV_DEMO_BASE_PATH}${path}`, { method, signal });
  } catch (cause) {
    // An aborted request is the caller navigating away, not a failure.
    if (isAbortError(cause)) throw cause;
    throw new JevDemoError("unreachable", null, null);
  }

  const payload = await readJson(response);

  if (!response.ok) {
    throw new JevDemoError(kindForStatus(response.status), response.status, detailOf(payload));
  }
  return payload as T;
}

export function kindForStatus(status: number): JevDemoErrorKind {
  // 404 and 422 both mean the path did not name one of the three scenarios:
  // 422 when the enum refused it, 404 when no demo router is mounted at all.
  if (status === 404 || status === 422) return "unknown_scenario";
  // 502 is the demo server telling us TypeSafe did not give it a usable
  // answer. Its own `detail` explains which way, and is shown unchanged.
  if (status === 502) return "evaluation_failed";
  if (status >= 500) return "failed";
  return "failed";
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    // A proxy failing outside FastAPI can answer with HTML. That is still a
    // real status; it simply carries no `detail`.
    return null;
  }
}

/**
 * Pull a readable message out of whatever the server sent.
 *
 * FastAPI produces two shapes under the same key: our handlers send
 * `{"detail": "a sentence"}`, and request validation sends
 * `{"detail": [{loc, msg}]}`. A validation failure here is a wiring bug rather
 * than something a reader can act on, so only the string form is surfaced —
 * the rest becomes `null` and the client's own wording is used.
 */
function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail !== "string") return null;
  return detail.trim() === "" ? null : detail;
}
