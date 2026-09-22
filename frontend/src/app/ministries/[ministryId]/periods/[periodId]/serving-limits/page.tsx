"use client";

/**
 * Per-person serving limits for one scheduling period: for each ministry
 * member, the most assignments they may receive in this ministry during this
 * period (Task 57; Task 72 continuation replaces the repeated
 * value + Save maximum + Clear row with one compact table and a single
 * "Save changes" action over every edited row).
 *
 * The limit is a *hard maximum*, scoped to this ministry and this scheduling
 * period only -- never church-wide, and never carried into a future period.
 * That scope is stated in one place, `SERVING_LIMIT_EXPLANATION`, and shown
 * on the page so the number is never ambiguous. Absence of a limit is "No
 * limit" and is never a stand-in for zero.
 */

import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import { SaveChangesBar } from "@/components/SaveChangesBar";
import { clearServingLimit, getServingLimits, setServingLimit } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MembershipServingLimit, PeriodServingLimitsResponse } from "@/lib/api/types";
import { SERVING_LIMIT_EXPLANATION } from "@/lib/labels";
import { parsePositiveIntegerField, positiveIntegerFieldText } from "@/lib/positiveIntegerField";
import { servingLimitChange, type ServingLimitChange } from "@/lib/servingLimitChanges";

export default function ServingLimitsPage() {
  const params = useParams<{ ministryId: string; periodId: string }>();
  const ministryId = Number(params.ministryId);
  const periodId = Number(params.periodId);
  const hasValidIds =
    Number.isInteger(ministryId) && ministryId > 0 && Number.isInteger(periodId) && periodId > 0;

  const [includeInactive, setIncludeInactive] = useState(false);
  const [data, setData] = useState<PeriodServingLimitsResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidIds);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getServingLimits(periodId, includeInactive, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [periodId, includeInactive],
  );

  useEffect(() => {
    if (!hasValidIds) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, hasValidIds]);

  const trail = [
    { label: "Home", href: "/" },
    { label: "Scheduling periods", href: `/ministries/${ministryId}/periods` },
    { label: "Serving limits" },
  ];

  if (!hasValidIds) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Serving limits" }]} />
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
        <LoadingNotice what="serving limits" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Serving limits</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (data === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{data.scheduling_period_name}</h1>
      <p className="page__subtitle">Serving limits</p>
      <p className="small muted" style={{ maxWidth: "42rem" }}>
        {SERVING_LIMIT_EXPLANATION}
      </p>

      {!data.can_operate && <ReadOnlyNotice what="how often each person serves" />}

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
        // Keyed on `includeInactive` so toggling it -- a different roster
        // entirely -- starts the edit table over with a clean draft, rather
        // than trying to reconcile drafts against a membership list that just
        // changed underneath them.
        <ServingLimitsTable
          key={String(includeInactive)}
          periodId={periodId}
          memberships={data.memberships}
          canOperate={data.can_operate}
          onReload={load}
        />
      )}
    </>
  );
}

function initialDrafts(memberships: readonly MembershipServingLimit[]): Map<number, string> {
  const drafts = new Map<number, string>();
  for (const membership of memberships) {
    drafts.set(membership.ministry_membership_id, positiveIntegerFieldText(membership.max_assignments));
  }
  return drafts;
}

