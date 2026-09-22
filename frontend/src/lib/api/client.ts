/**
 * The typed client for the V1 endpoints this release uses.
 *
 * Every call goes to this app's **own** origin, at `/api/backend/...`, which
 * the route handler in `app/api/backend/[...path]` forwards to FastAPI. Two
 * things follow from that, and both are deliberate:
 *
 * - the browser never talks to the backend directly, so no CORS policy has to
 *   be relaxed to make the UI work;
 * - the development actor header is attached on the server, so no Person id
 *   is ever present in code the browser can read.
 *
 * Components import these functions and never call `fetch` themselves, so
 * error handling exists in exactly one place.
 */

import { ApiError, extractDetail, kindForStatus } from "./errors";
import type {
  AvailabilityLockResponse,
  AvailabilityState,
  ChurchMembershipStatus,
  CreatePersonFields,
  CurrentActor,
  EventAvailabilityResponse,
  EventStaffingResponse,
  GenerateSchedulePolicy,
  GenerationResult,
  MembershipAvailability,
  MembershipQualification,
  MembershipServingLimit,
  MinistryRole,
  MinistryList,
  MinistryRolesResponse,
  MinistrySchedulingPeriods,
  MySchedule,
  PeopleDirectory,
  PersonDetail,
  PeriodEventsResponse,
  PeriodSchedulingRules,
  PeriodServingLimitsResponse,
  PersonMembership,
  PersonMemberships,
  RoleQualificationsResponse,
  RoleStaffing,
  ScheduleVersionDetail,
  StartedSchedule,
} from "./types";

/** Same-origin prefix served by this app's own route handler. */
export const API_BASE_PATH = "/api/backend";

export async function getMe(signal?: AbortSignal): Promise<CurrentActor> {
  return request<CurrentActor>("GET", "/api/v1/me", undefined, signal);
}

/**
 * The signed-in person's own upcoming commitments (Task 77).
 *
 * Takes no arguments, and deliberately cannot: the subject is the
 * authenticated actor, decided server-side. There is no person parameter to
 * pass and no endpoint that would accept one.
 */
export async function getMySchedule(signal?: AbortSignal): Promise<MySchedule> {
  return request<MySchedule>("GET", "/api/v1/me/schedule", undefined, signal);
}

/**
 * End the session (Task 76).
 *
 * Lives here rather than beside the sign-in URLs in `lib/auth.ts` for the
 * reason the whole module exists: this is a call to the backend, and every
 * call to the backend goes through `request()` so error handling stays in one
 * place. Sign-*in* is not here because it is not a fetch at all -- it is a
 * navigation to Google, so it is an `<a href>`.
 *
 * Answers 204 with no body, so there is nothing to return.
 */
export async function logout(signal?: AbortSignal): Promise<void> {
  await request<null>("POST", "/api/v1/auth/logout", undefined, signal);
}

export async function getSchedulingPeriods(
  ministryId: number,
  signal?: AbortSignal,
): Promise<MinistrySchedulingPeriods> {
  return request<MinistrySchedulingPeriods>(
    "GET",
    `/api/v1/ministries/${encodeURIComponent(ministryId)}/scheduling-periods`,
    undefined,
    signal,
  );
}

/**
 * Start a period's first schedule.
 *
 * `notes` is omitted entirely when blank: the backend rejects a
 * whitespace-only note, and an empty text box means "no note", not "a note
 * made of spaces".
 */
