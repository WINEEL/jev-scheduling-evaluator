/**
 * Response shapes of the V1 backend API.
 *
 * These mirror the FastAPI Pydantic models exactly (Tasks 36-40). They are
 * hand-written rather than generated so the compiler catches a drift the day
 * it happens; if the backend contract changes, this file is the one place to
 * update.
 *
 * Two conventions carried over from the backend deliberately:
 *
 * - a nullable field is `T | null`, never optional-and-absent, because the
 *   backend always sends the key;
 * - `status` is a plain string, not a union. The backend documents DRAFT /
 *   REVIEW / FINALIZED, and the UI labels those three, but a value it has not
 *   seen must render as itself rather than crash a type guard.
 */

/** ISO date, `YYYY-MM-DD`. */
export type IsoDate = string;
/** ISO 8601 timestamp with offset. */
export type IsoDateTime = string;

// -- GET /api/v1/me --------------------------------------------------------

export interface HeadedMinistry {
  ministry_id: number;
  name: string;
}

export interface CurrentActor {
  person_id: number;
  display_name: string;
  is_admin: boolean;
  /** Ministries this person actually heads. An Admin may legitimately head
   *  none: the backend reports membership, not reach. */
  headed_ministries: HeadedMinistry[];
}

// -- GET /api/v1/ministries/{id}/scheduling-periods ------------------------

/**
 * One commitment on the signed-in person's own schedule (Task 77).
 *
 * The ministry and role arrive **by name** as well as by id, unlike the
 * management payloads: this is the one screen whose reader may belong to no
 * team and have nowhere to look either up.
 */
export interface MyScheduleAssignment {
  assignment_id: number;
  event_id: number;
  event_date: IsoDate;
  /** SUNDAY or SPECIAL -- what kind of gathering this is. */
  event_kind: string;
  /** Only special events are named; a Sunday is identified by its date. */
  event_name: string | null;
  ministry_id: number;
  ministry_name: string;
  ministry_role_id: number;
  ministry_role_name: string;
  /**
   * True only for a FINALIZED schedule -- one a ministry head has stood
   * behind. A head previewing their own draft sees `false`, and the UI must
   * say so: a proposal shown as a commitment is how somebody turns up on the
   * wrong Sunday.
   */
  is_confirmed: boolean;
  /** DRAFT, REVIEW or FINALIZED, so the UI can be specific. */
  schedule_version_status: string;
}

/** The signed-in person's upcoming commitments, across every ministry. */
export interface MySchedule {
  person_id: number;
  display_name: string;
  /** The day "upcoming" was measured from, in the church's own timezone. */
  as_of_date: IsoDate;
  assignments: MyScheduleAssignment[];
}

export interface LatestScheduleSummary {
  schedule_id: number;
  latest_version_id: number | null;
  latest_version_number: number | null;
  latest_version_status: string | null;
}

export interface SchedulingPeriodSummary {
  scheduling_period_id: number;
  name: string;
  start_date: IsoDate;
  end_date: IsoDate;
  /** `null` while availability is still open. */
  availability_locked_at: IsoDateTime | null;
  /** `null` when scheduling has not started for this period. */
  schedule: LatestScheduleSummary | null;
}

