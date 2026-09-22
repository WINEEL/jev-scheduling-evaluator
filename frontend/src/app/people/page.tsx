"use client";

/**
 * The church-wide people directory (Task 79).
 *
 * The screen this product always described and never had: until now a Person
 * existed only if an import script made one, and a membership only if somebody
 * wrote it in psql.
 *
 * **Who reaches it.** Admins and ministry heads. The People link is hidden
 * from everybody else, and `canViewPeople` in `lib/peopleAccess.ts` is the one
 * place that decides -- but the *protection* is the API, which answers 403 to
 * a volunteer who types the URL. This component renders the refusal honestly
 * rather than pretending the page is empty.
 *
 * **What it shows, and what it deliberately does not.** Name, formal church
 * membership status, ministries, recorded serving, authority, active status.
 * No gender, birthday, age, child flag, affinity or avoidance: the domain has
 * none of them, nothing in scheduling reads one, and a directory that
 * collected them would be a church holding personal data it has no use for.
 *
 * **No contact details either**, and the backend does not send them for a
 * listing at all. This is the whole church's roll and every ministry head may
 * read it; a table that shows no address has no business receiving one for
 * every person in the church. They arrive on a person's own page, which is
 * where an administrator manages sign-in access from.
 *
 * **Church status is not ministry membership**, and the two columns sit side
 * by side saying so. Neither is ever worked out from the other.
 *
 * **Search is plain substring matching, server-side, and never fuzzy.** It
 * exists so somebody can type three letters and find the row they already know
 * is there. It does not rank, suggest or auto-select -- the moment a directory
 * guesses which person you meant, somebody eventually accepts the guess, and
 * that is how one human becomes two records.
 *
 * **Creating a person is a deliberate act with a deliberate speed bump.** The
 * flow is: search first, add the person you found, and only then offer to
 * create. If the name exactly matches somebody who already exists, the backend
 * refuses once and this screen shows that sentence with a button that says the
 * decision out loud. Nothing here or in the API merges two people, ever.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { useActor } from "@/components/AuthGate";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import {
  EmptyNotice,
  ErrorNotice,
  LoadingNotice,
  UnexpectedErrorNotice,
  asApiError,
} from "@/components/Feedback";
import { createPerson, getPeople } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { DirectoryPerson, PeopleDirectory } from "@/lib/api/types";
import {
  RECORDED_SERVING_EXPLANATION,
  RECORDED_SERVING_LABEL,
  activeMinistryNames,
  authorityLabel,
  canCreatePerson,
  canViewPeople,
  churchStatusLabel,
  headedMinistryNames,
  isPersonActive,
  managedMinistries,
  recordedServingText,
} from "@/lib/peopleAccess";

export default function PeoplePage() {
  const actor = useActor();

  // The decision lives in `lib/peopleAccess.ts` and is tested there; this
  // component only renders the screen it names. A volunteer who navigates here
  // directly is told plainly rather than shown an empty table -- and the API
  // would refuse them in any case.
  if (!canViewPeople(actor)) return <NotForYou />;
  return <Directory />;
}

function NotForYou() {
  return (
    <>
      <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "People" }]} />
      <div className="notice notice--warning" role="alert">
        <p className="notice__title">You do not have access</p>
        <p>The church people directory is available to administrators and ministry heads.</p>
      </div>
    </>
  );
}

function Directory() {
  const actor = useActor();

  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [includeInactive, setIncludeInactive] = useState(false);
  const [data, setData] = useState<PeopleDirectory | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isAdding, setIsAdding] = useState(false);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getPeople({ search: appliedSearch, includeInactive }, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          // An abort is not an outcome: this component is unmounting, or a
          // newer request (a search, most often) has replaced this one. The
          // previous list stays on screen rather than flashing back to a
          // loading state for a request that already has something to show.
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [appliedSearch, includeInactive],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const trail = [{ label: "Home", href: "/" }, { label: "People" }];

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">People</h1>
      <p className="page__subtitle">
        Everyone in the church, and the ministries they serve in. One person has one record,
        however many ministries they belong to.
      </p>

      <section className="section" aria-labelledby="find-heading">
        <h2 className="section__title" id="find-heading">
          Find someone
        </h2>

        <form
          className="people-search"
          onSubmit={(event) => {
            event.preventDefault();
            setAppliedSearch(search);
          }}
        >
          <div className="field">
            <label htmlFor="people-search">Search by name</label>
            <input
              id="people-search"
              type="text"
              value={search}
              placeholder="Part of a name"
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <button className="button" type="submit">
            Search
          </button>
          {appliedSearch !== "" && (
            <button
              className="button"
              type="button"
              onClick={() => {
                setSearch("");
                setAppliedSearch("");
              }}
            >
              Clear
            </button>
          )}
        </form>

        <div className="field field--inline">
          <input
            id="include-inactive"
            type="checkbox"
            checked={includeInactive}
            onChange={(event) => setIncludeInactive(event.target.checked)}
          />
          <label htmlFor="include-inactive">Show people who are no longer active</label>
        </div>

        {/* Add comes *after* search, deliberately. The order on the screen is
            the order of the decision: look for the person first, and create
            one only when they genuinely are not here. */}
        {canCreatePerson(actor) && !isAdding && (
          <div className="card__actions">
            <button className="button" type="button" onClick={() => setIsAdding(true)}>
              Add a person
            </button>
          </div>
        )}
      </section>

      {isAdding && (
        <AddPersonPanel
          onCancel={() => setIsAdding(false)}
          onCreated={() => {
            setIsAdding(false);
            void load();
          }}
        />
      )}

      <DirectoryResults
        data={data}
        error={error}
        isLoading={isLoading}
        search={appliedSearch}
      />
    </>
  );
}

