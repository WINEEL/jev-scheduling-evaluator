/**
 * Which sections of home an authenticated person gets.
 *
 * Extracted as a plain function rather than left as conditionals inside the
 * component, for the reason `vitest.config.ts` gives for having no component
 * renderer at all: the rule worth protecting here is *who sees what*, and that
 * is a decision about an actor, not about markup. Written this way it can be
 * tested directly, exhaustively, and without a browser.
 *
 * **It returns a set of sections rather than one view name, and that is Task
 * 79's change.** The old three-way choice — ministries / admin-with-nothing /
 * volunteer — could not describe the screen the product now describes, because
 * an administrator who *also* heads a ministry needs both their own ministries
 * and the church-wide list, and picking one view meant silently dropping the
 * other.
 *
 * The four sections, and who gets each:
 *
 * - **`schedule`** — everybody, and always first. A ministry head serves on
 *   the rota they build, and "where am I expected?" is the question anybody
 *   opening this page has.
 * - **`ledMinistries`** — anybody who actively heads at least one ministry.
 *   This is the operational section: the ministries they may actually run.
 * - **`allMinistries`** — administrators. Church-wide oversight, and a read:
 *   an administrator may inspect every ministry and may appoint somebody to
 *   lead one, and does not thereby become every ministry's head.
 * - **`administration`** — administrators. The People directory and the
 *   church-wide governance it carries.
 *
 * A volunteer gets `schedule` and nothing else, which is the whole of their
 * experience and is deliberate: no ministry-head control and no administrative
 * control is rendered for them at all.
 *
 * Note what does not appear here: nothing derived from a ministry's name, and
 * no notion of "the first ministry". Authority is `is_admin` plus the
 * memberships the server reported, and nothing else — and none of it is a
 * security boundary. Every rule below is enforced again in FastAPI on every
 * request; what these buy is a screen that tells the truth.
 */

import type { CurrentActor } from "./api/types";

export interface HomeSections {
  /** My upcoming schedule. Always true — everybody has one. */
  schedule: true;
  /** "Ministries you lead": the ones this person actively heads. */
  ledMinistries: boolean;
  /** "All ministries": every ministry in the church, for oversight. */
  allMinistries: boolean;
  /** "Administration": the People directory. */
  administration: boolean;
}

/**
 * Takes the two fields it actually reads rather than the whole DTO, so a change
 * to any other part of `CurrentActor` cannot silently change what somebody
 * sees.
 */
export function homeSectionsFor(
  actor: Pick<CurrentActor, "is_admin" | "headed_ministries">,
): HomeSections {
  return {
    schedule: true,
    ledMinistries: actor.headed_ministries.length > 0,
    allMinistries: actor.is_admin,
    administration: actor.is_admin,
  };
}

/**
 * Whether this person sees anything on home beyond their own schedule.
 *
 * Used to decide whether the page needs section headings at all: a volunteer's
 * home is one list, and a heading above a single section that has no siblings
 * is furniture rather than navigation.
 */
export function hasManagementSections(
  actor: Pick<CurrentActor, "is_admin" | "headed_ministries">,
): boolean {
  const sections = homeSectionsFor(actor);
  return sections.ledMinistries || sections.allMinistries || sections.administration;
}