export async function startFirstSchedule(
  periodId: number,
  notes?: string,
  signal?: AbortSignal,
): Promise<StartedSchedule> {
  const trimmed = notes?.trim();
  const body = trimmed ? { notes: trimmed } : {};
  return request<StartedSchedule>(
    "POST",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/schedule-versions`,
    body,
    signal,
  );
}

export async function generateSchedule(
  versionId: number,
  policy: GenerateSchedulePolicy,
  signal?: AbortSignal,
): Promise<GenerationResult> {
  return request<GenerationResult>(
    "POST",
    `/api/v1/schedule-versions/${encodeURIComponent(versionId)}/generate`,
    {
      allow_no_response: policy.allow_no_response ?? false,
      target_assignments_per_candidate:
        policy.target_assignments_per_candidate ?? null,
      // Always null -- see the note on GenerateSchedulePolicy.
      role_variety_role_ids: policy.role_variety_role_ids ?? null,
    },
    signal,
  );
}

export async function getScheduleVersion(
  versionId: number,
  signal?: AbortSignal,
): Promise<ScheduleVersionDetail> {
  return request<ScheduleVersionDetail>(
    "GET",
    `/api/v1/schedule-versions/${encodeURIComponent(versionId)}`,
    undefined,
    signal,
  );
}

/**
 * DRAFT -> REVIEW. Marks a schedule as ready for a human to check.
 *
 * Nothing becomes visible to volunteers here: REVIEW is still a working
 * state, and publication happens once, at {@link finalizeScheduleVersion}.
 * Only an active head of the ministry may call it; an administrator who does
 * not head it gets 403 from the backend, which is why the screen hides the
 * control when `can_operate` is false rather than letting it fail.
 *
 * Returns the same detail body {@link getScheduleVersion} does, so the review
 * screen gets the new status and the current checks without a second read.
 */
export async function submitScheduleVersionForReview(
  versionId: number,
  reason?: string | null,
  signal?: AbortSignal,
): Promise<ScheduleVersionDetail> {
  return request<ScheduleVersionDetail>(
    "POST",
    `/api/v1/schedule-versions/${encodeURIComponent(versionId)}/submit-for-review`,
    { reason: reason ?? null },
    signal,
  );
}

/**
 * REVIEW -> FINALIZED. **This is where a schedule becomes real.**
 *
 * From the moment it succeeds, volunteers see their own assignments in My
 * Schedule and every other ministry treats these as commitments already made.
 * The backend refuses it outright -- 409, with nothing changed -- unless the
 * schedule passes every readiness check, including the one nobody can
 * override: no person serving two ministries on the same Sunday.
 *
 * There is no reverse. A finalized schedule is corrected by creating a newer
 * version, never by un-finalizing this one, so the screen asks for explicit
 * confirmation before calling this.
 */
export async function finalizeScheduleVersion(
  versionId: number,
  reason?: string | null,
  signal?: AbortSignal,
): Promise<ScheduleVersionDetail> {
  return request<ScheduleVersionDetail>(
    "POST",
    `/api/v1/schedule-versions/${encodeURIComponent(versionId)}/finalize`,
    { reason: reason ?? null },
    signal,
  );
}

// -- Ministry role management (Task 53) -------------------------------------

export async function getMinistryRoles(
  ministryId: number,
  includeInactive: boolean,
  signal?: AbortSignal,
): Promise<MinistryRolesResponse> {
  const query = includeInactive ? "?include_inactive=true" : "";
  return request<MinistryRolesResponse>(
    "GET",
    `/api/v1/ministries/${encodeURIComponent(ministryId)}/roles${query}`,
    undefined,
    signal,
  );
}

/**
 * `description` is omitted entirely when blank, the same convention
 * {@link startFirstSchedule} uses for `notes`: an empty text box means "no
 * description", not one made of spaces.
 */
export async function createMinistryRole(
  ministryId: number,
  fields: { name: string; description?: string },
  signal?: AbortSignal,
): Promise<MinistryRole> {
  const description = fields.description?.trim();
  return request<MinistryRole>(
    "POST",
    `/api/v1/ministries/${encodeURIComponent(ministryId)}/roles`,
    { name: fields.name, description: description ? description : null },
    signal,
  );
}

export async function updateMinistryRole(
  roleId: number,
  fields: { name: string; description?: string },
  signal?: AbortSignal,
): Promise<MinistryRole> {
  const description = fields.description?.trim();
  return request<MinistryRole>(
    "PATCH",
    `/api/v1/ministry-roles/${encodeURIComponent(roleId)}`,
    { name: fields.name, description: description ? description : null },
    signal,
  );
}

export async function deactivateMinistryRole(
  roleId: number,
  signal?: AbortSignal,
): Promise<MinistryRole> {
  return request<MinistryRole>(
    "POST",
    `/api/v1/ministry-roles/${encodeURIComponent(roleId)}/deactivate`,
    {},
    signal,
  );
}

export async function reactivateMinistryRole(
  roleId: number,
  signal?: AbortSignal,
): Promise<MinistryRole> {
  return request<MinistryRole>(
    "POST",
    `/api/v1/ministry-roles/${encodeURIComponent(roleId)}/reactivate`,
    {},
    signal,
  );
}

// -- Staffing requirement management (Task 54) -------------------------------

export async function getPeriodEvents(
  periodId: number,
  signal?: AbortSignal,
): Promise<PeriodEventsResponse> {
  return request<PeriodEventsResponse>(
    "GET",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/events`,
    undefined,
    signal,
  );
}

