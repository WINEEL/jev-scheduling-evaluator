"use client";

/**
 * Who is qualified for one role: every membership of the role's ministry,
 * each with its qualification decision for this role (Task 55).
 *
 * "Member of this ministry" and "qualified for this role" are two different
 * facts -- every row here is a member; the qualification column is a
 * separate, per-role decision a head makes about some of them. There is no
 * "clear" action: once a decision exists it is only ever changed between
 * qualified and not qualified, never returned to "never assessed"
 * (:mod:`app.services.role_qualification`'s own model).
 */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import { getRoleQualifications, setRoleQualification } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MembershipQualification, RoleQualificationsResponse } from "@/lib/api/types";
import { qualificationLabel } from "@/lib/labels";

export default function RoleQualificationsPage() {
  const params = useParams<{ ministryId: string; roleId: string }>();
  const ministryId = Number(params.ministryId);
  const roleId = Number(params.roleId);
  const hasValidIds =
    Number.isInteger(ministryId) && ministryId > 0 && Number.isInteger(roleId) && roleId > 0;

  const [includeInactive, setIncludeInactive] = useState(false);
  const [data, setData] = useState<RoleQualificationsResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidIds);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getRoleQualifications(roleId, includeInactive, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          // An abort is not an outcome: this component is unmounting, or a
          // newer request (the include-inactive toggle, most often) has
          // replaced this one and is still in flight. The previous list stays
          // on screen until the new one arrives.
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [roleId, includeInactive],
  );

  useEffect(() => {
    if (!hasValidIds) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidIds]);

  const trail = [
    { label: "Home", href: "/" },
    { label: "Roles", href: `/ministries/${ministryId}/roles` },
    { label: data?.role_name ?? "Qualifications" },
  ];

  if (!hasValidIds) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Qualifications" }]} />
        <div className="notice notice--danger" role="alert">
          <p className="notice__title">Not found</p>
          <p>That address is not valid.</p>
        </div>
      </>
    );
  }

  if (isLoading) {
    return (
      <>
        <Breadcrumbs trail={trail} />
        <LoadingNotice what="qualifications" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Qualifications</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (data === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{data.role_name}</h1>
      <p className="page__subtitle">
        Who may be assigned to this role. Every row below is a member of this ministry;
        the status column is this role&rsquo;s own decision about them.
      </p>

      {!data.can_operate && <ReadOnlyNotice what="who is approved for this role" />}

      <div className="field field--inline">
        <input
          id="include-inactive"
          type="checkbox"
          checked={includeInactive}
          onChange={(event) => setIncludeInactive(event.target.checked)}
        />
        <label htmlFor="include-inactive">Show inactive members</label>
      </div>

      {data.memberships.length === 0 ? (
        <div className="notice notice--info">
          <p className="notice__title">No members yet</p>
          <p>
            {includeInactive
              ? "This ministry has no members at all yet."
              : "This ministry has no active members. Show inactive members above."}
          </p>
        </div>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Member</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.memberships.map((membership) => (
                <MembershipRow
                  key={membership.ministry_membership_id}
                  roleId={roleId}
                  membership={membership}
                  canOperate={data.can_operate}
                  onChanged={(updated) =>
                    setData((current) =>
                      current === null
                        ? current
                        : {
                            ...current,
                            memberships: current.memberships.map((m) =>
                              m.ministry_membership_id === membership.ministry_membership_id
                                ? updated
                                : m,
                            ),
                          },
                    )
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function MembershipRow({
  roleId,
  membership,
  canOperate,
  onChanged,
}: {
  roleId: number;
  membership: MembershipQualification;
  /** Whether this reader may record a decision. The server's answer, and the
   *  same one it will apply to the request. */
  canOperate: boolean;
  onChanged: (updated: MembershipQualification) => void;
}) {
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const isMembershipInactive = membership.membership_deactivated_at !== null;
  const isPersonInactive = membership.person_deactivated_at !== null;
  const isInactive = isMembershipInactive || isPersonInactive;

  async function handleSet(isQualified: boolean) {
    setIsSaving(true);
    setError(null);
    try {
      const updated = await setRoleQualification(roleId, membership.ministry_membership_id, isQualified);
      onChanged(updated);
    } catch (cause: unknown) {
      setError(cause);
    } finally {
      setIsSaving(false);
    }
  }

  const apiError = asApiError(error);

  return (
    <tr>
      <td>
        {membership.person_display_name}
        {isInactive && (
          <>
            {" "}
            <span className="badge badge--warning">
              {isPersonInactive ? "Inactive person" : "Inactive membership"}
            </span>
          </>
        )}
      </td>
      {/* The standing decision as a badge, and both actions as ordinary
          buttons (Task 75). A green *primary* "Mark qualified" on every row
          read as a status rather than as an action -- strongest colour on the
          screen, sitting beside people who are not qualified at all. The
          status column is where the answer belongs, and it carries the word
          as well as the colour. */}
      <td>
        <span className={membership.is_qualified === true ? "badge badge--ok" : "badge"}>
          {qualificationLabel(membership.is_qualified)}
        </span>
      </td>
      <td>
        {/* Hidden rather than disabled for a reader who does not run this
            ministry: a greyed-out button says "not now", and the answer here
            is "not yours" -- which the notice at the top of the page has
            already explained. */}
        {canOperate && (
          <>
            <button
              className="button"
              type="button"
              onClick={() => void handleSet(true)}
              disabled={isSaving || membership.is_qualified === true}
            >
              Mark qualified
            </button>{" "}
            <button
              className="button"
              type="button"
              onClick={() => void handleSet(false)}
              disabled={isSaving || membership.is_qualified === false}
            >
              Mark not qualified
            </button>
          </>
        )}
        {error !== null && (
          <div style={{ marginTop: "0.5rem" }}>
            {apiError !== null ? (
              <ErrorNotice error={apiError} title="That could not be saved" />
            ) : (
              <UnexpectedErrorNotice />
            )}
          </div>
        )}
      </td>
    </tr>
  );
}
