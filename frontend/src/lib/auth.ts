/**
 * The browser's half of Google sign-in: three URLs and a message table.
 *
 * **There is deliberately almost nothing here.** Every security decision --
 * generating and checking OAuth state, validating Google's response, deciding
 * which Person an address belongs to, minting the session -- happens on the
 * server, and none of it is reachable from this file. The browser's entire
 * role is to navigate to a URL and to render a sentence explaining a refusal.
 *
 * **`SIGN_IN_PATH` must be followed as a navigation, never fetched.** It
 * answers with a 302 to Google's consent screen, which the person has to see
 * and interact with. `fetch` would either follow it invisibly or fail CORS;
 * either way nobody would ever get to sign in. So it is rendered as an `<a>`,
 * not a button with a handler.
 *
 * Signing *out* is the opposite -- an ordinary backend call -- so it lives in
 * `lib/api/client.ts` as `logout()`, with every other call to the backend,
 * rather than here.
 */

import { API_BASE_PATH, logout } from "./api/client";

/** Start Google sign-in. A navigation target, not a fetch target. */
export const SIGN_IN_PATH = `${API_BASE_PATH}/api/v1/auth/google/login`;

/**
 * The query parameter the backend redirects back with when sign-in is
 * refused. Its values are the fixed reason codes in `app/auth/errors.py`.
 */
export const AUTH_ERROR_PARAM = "auth_error";

/**
 * What a refused sign-in is allowed to say.
 *
 * **One message, for every reason.** This used to be a table that explained
 * each refusal -- that the address was not linked, that an administrator
 * arranges access in advance, that the account was deactivated. Every one of
 * those sentences told an unauthenticated stranger something true about how
 * this organisation manages access, and the person who most needed the detail
 * (an administrator) can read the precise reason in the server log instead.
 *
 * So the browser gets a full stop. The backend still distinguishes the cases
 * and still records which one occurred; only the public wording is flattened.
 */
export const ACCESS_UNAVAILABLE_MESSAGE = "Access unavailable.";

/**
 * Whether the callback refused a sign-in attempt.
 *
 * The *reason* is deliberately not returned. Nothing rendered to the browser
 * varies by it, so exposing it here would only invite a future component to
 * start explaining again.
 */
export function wasSignInRefused(reason: string | null | undefined): boolean {
  return typeof reason === "string" && reason !== "";
}

/**
 * Sign out, then leave the page.
 *
 * **A full document load, not a client-side route change.** It discards every
 * piece of component state in the tab, so no ministry name, schedule or
 * person's details stays on screen after the session that was allowed to see
 * them has ended. `router.push("/")` would re-render from the same mounted
 * tree, and a component that already had data would keep showing it.
 *
 * The target is built as an absolute URL against the current origin -- the app
 * root, and nowhere else -- so this can never be steered somewhere off-site.
 *
 * **A failed request still navigates.** If the POST did not reach the server,
 * the person believes they are signed out, so the app should stop showing them
 * data either way; the next request is refused anyway if the session really is
 * still alive.
 */
export async function signOut(): Promise<void> {
  try {
    await logout();
  } catch {
    // Deliberately swallowed -- see above. There is no failure mode here worth
    // keeping somebody on a page they have asked to leave.
  }
  window.location.href = new URL("/", window.location.origin).toString();
}
