"use client";

/**
 * The scheduling rules one period sets for itself (Task 71; Task 72
 * continuation gives it the user-facing name "Events to skip after serving"
 * and a compact Rule / Setting / Meaning table in place of a large paragraph
 * and a plain Save/Clear pair).
 *
 * Today that is one rule: how many of this ministry's own events must pass
 * before the same person is scheduled again. It is a *hard* rule, scoped to
 * this ministry and this period only -- never church-wide, and never carried
 * into a future period. That scope is stated in one place,
 * `EVENT_GAP_EXPLANATION`, and shown on the page so the number is never
 * ambiguous.
 *
 * **Absence of a rule is "No rule" and is never a stand-in for zero.** An
 * empty box means the same person may serve events that follow one another,
 * which is what the product has always done; clearing is a real `DELETE`, not
 * a `0` written into the box.
 *
 * The underlying stored field is still `min_intervening_events` -- this page
 * changes only its name, its layout and its wording, never the API it calls
 * or the value it sends. The page is generic product copy: it names no
 * ministry, no weekday, and no unit of time. The rule counts *events*, and
 * the wording must not suggest otherwise.
 */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { ReadOnlyNotice } from "@/components/ReadOnlyNotice";
import {
  clearMinInterveningEvents,
  getSchedulingRules,
  setMinInterveningEvents,
} from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { PeriodSchedulingRules } from "@/lib/api/types";
import {
  EVENT_GAP_EXPLANATION,
  EVENT_GAP_HELP,
  EVENT_GAP_QUESTION,
  EVENT_GAP_RULE_NAME,
  LINKED_MEMBER_RULE_NAME,
  LINKED_MEMBER_RULE_STATUS,
  MEMBER_GROUP_LIMITS_EXPLANATION,
  MEMBER_GROUP_LIMITS_HEADING,
  MEMBER_GROUP_LIMITS_RULE_NAME,
  RULES_CONFIGURED_ELSEWHERE_NOTE,
  RULES_OVERVIEW_HEADING,
  RULES_OVERVIEW_NOTE,
  RULE_SOURCE_EXTERNAL,
  RULE_SOURCE_SERVING_LIMITS,
  RULE_SOURCE_THIS_PAGE,
  SAME_EVENT_SUPPORT_EXPLANATION,
  SAME_EVENT_SUPPORT_HEADING,
  SAME_EVENT_SUPPORT_RULE_NAME,
  SERVING_LIMITS_RULE_NAME,
  SERVING_LIMITS_RULE_STATUS,
  eventGapDescription,
  eventGapLabel,
  memberGroupLimitLabel,
  memberGroupLimitsStatus,
  supportRequirementLabel,
  supportRequirementsStatus,
} from "@/lib/labels";
import { parsePositiveIntegerField, positiveIntegerFieldText } from "@/lib/positiveIntegerField";

export default function SchedulingRulesPage() {
  const params = useParams<{ ministryId: string; periodId: string }>();
  const ministryId = Number(params.ministryId);
  const periodId = Number(params.periodId);
  const hasValidIds =
    Number.isInteger(ministryId) && ministryId > 0 && Number.isInteger(periodId) && periodId > 0;

  const [data, setData] = useState<PeriodSchedulingRules | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(hasValidIds);

  const load = useCallback(
    (signal?: AbortSignal) =>
      getSchedulingRules(periodId, signal)
        .then((result) => {
          setData(result);
          setError(null);
          setIsLoading(false);
        })
        .catch((cause: unknown) => {
          // An abort is not an outcome: the component is unmounting, or a
          // newer request has replaced this one and is still in flight.
          if (isAbortError(cause)) return;
          setError(cause);
          setIsLoading(false);
        }),
    [periodId],
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
    { label: "Scheduling rules" },
  ];

  if (!hasValidIds) {
    return (
      <>
        <Breadcrumbs trail={[{ label: "Home", href: "/" }, { label: "Scheduling rules" }]} />
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
        <LoadingNotice what="scheduling rules" />
      </>
    );
  }

  if (error !== null) {
    const apiError = asApiError(error);
    return (
      <>
        <Breadcrumbs trail={trail} />
        <h1 className="page__title">Scheduling rules</h1>
        {apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />}
      </>
    );
  }

  if (data === null) return <UnexpectedErrorNotice />;

  return (
    <>
      <Breadcrumbs trail={trail} />
      <h1 className="page__title">{data.scheduling_period_name}</h1>
      <p className="page__subtitle">
        Scheduling rules — the hard rules every generated schedule for this period must obey.
      </p>

      {!data.can_operate && <ReadOnlyNotice what="this period's scheduling rules" />}

      <RulesOverview rules={data} ministryId={ministryId} periodId={periodId} />
      <EventGapRule
        rules={data}
        periodId={periodId}
        canOperate={data.can_operate}
        onChanged={setData}
      />
      <MemberGroupLimits rules={data} />
      <SameEventSupportRequirements rules={data} />
    </>
  );
}

