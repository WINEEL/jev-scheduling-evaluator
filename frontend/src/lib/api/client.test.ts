/**
 * The API client: what it sends, and what it makes of what comes back.
 *
 * `fetch` is replaced so these run with no server. What is being checked is
 * the contract in both directions -- the exact method, path and body each
 * call produces, and that every failure the backend can return arrives at a
 * component as a distinguishable kind with the backend's own words intact.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  API_BASE_PATH,
  clearAvailability,
  clearMinInterveningEvents,
  clearServingLimit,
  clearStaffingRequirement,
  createMinistryRole,
  deactivateMinistryRole,
  generateSchedule,
  getEventAvailability,
  getEventStaffing,
  getMe,
  getMinistryRoles,
  getPeriodEvents,
  getRoleQualifications,
  getScheduleVersion,
  getSchedulingPeriods,
  getSchedulingRules,
  getServingLimits,
  lockAvailability,
  reactivateMinistryRole,
  setAvailability,
  setMinInterveningEvents,
  setRoleQualification,
  setServingLimit,
  setStaffingRequirement,
  startFirstSchedule,
  updateMinistryRole,
} from "./client";
import { ApiError } from "./errors";

interface Call {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string> | undefined;
}

let calls: Call[];

function mockFetch(status: number, payload: unknown, ok = status < 400) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({
      url,
      method: init?.method ?? "GET",
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      headers: init?.headers as Record<string, string> | undefined,
    });
    return {
      ok,
      status,
      json: async () => payload,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

// -- 1-5: request mapping --------------------------------------------------

describe("request mapping", () => {
  it("1. getMe issues a GET to the identity endpoint with no body", async () => {
    mockFetch(200, { person_id: 7, display_name: "Ada", is_admin: false, headed_ministries: [] });

    const actor = await getMe();

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/me`);
    expect(calls[0].method).toBe("GET");
    expect(calls[0].body).toBeUndefined();
    // No Content-Type on a bodyless request.
    expect(calls[0].headers).toBeUndefined();
    expect(actor.display_name).toBe("Ada");
  });

  it("2. getSchedulingPeriods puts the ministry in the path", async () => {
    mockFetch(200, { ministry_id: 5, ministry_name: "Setup", periods: [] });

    const result = await getSchedulingPeriods(5);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministries/5/scheduling-periods`);
    expect(calls[0].method).toBe("GET");
    expect(result.ministry_name).toBe("Setup");
  });

  it("3. startFirstSchedule POSTs to the period, sending a note when given", async () => {
    mockFetch(201, { schedule_version_id: 21 });

    await startFirstSchedule(12, "Initial Q4 schedule");

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/scheduling-periods/12/schedule-versions`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ notes: "Initial Q4 schedule" });
  });

  it("3b. a blank or omitted note is left out rather than sent as whitespace", async () => {
    mockFetch(201, {});
    await startFirstSchedule(12, "   ");
    expect(calls[0].body).toEqual({});

    await startFirstSchedule(12);
    expect(calls[1].body).toEqual({});
  });

  it("4. generateSchedule sends the full policy, with variety roles null", async () => {
    mockFetch(200, { schedule_version_id: 21 });

    await generateSchedule(21, { allow_no_response: true, target_assignments_per_candidate: 3 });

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/schedule-versions/21/generate`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({
      allow_no_response: true,
      target_assignments_per_candidate: 3,
      role_variety_role_ids: null,
    });
  });

  it("4b. an unset policy sends the backend's own defaults explicitly", async () => {
    mockFetch(200, {});

    await generateSchedule(21, {});

    expect(calls[0].body).toEqual({
      allow_no_response: false,
      target_assignments_per_candidate: null,
      role_variety_role_ids: null,
    });
  });

  it("5. getScheduleVersion issues a GET for one version", async () => {
    mockFetch(200, { schedule_version: { id: 21 } });

    await getScheduleVersion(21);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/schedule-versions/21`);
    expect(calls[0].method).toBe("GET");
  });

  it("6. getMinistryRoles issues a GET, adding the query only when requested", async () => {
    mockFetch(200, { ministry_id: 5, ministry_name: "Kids", roles: [] });
    await getMinistryRoles(5, false);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministries/5/roles`);
    expect(calls[0].method).toBe("GET");

    await getMinistryRoles(5, true);
    expect(calls[1].url).toBe(`${API_BASE_PATH}/api/v1/ministries/5/roles?include_inactive=true`);
  });

  it("7. createMinistryRole POSTs to the ministry, sending null for a blank description", async () => {
    mockFetch(201, { ministry_role_id: 700, name: "Check-in" });

    await createMinistryRole(5, { name: "Check-in", description: "Greets families." });
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministries/5/roles`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({ name: "Check-in", description: "Greets families." });

    await createMinistryRole(5, { name: "Check-in", description: "   " });
    expect(calls[1].body).toEqual({ name: "Check-in", description: null });

    await createMinistryRole(5, { name: "Check-in" });
    expect(calls[2].body).toEqual({ name: "Check-in", description: null });
  });

  it("8. updateMinistryRole PATCHes the role by id", async () => {
    mockFetch(200, { ministry_role_id: 700, name: "Registration" });

    await updateMinistryRole(700, { name: "Registration", description: "New text." });

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministry-roles/700`);
    expect(calls[0].method).toBe("PATCH");
    expect(calls[0].body).toEqual({ name: "Registration", description: "New text." });
  });

  it("9. deactivateMinistryRole and reactivateMinistryRole POST with no body fields", async () => {
    mockFetch(200, { ministry_role_id: 700, name: "Check-in", deactivated_at: "2026-09-01T00:00:00Z" });
    await deactivateMinistryRole(700);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministry-roles/700/deactivate`);
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body).toEqual({});

    await reactivateMinistryRole(700);
    expect(calls[1].url).toBe(`${API_BASE_PATH}/api/v1/ministry-roles/700/reactivate`);
    expect(calls[1].method).toBe("POST");
    expect(calls[1].body).toEqual({});
  });

  it("10. getPeriodEvents issues a GET for a period's events", async () => {
    mockFetch(200, { scheduling_period_id: 500, events: [] });

    await getPeriodEvents(500);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/scheduling-periods/500/events`);
    expect(calls[0].method).toBe("GET");
    expect(calls[0].body).toBeUndefined();
  });

  it("11. getEventStaffing issues a GET for one event's staffing", async () => {
    mockFetch(200, { event_id: 700, roles: [] });

    await getEventStaffing(700);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/staffing-requirements`);
    expect(calls[0].method).toBe("GET");
  });

  it("12. setStaffingRequirement PUTs the count to the event/role pair", async () => {
    mockFetch(200, { ministry_role_id: 12, required_count: 2 });

    await setStaffingRequirement(700, 12, 2);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/staffing-requirements/12`);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ required_count: 2 });
  });

  it("13. clearStaffingRequirement DELETEs with no body", async () => {
    mockFetch(204, null);

    await clearStaffingRequirement(700, 12);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/staffing-requirements/12`);
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].body).toBeUndefined();
  });

  it("14. getRoleQualifications issues a GET, adding the query only when requested", async () => {
    mockFetch(200, { ministry_role_id: 12, memberships: [] });
    await getRoleQualifications(12, false);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministry-roles/12/qualifications`);
    expect(calls[0].method).toBe("GET");

    await getRoleQualifications(12, true);
    expect(calls[1].url).toBe(
      `${API_BASE_PATH}/api/v1/ministry-roles/12/qualifications?include_inactive=true`,
    );
  });

  it("15. setRoleQualification PUTs the decision to the role/membership pair", async () => {
    mockFetch(200, { ministry_membership_id: 118, is_qualified: true });

    await setRoleQualification(12, 118, true);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/ministry-roles/12/qualifications/118`);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ is_qualified: true });

    await setRoleQualification(12, 118, false);
    expect(calls[1].body).toEqual({ is_qualified: false });
  });

  it("16. getEventAvailability issues a GET, adding the query only when requested", async () => {
    mockFetch(200, { event_id: 700, memberships: [] });
    await getEventAvailability(700, false);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/availability`);
    expect(calls[0].method).toBe("GET");

    await getEventAvailability(700, true);
    expect(calls[1].url).toBe(`${API_BASE_PATH}/api/v1/events/700/availability?include_inactive=true`);
  });

  it("17. setAvailability PUTs the answer to the event/membership pair", async () => {
    mockFetch(200, { ministry_membership_id: 118, availability_state: "BACKUP" });

    await setAvailability(700, 118, "BACKUP");

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/availability/118`);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ availability_state: "BACKUP" });
  });

  it("18. clearAvailability DELETEs with no body", async () => {
    mockFetch(204, null);

    await clearAvailability(700, 118);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/events/700/availability/118`);
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].body).toBeUndefined();
  });

  it("18b. lockAvailability POSTs to the period, with no options to get wrong", async () => {
    mockFetch(200, {
      scheduling_period_id: 500,
      scheduling_period_name: "Q4",
      ministry_id: 9,
      availability_locked_at: "2026-09-20T12:00:00Z",
    });

    const locked = await lockAvailability(500);

    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/scheduling-periods/500/availability-lock`);
    expect(calls[0].method).toBe("POST");
    // An empty body, not a flag: the domain has no unlock, so there is no
    // second value this call could carry.
    expect(calls[0].body).toEqual({});
    expect(locked.availability_locked_at).toBe("2026-09-20T12:00:00Z");
  });

  it("19. getServingLimits issues a GET, adding the query only when requested", async () => {
    mockFetch(200, { scheduling_period_id: 500, memberships: [] });
    await getServingLimits(500, false);
    expect(calls[0].url).toBe(`${API_BASE_PATH}/api/v1/scheduling-periods/500/serving-limits`);
    expect(calls[0].method).toBe("GET");

    await getServingLimits(500, true);
    expect(calls[1].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/serving-limits?include_inactive=true`,
    );
  });

  it("20. setServingLimit PUTs the maximum to the period/membership pair", async () => {
    mockFetch(200, { ministry_membership_id: 118, max_assignments: 4 });

    await setServingLimit(500, 118, 4);

    expect(calls[0].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/serving-limits/118`,
    );
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ max_assignments: 4 });
  });

  it("21. clearServingLimit DELETEs with no body", async () => {
    mockFetch(204, null);

    await clearServingLimit(500, 118);

    expect(calls[0].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/serving-limits/118`,
    );
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].body).toBeUndefined();
  });

  it("22. getSchedulingRules issues a GET for the period's whole rule set", async () => {
    mockFetch(200, { scheduling_period_id: 500, min_intervening_events: null });

    await getSchedulingRules(500);

    expect(calls[0].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/scheduling-rules`,
    );
    expect(calls[0].method).toBe("GET");
  });

  it("23. setMinInterveningEvents PUTs a positive number, never a null", async () => {
    mockFetch(200, { scheduling_period_id: 500, min_intervening_events: 1 });

    await setMinInterveningEvents(500, 1);

    expect(calls[0].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/scheduling-rules/min-intervening-events`,
    );
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ min_intervening_events: 1 });
  });

  it("24. clearMinInterveningEvents DELETEs with no body", async () => {
    // "No rule" is the absence of the row, never a zero written into the PUT
    // body -- the same shape clearServingLimit keeps.
    mockFetch(204, null);

    await clearMinInterveningEvents(500);

    expect(calls[0].url).toBe(
      `${API_BASE_PATH}/api/v1/scheduling-periods/500/scheduling-rules/min-intervening-events`,
    );
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].body).toBeUndefined();
  });

  it("every call goes to this app's own origin, never the backend directly", async () => {
    mockFetch(200, {});
    await getMe();
    await getSchedulingPeriods(5);
    await getScheduleVersion(21);

    for (const call of calls) {
      expect(call.url.startsWith("/api/backend/")).toBe(true);
      expect(call.url).not.toContain("://");
    }
  });
});

// -- 6-12: failures --------------------------------------------------------

describe("error handling", () => {
  it("6. preserves the backend's own detail message", async () => {
    mockFetch(409, {
      detail:
        "cannot create the initial schedule version while this period's availability is still open -- lock it first",
    });

    const error = await getMe().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).detail).toContain("availability is still open");
    expect((error as ApiError).message).toContain("availability is still open");
  });

  it.each([
    [401, "unauthenticated"],
    [403, "forbidden"],
    [404, "not_found"],
    [409, "conflict"],
    [422, "invalid"],
  ] as const)("7-11. %i is represented as %s", async (status, kind) => {
    mockFetch(status, { detail: "backend says so" });

    const error = (await getMe().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(error.detail).toBe("backend says so");
  });

  it("11b. flattens FastAPI's validation-error array into a sentence", async () => {
    mockFetch(422, {
      detail: [
        { loc: ["body", "target_assignments_per_candidate"], msg: "Input should be a valid integer" },
      ],
    });

    const error = (await getMe().catch((cause: unknown) => cause)) as ApiError;

    expect(error.kind).toBe("invalid");
    expect(error.detail).toBe(
      "body.target_assignments_per_candidate: Input should be a valid integer",
    );
    // Never a raw object rendered at a person.
    expect(error.detail).not.toContain("[object Object]");
  });

  it("12. a 500 is a server failure, not the caller's mistake", async () => {
    mockFetch(500, {});

    const error = (await getMe().catch((cause: unknown) => cause)) as ApiError;

    expect(error.kind).toBe("server");
    expect(error.status).toBe(500);
    expect(error.message).toBe("The server ran into a problem.");
  });

  it("12b. a failed connection becomes a network error, not a crash", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const error = (await getMe().catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe("network");
    expect(error.status).toBeNull();
    expect(error.message).toBe("Could not reach the server.");
  });

  it("12c. a non-JSON error body still yields the right kind", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => {
          throw new SyntaxError("Unexpected token <");
        },
      })) as unknown as typeof fetch,
    );

    const error = (await getMe().catch((cause: unknown) => cause)) as ApiError;

    expect(error.kind).toBe("server");
    expect(error.detail).toBeNull();
  });

  it("12d. an aborted request is passed through, not reported as a failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new DOMException("aborted", "AbortError");
      }),
    );

    const error = await getMe().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(DOMException);
    expect(error).not.toBeInstanceOf(ApiError);
  });

  it("does not retry a mutation", async () => {
    const fetchMock = mockFetch(500, { detail: "boom" });

    await generateSchedule(21, {}).catch(() => undefined);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