function DirectoryResults({
  data,
  error,
  isLoading,
  search,
}: {
  data: PeopleDirectory | null;
  error: unknown;
  isLoading: boolean;
  search: string;
}) {
  if (isLoading) return <LoadingNotice what="people" />;

  if (error !== null) {
    const apiError = asApiError(error);
    return apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />;
  }

  if (data === null) return <UnexpectedErrorNotice />;

  if (data.people.length === 0) {
    return (
      <EmptyNotice>
        <p className="notice__title">Nobody matches</p>
        <p>
          {search === ""
            ? "There is nobody in the directory yet."
            : `No name contains “${search}”. Matching is plain and literal, so check the spelling.`}
        </p>
      </EmptyNotice>
    );
  }

  return (
    <section className="section" aria-labelledby="directory-heading">
      <h2 className="section__title" id="directory-heading">
        {data.total === data.people.length
          ? `${data.total} ${data.total === 1 ? "person" : "people"}`
          : `Showing ${data.people.length} of ${data.total}`}
      </h2>

      {/* Wide content scrolls inside its own container rather than making the
          page scroll sideways -- the convention every other table here uses. */}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Church status</th>
              <th scope="col">Ministries</th>
              <th scope="col" title={RECORDED_SERVING_EXPLANATION}>
                {RECORDED_SERVING_LABEL}
              </th>
              <th scope="col">Authority</th>
              <th scope="col">Status</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {data.people.map((person) => (
              <DirectoryRow key={person.person_id} person={person} />
            ))}
          </tbody>
        </table>
      </div>

      {data.total > data.people.length && (
        <p className="small muted">
          Only the first {data.people.length} are shown. Narrow the list with a search.
        </p>
      )}
    </section>
  );
}

function DirectoryRow({ person }: { person: DirectoryPerson }) {
  const ministries = activeMinistryNames(person);
  const headed = new Set(headedMinistryNames(person));
  const authority = authorityLabel(person);
  const active = isPersonActive(person);

  return (
    <tr>
      <th scope="row">{person.display_name}</th>
      {/* Formal membership of the church. A different fact from the ministries
          in the next column, and never worked out from them. */}
      <td>{churchStatusLabel(person.church_membership_status)}</td>
      <td>
        {ministries.length === 0 ? (
          <span className="muted">—</span>
        ) : (
          <span className="ministry-list">
            {ministries.map((name) => (
              <span key={name}>
                {name}
                {headed.has(name) && <span className="badge badge--info">head</span>}
              </span>
            ))}
          </span>
        )}
      </td>
      {/* A number, never a dash: nought recorded Sundays is a fact, and a dash
          would read as "not counted". */}
      <td>{recordedServingText(person.recorded_serving_total)}</td>
      <td>{authority ?? <span className="muted">—</span>}</td>
      <td>
        {/* "Inactive", never "Deleted": nothing is. */}
        <span className={active ? "badge badge--ok" : "badge badge--warning"}>
          {active ? "Active" : "Inactive"}
        </span>
      </td>
      <td>
        <Link href={`/people/${person.person_id}`}>View</Link>
      </td>
    </tr>
  );
}