/**
 * Which rules this period actually applies, and where each one is set (Task
 * 75).
 *
 * The page already carried every rule it can edit or list; what it did not do
 * was answer the first question in one glance. This table does, for all five
 * rule families the product has -- including the two it cannot show here, each
 * marked as such rather than quietly absent, since a missing row reads as "no
 * such rule" and that would be wrong.
 *
 * Generic wording throughout. A rule is named by what it constrains, never by
 * why a ministry agreed it: the system stores no reason and must not imply it
 * knows one. A member group's own name appears only where the ministry itself
 * configured that name.
 */
function RulesOverview({
  rules,
  ministryId,
  periodId,
}: {
  rules: PeriodSchedulingRules;
  ministryId: number;
  periodId: number;
}) {
  return (
    <section className="section" aria-labelledby="rules-overview-heading">
      <h2 className="section__title" id="rules-overview-heading">
        {RULES_OVERVIEW_HEADING}
      </h2>
      <div className="table-scroll">
        <table className="rule-table">
          <thead>
            <tr>
              <th scope="col">Rule</th>
              <th scope="col">This period</th>
              <th scope="col">Where it is set</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">{EVENT_GAP_RULE_NAME}</th>
              <td className="rule-status">{eventGapLabel(rules.min_intervening_events)}</td>
              <td>{RULE_SOURCE_THIS_PAGE}</td>
            </tr>
            <tr>
              <th scope="row">{SERVING_LIMITS_RULE_NAME}</th>
              <td className="rule-status">{SERVING_LIMITS_RULE_STATUS}</td>
              <td>
                <Link href={`/ministries/${ministryId}/periods/${periodId}/serving-limits`}>
                  {RULE_SOURCE_SERVING_LIMITS}
                </Link>
              </td>
            </tr>
            <tr>
              <th scope="row">{LINKED_MEMBER_RULE_NAME}</th>
              <td className="rule-status">{LINKED_MEMBER_RULE_STATUS}</td>
              <td>{RULE_SOURCE_EXTERNAL}</td>
            </tr>
            <tr>
              <th scope="row">{MEMBER_GROUP_LIMITS_RULE_NAME}</th>
              <td className="rule-status">{memberGroupLimitsStatus(rules.member_group_limits)}</td>
              <td>{RULE_SOURCE_EXTERNAL}</td>
            </tr>
            <tr>
              <th scope="row">{SAME_EVENT_SUPPORT_RULE_NAME}</th>
              <td className="rule-status">
                {supportRequirementsStatus(rules.same_event_support_requirements)}
              </td>
              <td>{RULE_SOURCE_EXTERNAL}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="small muted" style={{ marginTop: "0.75rem", maxWidth: "46rem" }}>
        {RULES_OVERVIEW_NOTE}
      </p>
    </section>
  );
}

/**
 * The member group limits in force for this period, read-only (Task 74).
 *
 * Read-only on purpose, and the page says so: the rule has full domain and API
 * support, but the editor a head would need -- picking a group, choosing who is
 * in it, then capping it -- is a screen of its own rather than a third box on
 * this one. What a head needs here is to *see* what is active before they
 * generate, and that is what this is.
 *
 * A group with no cap is shown with "No limit" rather than hidden: the point of
 * the table is to make the ministry's whole configuration visible, and a group
 * somebody meant to cap and did not is exactly the thing worth noticing.
 */
