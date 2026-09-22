"use client";

/**
 * One person: their church-wide record, and the ministries they belong to
 * (Task 79).
 *
 * **The two halves of this screen are two different things, and the layout
 * says so.** ADR 0001's whole point is that one human is one `Person` and that
 * participation is a separate relation per ministry -- so this page is two
 * clearly separated sections with two different audiences and two different
 * sets of controls:
 *
 * - **Church-wide person** -- identity, sign-in access, and active state.
 *   Administrators only. Changing anything here affects every ministry the
 *   person serves in, including ones the viewer has nothing to do with.
 * - **Ministry memberships** -- one row per ministry, each with its own head
 *   badge, its own active state, and its own Remove control. A ministry head
 *   sees controls for the ministries they lead and reads the rest.
 *
 * Nothing on this page is called *Delete*, because nothing on this page
 * deletes. "Remove from Setup" takes somebody off one team and keeps every
 * assignment they ever served on it. "Deactivate person" is church-wide and
 * reversible. Both are one timestamp in the database, and both are reported to
 * the audit trail as what they are.
 *
 * **Every control here is also enforced in the API.** Hiding a button is how
 * this screen avoids promising something that would be refused; the refusal
 * itself is FastAPI's, on every request.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";

import { useActor } from "@/components/AuthGate";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import {
  ErrorNotice,
  LoadingNotice,
  UnexpectedErrorNotice,
  asApiError,
} from "@/components/Feedback";
import {
  addPersonToMinistry,
  deactivatePerson,
  getPerson,
  getMinistries,
  reactivatePerson,
  removePersonFromMinistry,
  setChurchMembershipStatus,
  setMinistryHead,
  setPersonAuthLink,
  updatePerson,
} from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type {
  ChurchMembershipStatus,
  MinistryOverview,
  PersonDetail,
  PersonMembership,
} from "@/lib/api/types";
import {
  CHURCH_STATUS_EXPLANATION,
  CHURCH_STATUS_ORDER,
  DEACTIVATE_PERSON_EXPLANATION,
  DEACTIVATE_PERSON_LABEL,
  REACTIVATE_PERSON_LABEL,
  RECORDED_SERVING_EXPLANATION,
  RECORDED_SERVING_LABEL,
  REMOVE_FROM_MINISTRY_EXPLANATION,
  canChangeChurchStatus,
  canManagePersonRecord,
  canRemoveMembership,
  canViewPeople,
  churchStatusLabel,
  isMembershipActive,
  isPersonActive,
  ministriesAvailableFor,
  removeFromMinistryLabel,
} from "@/lib/peopleAccess";

export default function PersonPage() {
  const actor = useActor();
  const params = useParams<{ personId: string }>();
  const personId = Number(params.personId);
  const hasValidId = Number.isInteger(personId) && personId > 0;

  const [person, setPerson] = useState<PersonDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidId);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getPerson(personId, signal)
        .then((result) => {
          setPerson(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [personId],
  );

  useEffect(() => {
    if (!hasValidId || !canViewPeople(actor)) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidId, actor]);

  const trail = [
    { label: "Home", href: "/" },
    { label: "People", href: "/people" },
    { label: person?.display_name ?? "Person" },
  ];

  // Checked before anything is fetched, so a volunteer who types this URL
  // never even issues the request. The API would refuse it anyway.
  if (!canViewPeople(actor)) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Person" }]} />
        <div className="notice notice--warning" role="alert">
          <p className="notice__title">You do not have access</p>
          <p>Church people records are available to administrators and ministry heads.</p>
        </div>
      </>
    );
  }

  if (!hasValidId) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That person address is not valid.</p>
        </div>
      </>
    );
  }

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <LoadingNotice what="this person" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Person</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (person === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{person.display_name}</h1>
      <p className="page__subtitle">
        One church-wide record, and one membership for each ministry they serve in.
      </p>

      <ChurchWideSection person={person} onChanged={setPerson} />
      <RecordedServingSection person={person} />
      <MembershipsSection person={person} onChanged={() => void load()} />
    </>
  );
}

/**
 * The church-wide half: identity, sign-in access, active state.
 *
 * Administrators only. A ministry head reads it and is told plainly why there
 * are no controls -- which is more useful than silently absent buttons, since
 * "who do I ask?" is the actual question they have.
 */
