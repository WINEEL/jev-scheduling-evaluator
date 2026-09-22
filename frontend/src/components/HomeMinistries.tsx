"use client";

/**
 * Home, for somebody who is already signed in.
 *
 * **It renders sections rather than one of three screens** (Task 79). The
 * decision lives in `lib/homeView.ts` and is tested there; this component only
 * renders what it names. The old three-way choice could not describe an
 * administrator who also heads a ministry — it had to pick one view, and
 * either choice dropped something that person needs.
 *
 * What each kind of account gets, which is the whole of this file:
 *
 * - **Everybody** — their own upcoming schedule, first. A ministry head serves
 *   on the rota they build, and "where am I expected?" is the question anybody
 *   opening this page has. The management sections follow it rather than
 *   displacing it.
 * - **Ministry head** — "Ministries you lead": the ministries they actually
 *   head, each with the one link that continues the scheduling workflow. This
 *   is the operational section.
 * - **Administrator** — "All ministries" (church-wide oversight, and a read)
 *   and "Administration" (the People directory). Task 79 replaced the notice
 *   that used to say no such list existed with the list.
 * - **Volunteer** — their schedule, and nothing else. No ministry-head control
 *   and no administrative control is rendered here at all, which a structure
 *   test pins.
 *
 * **Seeing a ministry is not running it.** An administrator reaches every
 * ministry through "All ministries" and can open its screens; the operational
 * controls stay behind "Ministries you lead", which lists only the ministries
 * they personally head. An administrator who heads none sees no operational
 * control anywhere on this page, and the API refuses them one.
 */

import Link from "next/link";

import { AllMinistries } from "@/components/AllMinistries";
import { useActor } from "@/components/AuthGate";
import { MySchedule } from "@/components/MySchedule";
import { homeSectionsFor } from "@/lib/homeView";
import {
  EXAMPLE_MINISTRY_LABEL,
  VALIDATED_MINISTRY_LABEL,
  isValidatedMinistry,
  pilotStatusNote,
} from "@/lib/pilotStatus";

export function HomeMinistries({
  /** Configured on the server; empty means no claim is made either way. */
  validatedMinistries,
}: {
  validatedMinistries: readonly string[];
}) {
  const actor = useActor();
  const sections = homeSectionsFor(actor);

  return (
    <>
      <h1 className="page__title">
        Signed in as {actor.display_name}
        {actor.is_admin ? " · administrator" : ""}
      </h1>

      {/* First, for everybody. */}
      <MySchedule />

      {sections.ledMinistries && (
        <LedMinistries validatedMinistries={validatedMinistries} />
      )}

      {sections.allMinistries && <AllMinistries />}

      {sections.administration && <Administration />}
    </>
  );
}

/**
 * The ministries this person actually heads: the operational section.
 *
 * Shown to anybody with at least one active head membership, administrator or
 * not — and an administrator's membership is what puts a ministry here, never
 * their `is_admin` flag. That is the same rule the backend applies, so a
 * ministry that appears here is one whose rota they may really change.
 */
function LedMinistries({
  validatedMinistries,
}: {
  validatedMinistries: readonly string[];
}) {
  const actor = useActor();
  const showsPilotStatus = validatedMinistries.length > 0;
  const hasUnvalidated = actor.headed_ministries.some(
    (ministry) => !isValidatedMinistry(ministry.name, validatedMinistries),
  );

  return (
    <section className="section" aria-labelledby="led-ministries-heading">
      <h2 className="page__section-title" id="led-ministries-heading">
        Ministries you lead
      </h2>

      <ul className="list-reset">
        {actor.headed_ministries.map((ministry) => {
          const isValidated = isValidatedMinistry(ministry.name, validatedMinistries);
          return (
            <li className="card" key={ministry.ministry_id}>
              <div className="card__header">
                <h3 className="card__title">{ministry.name}</h3>
                {showsPilotStatus && (
                  <span className={isValidated ? "badge badge--ok" : "badge"}>
                    {isValidated ? VALIDATED_MINISTRY_LABEL : EXAMPLE_MINISTRY_LABEL}
                  </span>
                )}
              </div>
              {/* One way in, deliberately. `Roles and qualifications` used to
                  sit beside this link and pointed at the very same route the
                  scheduling workflow's third step points at -- the same
                  ministry-level screen, not a second one. Home names the
                  ministry and opens its periods; the workflow, which is where
                  a head is already looking when roles matter, keeps the step. */}
              <div className="card__actions">
                <Link
                  className="button button--primary link-button"
                  href={`/ministries/${ministry.ministry_id}/periods`}
                >
                  Scheduling periods
                </Link>
              </div>
            </li>
          );
        })}
      </ul>

      {showsPilotStatus && <p className="pilot-note">{pilotStatusNote(hasUnvalidated)}</p>}
    </section>
  );
}

/**
 * Church-wide governance, which is the administrator's own responsibility.
 *
 * One entry today — the People directory — and it is deliberately a link
 * rather than the directory itself: the whole church's membership roll does
 * not belong on a home screen, and somebody opening this page is usually here
 * for their own schedule.
 */
function Administration() {
  return (
    <section className="section" aria-labelledby="administration-heading">
      <h2 className="page__section-title" id="administration-heading">
        Administration
      </h2>
      <ul className="list-reset">
        <li className="card">
          <div className="card__header">
            <h3 className="card__title">People</h3>
          </div>
          <p className="small muted">
            Everyone in the church, the ministries they serve in, and their church membership
            status. One person has one record, however many ministries they belong to.
          </p>
          <div className="card__actions">
            <Link className="button button--primary link-button" href="/people">
              Open the people directory
            </Link>
          </div>
        </li>
      </ul>
    </section>
  );
}
