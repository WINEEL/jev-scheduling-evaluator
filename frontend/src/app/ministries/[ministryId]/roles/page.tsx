"use client";

/**
 * A ministry's roles: view them, create one, rename or re-describe one,
 * deactivate one, and reactivate a previously deactivated one (Task 53).
 *
 * Deactivated roles are hidden by default -- a head managing an active
 * ministry mostly wants to see what is currently assignable -- and revealed
 * with one checkbox rather than a second screen. Nothing here reorders
 * roles: `display_order` is shown for context, never edited (backend
 * docstring), and there is no drag-and-drop in this release.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import {
  createMinistryRole,
  deactivateMinistryRole,
  getMinistryRoles,
  reactivateMinistryRole,
  updateMinistryRole,
} from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MinistryRole, MinistryRolesResponse } from "@/lib/api/types";
import { isRoleActive, roleStatusLabel } from "@/lib/labels";

export default function MinistryRolesPage() {
  const params = useParams<{ ministryId: string }>();
  const ministryId = Number(params.ministryId);
  const hasValidId = Number.isInteger(ministryId) && ministryId > 0;

  const [includeInactive, setIncludeInactive] = useState(false);
  const [data, setData] = useState<MinistryRolesResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidId);
  const [isCreating, setIsCreating] = useState(false);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getMinistryRoles(ministryId, includeInactive, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          // An abort is not an outcome: this component is unmounting, or a
          // newer request (the include-inactive toggle, most often) has
          // replaced this one and is still in flight. The previous list stays
          // on screen until the new one arrives -- there is no flash back to
          // a loading state for a toggle that already has something to show.
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [ministryId, includeInactive],
  );

  useEffect(() => {
    if (!hasValidId) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidId]);

  if (!hasValidId) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Roles" }]} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That ministry address is not valid.</p>
        </div>
      </>
    );
  }

  const trail = [
    { label: "Home", href: "/" },
    { label: data?.ministry_name ?? "Roles" },
  ];

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <LoadingNotice what="roles" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Roles</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (data === null) return <UnexpectedErrorNotice />;

  const visibleRoles = includeInactive
    ? data.roles
    : data.roles.filter((role) => isRoleActive(role.deactivated_at));

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{data.ministry_name}</h1>
      <p className="page__subtitle">
        Roles, and who is approved for each — the positions this ministry fills at an event.
      </p>

      {!data.can_operate && <ReadOnlyNotice what="this ministry's roles" />}

      <section className="section" aria-labelledby="new-role-heading">
        {/* Not "Roles" a second time, two lines under the subtitle that
            already says it: this section is the roster of roles and the one
            action that adds to it. */}
        <h2 className="section__title" id="new-role-heading">
          {isCreating ? "New role" : "This ministry's roles"}
        </h2>

        <div className="field field--inline">
          <input
            id="include-inactive"
            type="checkbox"
            checked={includeInactive}
            onChange={(event) => setIncludeInactive(event.target.checked)}
          />
          <label htmlFor="include-inactive">Show inactive roles</label>
        </div>

        {!data.can_operate ? null : isCreating ? (
          <CreateRoleForm
            ministryId={ministryId}
            onCreated={() => {
              setIsCreating(false);
              void load();
            }}
            onCancel={() => setIsCreating(false)}
          />
        ) : (
          <div className="card__actions">
            <button className="button button--primary" type="button" onClick={() => setIsCreating(true)}>
              New role
            </button>
          </div>
        )}
      </section>

      <section className="section" aria-labelledby="roles-list-heading">
        <h2 className="section__title" id="roles-list-heading" style={{ display: "none" }}>
          Existing roles
        </h2>

        {visibleRoles.length === 0 ? (
          <div className="notice notice--info">
            <p className="notice__title">No roles yet</p>
            <p>
              {includeInactive
                ? "This ministry has no roles at all yet."
                : "This ministry has no active roles. Show inactive roles above, or create one."}
            </p>
          </div>
        ) : (
          <ul className="list-reset">
            {visibleRoles.map((role) => (
              <RoleCard
                key={role.ministry_role_id}
                role={role}
                ministryId={ministryId}
                canOperate={data.can_operate}
                onChanged={() => void load()}
              />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

function CreateRoleForm({
  ministryId,
  onCreated,
  onCancel,
}: {
  ministryId: number;
  onCreated: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);
    try {
      await createMinistryRole(ministryId, { name, description });
      onCreated();
    } catch (cause: unknown) {
      setError(cause);
      setIsSubmitting(false);
    }
  }

  const apiError = asApiError(error);

  return (
    <form className="card" onSubmit={(event) => void handleSubmit(event)}>
      <div className="field">
        <label htmlFor="new-role-name">Name</label>
        <input
          id="new-role-name"
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          autoFocus
        />
      </div>
      <div className="field">
        <label htmlFor="new-role-description">Description</label>
        <input
          id="new-role-description"
          type="text"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
        <p className="field__hint">Optional.</p>
      </div>

      {error !== null &&
        (apiError !== null ? (
          <ErrorNotice error={apiError} title="The role could not be created" />
        ) : (
          <UnexpectedErrorNotice />
        ))}

      <div className="card__actions">
        <button className="button button--primary" type="submit" disabled={isSubmitting || name.trim() === ""}>
          {isSubmitting ? "Creating…" : "Create role"}
        </button>
        <button className="button" type="button" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RoleCard({
  role,
  ministryId,
  canOperate,
  onChanged,
}: {
  role: MinistryRole;
  ministryId: number;
  /** Whether this reader may change this ministry's roles. The server's
   *  answer, and the same one it will apply to the request. */
  canOperate: boolean;
  onChanged: () => void;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const active = isRoleActive(role.deactivated_at);

  async function handleDeactivate() {
    if (!window.confirm(`Deactivate the role "${role.name}"? It will no longer be usable for new configuration, but every past record naming it stays as it was.`)) {
      return;
    }
    setIsProcessing(true);
    setError(null);
    try {
      await deactivateMinistryRole(role.ministry_role_id);
      onChanged();
    } catch (cause: unknown) {
      setError(cause);
      setIsProcessing(false);
    }
  }

  async function handleReactivate() {
    setIsProcessing(true);
    setError(null);
    try {
      await reactivateMinistryRole(role.ministry_role_id);
      onChanged();
    } catch (cause: unknown) {
      setError(cause);
      setIsProcessing(false);
    }
  }

  const apiError = asApiError(error);

  if (isEditing) {
    return (
      <li className="card">
        <EditRoleForm
          role={role}
          onSaved={() => {
            setIsEditing(false);
            onChanged();
          }}
          onCancel={() => setIsEditing(false)}
        />
      </li>
    );
  }

  return (
    <li className="card">
      <div className="card__header">
        <div>
          <h2 className="card__title">{role.name}</h2>
          {role.description !== null && role.description !== "" && (
            <p className="card__meta">{role.description}</p>
          )}
          <p className="card__meta small muted">Position {role.display_order + 1}</p>
        </div>
        <div>
          <span className={`badge ${active ? "badge--ok" : "badge--warning"}`}>
            {roleStatusLabel(role.deactivated_at)}
          </span>
        </div>
      </div>

      <div className="card__actions">
        {canOperate && (
          <>
            <button className="button" type="button" onClick={() => setIsEditing(true)} disabled={isProcessing}>
              Edit
            </button>
            {active ? (
              <button className="button" type="button" onClick={() => void handleDeactivate()} disabled={isProcessing}>
                {isProcessing ? "Deactivating…" : "Deactivate"}
              </button>
            ) : (
              <button
                className="button button--primary"
                type="button"
                onClick={() => void handleReactivate()}
                disabled={isProcessing}
              >
                {isProcessing ? "Reactivating…" : "Reactivate"}
              </button>
            )}
          </>
        )}
        {/* The qualifications screen is worth opening either way -- it is a
            read for somebody who does not run the ministry, and gates its own
            controls the same way this card does. */}
        <Link
          className="button link-button"
          href={`/ministries/${ministryId}/roles/${role.ministry_role_id}/qualifications`}
        >
          {canOperate ? "Manage qualifications" : "View qualifications"}
        </Link>
      </div>

      {error !== null &&
        (apiError !== null ? (
          <div style={{ marginTop: "0.85rem" }}>
            <ErrorNotice error={apiError} title="That could not be done" />
          </div>
        ) : (
          <div style={{ marginTop: "0.85rem" }}>
            <UnexpectedErrorNotice />
          </div>
        ))}
    </li>
  );
}

function EditRoleForm({
  role,
  onSaved,
  onCancel,
}: {
  role: MinistryRole;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(role.name);
  const [description, setDescription] = useState(role.description ?? "");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setIsSubmitting(true);
    setError(null);
    try {
      await updateMinistryRole(role.ministry_role_id, { name, description });
      onSaved();
    } catch (cause: unknown) {
      setError(cause);
      setIsSubmitting(false);
    }
  }

  const apiError = asApiError(error);

  return (
    <form onSubmit={(event) => void handleSubmit(event)}>
      <div className="field">
        <label htmlFor={`edit-role-name-${role.ministry_role_id}`}>Name</label>
        <input
          id={`edit-role-name-${role.ministry_role_id}`}
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          required
          autoFocus
        />
      </div>
      <div className="field">
        <label htmlFor={`edit-role-description-${role.ministry_role_id}`}>Description</label>
        <input
          id={`edit-role-description-${role.ministry_role_id}`}
          type="text"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>

      {error !== null &&
        (apiError !== null ? (
          <ErrorNotice error={apiError} title="The role could not be saved" />
        ) : (
          <UnexpectedErrorNotice />
        ))}

      <div className="card__actions">
        <button className="button button--primary" type="submit" disabled={isSubmitting || name.trim() === ""}>
          {isSubmitting ? "Saving…" : "Save"}
        </button>
        <button className="button" type="button" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </button>
      </div>
    </form>
  );
}