function MemberGroupLimits({ rules }: { rules: PeriodSchedulingRules }) {
  const groups = rules.member_group_limits ?? [];
  if (groups.length === 0) return null;

  return (
    <section className="section">
      <div className="section__heading">
        <h2 className="section__title">{MEMBER_GROUP_LIMITS_HEADING}</h2>
        <span className="badge">Read-only</span>
      </div>
      <div className="table-scroll" style={{ marginTop: "0.6rem" }}>
        <table className="rule-table">
          <thead>
            <tr>
              <th scope="col">Member group</th>
              <th scope="col">People</th>
              <th scope="col">Limit</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <tr key={group.member_group_id}>
                <th scope="row">{group.name}</th>
                <td>{group.member_count}</td>
                <td>{memberGroupLimitLabel(group.max_per_event)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small muted" style={{ marginTop: "0.75rem", maxWidth: "42rem" }}>
        {MEMBER_GROUP_LIMITS_EXPLANATION}
      </p>
      <p className="small muted" style={{ marginTop: "0.35rem", maxWidth: "42rem" }}>
        {RULES_CONFIGURED_ELSEWHERE_NOTE}
      </p>
    </section>
  );
}

/**
 * The same-event support requirements in force for this period, read-only.
 *
 * Names the member and the members approved to serve with them, because that is
 * what a head needs to see before generating. It says nothing about **why** the
 * rule exists -- the system does not store a reason, does not know one, and must
 * not imply that it does.
 */
function SameEventSupportRequirements({ rules }: { rules: PeriodSchedulingRules }) {
  const requirements = rules.same_event_support_requirements ?? [];
  if (requirements.length === 0) return null;

  return (
    <section className="section">
      <div className="section__heading">
        <h2 className="section__title">{SAME_EVENT_SUPPORT_HEADING}</h2>
        <span className="badge">Read-only</span>
      </div>
      <div className="table-scroll" style={{ marginTop: "0.6rem" }}>
        <table className="rule-table">
          <thead>
            <tr>
              <th scope="col">Member</th>
              <th scope="col">Rule</th>
              <th scope="col">Approved supporting members</th>
            </tr>
          </thead>
          <tbody>
            {requirements.map((requirement) => (
              <tr key={requirement.subject_membership_id}>
                <th scope="row">{requirement.subject_display_name}</th>
                <td>{supportRequirementLabel(requirement.min_supporters)}</td>
                <td>{requirement.supporter_display_names.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small muted" style={{ marginTop: "0.75rem", maxWidth: "42rem" }}>
        {SAME_EVENT_SUPPORT_EXPLANATION}
      </p>
      <p className="small muted" style={{ marginTop: "0.35rem", maxWidth: "42rem" }}>
        {RULES_CONFIGURED_ELSEWHERE_NOTE}
      </p>
    </section>
  );
}

function EventGapRule({
  rules,
  periodId,
  canOperate,
  onChanged,
}: {
  rules: PeriodSchedulingRules;
  periodId: number;
  /** False leaves the rule and its meaning on screen -- somebody overseeing
   *  the ministry needs to know what the generator will obey -- and removes
   *  the two buttons that would change it. */
  canOperate: boolean;
  onChanged: (updated: PeriodSchedulingRules) => void;
}) {
  const [draft, setDraft] = useState(positiveIntegerFieldText(rules.min_intervening_events));
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const { isValid, value } = parsePositiveIntegerField(draft);
  // Dirty covers every way the box now disagrees with what is saved,
  // including "typed nothing" -- that state is real, and is what "Clear rule"
  // below acts on. Only a *settable* value -- valid, present, and different
  // from what is saved -- may actually be sent through Save.
  const isDirty = value !== rules.min_intervening_events;
  const canSaveValue = isValid && value !== null && value !== rules.min_intervening_events;

  async function handleSave() {
    if (!canSaveValue || value === null) return;
    setIsSaving(true);
    setError(null);
    try {
      onChanged(await setMinInterveningEvents(periodId, value));
    } catch (cause: unknown) {
      setError(cause);
    } finally {
      setIsSaving(false);
    }
  }

  async function handleClear() {
    setIsSaving(true);
    setError(null);
    try {
      await clearMinInterveningEvents(periodId);
      onChanged({ ...rules, min_intervening_events: null });
      setDraft("");
    } catch (cause: unknown) {
      setError(cause);
    } finally {
      setIsSaving(false);
    }
  }

  const apiError = asApiError(error);

  return (
    <section className="section" aria-labelledby="event-gap-heading">
      <h2 className="section__title" id="event-gap-heading">
        {EVENT_GAP_RULE_NAME}
      </h2>
      <p className="small muted" style={{ maxWidth: "42rem" }}>
        {EVENT_GAP_QUESTION}
      </p>

      <div className="table-scroll" style={{ marginTop: "0.6rem" }}>
        <table className="rule-table">
          <thead>
            <tr>
              <th scope="col">Rule</th>
              <th scope="col">Setting</th>
              <th scope="col">Meaning</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">{EVENT_GAP_RULE_NAME}</th>
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
                  step={1}
                  inputMode="numeric"
                  aria-label={EVENT_GAP_RULE_NAME}
                  placeholder="No rule"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  disabled={isSaving}
                  readOnly={!canOperate}
                />
              </td>
              <td>{eventGapDescription(rules.min_intervening_events)}</td>
            </tr>
          </tbody>
        </table>
      </div>

      {canOperate && (
        <div className="card__actions" style={{ marginTop: "0.75rem" }}>
          <button
            className="button button--primary"
            type="button"
            onClick={() => void handleSave()}
            disabled={isSaving || !canSaveValue}
          >
            {isSaving ? "Saving…" : "Save changes"}
          </button>
          <button
            className="button"
            type="button"
            onClick={() => void handleClear()}
            disabled={isSaving || rules.min_intervening_events === null}
          >
            Clear rule
          </button>
        </div>
      )}

      {!isValid && (
        <p className="small muted" style={{ marginTop: "0.5rem" }}>
          Enter a whole number of 1 or more, or clear the box and use Clear rule to remove it.
        </p>
      )}

      <p className="small muted" style={{ marginTop: "0.75rem", maxWidth: "42rem" }}>
        {EVENT_GAP_HELP}
      </p>
      <p className="small muted" style={{ marginTop: "0.35rem", maxWidth: "42rem" }}>
        {EVENT_GAP_EXPLANATION}
      </p>

      {error !== null && (
        <div style={{ marginTop: "0.6rem" }}>
          {apiError !== null ? (
            <ErrorNotice error={apiError} title="That could not be saved" />
          ) : (
            <UnexpectedErrorNotice />
          )}
        </div>
      )}
    </section>
  );
}
