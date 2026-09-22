/**
 * Who sees the People screen, and what they may do on it.
 *
 * Extracted as plain functions rather than left as conditionals inside the
 * components, for exactly the reason `lib/homeView.ts` gives and
 * `vitest.config.ts` explains: the rules worth protecting are decisions about
 * an actor, not about markup, and written this way they can be tested
 * directly, exhaustively, and without a browser.
 *
 * **None of this is a security boundary, and saying so is not a disclaimer.**
 * Every rule below is enforced again in FastAPI, in the service layer, on
 * every request -- a volunteer who guesses the URL gets 403 from the API, not
 * a page. What these functions buy is a UI that tells the truth: no button
 * that leads to a refusal, no control for an act the person cannot perform,
 * and no navigation to a screen that would only ever show them an error.
 *
 * The three roles, and the one asymmetry that runs through all of them:
 *
 * - **Admin** -- may see the directory and may change the *church-wide*
 *   record: edit, deactivate, reactivate, manage sign-in access, and grant or
 *   revoke Ministry Head authority.
 * - **Ministry Head** -- may see the same directory, because that is how they
 *   find an existing person instead of creating a second copy of one, but may
 *   only change *memberships of ministries they lead*.
 * - **Everybody else** -- sees no People navigation at all.
 *
 * Reading is church-wide; writing is scoped. That is the asymmetry, and it is
 * the whole shape of this file.
 */

import type {
  ChurchMembershipStatus,
  CurrentActor,
  DirectoryPerson,
  PersonMembership,
} from "./api/types";

/** The two fields every decision here reads, and deliberately no more.
 *
 * Taking a narrow shape rather than the whole DTO means a change to any other
 * part of `CurrentActor` cannot silently change who sees what -- the same
 * reasoning `homeSectionsFor` applies. */
export type ActorAuthority = Pick<CurrentActor, "is_admin" | "headed_ministries">;

/**
 * Whether the People navigation and screen are shown at all.
 *
 * An Admin, or somebody who heads at least one ministry. **Which** ministry
 * does not matter here; that one is led at all does.
 */
export function canViewPeople(actor: ActorAuthority): boolean {
  return actor.is_admin || actor.headed_ministries.length > 0;
}

/**
 * Whether this actor may change the church-wide Person record.
 *
 * Covers editing identity, deactivating and reactivating somebody, managing
 * their sign-in access, and granting or revoking head authority -- every one
 * of which is Admin-only in the backend, and for the same reason: a Ministry
 * Head's authority is scoped to a ministry, and these acts are not.
 */
export function canManagePersonRecord(actor: ActorAuthority): boolean {
  return actor.is_admin;
}

/**
 * The ministries this actor may add people to and remove them from.
 *
 * **An administrator who leads nothing gets an empty list, and that is the
 * answer rather than a gap.** They may now *see* every ministry — Task 79
 * added that list — and seeing one is not running it. Putting somebody on a
 * team is that team's head's decision, so an administrator who leads none has
 * no roster to change.
 */
export function managedMinistries(actor: ActorAuthority): CurrentActor["headed_ministries"] {
  return actor.headed_ministries;
}

/**
 * Whether the actor may change *some* ministry's membership list.
 *
 * **An administrator who leads nothing gets `false`, and that is the rule
 * rather than a gap.** Rostering a team belongs to whoever runs it; an
 * administrator oversees the church, may see every ministry, and may appoint
 * somebody to lead one — but does not silently become every ministry's head.
 * The backend refuses them too, so a button shown here would only lead to a
 * 403.
 */
export function canManageAnyMinistry(actor: ActorAuthority): boolean {
  return actor.headed_ministries.length > 0;
}

/**
 * Whether this actor may add to, or remove from, one specific ministry.
 *
 * Mirrors the backend's `require_ministry_manager` exactly: an Admin for any
 * ministry, a head for the ministries they actually lead, and nobody else.
 */
export function canManageMinistry(actor: ActorAuthority, ministryId: number): boolean {
  return actor.headed_ministries.some((ministry) => ministry.ministry_id === ministryId);
}