export async function getEventStaffing(
  eventId: number,
  signal?: AbortSignal,
): Promise<EventStaffingResponse> {
  return request<EventStaffingResponse>(
    "GET",
    `/api/v1/events/${encodeURIComponent(eventId)}/staffing-requirements`,
    undefined,
    signal,
  );
}

/**
 * Set (or create) how many people a role needs at an event.
 * `required_count` must be at least 1 -- to clear a requirement entirely,
 * call {@link clearStaffingRequirement} instead.
 */
export async function setStaffingRequirement(
  eventId: number,
  roleId: number,
  requiredCount: number,
  signal?: AbortSignal,
): Promise<RoleStaffing> {
  return request<RoleStaffing>(
    "PUT",
    `/api/v1/events/${encodeURIComponent(eventId)}/staffing-requirements/${encodeURIComponent(roleId)}`,
    { required_count: requiredCount },
    signal,
  );
}

/** Clears a role's requirement for an event. A no-op if none was set. */
export async function clearStaffingRequirement(
  eventId: number,
  roleId: number,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>(
    "DELETE",
    `/api/v1/events/${encodeURIComponent(eventId)}/staffing-requirements/${encodeURIComponent(roleId)}`,
    undefined,
    signal,
  );
}

// -- Role qualification management (Task 55) ---------------------------------

export async function getRoleQualifications(
  roleId: number,
  includeInactive: boolean,
  signal?: AbortSignal,
): Promise<RoleQualificationsResponse> {
  const query = includeInactive ? "?include_inactive=true" : "";
  return request<RoleQualificationsResponse>(
    "GET",
    `/api/v1/ministry-roles/${encodeURIComponent(roleId)}/qualifications${query}`,
    undefined,
    signal,
  );
}

/**
 * Record a qualification decision. There is no way to clear a decision back
 * to "never assessed" -- once a decision exists it is only ever `true` or
 * `false` (the backend never deletes a qualification row).
 */
export async function setRoleQualification(
  roleId: number,
  membershipId: number,
  isQualified: boolean,
  signal?: AbortSignal,
): Promise<MembershipQualification> {
  return request<MembershipQualification>(
    "PUT",
    `/api/v1/ministry-roles/${encodeURIComponent(roleId)}/qualifications/${encodeURIComponent(membershipId)}`,
    { is_qualified: isQualified },
    signal,
  );
}

// -- Availability management (Task 56) ---------------------------------------

export async function getEventAvailability(
  eventId: number,
  includeInactive: boolean,
  signal?: AbortSignal,
): Promise<EventAvailabilityResponse> {
  const query = includeInactive ? "?include_inactive=true" : "";
  return request<EventAvailabilityResponse>(
    "GET",
    `/api/v1/events/${encodeURIComponent(eventId)}/availability${query}`,
    undefined,
    signal,
  );
}

/**
 * Record an explicit answer. To clear a response back to "no response", call
 * {@link clearAvailability} instead -- there is no sentinel value for that
 * here.
 */
export async function setAvailability(
  eventId: number,
  membershipId: number,
  state: AvailabilityState,
  signal?: AbortSignal,
): Promise<MembershipAvailability> {
  return request<MembershipAvailability>(
    "PUT",
    `/api/v1/events/${encodeURIComponent(eventId)}/availability/${encodeURIComponent(membershipId)}`,
    { availability_state: state },
    signal,
  );
}

/** Clears a membership's availability answer back to no response. A no-op if
 *  none was set. */
