/**
 * The client: what it sends, and what it cannot express.
 *
 * `fetch` is replaced, so these run with no server and — the point worth
 * stating — no TypeSafe. Nothing in the frontend suite can reach a live model:
 * the only thing this client sends is a scenario name, and the evaluation
 * happens in Python behind an endpoint that is mocked here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  JEV_DEMO_BASE_PATH,
  JevDemoError,
  evaluateScenario,
  getScenarios,
  isAbortError,
  kindForStatus,
} from "./client";

interface Call {
  url: string;
  method: string;
  init: RequestInit | undefined;
}

let calls: Call[];

function mockFetch(status: number, payload: unknown, ok = status < 400) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method ?? "GET", init });
      return { ok, status, json: async () => payload } as unknown as Response;
    }),
  );
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the transport boundary", () => {
  it("calls this app's own origin, so the backend address stays server-side", async () => {
    mockFetch(200, { scenarios: [] });

    await getScenarios();

    expect(JEV_DEMO_BASE_PATH).toBe("/api/jev-demo");
    expect(calls[0].url).toBe("/api/jev-demo/api/v1/jev-demo/scenarios");
    expect(calls[0].url.startsWith("http")).toBe(false);
  });

  it("never asks for a user or an identity", async () => {
    mockFetch(200, { scenarios: [] });

    await getScenarios();
    await evaluateScenario("balanced").catch(() => undefined);

    for (const call of calls) {
      expect(call.url).not.toContain("/me");
      expect(call.url).not.toContain("auth");
    }
  });

  it("sends no credentials and no headers of its own", async () => {
    // A GET with no `credentials` option and no headers cannot carry an
    // Authorization header, and the demo proxy forwards no cookie upstream.
    mockFetch(200, { scenarios: [] });

    await getScenarios();

    expect(calls[0].init?.headers).toBeUndefined();
    expect(calls[0].init?.credentials).toBeUndefined();
  });

  it("GETs the scenario list and POSTs an evaluation with no body", async () => {
    // No request body anywhere: there is no field in which a real person could
    // travel to a third party.
    mockFetch(200, {});

    await getScenarios();
    await evaluateScenario("ambiguous");

    expect(calls[0].method).toBe("GET");
    expect(calls[1].method).toBe("POST");
    expect(calls[1].url).toBe("/api/jev-demo/api/v1/jev-demo/scenarios/ambiguous/evaluate");
    expect(calls[0].init?.body).toBeUndefined();
    expect(calls[1].init?.body).toBeUndefined();
  });

  it("encodes the scenario name into the path", async () => {
    mockFetch(200, {});

    await evaluateScenario("not a scenario/../etc");

    expect(calls[0].url).toBe(
      "/api/jev-demo/api/v1/jev-demo/scenarios/not%20a%20scenario%2F..%2Fetc/evaluate",
    );
  });
});

describe("results", () => {
  it("returns the scenarios the server sent", async () => {
    mockFetch(200, {
      scenarios: [{ name: "imbalanced" }, { name: "balanced" }, { name: "ambiguous" }],
    });

    const result = await getScenarios();

    expect(result.scenarios.map((entry) => entry.name)).toEqual([
      "imbalanced",
      "balanced",
      "ambiguous",
    ]);
  });

  it("returns the three-part evaluation intact", async () => {
    mockFetch(200, {
      state: { name: "balanced" },
      judgments: { model_name: "jev-test" },
      policy: { status: "acceptable", reasons: [], thresholds: {} },
    });

    const result = await evaluateScenario("balanced");

    expect(result.state.name).toBe("balanced");
    expect(result.judgments.model_name).toBe("jev-test");
    expect(result.policy.status).toBe("acceptable");
  });
});

describe("failures, in the demo's own vocabulary", () => {
  it("has no way to express an authentication failure", () => {
    // Deliberate: no demo response can be a 401, because there is nothing to
    // authenticate against. A client that could represent one would invite a
    // component to offer a sign-in.
    const kinds = [401, 403, 404, 422, 500, 502].map(kindForStatus);

    expect(kinds).not.toContain("unauthenticated");
    expect(new Set(kinds)).toEqual(new Set(["failed", "unknown_scenario", "evaluation_failed"]));
  });

  it("reports an unreachable server without a status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const error = await getScenarios().catch((cause) => cause);

    expect(error).toBeInstanceOf(JevDemoError);
    expect((error as JevDemoError).kind).toBe("unreachable");
    expect((error as JevDemoError).status).toBeNull();
  });

  it("reports a 502 as an evaluation failure, keeping the server's sentence", async () => {
    mockFetch(502, {
      detail: "The evaluation service could not be reached. This demo needs a live TypeSafe API key on the server.",
    });

    const error = (await evaluateScenario("balanced").catch((cause) => cause)) as JevDemoError;

    expect(error.kind).toBe("evaluation_failed");
    expect(error.detail).toContain("TypeSafe API key");
  });

  it("reports an unknown scenario as such, whether 404 or 422", async () => {
    mockFetch(422, { detail: [{ loc: ["path", "scenario"], msg: "Input should be..." }] });
    const invalid = (await evaluateScenario("nope").catch((cause) => cause)) as JevDemoError;

    mockFetch(404, { detail: "Not Found" });
    const missing = (await evaluateScenario("nope").catch((cause) => cause)) as JevDemoError;

    expect(invalid.kind).toBe("unknown_scenario");
    expect(missing.kind).toBe("unknown_scenario");
    // FastAPI's validation array is not rendered at a reader: it is a wiring
    // bug, not something anybody can act on.
    expect(invalid.detail).toBeNull();
  });

  it("rethrows an abort rather than reporting it as a failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("aborted", "AbortError");
      }),
    );

    const error = await evaluateScenario("balanced").catch((cause) => cause);

    expect(isAbortError(error)).toBe(true);
    expect(error).not.toBeInstanceOf(JevDemoError);
  });
});
