/**
 * The proxy handler, exercised as it will actually run.
 *
 * The route module is imported and called with real `NextRequest`s, with only
 * `fetch` and the environment replaced. What matters is that it forwards
 * faithfully and reports the backend's answer unchanged -- a proxy that
 * softened a 403 into a 200 would defeat every permission rule behind it.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEV_ACTOR_HEADER } from "@/lib/devAuth";

import { DELETE, GET, PATCH, POST, PUT } from "./[...path]/route";

interface Upstream {
  url: string;
  method: string;
  headers: Headers;
  body: string | undefined;
}

let upstreamCalls: Upstream[];

function mockUpstream(
  status: number,
  payload: string,
  contentType = "application/json",
  /** Extra response headers the backend sends -- Set-Cookie, Location (Task 76). */
  extraHeaders: [string, string][] = [],
) {
  upstreamCalls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      upstreamCalls.push({
        url,
        method: init.method ?? "GET",
        headers: new Headers(init.headers),
        body: typeof init.body === "string" ? init.body : undefined,
      });
      // A 204 (Task 54's DELETE response) must have a null body -- the Fetch
      // spec's Response constructor throws even on an empty string for a
      // null-body status, so this cannot use `payload` unconditionally.
      const body = status === 204 || (status >= 300 && status < 400) ? null : payload;
      const headers = new Headers({ "content-type": contentType });
      for (const [name, value] of extraHeaders) headers.append(name, value);
      return new Response(body, { status, headers });
    }),
  );
}

function context(path: string[]) {
  return { params: Promise.resolve({ path }) } as Parameters<typeof GET>[1];
}

beforeEach(() => {
  upstreamCalls = [];
  vi.stubEnv("CHURCH_SCHEDULING_API_URL", "http://127.0.0.1:8000");
  vi.stubEnv("CHURCH_SCHEDULING_FRONTEND_DEV_AUTH", "1");
  vi.stubEnv("CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID", "42");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("forwarding", () => {
  it("preserves the path and query string", async () => {
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me?verbose=1"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].url).toBe("http://127.0.0.1:8000/api/v1/me?verbose=1");
    expect(upstreamCalls[0].method).toBe("GET");
  });

  it("forwards a JSON body on POST", async () => {
    mockUpstream(201, "{}");

    await POST(
      new NextRequest("http://localhost:3000/api/backend/api/v1/schedule-versions/7/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ allow_no_response: true }),
      }),
      context(["api", "v1", "schedule-versions", "7", "generate"]),
    );

    expect(upstreamCalls[0].method).toBe("POST");
    expect(upstreamCalls[0].body).toBe('{"allow_no_response":true}');
    expect(upstreamCalls[0].headers.get("content-type")).toBe("application/json");
  });

  it("forwards a JSON body on PATCH (Task 53)", async () => {
    mockUpstream(200, "{}");

    await PATCH(
      new NextRequest("http://localhost:3000/api/backend/api/v1/ministry-roles/7", {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: "Registration" }),
      }),
      context(["api", "v1", "ministry-roles", "7"]),
    );

    expect(upstreamCalls[0].method).toBe("PATCH");
    expect(upstreamCalls[0].body).toBe('{"name":"Registration"}');
    expect(upstreamCalls[0].headers.get("content-type")).toBe("application/json");
  });

  it("forwards a JSON body on PUT (Task 54)", async () => {
    mockUpstream(200, "{}");

    await PUT(
      new NextRequest(
        "http://localhost:3000/api/backend/api/v1/events/700/staffing-requirements/12",
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ required_count: 2 }),
        },
      ),
      context(["api", "v1", "events", "700", "staffing-requirements", "12"]),
    );

    expect(upstreamCalls[0].method).toBe("PUT");
    expect(upstreamCalls[0].body).toBe('{"required_count":2}');
    expect(upstreamCalls[0].headers.get("content-type")).toBe("application/json");
  });

  it("forwards a bodyless DELETE and its query string (Task 54)", async () => {
    mockUpstream(204, "");

    const response = await DELETE(
      new NextRequest(
        "http://localhost:3000/api/backend/api/v1/events/700/staffing-requirements/12?reason=No%20longer%20needed",
        { method: "DELETE" },
      ),
      context(["api", "v1", "events", "700", "staffing-requirements", "12"]),
    );

    expect(upstreamCalls[0].method).toBe("DELETE");
    expect(upstreamCalls[0].body).toBeUndefined();
    expect(upstreamCalls[0].url).toBe(
      "http://127.0.0.1:8000/api/v1/events/700/staffing-requirements/12?reason=No%20longer%20needed",
    );
    expect(response.status).toBe(204);
  });

  it("17. preserves the backend's status and body exactly", async () => {
    for (const status of [200, 201, 401, 403, 404, 409, 422, 500]) {
      mockUpstream(status, JSON.stringify({ detail: `status ${status}` }));

      const response = await GET(
        new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
        context(["api", "v1", "me"]),
      );

      expect(response.status).toBe(status);
      expect(await response.json()).toEqual({ detail: `status ${status}` });
    }
  });

  it("17b. never turns a refusal into a success", async () => {
    mockUpstream(403, JSON.stringify({ detail: "You do not manage Setup." }));

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/schedule-versions/7"),
      context(["api", "v1", "schedule-versions", "7"]),
    );

    expect(response.status).not.toBe(200);
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "You do not manage Setup." });
  });

  it("reports an unreachable backend as a readable error, not a crash", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ detail: "Could not reach the scheduling API." });
  });
});