export interface MinistrySchedulingPeriods {
  ministry_id: number;
  ministry_name: string;
  periods: SchedulingPeriodSummary[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

// -- POST /api/v1/scheduling-periods/{id}/schedule-versions ----------------

export interface StartFirstScheduleRequest {
  notes?: string | null;
}

export interface StartedSchedule {
  schedule_id: number;
  schedule_version_id: number;
  scheduling_period_id: number;
  version_number: number;
  status: string;
  requirement_snapshot_count: number;
}

// -- POST /api/v1/schedule-versions/{id}/generate --------------------------

export interface GenerateSchedulePolicy {
  allow_no_response?: boolean;
  target_assignments_per_candidate?: number | null;
  /**
   * Always sent as `null` by this frontend. Choosing variety roles means
   * picking ministry role ids, and no endpoint exposes a ministry's roles
   * yet -- asking a Ministry Head to type database ids would be worse than
   * leaving the preference off. The backend capability is untouched.
   */
  role_variety_role_ids?: number[] | null;
}

export interface CreatedAssignment {
  assignment_id: number;
  requirement_id: number;
  membership_id: number;
  event_id: number;
}

export interface UnfilledRequirementReport {
  requirement_id: number;
  missing_count: number;
  diagnostic_codes: string[];
}

export interface AssignmentLoad {
  membership_id: number;
  assignment_count: number;
}

export interface GenerationMetrics {
  assignment_loads: AssignmentLoad[];
  target_excess_total: number | null;
  fairness_cost: number | null;
  role_variety_cost: number | null;
}

export interface GenerationResult {
  schedule_version_id: number;
  /** False is a normal outcome, not an error: some positions could not be
   *  filled and are listed in `unfilled_requirements`. */
  is_complete: boolean;
  created_count: number;
  created_assignments: CreatedAssignment[];
  unfilled_requirements: UnfilledRequirementReport[];
  metrics: GenerationMetrics;
}

// -- GET /api/v1/schedule-versions/{id} ------------------------------------

export interface ScheduleVersionSummary {
  id: number;
  schedule_id: number;
  scheduling_period_id: number;
  version_number: number;
  status: string;
  finalized_at: IsoDateTime | null;
  amends_version_id: number | null;
  amendment_reason: string | null;
  notes: string | null;
}

export interface PeriodSummary {
  id: number;
  name: string;
  ministry_id: number;
  ministry_name: string;
  start_date: IsoDate;
  end_date: IsoDate;
  availability_locked_at: IsoDateTime | null;
}

export interface RequirementDetail {
  requirement_id: number;
  event_id: number;
  /** The version's own frozen date, not today's event row. */
  event_date: IsoDate;
  event_name: string | null;
  event_kind: string | null;
  role_id: number;
  role_name: string | null;
  required_count: number;
  /** May exceed `required_count` where an overfill was authorized. */
  assigned_count: number;
}

export interface AssignmentDetail {
  assignment_id: number;
  requirement_id: number;
  event_id: number;
  membership_id: number;
  person_id: number;
  person_display_name: string;
  role_id: number;
  role_name: string | null;
  is_override: boolean;
  override_reason: string | null;
}

export interface StaffingSummary {
  required_positions: number;
  assigned_positions: number;
  unfilled_positions: number;
  is_fully_staffed: boolean;
}

export interface RequirementFingerprint {
  event_id: number;
  event_date: IsoDate;
  ministry_role_id: number;
  required_count: number;
}

export interface ScheduleVersionStaleness {
  is_stale: boolean;
  current_only: RequirementFingerprint[];
  snapshot_only: RequirementFingerprint[];
}

export interface FinalizationIssue {
  code: string;
  message: string;
  assignment_id: number | null;
  schedule_version_requirement_id: number | null;
}

/**
 * Descriptive diagnostics, not permission. `is_ready` means nothing about the
 * version's *contents* would block finalization; it is not a statement that
 * finalizing is allowed now, which also depends on lifecycle state this
 * release does not act on.
 */
export interface FinalizationReadiness {
  is_ready: boolean;
  issues: FinalizationIssue[];
}

export interface ScheduleVersionDetail {
  schedule_version: ScheduleVersionSummary;
  period: PeriodSummary;
  requirements: RequirementDetail[];
  assignments: AssignmentDetail[];
  summary: StaffingSummary;
  staleness: ScheduleVersionStaleness;
  finalization_readiness: FinalizationReadiness;
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

// -- Ministry role management (Task 53) -------------------------------------

export interface MinistryRole {
  ministry_role_id: number;
  name: string;
  description: string | null;
  /** Display order only -- never a scheduling priority (backend docstring).
   *  This app has no reordering UI; it is shown, never edited. */
  display_order: number;
  /** `null` while the role is active. Non-null means deactivated, and
   *  since when -- never deleted. */
  deactivated_at: IsoDateTime | null;
}

export interface MinistryRolesResponse {
  ministry_id: number;
  ministry_name: string;
  roles: MinistryRole[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface CreateMinistryRoleRequest {
  name: string;
  description?: string | null;
  reason?: string | null;
}

export interface UpdateMinistryRoleRequest {
  name: string;
  description?: string | null;
  reason?: string | null;
}

export interface ReasonRequest {
  reason?: string | null;
}

// -- Staffing requirement management (Task 54) ------------------------------

export interface EventSummary {
  event_id: number;
  event_date: IsoDate;
  /** `null` for an ordinary Sunday service; set for a SPECIAL event. */
  event_name: string | null;
  event_kind: string;
  /** `null` unless the event has been cancelled. */
  cancelled_at: IsoDateTime | null;
}

export interface PeriodEventsResponse {
  scheduling_period_id: number;
  events: EventSummary[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface RoleStaffing {
  ministry_role_id: number;
  name: string;
  description: string | null;
  display_order: number;
  /** `null` means this role is not required for this event -- never `0`. */
  required_count: number | null;
}

export interface EventStaffingResponse {
  event_id: number;
  event_date: IsoDate;
  event_name: string | null;
  event_kind: string;
  ministry_id: number;
  roles: RoleStaffing[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface SetStaffingRequirementRequest {
  required_count: number;
  reason?: string | null;
}

// -- Role qualification management (Task 55) --------------------------------

export interface MembershipQualification {
  ministry_membership_id: number;
  person_id: number;
  person_display_name: string;
  /** `null` while the membership is active. */
  membership_deactivated_at: IsoDateTime | null;
  /** `null` while the person is active church-wide -- a separate fact from
   *  the membership's own activity. */
  person_deactivated_at: IsoDateTime | null;
  /** `null` = never assessed; otherwise the standing decision. Never a
   *  stand-in for `false`. */
  is_qualified: boolean | null;
  /** `null` exactly when `is_qualified` is `null`. */
  decided_at: IsoDateTime | null;
}

export interface RoleQualificationsResponse {
  ministry_role_id: number;
  role_name: string;
  ministry_id: number;
  memberships: MembershipQualification[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface SetRoleQualificationRequest {
  is_qualified: boolean;
  reason?: string | null;
}

// -- Availability management (Task 56) ---------------------------------------

/** The three stored answers. `null` (absence of a row) means no response and
 *  is never one of these -- see `MembershipAvailability.availability_state`. */
export type AvailabilityState = "AVAILABLE" | "BACKUP" | "UNAVAILABLE";

export interface MembershipAvailability {
  ministry_membership_id: number;
  person_id: number;
  person_display_name: string;
  /** `null` while the membership is active. */
  membership_deactivated_at: IsoDateTime | null;
  /** `null` while the person is active church-wide -- a separate fact from
   *  the membership's own activity. */
  person_deactivated_at: IsoDateTime | null;
  /** `null` = no response; otherwise the stored answer. Never a fourth
   *  stored value. */
  availability_state: AvailabilityState | null;
}

export interface EventAvailabilityResponse {
  event_id: number;
  event_date: IsoDate;
  event_name: string | null;
  event_kind: string;
  ministry_id: number;
  /** `null` while availability is still open for this event's period. Once
   *  set, every change below is refused. */
  availability_locked_at: IsoDateTime | null;
  memberships: MembershipAvailability[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface SetAvailabilityRequest {
  availability_state: AvailabilityState;
  reason?: string | null;
}

/**
 * The lock state of a period after closing its availability collection.
 *
 * `availability_locked_at` is never `null` here, unlike everywhere else it
 * appears: the call either locked the period or found it already locked, and
 * both outcomes have an instant. There is no unlock in the domain, so there is
 * no request shape for one either.
 */
export interface AvailabilityLockResponse {
  scheduling_period_id: number;
  scheduling_period_name: string;
  ministry_id: number;
  availability_locked_at: IsoDateTime;
}

// -- Serving limit management (Task 57) ------------------------------------

export interface MembershipServingLimit {
  ministry_membership_id: number;
  person_id: number;
  person_display_name: string;
  /** `null` while the membership is active. */
  membership_deactivated_at: IsoDateTime | null;
  /** `null` while the person is active church-wide -- a separate fact from
   *  the membership's own activity. */
  person_deactivated_at: IsoDateTime | null;
  /** `null` = no hard maximum; otherwise the positive maximum number of
   *  assignments this membership may hold in this period, in this ministry.
   *  Never `0`. */
  max_assignments: number | null;
}

export interface PeriodServingLimitsResponse {
  scheduling_period_id: number;
  scheduling_period_name: string;
  ministry_id: number;
  memberships: MembershipServingLimit[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

export interface SetServingLimitRequest {
  max_assignments: number;
}

// -- Period scheduling rules (Task 71) --------------------------------------

export interface PeriodSchedulingRules {
  scheduling_period_id: number;
  scheduling_period_name: string;
  ministry_id: number;
  /** `null` = no rule, and the same person may serve consecutive events of
   *  this ministry; otherwise the positive number of this ministry's own
   *  events that must fall between two assignments of the same person in this
   *  period. Never `0` -- "consecutive is allowed" is the absence of the rule,
   *  not a value of it. */
  min_intervening_events: number | null;
  /** Every member group this ministry defines, with its cap for this period.
   *  A group with no cap appears with `max_per_event: null` -- that is the
   *  case a head configuring the rule most needs to see. Absent on responses
   *  from a backend that predates Task 74, hence the optional marker. */
  member_group_limits?: MemberGroupLimitEntry[];
  /** Every same-event support requirement configured for this period. Only
   *  configured rules appear: a requirement that does not exist is not a row
   *  anybody is looking at. */
  same_event_support_requirements?: SameEventSupportEntry[];
  /** Whether the signed-in caller may perform operational writes on this
   *  ministry -- that is, whether they are an active Ministry Head of it. An
   *  administrator who does not head it reads it with `false`.
   *
   *  **Advisory, never a security boundary.** The backend authorizes every
   *  write again on every request; this field exists so a screen can render
   *  the controls that are real for its reader instead of re-deriving the
   *  rule from `is_admin`, which is exactly how the two would come to
   *  disagree (Task 80). */
  can_operate: boolean;
}

/** One member group of this period's ministry, and its cap for this period. */
export interface MemberGroupLimitEntry {
  member_group_id: number;
  name: string;
  /** How many memberships are in the group, whatever their activity. */
  member_count: number;
  /** `null` = no cap for this period; otherwise the positive maximum number of
   *  this group's members who may serve any one event. Never `0`. */
  max_per_event: number | null;
}

/**
 * One same-event support requirement.
 *
 * It names the subject and the approved supporting members and carries **no
 * reason, category or relationship**, because none is stored.
 */
export interface SameEventSupportEntry {
  subject_membership_id: number;
  subject_display_name: string;
  min_supporters: number;
  supporter_membership_ids: number[];
  supporter_display_names: string[];
}

export interface SetMinInterveningEventsRequest {
  min_intervening_events: number;
}

// -- People and membership management (Task 79) -----------------------------

/**
 * One ministry a person belongs to, as the people API reports it.
 *
 * `deactivated_at` here is the **membership's**, never the person's. The two
 * are separate facts in the domain -- somebody can be active in the church and
 * removed from one team, or deactivated church-wide with every membership
 * intact -- and this app must never collapse them into one "active" flag,
 * because it would then have to pick which one it meant.
 */
export interface PersonMembership {
  ministry_membership_id: number;
  ministry_id: number;
  ministry_name: string;
  /** Head authority for this one ministry. There is no church-wide head
   *  permission for this to be a summary of. */
  is_ministry_head: boolean;
  /** `null` while they are on this team; otherwise when they were removed.
   *  Never deleted. */
  deactivated_at: IsoDateTime | null;
  notes: string | null;
  joined_on: IsoDate | null;
}

/**
 * One person, church-wide.
 *
 * **These fields are the whole list, and it is deliberately short.** No
 * gender, birthday, age, child flag, affinity or avoidance appears here,
 * because none exists in the domain and no current scheduling requirement
 * reads one (Task 79). If a field is not in this interface, the backend does
 * not send it and this app must not invent it.
 *
 * `email` is the address they sign in with -- not a general contact field --
 * and it reaches the browser only on a screen an Admin or Ministry Head is
 * already authorized to see.
 */
export type ChurchMembershipStatus = "MEMBER" | "NON_MEMBER" | "UNKNOWN";

/** How many past dates one person has recorded serving in one ministry. */
export interface MinistryServingCount {
  ministry_id: number;
  ministry_name: string;
  count: number;
}

/**
 * A date on which the record puts one person in two ministries at once.
 *
 * **Not two services.** The church-wide hard rule is one ministry per person
 * per Sunday, so this is a contradiction in the record — reported, never added
 * up. Empty in every healthy state.
 */
export interface ServingConflict {
  event_date: IsoDate;
  ministry_names: string[];
}

/**
 * One person's recorded serving.
 *
 * `total` is the sum of `by_ministry` by construction — the backend builds
 * both from one query — so the two can never disagree on screen.
 *
 * **"Recorded serving", never "attended".** The domain records who was
 * scheduled; nothing records who turned up.
 */
export interface ServingSummary {
  total: number;
  by_ministry: MinistryServingCount[];
  same_date_conflicts: ServingConflict[];
}

export interface DirectoryPerson {
  person_id: number;
  display_name: string;
  /** Church-wide Admin authority, distinct from the per-ministry head flag on
   *  each membership below. */
  is_admin: boolean;
  /**
   * Formal membership of the church: MEMBER, NON_MEMBER or UNKNOWN.
   *
   * **A different fact from `memberships` below**, which is participation in
   * ministries. Neither is ever derived from the other, in either direction —
   * somebody may serve every week without being a formal member, and a member
   * may serve nowhere. Admin-only to change; a ministry head reads it.
   */
  church_membership_status: ChurchMembershipStatus;
  /** `null` while active in the church; otherwise when they were deactivated.
   *  Never deleted. */
  deactivated_at: IsoDateTime | null;
  memberships: PersonMembership[];
  /** Past dates the authoritative record shows them serving on. The directory
   *  carries this total; the detail read carries `serving` as well. */
  recorded_serving_total: number;
}

/**
 * One person, in full — what the detail read returns.
 *
 * **The extra fields are the reason the two types are separate.** The backend
 * does not send contact details on the directory listing at all: that listing
 * is the whole church's roll, every ministry head may read it, and the table
 * shows no address or phone number. They arrive only here, on the one screen
 * an administrator manages sign-in access from.
 *
 * Keeping them off {@link DirectoryPerson} rather than marking them optional
 * means a directory row cannot be handed to something expecting an address and
 * quietly read as "they have none".
 */
export interface PersonDetail extends DirectoryPerson {
  /** The address they sign in with, or `null` if they cannot sign in at all. */
  email: string | null;
  phone: string | null;
  /** The per-ministry breakdown. `null` would mean the read did not include
   *  one; the detail read always does. */
  serving: ServingSummary | null;
}

/** A bounded page of the directory. `total` counts everybody matching the
 *  filters, not the page, so a screen can say "showing 25 of 214". */
export interface PeopleDirectory {
  people: DirectoryPerson[];
  total: number;
  limit: number;
  offset: number;
}

/** One person's memberships, for repainting that panel after a change. */
export interface PersonMemberships {
  person_id: number;
  memberships: PersonMembership[];
}

/**
 * Everything the browser may say when creating a person.
 *
 * `acknowledge_duplicate_name` is the explicit human decision: the backend
 * refuses once when somebody already has this exact name, and the caller
 * repeats the request with this set only after a person has looked. Nothing
 * merges, fuzzy-matches or auto-selects at either end.
 */
export interface CreatePersonFields {
  display_name: string;
  email?: string;
  phone?: string;
  /** Required by the backend when the actor is a Ministry Head rather than an
   *  Admin: a head creates people *for* a team they lead. */
  initial_ministry_id?: number;
  acknowledge_duplicate_name?: boolean;
}

// -- The Admin's church-wide ministry list (Task 79) -------------------------

/** One person who actively leads a ministry. Name and id only: this is an
 *  oversight list, not a contact directory. */
export interface MinistryHead {
  person_id: number;
  display_name: string;
}

/**
 * The scheduling period a ministry is currently in.
 *
 * `is_current` false means this is simply the most recently started period —
 * the ministry is between quarters. `latest_version_status` is `null` when no
 * schedule version exists yet, which is not the same as "DRAFT".
 */
export interface MinistryPeriodSummary {
  scheduling_period_id: number;
  name: string;
  start_date: IsoDate;
  end_date: IsoDate;
  is_current: boolean;
  latest_version_number: number | null;
  latest_version_status: string | null;
}

/**
 * One ministry, as an administrator overseeing the church sees it.
 *
 * Every field is a fact the backend already stores. A ministry with no head,
 * no members or no period reports that, and this app must never fill a column
 * in to keep a table looking complete.
 */
export interface MinistryOverview {
  ministry_id: number;
  name: string;
  description: string | null;
  /** `null` while active; otherwise archived. Never deleted — past schedules
   *  still name it. */
  deactivated_at: IsoDateTime | null;
  heads: MinistryHead[];
  active_member_count: number;
  period: MinistryPeriodSummary | null;
}

export interface MinistryList {
  ministries: MinistryOverview[];
  total: number;
}