function ChurchWideSection({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: (person: PersonDetail) => void;
}) {
  const actor = useActor();
  const mayManage = canManagePersonRecord(actor);
  const active = isPersonActive(person);

  const [isEditing, setIsEditing] = useState(false);

  return (
    <section className="section" aria-labelledby="church-wide-heading">
      <h2 className="section__title" id="church-wide-heading">
        Church-wide person
      </h2>
      <p className="small muted">
        Everything here applies to this person everywhere in the church, in every ministry they
        serve in.
      </p>

      {isEditing ? (
        <EditPersonForm
          person={person}
          onCancel={() => setIsEditing(false)}
          onSaved={(updated) => {
            setIsEditing(false);
            onChanged(updated);
          }}
        />
      ) : (
        <>
          <dl className="person-facts">
            <div>
              <dt>Name</dt>
              <dd>{person.display_name}</dd>
            </div>
            <div>
              <dt>Phone</dt>
              <dd>{person.phone ?? <span className="muted">Not recorded</span>}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>
                <span className={active ? "badge badge--ok" : "badge badge--warning"}>
                  {active ? "Active" : "Inactive"}
                </span>
              </dd>
            </div>
            <div>
              <dt>Church membership</dt>
              <dd>
                {churchStatusLabel(person.church_membership_status)}
                <p className="small muted">{CHURCH_STATUS_EXPLANATION}</p>
              </dd>
            </div>
            <div>
              <dt>Church-wide authority</dt>
              <dd>
                {person.is_admin ? (
                  "Administrator"
                ) : (
                  <span className="muted">None</span>
                )}
              </dd>
            </div>
          </dl>

          {canChangeChurchStatus(actor) && (
            <ChurchStatusControl person={person} onChanged={onChanged} />
          )}

          {mayManage && (
            <div className="card__actions">
              <button className="button" type="button" onClick={() => setIsEditing(true)}>
                Edit details
              </button>
              <DeactivateControl person={person} onChanged={onChanged} />
            </div>
          )}
        </>
      )}

      {mayManage ? (
        <AuthLinkPanel person={person} onChanged={onChanged} />
      ) : (
        <p className="small muted">
          Only an administrator can change a person&rsquo;s church-wide record or their sign-in
          access. You can manage their membership of the ministries you lead, below.
        </p>
      )}
    </section>
  );
}

/**
 * Record whether this person is a formal member of the church.
 * **Administrators only.**
 *
 * **Deliberately sitting in the church-wide section and nowhere near the
 * memberships table.** The two "memberships" are the thing most likely to be
 * confused, and the layout is the first defence: this is a church-wide
 * governance fact an administrator states, and the table below is which teams
 * somebody serves on. Neither is ever worked out from the other.
 *
 * "Unknown" is offered as a real choice, not left out: nobody having said is
 * the honest state of most records, and setting somebody back to it is a
 * correction rather than a deletion.
 */
