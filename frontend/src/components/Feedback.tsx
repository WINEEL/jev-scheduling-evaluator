/**
 * Loading, error and empty states, shared by every screen.
 *
 * The error component is the important one: it turns an `ApiError` into
 * something a Ministry Head can act on. It always shows the backend's own
 * `detail` where there is one, because that sentence was written by the rule
 * that refused -- and adds a line of local context for the two failures a
 * person cannot do anything about from inside the app (the backend being
 * down, and development identity not being configured).
 */

import { ApiError, type ApiErrorKind } from "@/lib/api/errors";

export function LoadingNotice({ what }: { what: string }) {
  return (
    <p className="muted" role="status">
      Loading {what}…
    </p>
  );
}

export function EmptyNotice({ children }: { children: React.ReactNode }) {
  return <div className="notice notice--info">{children}</div>;
}

/** Extra guidance for failures whose fix is outside this screen. */
function guidanceFor(kind: ApiErrorKind): string | null {
  switch (kind) {
    case "network":
      return "The scheduling API did not respond. Check that the backend is running.";
    case "unauthenticated":
      return "No signed-in person could be established. For local development, both the backend and this app need their development identity settings configured — see the README.";
    case "server":
      return "This is a problem on the server, not with what you did.";
    default:
      return null;
  }
}

function titleFor(kind: ApiErrorKind): string {
  switch (kind) {
    case "unauthenticated":
      return "Not signed in";
    case "forbidden":
      return "You do not have access";
    case "not_found":
      return "Not found";
    case "conflict":
      return "That cannot be done right now";
    case "invalid":
      return "That request could not be used";
    case "server":
      return "Something went wrong on the server";
    case "network":
      return "Could not reach the scheduling API";
  }
}

export function ErrorNotice({ error, title }: { error: ApiError; title?: string }) {
  const guidance = guidanceFor(error.kind);
  const tone = error.kind === "forbidden" || error.kind === "conflict" ? "warning" : "danger";

  return (
    <div className={`notice notice--${tone}`} role="alert">
      <p className="notice__title">{title ?? titleFor(error.kind)}</p>
      {error.detail !== null && <p>{error.detail}</p>}
      {guidance !== null && <p className="small">{guidance}</p>}
    </div>
  );
}

/** Anything that is not an `ApiError` -- a genuine bug in this app. */
export function UnexpectedErrorNotice() {
  return (
    <div className="notice notice--danger" role="alert">
      <p className="notice__title">Something went wrong</p>
      <p>The page could not be displayed. Reloading may help.</p>
    </div>
  );
}

export function asApiError(cause: unknown): ApiError | null {
  return cause instanceof ApiError ? cause : null;
}