/**
 * Whether this actor may remove *this particular* membership.
 *
 * Two rules, and the second is the one that is easy to miss: managing the
 * ministry is necessary but not sufficient, because **removing somebody who
 * leads a ministry necessarily revokes their head authority, and revoking is
 * Admin-only**. A head therefore cannot remove a co-head -- the backend
 * refuses it, and this function is what keeps the button from appearing and
 * promising otherwise.
 */
export function canRemoveMembership(
  actor: ActorAuthority,
  membership: Pick<PersonMembership, "ministry_id" | "is_ministry_head">,
): boolean {
  if (!canManageMinistry(actor, membership.ministry_id)) return false;
  if (membership.is_ministry_head) return actor.is_admin;
  return true;
}

/**
 * The ministries in `managedMinistries` this person is not already active in.
 *
 * What the "Add to a ministry" control offers. A ministry they were *removed*
 * from is offered again -- adding them back restores their original membership
 * and the serving history hanging off it, which is the point of offering it.
 */
export function ministriesAvailableFor(
  actor: ActorAuthority,
  person: Pick<DirectoryPerson, "memberships">,
): CurrentActor["headed_ministries"] {
  const active = new Set(
    person.memberships
      .filter((membership) => membership.deactivated_at === null)
      .map((membership) => membership.ministry_id),
  );
  return managedMinistries(actor).filter((ministry) => !active.has(ministry.ministry_id));
}

/**
 * How a person's ministries read in one directory cell.
 *
 * Active memberships only, by name, in the order the backend sent them (which
 * is by name). A head badge is rendered separately rather than baked into this
 * string -- a summary that said "AV (head)" would be a sentence, not data, and
 * the table needs to style the two differently.
 *
 * Somebody in no ministry gets an empty array, never the word "None": how to
 * render nothing is the table's decision, not this function's.
 */
export function activeMinistryNames(
  person: Pick<DirectoryPerson, "memberships">,
): string[] {
  return person.memberships
    .filter((membership) => membership.deactivated_at === null)
    .map((membership) => membership.ministry_name);
}

/** The ministries this person currently leads, by name. */
export function headedMinistryNames(
  person: Pick<DirectoryPerson, "memberships">,
): string[] {
  return person.memberships
    .filter((membership) => membership.deactivated_at === null && membership.is_ministry_head)
    .map((membership) => membership.ministry_name);
}

/**
 * The authority column's single word.
 *
 * Church-wide Admin outranks a per-ministry head in this one cell, because a
 * person who is both is more usefully found by the rarer fact -- their head
 * badges appear beside their ministries in the adjacent column either way.
 */
export function authorityLabel(
  person: Pick<DirectoryPerson, "is_admin" | "memberships">,
): string | null {
  if (person.is_admin) return "Administrator";
  return headedMinistryNames(person).length > 0 ? "Ministry head" : null;
}

/** Whether a person is active church-wide. */
export function isPersonActive(person: Pick<DirectoryPerson, "deactivated_at">): boolean {
  return person.deactivated_at === null;
}

/** Whether a membership is current, as distinct from the person's own state. */
export function isMembershipActive(
  membership: Pick<PersonMembership, "deactivated_at">,
): boolean {
  return membership.deactivated_at === null;
}

/**
 * The wording for taking somebody off one team.
 *
 * **A function, so the wording exists once.** Task 79 requires the label to
 * name the ministry and to say "Remove from", never "Delete" -- because
 * nothing is deleted, and a button that says otherwise tells a ministry head
 * that pressing it will destroy somebody's serving history.
 */
export function removeFromMinistryLabel(ministryName: string): string {
  return `Remove from ${ministryName}`;
}

/**
 * The wording for the church-wide act, which is a different act.
 *
 * Also not "Delete": the person, their memberships and every assignment they
 * have ever had survive it, and it is reversible.
 */
export const DEACTIVATE_PERSON_LABEL = "Deactivate person";
export const REACTIVATE_PERSON_LABEL = "Reactivate person";