function ChurchStatusControl({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: (person: PersonDetail) => void;
}) {
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const save = (status: ChurchMembershipStatus) => {
    if (status === person.church_membership_status) return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);

    setChurchMembershipStatus(person.person_id, status, next.signal)
      .then((updated) => {
        setIsSaving(false);
        onChanged(updated);
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  return (
    <div className="field">
      <label htmlFor="church-status">Church membership status</label>
      <select
        id="church-status"
        value={person.church_membership_status}
        disabled={isSaving}
        onChange={(event) => save(event.target.value as ChurchMembershipStatus)}
      >
        {CHURCH_STATUS_ORDER.map((status) => (
          <option key={status} value={status}>
            {churchStatusLabel(status)}
          </option>
        ))}
      </select>
      <p className="field__hint">{CHURCH_STATUS_EXPLANATION}</p>
      <FormError error={error} />
    </div>
  );
}

/**
 * How often this person has been scheduled, church-wide and per ministry.
 *
 * **The words matter more than the number.** This counts past Sundays for
 * which a finalized schedule committed them to serve. Nothing in this
 * application records who actually turned up, so the heading says "recorded
 * serving" and the sentence underneath says what that does and does not mean.
 * Calling it "times served" would put a claim about somebody's conduct on a
 * screen that other people make decisions from.
 *
 * Only ministries they have history in are listed: a row reading "AV: 0" would
 * assert an absence that the absence of the row already states.
 */
function RecordedServingSection({ person }: { person: PersonDetail }) {
  const serving = person.serving ?? null;

  return (
    <section className="section" aria-labelledby="serving-heading">
      <h2 className="section__title" id="serving-heading">
        {RECORDED_SERVING_LABEL}
      </h2>
      <p className="small muted">{RECORDED_SERVING_EXPLANATION}</p>

      <p className="serving-total">
        Total: <strong>{person.recorded_serving_total}</strong>
      </p>

      {serving !== null && serving.same_date_conflicts.length > 0 && (
        // Not folded into the number. Two ministries on one Sunday breaks the
        // church-wide rule that one person serves at most one ministry that
        // day, so it is a contradiction in the record rather than two
        // services, and the figure beside it should be read as suspect.
        <div className="notice notice--warning" role="alert">
          <p className="notice__title">This record contradicts itself</p>
          <p>
            One person serves at most one ministry on a Sunday. The finalized schedules put
            this person in more than one on
            {" "}
            {serving.same_date_conflicts
              .map((conflict) => `${conflict.event_date} (${conflict.ministry_names.join(", ")})`)
              .join("; ")}
            . The total above counts each of those as separate serving, so treat it as
            unreliable until this is sorted out.
          </p>
        </div>
      )}

      {serving === null || serving.by_ministry.length === 0 ? (
        <p className="muted">No recorded serving yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Ministry</th>
                <th scope="col">{RECORDED_SERVING_LABEL}</th>
              </tr>
            </thead>
            <tbody>
              {serving.by_ministry.map((entry) => (
                <tr key={entry.ministry_id}>
                  <th scope="row">{entry.ministry_name}</th>
                  <td>{entry.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function EditPersonForm({
  person,
  onCancel,
  onSaved,
}: {
  person: PersonDetail;
  onCancel: () => void;
  onSaved: (person: PersonDetail) => void;
}) {
  const [displayName, setDisplayName] = useState(person.display_name);
  const [phone, setPhone] = useState(person.phone ?? "");
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        controller.current?.abort();
        const next = new AbortController();
        controller.current = next;
        setIsSaving(true);
        updatePerson(person.person_id, { display_name: displayName, phone }, next.signal)
          .then((updated) => {
            setIsSaving(false);
            onSaved(updated);
          })
          .catch((cause: unknown) => {
            if (isAbortError(cause)) return;
            setIsSaving(false);
            setError(cause);
          });
      }}
    >
      <div className="field">
        <label htmlFor="edit-person-name">Name</label>
        <input
          id="edit-person-name"
          type="text"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="edit-person-phone">Phone</label>
        <input
          id="edit-person-phone"
          type="text"
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
        />
      </div>

      {/* The sign-in address is deliberately not a field in this form: it
          decides which Google account *is* this person, so it is its own
          operation below with its own confirmation. */}

      <FormError error={error} />

      <div className="card__actions">
        <button
          className="button button--primary"
          type="submit"
          disabled={displayName.trim() === "" || isSaving}
        >
          {isSaving ? "Saving…" : "Save changes"}
        </button>
        <button className="button" type="button" onClick={onCancel} disabled={isSaving}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/**
 * Deactivate or reactivate, church-wide.
 *
 * Two-step, because this one act reaches every ministry in the church at once
 * and the person pressing it may lead only one of them. The confirmation
 * states what actually happens: nothing is deleted, and it can be undone.
 */
function DeactivateControl({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: (person: PersonDetail) => void;
}) {
  const [isConfirming, setIsConfirming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const active = isPersonActive(person);

  const run = () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);
    const call = active ? deactivatePerson : reactivatePerson;
    call(person.person_id, next.signal)
      .then((updated) => {
        setIsSaving(false);
        setIsConfirming(false);
        onChanged(updated);
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  if (!active) {
    return (
      <>
        <button className="button" type="button" onClick={run} disabled={isSaving}>
          {isSaving ? "Working…" : REACTIVATE_PERSON_LABEL}
        </button>
        <FormError error={error} />
      </>
    );
  }

  if (!isConfirming) {
    return (
      <button className="button" type="button" onClick={() => setIsConfirming(true)}>
        {DEACTIVATE_PERSON_LABEL}
      </button>
    );
  }

  return (
    <div className="notice notice--warning" role="alert">
      <p className="notice__title">{DEACTIVATE_PERSON_LABEL}?</p>
      <p>{DEACTIVATE_PERSON_EXPLANATION}</p>
      <p className="small">
        This takes them out of every ministry&rsquo;s pool at once, not just yours.
      </p>
      <FormError error={error} />
      <div className="card__actions">
        <button className="button button--primary" type="button" onClick={run} disabled={isSaving}>
          {isSaving ? "Working…" : `Yes, ${DEACTIVATE_PERSON_LABEL.toLowerCase()}`}
        </button>
        <button
          className="button"
          type="button"
          onClick={() => setIsConfirming(false)}
          disabled={isSaving}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * Whether this person can sign in, and the address they sign in with.
 *
 * Administrators only, and a small surface on purpose: none of OAuth is
 * redesigned here. The backend validates with the same function the Google
 * callback and the admin CLI use, so an address linked on this screen and one
 * linked from the command line behave identically at sign-in.
 */
function AuthLinkPanel({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: (person: PersonDetail) => void;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [email, setEmail] = useState(person.email ?? "");
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const save = (value: string | null) => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);
    setPersonAuthLink(person.person_id, value, next.signal)
      .then((updated) => {
        setIsSaving(false);
        setIsEditing(false);
        setEmail(updated.email ?? "");
        onChanged(updated);
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  return (
    <div className="subsection">
      <h3 className="subsection__title">Sign-in access</h3>

      {!isEditing ? (
        <>
          <p>
            {person.email === null ? (
              <span className="muted">
                No address linked — they cannot sign in.
              </span>
            ) : (
              <>
                Signs in as <strong>{person.email}</strong>
              </>
            )}
          </p>
          <div className="card__actions">
            <button className="button" type="button" onClick={() => setIsEditing(true)}>
              {person.email === null ? "Link an address" : "Change or remove"}
            </button>
          </div>
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save(email.trim() === "" ? null : email);
          }}
        >
          <div className="field">
            <label htmlFor="auth-link-email">Google address</label>
            <input
              id="auth-link-email"
              type="text"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            <p className="field__hint">
              One address belongs to one person. Changing it stops the previous address working
              immediately; clearing it removes their access without touching anything else.
            </p>
          </div>

          <FormError error={error} />

          <div className="card__actions">
            <button className="button button--primary" type="submit" disabled={isSaving}>
              {isSaving ? "Saving…" : "Save access"}
            </button>
            {person.email !== null && (
              <button
                className="button"
                type="button"
                disabled={isSaving}
                onClick={() => save(null)}
              >
                Remove access
              </button>
            )}
            <button
              className="button"
              type="button"
              disabled={isSaving}
              onClick={() => {
                setEmail(person.email ?? "");
                setError(null);
                setIsEditing(false);
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

/**
 * The ministry-scoped half: one row per ministry, each with its own controls.
 *
 * A ministry head sees Remove on the ministries they lead and nothing on the
 * others. An administrator sees Remove everywhere, plus the head grant/revoke
 * control, which is theirs alone.
 */
function MembershipsSection({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: () => void;
}) {
  const actor = useActor();
  const available = ministriesAvailableFor(actor, person);

  return (
    <section className="section" aria-labelledby="memberships-heading">
      <h2 className="section__title" id="memberships-heading">
        Ministry memberships
      </h2>
      <p className="small muted">
        Each ministry is separate. Changing one of these leaves the rest of the church, and this
        person&rsquo;s record, exactly as it is. You can change a membership only for a ministry
        you lead &mdash; being an administrator lets you see every ministry, not run them.
      </p>

      {person.memberships.length === 0 ? (
        <p className="muted">They do not belong to any ministry yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Ministry</th>
                <th scope="col">In this ministry</th>
                <th scope="col">Status</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {person.memberships.map((membership) => (
                <MembershipRow
                  key={membership.ministry_membership_id}
                  person={person}
                  membership={membership}
                  onChanged={onChanged}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {available.length > 0 && (
        <AddToMinistry person={person} choices={available} onChanged={onChanged} />
      )}

      {/* Administrators only, and deliberately separate from "Add to a
          ministry" above: adding somebody to a team is that team's head's
          decision, while appointing its leader is church governance. An
          administrator who leads nothing sees the second control and not the
          first, which is exactly the separation. */}
      {canManagePersonRecord(actor) && (
        <AppointHeadElsewhere person={person} onChanged={onChanged} />
      )}
    </section>
  );
}

function MembershipRow({
  person,
  membership,
  onChanged,
}: {
  person: PersonDetail;
  membership: PersonMembership;
  onChanged: () => void;
}) {
  const actor = useActor();
  const active = isMembershipActive(membership);
  const mayRemove = active && canRemoveMembership(actor, membership);
  const mayChangeAuthority = canManagePersonRecord(actor) && active;

  return (
    <tr>
      <th scope="row">{membership.ministry_name}</th>
      <td>
        {membership.is_ministry_head ? (
          <span className="badge badge--info">Ministry head</span>
        ) : (
          <span className="muted">Member</span>
        )}
        {membership.notes !== null && <p className="small muted">{membership.notes}</p>}
      </td>
      <td>
        {/* The membership's own state, never the person's. */}
        <span className={active ? "badge badge--ok" : "badge badge--warning"}>
          {active ? "Active" : "Removed"}
        </span>
      </td>
      <td>
        <div className="row-actions">
          {mayChangeAuthority && (
            <HeadAuthorityControl
              person={person}
              ministryId={membership.ministry_id}
              ministryName={membership.ministry_name}
              isMinistryHead={membership.is_ministry_head}
              onChanged={onChanged}
            />
          )}
          {mayRemove && (
            <RemoveFromMinistry
              person={person}
              membership={membership}
              onChanged={onChanged}
            />
          )}
        </div>
      </td>
    </tr>
  );
}

/**
 * Appoint this person to lead a ministry, or revoke that authority.
 * **Administrators only.**
 *
 * Person, ministry and direction are all explicit — which is what the backend
 * now takes, and the reason it changed: a ministry nobody leads has no
 * membership to hang a promotion off, so a promotion addressed to a membership
 * could never give a brand-new ministry its first head. Granting here creates
 * the membership when there is not one.
 *
 * **Heading a ministry confers no say in who leads it.** This control is
 * rendered for an administrator and for nobody else, including the head of the
 * very ministry in question.
 */
function HeadAuthorityControl({
  person,
  ministryId,
  ministryName,
  isMinistryHead,
  onChanged,
}: {
  person: PersonDetail;
  ministryId: number;
  ministryName: string;
  isMinistryHead: boolean;
  onChanged: () => void;
}) {
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const run = () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);
    setMinistryHead(person.person_id, ministryId, !isMinistryHead, next.signal)
      .then(() => {
        setIsSaving(false);
        onChanged();
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  return (
    <>
      <button className="button" type="button" onClick={run} disabled={isSaving}>
        {isSaving
          ? "Working…"
          : isMinistryHead
            ? `Revoke head of ${ministryName}`
            : `Make head of ${ministryName}`}
      </button>
      <FormError error={error} />
    </>
  );
}

/**
 * Appoint this person to lead a ministry they are not yet a member of.
 * **Administrators only.**
 *
 * **This is the control that lets a brand-new ministry acquire its first
 * head**, which nothing else can do: rostering a ministry needs an active head
 * of that ministry, so until one exists nobody may add anybody — including an
 * administrator. Appointing a leader is the governance act that breaks that
 * circle, and it creates the membership as part of the appointment.
 *
 * The ministry list comes from the administrator's own church-wide view, which
 * is the only place in this app that knows every ministry.
 */
function AppointHeadElsewhere({
  person,
  onChanged,
}: {
  person: PersonDetail;
  onChanged: () => void;
}) {
  const [ministries, setMinistries] = useState<MinistryOverview[] | null>(null);
  const [ministryId, setMinistryId] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    const loader = new AbortController();
    getMinistries(loader.signal)
      .then((result) => setMinistries(result.ministries))
      .catch((cause: unknown) => {
        // A failure here costs the administrator one optional control, not the
        // page: everything else on it still works, so it is not an error
        // notice.
        if (!isAbortError(cause)) setMinistries([]);
      });
    return () => loader.abort();
  }, []);

  useEffect(() => () => controller.current?.abort(), []);

  const alreadyHeads = new Set(
    person.memberships
      .filter((membership) => membership.deactivated_at === null && membership.is_ministry_head)
      .map((membership) => membership.ministry_id),
  );
  const choices = (ministries ?? []).filter(
    (ministry) => ministry.deactivated_at === null && !alreadyHeads.has(ministry.ministry_id),
  );

  if (choices.length === 0) return null;

  const submit = () => {
    if (ministryId === "") return;
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);
    setMinistryHead(person.person_id, Number(ministryId), true, next.signal)
      .then(() => {
        setIsSaving(false);
        setMinistryId("");
        onChanged();
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  return (
    <div className="field">
      <label htmlFor="appoint-head-ministry">Appoint as head of a ministry</label>
      <select
        id="appoint-head-ministry"
        value={ministryId}
        onChange={(event) => setMinistryId(event.target.value)}
      >
        <option value="">Choose a ministry</option>
        {choices.map((ministry) => (
          <option key={ministry.ministry_id} value={ministry.ministry_id}>
            {ministry.name}
          </option>
        ))}
      </select>
      <p className="field__hint">
        They will be added to that ministry if they are not already in it. Use this to give a
        new ministry its first head.
      </p>
      <div className="card__actions">
        <button
          className="button"
          type="button"
          onClick={submit}
          disabled={isSaving || ministryId === ""}
        >
          {isSaving ? "Working…" : "Appoint as head"}
        </button>
      </div>
      <FormError error={error} />
    </div>
  );
}

/**
 * Take somebody off one team.
 *
 * **"Remove from Setup", never "Delete".** The confirmation says what is
 * actually kept, because a ministry head deciding whether to press this needs
 * to know that a year of serving history is not about to disappear.
 */
function RemoveFromMinistry({
  person,
  membership,
  onChanged,
}: {
  person: PersonDetail;
  membership: PersonMembership;
  onChanged: () => void;
}) {
  const [isConfirming, setIsConfirming] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  const label = removeFromMinistryLabel(membership.ministry_name);

  const run = () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setIsSaving(true);
    removePersonFromMinistry(membership.ministry_membership_id, next.signal)
      .then(() => {
        setIsSaving(false);
        setIsConfirming(false);
        onChanged();
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setIsSaving(false);
        setError(cause);
      });
  };

  if (!isConfirming) {
    return (
      <button className="button" type="button" onClick={() => setIsConfirming(true)}>
        {label}
      </button>
    );
  }

  return (
    <div className="notice notice--warning" role="alert">
      <p className="notice__title">
        {label.replace(/^Remove/, "Remove")} — {person.display_name}?
      </p>
      <p>{REMOVE_FROM_MINISTRY_EXPLANATION}</p>
      <FormError error={error} />
      <div className="card__actions">
        <button className="button button--primary" type="button" onClick={run} disabled={isSaving}>
          {isSaving ? "Working…" : `Yes, ${label.toLowerCase()}`}
        </button>
        <button
          className="button"
          type="button"
          onClick={() => setIsConfirming(false)}
          disabled={isSaving}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * Add this person to one of the ministries the viewer manages.
 *
 * The ministry is chosen explicitly, always — even for somebody who leads only
 * one. A head who leads three must say which, and a control that guessed for
 * the single-ministry case would be a different control for different people.
 */
function AddToMinistry({
  person,
  choices,
  onChanged,
}: {
  person: PersonDetail;
  choices: readonly { ministry_id: number; name: string }[];
  onChanged: () => void;
}) {
  const [ministryId, setMinistryId] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => () => controller.current?.abort(), []);

  return (
    <div className="subsection">
      <h3 className="subsection__title">Add to a ministry</h3>
      <form
        className="people-search"
        onSubmit={(event) => {
          event.preventDefault();
          controller.current?.abort();
          const next = new AbortController();
          controller.current = next;
          setIsSaving(true);
          addPersonToMinistry(person.person_id, Number(ministryId), next.signal)
            .then(() => {
              setIsSaving(false);
              setMinistryId("");
              onChanged();
            })
            .catch((cause: unknown) => {
              if (isAbortError(cause)) return;
              setIsSaving(false);
              setError(cause);
            });
        }}
      >
        <div className="field">
          <label htmlFor="add-to-ministry">Ministry</label>
          <select
            id="add-to-ministry"
            value={ministryId}
            onChange={(event) => setMinistryId(event.target.value)}
          >
            <option value="">Choose a ministry</option>
            {choices.map((ministry) => (
              <option key={ministry.ministry_id} value={ministry.ministry_id}>
                {ministry.name}
              </option>
            ))}
          </select>
        </div>
        <button
          className="button button--primary"
          type="submit"
          disabled={ministryId === "" || isSaving}
        >
          {isSaving ? "Adding…" : "Add"}
        </button>
      </form>
      <FormError error={error} />
    </div>
  );
}

/**
 * One rendering of a failed action, used by every control on this page.
 *
 * Shows the backend's own sentence where there is one -- "only an Admin may
 * perform this action", "cannot add a person who is deactivated church-wide"
 * -- because that wording came from the rule that actually refused, and
 * re-writing it here would drift from what the API enforces.
 */
function FormError({ error }: { error: unknown }) {
  if (error === null) return null;
  const apiError = asApiError(error);
  return apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />;
}
