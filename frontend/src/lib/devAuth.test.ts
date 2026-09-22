/**
 * The development-identity guard.
 *
 * This is the security-shaped part of the frontend, so the tests are written
 * as the rules themselves: it must fail closed, it must never invent a
 * Person, and it must refuse outright in a production build no matter what
 * the environment says.
 */

import { describe, expect, it } from "vitest";

import {
  API_URL,
  DEV_ACTOR_HEADER,
  DEV_ACTOR_PERSON_ID,
  DEV_AUTH_FLAG,
  isDevActorForwardingAllowed,
  resolveApiUrl,
  resolveDevActorHeaders,
  type EnvLike,
} from "./devAuth";

/** A correctly configured local development environment. */
function workingEnv(overrides: EnvLike = {}): EnvLike {
  return {
    NODE_ENV: "development",
    [DEV_AUTH_FLAG]: "1",
    [DEV_ACTOR_PERSON_ID]: "42",
    ...overrides,
  };
}

describe("development actor forwarding", () => {
  it("attaches the header when every condition is met", () => {
    expect(resolveDevActorHeaders(workingEnv())).toEqual({ [DEV_ACTOR_HEADER]: "42" });
  });

  it("13. has no fallback Person when the id is absent", () => {
    const env = workingEnv();
    delete env[DEV_ACTOR_PERSON_ID];

    expect(resolveDevActorHeaders(env)).toEqual({});
  });

  it("13b. invents no Person for a blank or unusable id", () => {
    for (const value of ["", "   ", "0", "-1", "abc", "1.5", "+5", "1e3", "0x2a", "Infinity", "٤٢"]) {
      expect(resolveDevActorHeaders(workingEnv({ [DEV_ACTOR_PERSON_ID]: value }))).toEqual({});
    }
  });

  it("13c. never falls back to person 1", () => {
    const env = workingEnv();
    delete env[DEV_ACTOR_PERSON_ID];
    delete env[DEV_AUTH_FLAG];

    const headers = resolveDevActorHeaders(env);

    expect(headers).toEqual({});
    expect(Object.values(headers)).not.toContain("1");
  });

  it("14. is off unless the flag is exactly \"1\"", () => {
    for (const value of [undefined, "", "0", "true", "yes", "TRUE", "on", "2", " 1"]) {
      const env = workingEnv({ [DEV_AUTH_FLAG]: value });
      expect(isDevActorForwardingAllowed(env)).toBe(false);
      expect(resolveDevActorHeaders(env)).toEqual({});
    }
  });

  it("14b. is off by default, with nothing configured at all", () => {
    expect(resolveDevActorHeaders({})).toEqual({});
    expect(isDevActorForwardingAllowed({})).toBe(false);
  });

  it("15. refuses in a production build even when fully configured", () => {
    const env = workingEnv({ NODE_ENV: "production" });

    expect(isDevActorForwardingAllowed(env)).toBe(false);
    expect(resolveDevActorHeaders(env)).toEqual({});
  });

  it("16. reads the id only from the environment it is given", () => {
    // Nothing is read from module scope, a constant, or the real process
    // environment: a different env object produces a different answer.
    expect(resolveDevActorHeaders(workingEnv({ [DEV_ACTOR_PERSON_ID]: "7" }))).toEqual({
      [DEV_ACTOR_HEADER]: "7",
    });
    expect(resolveDevActorHeaders(workingEnv({ [DEV_ACTOR_PERSON_ID]: "9001" }))).toEqual({
      [DEV_ACTOR_HEADER]: "9001",
    });
  });

  it("16b. trims surrounding whitespace but accepts nothing else", () => {
    expect(resolveDevActorHeaders(workingEnv({ [DEV_ACTOR_PERSON_ID]: " 42 " }))).toEqual({
      [DEV_ACTOR_HEADER]: "42",
    });
  });
});

describe("backend URL", () => {
  it("uses the configured API URL", () => {
    expect(resolveApiUrl({ [API_URL]: "http://localhost:9000" })).toBe("http://localhost:9000");
  });

  it("trims trailing slashes so paths join cleanly", () => {
    expect(resolveApiUrl({ [API_URL]: "http://localhost:9000///" })).toBe("http://localhost:9000");
  });

  it("falls back to the local backend when unset", () => {
    expect(resolveApiUrl({})).toBe("http://127.0.0.1:8000");
    expect(resolveApiUrl({ [API_URL]: "  " })).toBe("http://127.0.0.1:8000");
  });

  it("never exposes a database URL", () => {
    // The frontend has no business knowing one, and nothing here reads it.
    const env = { DATABASE_URL: "postgresql+psycopg://user:secret@host/db" };
    expect(resolveApiUrl(env)).toBe("http://127.0.0.1:8000");
    expect(JSON.stringify(resolveDevActorHeaders(env))).not.toContain("secret");
  });
});