describe("development actor header", () => {
  it("attaches it when local development is configured", async () => {
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBe("42");
  });

  it("15b. sends no actor header in a production build", async () => {
    vi.stubEnv("NODE_ENV", "production");
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBeNull();
  });

  it("14c. sends no actor header when the flag is not set", async () => {
    vi.stubEnv("CHURCH_SCHEDULING_FRONTEND_DEV_AUTH", "");
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBeNull();
  });

  it("13d. sends no actor header when no id is configured", async () => {
    vi.stubEnv("CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID", "");
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBeNull();
  });

  it("ignores an actor header the browser tried to set itself", async () => {
    vi.stubEnv("CHURCH_SCHEDULING_FRONTEND_DEV_AUTH", "");
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me", {
        headers: { [DEV_ACTOR_HEADER]: "999" },
      }),
      context(["api", "v1", "me"]),
    );

    // Only headers this handler chooses are forwarded; the client cannot name
    // its own actor by setting the header itself.
    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBeNull();
  });
});


// ==========================================================================
// Task 76: the proxy carries the sign-in session
// ==========================================================================

/**
 * These are what make the whole authentication topology work. The browser only
 * ever talks to this app's origin, so the session cookie FastAPI signs has to
 * travel through this handler in both directions -- and the redirect to
 * Google's consent screen has to survive it. If any of the three stopped being
 * forwarded, sign-in would break in a way no backend test could see.
 */
describe("session and redirect forwarding (Task 76)", () => {
  it("forwards the browser's cookies to the backend", async () => {
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me", {
        headers: { cookie: "church_scheduling_session=signed-value" },
      }),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get("cookie")).toBe(
      "church_scheduling_session=signed-value",
    );
  });

  it("sends no cookie header when the browser sent none", async () => {
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me"),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get("cookie")).toBeNull();
  });

  it("returns the backend's Set-Cookie to the browser", async () => {
    mockUpstream(303, "", "application/json", [
      ["set-cookie", "church_scheduling_session=new; path=/; httponly; samesite=lax"],
    ]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/auth/google/callback?code=c&state=s"),
      context(["api", "v1", "auth", "google", "callback"]),
    );

    expect(response.headers.getSetCookie()).toEqual([
      "church_scheduling_session=new; path=/; httponly; samesite=lax",
    ]);
  });

  it("returns every Set-Cookie, not just the first", async () => {
    // Sign-in sets the session and clears Authlib's transient state in one
    // response. `Headers.get("set-cookie")` would collapse them into one
    // string and the browser would end up with a single malformed cookie.
    mockUpstream(303, "", "application/json", [
      ["set-cookie", "church_scheduling_session=new; path=/"],
      ["set-cookie", "oauth_state=; path=/; max-age=0"],
    ]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/auth/google/callback"),
      context(["api", "v1", "auth", "google", "callback"]),
    );

    expect(response.headers.getSetCookie()).toHaveLength(2);
  });

  it("forwards a redirect to Google without following it", async () => {
    // `redirect: "manual"` is what keeps the 302 intact. If fetch followed it,
    // the *server* would go to Google's consent screen instead of the person.
    mockUpstream(302, "", "application/json", [
      ["location", "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y"],
    ]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/auth/google/login"),
      context(["api", "v1", "auth", "google", "login"]),
    );

    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe(
      "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y",
    );
    expect(upstreamCalls).toHaveLength(1);
    expect(vi.mocked(fetch).mock.calls[0][1]).toMatchObject({ redirect: "manual" });
  });

  it("forwards the relative redirect back into the app", async () => {
    mockUpstream(303, "", "application/json", [["location", "/?auth_error=not_linked"]]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/auth/google/callback"),
      context(["api", "v1", "auth", "google", "callback"]),
    );

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/?auth_error=not_linked");
  });

  it("preserves the OAuth code and state on the callback", async () => {
    mockUpstream(303, "", "application/json", [["location", "/"]]);

    await GET(
      new NextRequest(
        "http://localhost:3000/api/backend/api/v1/auth/google/callback?code=abc123&state=xyz789",
      ),
      context(["api", "v1", "auth", "google", "callback"]),
    );

    expect(upstreamCalls[0].url).toBe(
      "http://127.0.0.1:8000/api/v1/auth/google/callback?code=abc123&state=xyz789",
    );
  });

  it("forwards a logout POST and returns its 204", async () => {
    mockUpstream(204, "", "application/json", [
      ["set-cookie", "church_scheduling_session=null; path=/; max-age=0"],
    ]);

    const response = await POST(
      new NextRequest("http://localhost:3000/api/backend/api/v1/auth/logout", {
        method: "POST",
        headers: { cookie: "church_scheduling_session=signed-value" },
      }),
      context(["api", "v1", "auth", "logout"]),
    );

    expect(response.status).toBe(204);
    expect(upstreamCalls[0].method).toBe("POST");
    expect(response.headers.getSetCookie()).toHaveLength(1);
  });

  it("still sends the server's dev actor header, not one the browser supplied", async () => {
    // The dev header is set after the cookie, so a browser that sent its own
    // cannot have it survive alongside a forwarded session.
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/backend/api/v1/me", {
        headers: {
          cookie: "church_scheduling_session=x",
          [DEV_ACTOR_HEADER]: "999",
        },
      }),
      context(["api", "v1", "me"]),
    );

    expect(upstreamCalls[0].headers.get(DEV_ACTOR_HEADER)).toBe("42");
  });
});