function ServingLimitsTable({
  periodId,
  memberships,
  canOperate,
  onReload,
}: {
  periodId: number;
  memberships: MembershipServingLimit[];
  /** False keeps the table -- the numbers are worth reading -- and makes it a
   *  table: the cells cannot be typed into, and the save bar that would
   *  follow an edit is not rendered at all. */
  canOperate: boolean;
  onReload: () => Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Map<number, string>>(() => initialDrafts(memberships));
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [failedIds, setFailedIds] = useState<ReadonlySet<number>>(new Set());

  const changes = useMemo(() => {
    const list: ServingLimitChange[] = [];
    for (const membership of memberships) {
      const change = servingLimitChange(
        membership.ministry_membership_id,
        drafts.get(membership.ministry_membership_id) ?? "",
        membership.max_assignments,
      );
      if (change !== null) list.push(change);
    }
    return list;
  }, [memberships, drafts]);

  const dirtyIds = useMemo(() => new Set(changes.map((c) => c.membershipId)), [changes]);

  const hasInvalidDraft = useMemo(
    () => [...drafts.values()].some((draft) => !parsePositiveIntegerField(draft).isValid),
    [drafts],
  );

  function setDraft(membershipId: number, value: string) {
    setDrafts((current) => {
      const next = new Map(current);
      next.set(membershipId, value);
      return next;
    });
  }

  function handleDiscard() {
    setDrafts(initialDrafts(memberships));
    setFailedIds(new Set());
    setSaveError(null);
  }

  async function handleSave() {
    setIsSaving(true);
    setSaveError(null);
    const results = await Promise.allSettled(
      changes.map((change) =>
        change.kind === "set"
          ? setServingLimit(periodId, change.membershipId, change.maxAssignments)
          : clearServingLimit(periodId, change.membershipId),
      ),
    );

    setDrafts((current) => {
      const next = new Map(current);
      results.forEach((result, index) => {
        if (result.status !== "fulfilled") return;
        const change = changes[index];
        const savedValue = change.kind === "set" ? change.maxAssignments : null;
        next.set(change.membershipId, positiveIntegerFieldText(savedValue));
      });
      return next;
    });

    const failed = new Set<number>();
    let firstError: unknown = null;
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        failed.add(changes[index].membershipId);
        if (firstError === null) firstError = result.reason;
      }
    });
    setFailedIds(failed);
    setSaveError(firstError);
    setIsSaving(false);

    if (failed.size < changes.length) {
      await onReload();
    }
  }

  const saveApiError = asApiError(saveError);

  return (
    <>
      {canOperate && (
      <SaveChangesBar
        dirtyCount={changes.length}
        isSaving={isSaving}
        canSave={!hasInvalidDraft}
        onSave={() => void handleSave()}
        onDiscard={handleDiscard}
        error={
          saveError !== null ? (
            <div style={{ marginTop: "0.5rem" }}>
              {saveApiError !== null ? (
                <ErrorNotice
                  error={saveApiError}
                  title={
                    failedIds.size === changes.length
                      ? "Nothing could be saved"
                      : `${failedIds.size} of ${changes.length} change(s) could not be saved`
                  }
                />
              ) : (
                <UnexpectedErrorNotice />
              )}
            </div>
          ) : undefined
        }
      />
      )}

      <div className="matrix-scroll">
        <table className="matrix">
          <caption className="small">Maximum assignments per person for this period</caption>
          <thead>
            <tr>
              <th scope="col">Member</th>
              <th scope="col">Maximum assignments</th>
            </tr>
          </thead>
          <tbody>
            {memberships.map((membership) => {
              const isPersonInactive = membership.person_deactivated_at !== null;
              const isMembershipInactive = membership.membership_deactivated_at !== null;
              const isInactive = isPersonInactive || isMembershipInactive;
              const draft = drafts.get(membership.ministry_membership_id) ?? "";
              const { isValid } = parsePositiveIntegerField(draft);
              const isDirty = dirtyIds.has(membership.ministry_membership_id);

              return (
                <tr key={membership.ministry_membership_id}>
                  <th scope="row" className="matrix__row-head">
                    {membership.person_display_name}
                    {isInactive && (
                      <span className="matrix__row-sub">
                        <span className="badge badge--warning">
                          {isPersonInactive ? "Inactive person" : "Inactive membership"}
                        </span>
                      </span>
                    )}
                  </th>
                  <td>
                    <input
                      className={[
                        "matrix__cell-input",
                        "matrix__cell-input--wide",
                        isDirty ? "matrix__cell-input--dirty" : "",
                        !isValid ? "matrix__cell-input--invalid" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      type="number"
                      min={1}
                      inputMode="numeric"
                      placeholder="No limit"
                      value={draft}
                      disabled={isSaving}
                      readOnly={!canOperate}
                      onChange={(event) => setDraft(membership.ministry_membership_id, event.target.value)}
                      aria-label={`Maximum assignments for ${membership.person_display_name}`}
                    />
                    {failedIds.has(membership.ministry_membership_id) && (
                      <span className="matrix__cell-error" title="Could not be saved">
                        !
                      </span>
                    )}
                    {!isValid && (
                      <p className="small muted" style={{ marginTop: "0.3rem" }}>
                        Whole number of 1 or more, or blank for no limit.
                      </p>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