export async function clearAvailability(
  eventId: number,
  membershipId: number,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>(
    "DELETE",
    `/api/v1/events/${encodeURIComponent(eventId)}/availability/${encodeURIComponent(membershipId)}`,
    undefined,
    signal,
  );
}

/**
 * Close availability collection for a period, which is what starting a
 * schedule requires.
 *
 * Idempotent on the server: a period that is already locked comes back with
 * its original instant and a 200, not an error. There is deliberately no
 * matching unlock -- the domain has none.
 */
export async function lockAvailability(
  periodId: number,
  signal?: AbortSignal,
): Promise<AvailabilityLockResponse> {
  return request<AvailabilityLockResponse>(
    "POST",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/availability-lock`,
    {},
    signal,
  );
}

// -- Serving limit management (Task 57) -------------------------------------

export async function getServingLimits(
  periodId: number,
  includeInactive: boolean,
  signal?: AbortSignal,
): Promise<PeriodServingLimitsResponse> {
  const query = includeInactive ? "?include_inactive=true" : "";
  return request<PeriodServingLimitsResponse>(
    "GET",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/serving-limits${query}`,
    undefined,
    signal,
  );
}

/**
 * Set (or change) a member's hard serving maximum for a period.
 * `maxAssignments` must be at least 1 -- to clear the maximum entirely, call
 * {@link clearServingLimit} instead. There is no sentinel value for "no
 * limit" on this endpoint.
 */
export async function setServingLimit(
  periodId: number,
  membershipId: number,
  maxAssignments: number,
  signal?: AbortSignal,
): Promise<MembershipServingLimit> {
  return request<MembershipServingLimit>(
    "PUT",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/serving-limits/${encodeURIComponent(membershipId)}`,
    { max_assignments: maxAssignments },
    signal,
  );
}

/** Clears a member's serving maximum for a period back to "no limit". A
 *  no-op if none was set. */
export async function clearServingLimit(
  periodId: number,
  membershipId: number,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>(
    "DELETE",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/serving-limits/${encodeURIComponent(membershipId)}`,
    undefined,
    signal,
  );
}

// -- Period scheduling rules (Task 71) --------------------------------------

export async function getSchedulingRules(
  periodId: number,
  signal?: AbortSignal,
): Promise<PeriodSchedulingRules> {
  return request<PeriodSchedulingRules>(
    "GET",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/scheduling-rules`,
    undefined,
    signal,
  );
}

/**
 * Set (or change) how many of this ministry's events must be skipped between
 * two assignments of the same person in this period.
 *
 * `minInterveningEvents` must be at least 1 -- to clear the rule entirely,
 * call {@link clearMinInterveningEvents} instead. There is no sentinel value
 * for "no rule" on this endpoint, because "consecutive assignments are
 * allowed" is the *absence* of the rule rather than a value of it.
 */
export async function setMinInterveningEvents(
  periodId: number,
  minInterveningEvents: number,
  signal?: AbortSignal,
): Promise<PeriodSchedulingRules> {
  return request<PeriodSchedulingRules>(
    "PUT",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/scheduling-rules/min-intervening-events`,
    { min_intervening_events: minInterveningEvents },
    signal,
  );
}

/** Clears the period's event-gap rule. A no-op if none was set. */
export async function clearMinInterveningEvents(
  periodId: number,
  signal?: AbortSignal,
): Promise<void> {
  await request<null>(
    "DELETE",
    `/api/v1/scheduling-periods/${encodeURIComponent(periodId)}/scheduling-rules/min-intervening-events`,
    undefined,
    signal,
  );
}

// -- People and membership management (Task 79) -----------------------------

/**
 * A bounded page of the church-wide directory.
 *
 * **Every argument is a filter, and none of them is an actor.** Who is asking
 * comes from the session cookie the proxy forwards, so a volunteer calling
 * this receives 403 no matter what the browser sends -- the hidden navigation
 * is a courtesy, and this is not where the rule lives.
 *
 * `search` is plain substring matching on the display name, server-side. It is
 * deliberately not fuzzy: the moment a search guesses, somebody eventually
 * picks the candidate it guessed, and that is how one human becomes two
 * records -- or two humans become one.
 */
