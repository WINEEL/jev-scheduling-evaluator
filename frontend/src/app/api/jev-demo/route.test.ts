/**
 * The proxy, and everything it refuses to carry.
 *
 * The route module is imported and called with real `NextRequest`s, with only
 * `fetch` and the environment replaced. The assertions that matter here are
 * the negative ones: a proxy in front of an API with no users should be
 * incapable of carrying a credential, and these pin that it is.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GET, JEV_DEMO_API_URL_ENV, POST } from "./[...path]/route";

interface Upstream {
  url: string;
  method: string;
  headers: Headers;
}

let upstreamCalls: Upstream[];

function mockUpstream(status: number, payload: string, extraHeaders: [string, string][] = []) {
  upstreamCalls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      upstreamCalls.push({
        url,
        method: init.method ?? "GET",
        headers: new Headers(init.headers),
      });
      const headers = new Headers({ "content-type": "application/json" });
      for (const [name, value] of extraHeaders) headers.append(name, value);
      return new Response(status === 204 ? null : payload, { status, headers });
    }),
  );
}

function unreachableUpstream() {
  upstreamCalls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("fetch failed");
    }),
  );
}

function context(path: string[]) {
  return { params: Promise.resolve({ path }) } as Parameters<typeof GET>[1];
}

/** A request carrying everything a browser might have picked up elsewhere. */
function requestWithSession(url: string, method = "GET") {
  return new NextRequest(url, {
    method,
    headers: {
      cookie: "session=a-signed-session-from-somewhere; other=value",
      authorization: "Bearer a-token-from-somewhere",
    },
  });
}

beforeEach(() => {
  upstreamCalls = [];
  vi.stubEnv(JEV_DEMO_API_URL_ENV, "http://127.0.0.1:8000");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("forwarding", () => {
  it("sends a GET to the demo server at the captured path", async () => {
    mockUpstream(200, '{"scenarios":[]}');

    const response = await GET(
      new NextRequest("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(upstreamCalls[0].url).toBe("http://127.0.0.1:8000/api/v1/jev-demo/scenarios");
    expect(upstreamCalls[0].method).toBe("GET");
    expect(response.status).toBe(200);
    expect(await response.text()).toBe('{"scenarios":[]}');
  });

  it("sends a POST to evaluate, and reports the status unchanged", async () => {
    mockUpstream(200, '{"policy":{"status":"acceptable"}}');

    const response = await POST(
      new NextRequest("http://localhost:3000/api/jev-demo/x", { method: "POST" }),
      context(["api", "v1", "jev-demo", "scenarios", "balanced", "evaluate"]),
    );

    expect(upstreamCalls[0].url).toBe(
      "http://127.0.0.1:8000/api/v1/jev-demo/scenarios/balanced/evaluate",
    );
    expect(upstreamCalls[0].method).toBe("POST");
    expect(response.status).toBe(200);
  });

  it("passes a 502 from the demo server through rather than softening it", async () => {
    mockUpstream(502, '{"detail":"The evaluation service could not be reached."}');

    const response = await POST(
      new NextRequest("http://localhost:3000/api/jev-demo/x", { method: "POST" }),
      context(["api", "v1", "jev-demo", "scenarios", "balanced", "evaluate"]),
    );

    expect(response.status).toBe(502);
    expect(await response.text()).toContain("could not be reached");
  });

  it("encodes each segment so a value cannot escape its path", async () => {
    mockUpstream(200, "{}");

    await POST(
      new NextRequest("http://localhost:3000/api/jev-demo/x", { method: "POST" }),
      context(["api", "v1", "jev-demo", "scenarios", "../../../etc/passwd", "evaluate"]),
    );

    expect(upstreamCalls[0].url).toContain("%2F..%2F");
    expect(upstreamCalls[0].url).not.toContain("/etc/passwd");
  });
});

describe("what it refuses to carry", () => {
  it("forwards no cookie, even when the browser sent one", async () => {
    // A cookie set by something else on the same origin must not travel to
    // the backend as though this app had chosen to send it.
    mockUpstream(200, "{}");

    await GET(
      requestWithSession("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(upstreamCalls[0].headers.get("cookie")).toBeNull();
  });

  it("forwards no Authorization header", async () => {
    // The backend authenticates nobody, so a token arriving here is either a
    // mistake or somebody probing. Either way it does not travel onward.
    mockUpstream(200, "{}");

    await GET(
      requestWithSession("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(upstreamCalls[0].headers.get("authorization")).toBeNull();
  });

  it("sends exactly one header upstream, and it is not a credential", async () => {
    mockUpstream(200, "{}");

    await GET(
      requestWithSession("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect([...upstreamCalls[0].headers.keys()]).toEqual(["accept"]);
  });

  it("copies no Set-Cookie back, so nothing here can establish a session", async () => {
    mockUpstream(200, "{}", [["set-cookie", "session=forged; Path=/"]]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(response.headers.getSetCookie()).toEqual([]);
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("copies no Location, so it can never redirect the browser elsewhere", async () => {
    // A proxy that forwarded a redirect could take somebody off this site on
    // the say-so of whatever it was pointed at.
    mockUpstream(302, "", [["location", "https://elsewhere.invalid/somewhere"]]);

    const response = await GET(
      new NextRequest("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(response.headers.get("location")).toBeNull();
  });
});

describe("configuration", () => {
  it("defaults to the port the documented command uses", async () => {
    // The demo must run for somebody who has configured nothing at all.
    vi.stubEnv(JEV_DEMO_API_URL_ENV, "");
    mockUpstream(200, "{}");

    await GET(
      new NextRequest("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );

    expect(upstreamCalls[0].url).toBe("http://127.0.0.1:8000/api/v1/jev-demo/scenarios");
  });

  it("reports an unreachable backend with the command that fixes it", async () => {
    // The commonest failure on a fresh clone is "the backend is not running",
    // and its fix is one line. Saying so beats a generic network error.
    unreachableUpstream();

    const response = await GET(
      new NextRequest("http://localhost:3000/api/jev-demo/api/v1/jev-demo/scenarios"),
      context(["api", "v1", "jev-demo", "scenarios"]),
    );
    const body = (await response.json()) as { detail: string };

    expect(response.status).toBe(502);
    expect(body.detail).toContain("evaluation backend");
    expect(body.detail).toContain("app.main:app");
  });
});