/**
 * Creating a canonical person.
 *
 * **The whole point of this panel is the refusal it can receive.** When an
 * exact same-name person already exists the backend answers 409 with a
 * sentence saying so, and this panel shows that sentence and offers a second
 * button whose label states the decision: "Create anyway". Pressing the first
 * button again would be ambiguous; a different button with different words is
 * the explicit human decision Task 79 requires.
 *
 * A Ministry Head must name a ministry they lead -- the backend requires it,
 * and the field below is therefore required for them and optional for an
 * Admin. An Admin who heads nothing sees no ministry choice at all, because
 * nothing in the API lists every ministry for them to pick from.
 */
function AddPersonPanel({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: () => void;
}) {
  const actor = useActor();
  const choices = managedMinistries(actor);

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [ministryId, setMinistryId] = useState<string>(
    choices.length === 1 ? String(choices[0].ministry_id) : "",
  );
  const [error, setError] = useState<unknown>(null);
  const [needsConfirmation, setNeedsConfirmation] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const ministryRequired = !actor.is_admin;
  const canSubmit =
    displayName.trim() !== "" && (!ministryRequired || ministryId !== "") && !isSaving;

  const submit = (acknowledgeDuplicateName: boolean) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);

    createPerson(
      {
        display_name: displayName,
        email,
        phone,
        initial_ministry_id: ministryId === "" ? undefined : Number(ministryId),
        acknowledge_duplicate_name: acknowledgeDuplicateName,
      },
      next.signal,
    )
      .then(() => {
        setIsSaving(false);
        onCreated();
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
        // A 409 from this endpoint is the duplicate-name refusal. It is the
        // only failure here that a person can answer by deciding, so it is the
        // only one that unlocks the second button.
        setNeedsConfirmation(asApiError(cause)?.kind === "conflict");
      });
  };

  const apiError = asApiError(error);

  return (
    <section className="section" aria-labelledby="add-person-heading">
      <h2 className="section__title" id="add-person-heading">
        Add a person
      </h2>
      <p className="small muted">
        Search the directory first. One person has one record — if they already serve in another
        ministry, add that existing person to yours instead of creating a second one.
      </p>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit(false);
        }}
      >
        <div className="field">
          <label htmlFor="new-person-name">Name</label>
          <input
            id="new-person-name"
            type="text"
            value={displayName}
            onChange={(event) => {
              setDisplayName(event.target.value);
              // A different name is a different question: the previous
              // refusal, and the permission it granted, no longer apply.
              setNeedsConfirmation(false);
              setError(null);
            }}
          />
        </div>

        <div className="field">
          <label htmlFor="new-person-email">Sign-in email (optional)</label>
          <input
            id="new-person-email"
            type="text"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <p className="field__hint">
            Only needed when they will sign in. It can be added later.
          </p>
        </div>

        <div className="field">
          <label htmlFor="new-person-phone">Phone (optional)</label>
          <input
            id="new-person-phone"
            type="text"
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
          />
        </div>

        {choices.length > 0 && (
          <div className="field">
            <label htmlFor="new-person-ministry">
              {ministryRequired ? "Add them to" : "Add them to (optional)"}
            </label>
            <select
              id="new-person-ministry"
              value={ministryId}
              onChange={(event) => setMinistryId(event.target.value)}
            >
              <option value="">
                {ministryRequired ? "Choose a ministry" : "No ministry yet"}
              </option>
              {choices.map((ministry) => (
                <option key={ministry.ministry_id} value={ministry.ministry_id}>
                  {ministry.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {apiError !== null && <ErrorNotice error={apiError} />}
        {apiError === null && error !== null && <UnexpectedErrorNotice />}

        <div className="card__actions">
          <button className="button button--primary" type="submit" disabled={!canSubmit}>
            {isSaving ? "Creating…" : "Create person"}
          </button>
          {needsConfirmation && (
            // A second, differently-worded button. The first one cannot become
            // this one, because pressing the same control twice is not a
            // decision -- it is a double-click.
            <button
              className="button"
              type="button"
              disabled={isSaving}
              onClick={() => submit(true)}
            >
              Create anyway — this is a different person
            </button>
          )}
          <button className="button" type="button" onClick={onCancel} disabled={isSaving}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