export async function getPeople(
  options: { search?: string; includeInactive?: boolean; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<PeopleDirectory> {
  const query = new URLSearchParams();
  const search = options.search?.trim();
  if (search) query.set("search", search);
  if (options.includeInactive) query.set("include_inactive", "true");
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.offset !== undefined) query.set("offset", String(options.offset));
  const suffix = query.size > 0 ? `?${query}` : "";
  return request<PeopleDirectory>("GET", `/api/v1/people${suffix}`, undefined, signal);
}

export async function getPerson(
  personId: number,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  return request<PersonDetail>(
    "GET",
    `/api/v1/people/${encodeURIComponent(personId)}`,
    undefined,
    signal,
  );
}

/**
 * Create one canonical person.
 *
 * **409 is a normal outcome here, not a bug.** When somebody already has this
 * exact name the backend refuses and explains; the screen shows that sentence
 * and offers to proceed, which resends with `acknowledge_duplicate_name`. That
 * round trip *is* the explicit human decision -- nothing on either side merges
 * an identity or picks a candidate.
 *
 * Blank optional fields are sent as `null` rather than as empty strings, the
 * same convention {@link startFirstSchedule} uses for notes.
 */
export async function createPerson(
  fields: CreatePersonFields,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  const email = fields.email?.trim();
  const phone = fields.phone?.trim();
  return request<PersonDetail>(
    "POST",
    "/api/v1/people",
    {
      display_name: fields.display_name.trim(),
      email: email ? email : null,
      phone: phone ? phone : null,
      initial_ministry_id: fields.initial_ministry_id ?? null,
      acknowledge_duplicate_name: fields.acknowledge_duplicate_name ?? false,
    },
    signal,
  );
}

/**
 * Replace a person's safe church-wide fields. **Admin only, server-side.**
 *
 * Deliberately cannot change three things, because the backend does not accept
 * them here: the sign-in address (its own call, below), the active state (its
 * own two calls), and Admin authority (no endpoint at all).
 */
export async function updatePerson(
  personId: number,
  fields: { display_name: string; phone?: string },
  signal?: AbortSignal,
): Promise<PersonDetail> {
  const phone = fields.phone?.trim();
  return request<PersonDetail>(
    "PATCH",
    `/api/v1/people/${encodeURIComponent(personId)}`,
    { display_name: fields.display_name.trim(), phone: phone ? phone : null },
    signal,
  );
}

/**
 * Deactivate somebody **church-wide**. Admin only, server-side.
 *
 * This is not a delete and the UI must never call it one: every membership,
 * assignment, qualification and audit row survives, which is exactly why
 * {@link reactivatePerson} can restore the person unchanged.
 */
export async function deactivatePerson(
  personId: number,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  return request<PersonDetail>(
    "POST",
    `/api/v1/people/${encodeURIComponent(personId)}/deactivate`,
    {},
    signal,
  );
}

export async function reactivatePerson(
  personId: number,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  return request<PersonDetail>(
    "POST",
    `/api/v1/people/${encodeURIComponent(personId)}/reactivate`,
    {},
    signal,
  );
}

/**
 * Link, replace or remove the address a person signs in with. Admin only.
 *
 * `PUT` because it sets the link to whatever is sent; `null` removes it and
 * they can no longer sign in. The backend validates with the very same
 * function the Google callback and the admin CLI use -- no part of OAuth is
 * reimplemented in the browser, and nothing here is a password.
 */
export async function setPersonAuthLink(
  personId: number,
  email: string | null,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  const trimmed = email?.trim();
  return request<PersonDetail>(
    "PUT",
    `/api/v1/people/${encodeURIComponent(personId)}/auth-link`,
    { email: trimmed ? trimmed : null },
    signal,
  );
}

export async function getPersonMemberships(
  personId: number,
  signal?: AbortSignal,
): Promise<PersonMemberships> {
  return request<PersonMemberships>(
    "GET",
    `/api/v1/people/${encodeURIComponent(personId)}/memberships`,
    undefined,
    signal,
  );
}

/**
 * Add a person to one ministry.
 *
 * **The ministry is always named explicitly**, never inferred from who is
 * signed in, even when they lead exactly one. A head who leads three must say
 * which one they mean, and a rule that applied only to some people is a rule
 * that gets got wrong.
 *
 * Idempotent on the server, and somebody who was removed earlier has their
 * original membership restored rather than duplicated -- which is what keeps
 * their serving history attached to them.
 */
export async function addPersonToMinistry(
  personId: number,
  ministryId: number,
  signal?: AbortSignal,
): Promise<PersonMemberships> {
  return request<PersonMemberships>(
    "POST",
    `/api/v1/people/${encodeURIComponent(personId)}/memberships`,
    { ministry_id: ministryId },
    signal,
  );
}

/**
 * Remove a person from **one** ministry, keeping their history.
 *
 * Named `remove`, and a POST rather than a DELETE, because that is what
 * happens: one timestamp is written and nothing is deleted. Removing somebody
 * who currently leads the ministry is Admin-only on the server.
 */
export async function removePersonFromMinistry(
  membershipId: number,
  signal?: AbortSignal,
): Promise<PersonMembership> {
  return request<PersonMembership>(
    "POST",
    `/api/v1/ministry-memberships/${encodeURIComponent(membershipId)}/remove`,
    {},
    signal,
  );
}

/**
 * Appoint somebody to lead a ministry, or revoke that authority.
 *
 * **Admin only, server-side** — a ministry head cannot promote another, in
 * their own ministry or any other, and cannot promote themselves.
 *
 * Person in the URL, ministry and direction in the body: the three things an
 * administrator chooses explicitly. Nothing is inferred from who is signed in.
 *
 * Granting creates the membership when there is not one, which is the only way
 * a newly created ministry can ever acquire its first head. Revoking leaves
 * the ordinary membership, every qualification and every past assignment
 * exactly where they are.
 */
export async function setMinistryHead(
  personId: number,
  ministryId: number,
  isMinistryHead: boolean,
  signal?: AbortSignal,
): Promise<PersonMembership> {
  return request<PersonMembership>(
    "PUT",
    `/api/v1/people/${encodeURIComponent(personId)}/ministry-head`,
    { ministry_id: ministryId, is_ministry_head: isMinistryHead },
    signal,
  );
}

/**
 * Record whether somebody is a formal member of the church. **Admin only.**
 *
 * **Not ministry membership**, and nothing in this app derives either from the
 * other. Somebody may serve every week without being a formal member, and a
 * member may serve nowhere. A ministry head reads this and cannot change it.
 */
export async function setChurchMembershipStatus(
  personId: number,
  status: ChurchMembershipStatus,
  signal?: AbortSignal,
): Promise<PersonDetail> {
  return request<PersonDetail>(
    "PUT",
    `/api/v1/people/${encodeURIComponent(personId)}/church-membership-status`,
    { church_membership_status: status },
    signal,
  );
}

// -- The administrator's church-wide ministry list (Task 79) ----------------

/**
 * Every ministry in the church. **Admin only, server-side.**
 *
 * A ministry head calling this receives 403: the ministries they lead already
 * reach them through `/api/v1/me`, and a church-wide inventory is a different
 * thing. Archived ministries are included by default — one that vanished from
 * the list would look deleted, and nothing here deletes.
 */
export async function getMinistries(signal?: AbortSignal): Promise<MinistryList> {
  return request<MinistryList>("GET", "/api/v1/ministries", undefined, signal);
}

/**
 * The single place a network response becomes either data or an `ApiError`.
 *
 * **No retries.** Several of these calls create or change rows; retrying one
 * automatically could start a second schedule, run a second generation
 * against a schedule that is already being written, or resubmit an edit
 * twice. A person deciding to press the button again is a different thing
 * from a client deciding for them.
 */
async function request<T>(
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_PATH}${path}`, {
      method,
      signal,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // An aborted request is the caller navigating away, not a failure worth
    // showing anybody.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError("network", null, null);
  }

  const payload = await readJson(response);

  if (!response.ok) {
    throw new ApiError(kindForStatus(response.status), response.status, extractDetail(payload));
  }
  return payload as T;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    // A proxy or gateway failing outside FastAPI can answer with HTML. That
    // is still a real status; it simply carries no `detail`.
    return null;
  }
}