/**
 * What the two removals actually do, in one sentence each.
 *
 * Kept beside the labels so the explanation cannot drift away from the button
 * it explains.
 */
export const REMOVE_FROM_MINISTRY_EXPLANATION =
  "They stay in the church and keep every past assignment. Only this ministry changes.";
export const DEACTIVATE_PERSON_EXPLANATION =
  "Church-wide, and reversible. Their memberships, assignments and serving history are all kept.";


// -- Church-wide oversight, which is a read (Task 79 §4) --------------------

/**
 * Whether this actor may create a canonical Person at all.
 *
 * **Two different routes to `true`, and the distinction is the backend's**:
 * an administrator may create somebody **unattached** — an entry in the
 * church's own roll that belongs to no team — while a ministry head may only
 * create somebody **into a ministry they lead**, because a head's reason for
 * creating a person is always "to put them on my team".
 *
 * So an administrator who leads nothing can still create a person, and the
 * ministry chooser they are shown is empty rather than absent-because-refused.
 */
export function canCreatePerson(actor: ActorAuthority): boolean {
  return actor.is_admin || canManageAnyMinistry(actor);
}

/**
 * Whether this actor gets the "All ministries" list.
 *
 * **Administrators only.** A ministry head reaches the ministries they lead
 * through `headed_ministries` on `/api/v1/me`, which is a statement about
 * their own memberships; a church-wide inventory is a different thing and is
 * not theirs. A volunteer sees neither.
 */
export function canSeeAllMinistries(actor: ActorAuthority): boolean {
  return actor.is_admin;
}

/**
 * Whether this actor may change somebody's formal church membership status.
 *
 * Administrators only. A ministry head *reads* it — useful context when
 * deciding who to approach — and cannot set it: whether somebody is a member
 * of the church is not a fact about one team's rota.
 */
export function canChangeChurchStatus(actor: ActorAuthority): boolean {
  return actor.is_admin;
}

// -- Church membership status, as words on a screen -------------------------

/**
 * How each status reads in the interface.
 *
 * "Unknown" is deliberately its own word and not a dash: nobody having said
 * is a real answer, and a blank cell would read as missing data somebody
 * ought to fill in rather than as the honest state of most records.
 */
export const CHURCH_STATUS_LABELS: Record<ChurchMembershipStatus, string> = {
  MEMBER: "Member",
  NON_MEMBER: "Not a member",
  UNKNOWN: "Unknown",
};

/** The three statuses, in the order the controls offer them. */
export const CHURCH_STATUS_ORDER: readonly ChurchMembershipStatus[] = [
  "MEMBER",
  "NON_MEMBER",
  "UNKNOWN",
];

export function churchStatusLabel(status: ChurchMembershipStatus): string {
  return CHURCH_STATUS_LABELS[status] ?? CHURCH_STATUS_LABELS.UNKNOWN;
}

/**
 * The sentence that keeps the two "memberships" apart wherever both appear.
 *
 * Written once, because the whole risk with this field is somebody reading it
 * as "is on a ministry".
 */
export const CHURCH_STATUS_EXPLANATION =
  "Formal membership of the church. Separate from the ministries they serve in, and never worked out from them.";

// -- Recorded serving (Task 79 §12–§13) -------------------------------------

/**
 * The one term for this number, matching the backend's own constant.
 *
 * **Never "attended" and never "times served".** The domain records who was
 * *scheduled*; nothing in it records who turned up. A label claiming otherwise
 * would put a statement about somebody's conduct on screen on evidence that
 * cannot support it.
 */
export const RECORDED_SERVING_LABEL = "Recorded serving";

/**
 * The sentence under the number, kept beside the label so the two cannot
 * drift apart.
 */
export const RECORDED_SERVING_EXPLANATION =
  "Past Sundays this person was scheduled to serve, from finalized schedules. It does not record attendance.";

/**
 * How a recorded-serving total reads in a table cell.
 *
 * Zero is rendered as "0", never as a dash: they have served no recorded
 * Sundays, which is a fact, and a dash would read as "not counted".
 */
export function recordedServingText(total: number): string {
  return String(total);
}
