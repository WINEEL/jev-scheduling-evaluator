/**
 * The two halves of the transport, composed.
 *
 * `lib/client.test.ts` pins the path the browser asks for. `route.test.ts`
 * pins what the proxy does with the segments it captures. Both passed while
 * the question that actually decides whether the demo runs went unasked: do
 * they *compose* into a URL the standalone backend serves?
 *
 * They do, and the split is deliberate — the client names the backend's full
 * path (`/api/v1/jev-demo/...`) and `JEV_DEMO_API_URL` names only an origin —
 * but nothing pinned the seam, so reading either file alone invites the
 * conclusion that the default is wrong. These tests run the real client into
 * the real route handler with only the far end of the network stubbed, so the
 * documented setup is asserted end to end rather than in two halves.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { evaluateScenario, getScenarios } from "@/lib/client";

import { GET, JEV_DEMO_API_URL_ENV, POST } from "./[...path]/route";

/** Where the backend serves the demo when it is started as documented. */
const STANDALONE_SCENARIOS = "http://127.0.0.1:8000/api/v1/jev-demo/scenarios";

/**
 * Run one client call the whole way through the proxy.
 *
 * The client's `fetch` is captured rather than answered, the path it asked for
 * is split exactly as Next's `[...path]` would split it, and those segments go
 * to the real handler. What comes back is the URL the backend would have been
 * asked for.
 */
async function upstreamUrlFor(call: () => Promise<unknown>): Promise<string> {
  let browserUrl = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      browserUrl = url;
      return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
    }),
  );
  await call();

  const captured = browserUrl.replace(/^\/api\/jev-demo\//, "");
  expect(captured, "the client must call this app's own proxy").not.toBe(browserUrl);
  const segments = captured.split("/");

  let upstreamUrl = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      upstreamUrl = url;
      return new Response("{}", { headers: { "content-type": "application/json" } });
    }),
  );

  const request = new NextRequest(`http://localhost:3000${browserUrl}`, {
    method: segments.includes("evaluate") ? "POST" : "GET",
  });
  const context = { params: Promise.resolve({ path: segments }) };
  if (segments.includes("evaluate")) {
    await POST(request, context as Parameters<typeof POST>[1]);
  } else {
    await GET(request, context as Parameters<typeof GET>[1]);
  }
  return upstreamUrl;
}

beforeEach(() => {
  vi.unstubAllEnvs();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("the documented local setup, with nothing configured", () => {
  it("asks the standalone backend for its scenarios", async () => {
    // `npm run dev`, backend on port 8000, `JEV_DEMO_API_URL` never set. This
    // is the path a fresh clone takes, and it has to land on a real route.
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "");

    expect(await upstreamUrlFor(() => getScenarios())).toBe(STANDALONE_SCENARIOS);
  });

  it("asks the standalone backend to evaluate one named scenario", async () => {
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "");

    expect(await upstreamUrlFor(() => evaluateScenario("ambiguous"))).toBe(
      "http://127.0.0.1:8000/api/v1/jev-demo/scenarios/ambiguous/evaluate",
    );
  });

  it("needs no environment variable at all", async () => {
    // Not merely "empty is handled": the variable is absent, as it is for
    // somebody who copied `.env.example` and set only their TypeSafe key.
    expect(await upstreamUrlFor(() => getScenarios())).toBe(STANDALONE_SCENARIOS);
  });
});

describe("the override, for a backend somewhere else", () => {
  it("uses the configured origin and keeps the backend's own path", async () => {
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "http://demo-backend.internal:9000");

    expect(await upstreamUrlFor(() => getScenarios())).toBe(
      "http://demo-backend.internal:9000/api/v1/jev-demo/scenarios",
    );
  });

  it("tolerates a trailing slash rather than doubling it", async () => {
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "http://demo-backend.internal:9000/");

    expect(await upstreamUrlFor(() => getScenarios())).toBe(
      "http://demo-backend.internal:9000/api/v1/jev-demo/scenarios",
    );
  });
});

describe("what the composed URL must never become", () => {
  it("names no scheduler, no auth and no backend-of-a-backend path", async () => {
    // This demo stands alone. A path segment borrowed from the application it
    // grew out of would mean the standalone copy had quietly reacquired a
    // dependency on it.
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "");
    const url = await upstreamUrlFor(() => getScenarios());

    for (const forbidden of [
      "/api/backend",
      "/auth",
      "/login",
      "/session",
      "/scheduler",
      "/schedules",
      "/me",
    ]) {
      expect(url, forbidden).not.toContain(forbidden);
    }
  });

  it("asks one origin, and it is the one the backend was started on", async () => {
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "");
    const url = new URL(await upstreamUrlFor(() => getScenarios()));

    expect(url.origin).toBe("http://127.0.0.1:8000");
    expect(url.pathname).toBe("/api/v1/jev-demo/scenarios");
    expect(url.search).toBe("");
  });
});
