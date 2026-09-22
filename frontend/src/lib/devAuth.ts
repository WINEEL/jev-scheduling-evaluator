/**
 * Local development identity, resolved on the server and nowhere else.
 *
 * **This is not authentication, and it is not a step toward it.** Google
 * sign-in is a later task. Until then the backend accepts an
 * `X-Dev-Actor-Person-Id` header naming the Person to act as, and only when
 * it is started with `CHURCH_SCHEDULING_DEV_AUTH=1`. This module decides
 * whether this app may send that header.
 *
 * **It fails closed at every step.** Four separate conditions must all hold,
 * and any one of them failing yields no header at all -- which produces a 401
 * from the backend, not a silent fallback to somebody:
 *
 * 1. the process is not running in production;
 * 2. `CHURCH_SCHEDULING_FRONTEND_DEV_AUTH` is exactly `"1"`;
 * 3. `CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID` is set;
 * 4. that value is a positive integer.
 *
 * There is deliberately **no default Person id**. A fallback -- "person 1",
 * "the first Admin" -- is the failure mode this whole design exists to
 * prevent: it would make a misconfigured deployment quietly authenticate
 * every visitor as somebody, which is worse than a broken page.
 *
 * The environment is taken as an argument so the rules can be tested exactly
 * as they will run, without mutating the real process environment.
 */

export const DEV_AUTH_FLAG = "CHURCH_SCHEDULING_FRONTEND_DEV_AUTH";
export const DEV_ACTOR_PERSON_ID = "CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID";
export const API_URL = "CHURCH_SCHEDULING_API_URL";
export const DEV_ACTOR_HEADER = "X-Dev-Actor-Person-Id";

/** Where FastAPI is listening locally. Not a secret, and never a database URL. */
const DEFAULT_API_URL = "http://127.0.0.1:8000";

export type EnvLike = Record<string, string | undefined>;

/**
 * The headers to add to a proxied request: either exactly one, or none.
 */
export function resolveDevActorHeaders(env: EnvLike): Record<string, string> {
  if (!isDevActorForwardingAllowed(env)) return {};

  const personId = readPersonId(env);
  if (personId === null) return {};

  return { [DEV_ACTOR_HEADER]: personId };
}

/**
 * Whether the *environment* permits forwarding at all, ignoring whether an id
 * happens to be configured. Split out so the UI can explain a misconfigured
 * setup without needing to know the id itself.
 */
export function isDevActorForwardingAllowed(env: EnvLike): boolean {
  if (env.NODE_ENV === "production") return false;
  return env[DEV_AUTH_FLAG] === "1";
}

/**
 * The configured Person id, or `null`.
 *
 * Parsed strictly rather than with `Number()`, which accepts `" 5 "`, `"5e0"`,
 * `"0x5"` and `"Infinity"` -- none of which is an id anybody meant to type.
 */
function readPersonId(env: EnvLike): string | null {
  const raw = env[DEV_ACTOR_PERSON_ID];
  if (typeof raw !== "string") return null;

  const candidate = raw.trim();
  if (!/^[0-9]+$/.test(candidate)) return null;
  if (Number(candidate) <= 0) return null;

  return candidate;
}

/** The backend's base URL. Trailing slashes are trimmed so joining is safe. */
export function resolveApiUrl(env: EnvLike): string {
  const configured = env[API_URL]?.trim();
  const base = configured && configured !== "" ? configured : DEFAULT_API_URL;
  return base.replace(/\/+$/, "");
}
