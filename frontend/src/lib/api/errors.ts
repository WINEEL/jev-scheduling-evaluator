/**
 * One error type for every way an API call can fail.
 *
 * Components should never inspect a status code. They ask what *kind* of
 * failure it was and show the backend's own `detail` message, which is
 * written for humans -- "You do not manage Setup.", "cannot create the
 * initial schedule version while this period's availability is still open".
 * Re-wording those here would drift from what the backend actually enforces.
 */

export type ApiErrorKind =
  /** 401 -- no active actor could be established. Locally this almost always
   *  means the development identity is not configured on both sides. */
  | "unauthenticated"
  /** 403 -- identity was established, permission was not. */
  | "forbidden"
  /** 404 -- no such ministry, period or schedule. */
  | "not_found"
  /** 409 -- the request was well formed but the thing's current state
   *  disagrees: availability still open, or a schedule already started. */
  | "conflict"
  /** 422 -- the values sent cannot be used. */
  | "invalid"
  /** 5xx -- the server failed. Not the caller's mistake. */
  | "server"
  /** The request never completed: backend down, wrong URL, DNS, offline. */
  | "network";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  /** `null` when the request never reached the server. */
  readonly status: number | null;
  /** The backend's own `detail`, preserved verbatim where it sent one. */
  readonly detail: string | null;

  constructor(kind: ApiErrorKind, status: number | null, detail: string | null) {
    super(detail ?? defaultMessage(kind));
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Whether a rejection is an aborted request rather than a failure.
 *
 * **An abort is not an outcome.** `AbortController.abort()` is called from an
 * effect's cleanup, which means one of exactly two things: the component is
 * unmounting, or a newer request has replaced this one and is still in
 * flight. Neither is "the load finished". A caller that treats an abort as a
 * settled result reports "no data, no error", which renders as a permanent
 * failure for a request that was never allowed to answer.
 *
 * `fetch` rejects an aborted request with a `DOMException` named
 * `AbortError`, and `request()` rethrows it unchanged so this stays true of
 * every call in the client.
 */
export function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

export function kindForStatus(status: number): ApiErrorKind {
  switch (status) {
    case 401:
      return "unauthenticated";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 422:
      return "invalid";
    default:
      return status >= 500 ? "server" : "invalid";
  }
}

/** Used only when the backend sent no usable `detail`. */
function defaultMessage(kind: ApiErrorKind): string {
  switch (kind) {
    case "unauthenticated":
      return "Not signed in.";
    case "forbidden":
      return "You do not have access to this.";
    case "not_found":
      return "Not found.";
    case "conflict":
      return "That cannot be done right now.";
    case "invalid":
      return "The request could not be processed.";
    case "server":
      return "The server ran into a problem.";
    case "network":
      return "Could not reach the server.";
  }
}

/**
 * Pull a readable message out of whatever the backend sent.
 *
 * FastAPI produces two different shapes under the same key: our own handlers
 * send `{"detail": "a sentence"}`, and request validation sends
 * `{"detail": [{loc, msg, ...}]}`. Both are flattened here so a caller never
 * has to know which it got -- and anything else returns `null` rather than a
 * JSON blob rendered at a user.
 */
export function extractDetail(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;

  if (typeof detail === "string") {
    return detail.trim() === "" ? null : detail;
  }

  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => validationMessage(entry))
      .filter((part): part is string => part !== null);
    return parts.length > 0 ? parts.join("; ") : null;
  }

  return null;
}

function validationMessage(entry: unknown): string | null {
  if (typeof entry !== "object" || entry === null) return null;
  const { msg, loc } = entry as { msg?: unknown; loc?: unknown };
  if (typeof msg !== "string") return null;

  // "body.target_assignments_per_candidate: Input should be a valid integer"
  if (Array.isArray(loc) && loc.length > 0) {
    const field = loc
      .filter((part): part is string | number => typeof part === "string" || typeof part === "number")
      .join(".");
    return field === "" ? msg : `${field}: ${msg}`;
  }
  return msg;
}
