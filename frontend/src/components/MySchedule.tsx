"use client";

/**
 * "My upcoming schedule" -- where the signed-in person is expected.
 *
 * **The one screen in this app written for somebody who manages nothing.**
 * Every other view is a ministry head configuring or reviewing a ministry;
 * this is a volunteer asking "when am I serving?". That shapes three
 * decisions:
 *
 * - **Grouped by date, not by ministry.** People think in Sundays, not in
 *   departments. Serving in two ministries on one morning is one entry in your
 *   week with two things in it, not two rows in two sections.
 * - **Names, not ids.** The reader may belong to no team and have nowhere to
 *   look a ministry or role up, so the API returns both by name.
 * - **A proposal is never shown as a commitment.** A ministry head previewing
 *   their own draft sees it marked "Not confirmed yet", with an explanation.
 *   A volunteer never sees a draft at all -- the API withholds it, which is the
 *   part that actually enforces this; the badge here is the honest label on
 *   what does arrive.
 *
 * Empty is a normal, common state -- most volunteers have nothing scheduled
 * most of the time -- so it reads as an answer rather than as a failure.
 */

import { useEffect, useState } from "react";

import { ErrorNotice, LoadingNotice, UnexpectedErrorNotice, asApiError } from "@/components/Feedback";
import { getMySchedule } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MySchedule as MyScheduleData } from "@/lib/api/types";
import { formatEventDate } from "@/lib/labels";
import { groupByDay, hasUnconfirmed } from "@/lib/myScheduleGroups";

export function MySchedule() {
  const [schedule, setSchedule] = useState<MyScheduleData | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    getMySchedule(controller.signal)
      .then((result) => {
        setSchedule(result);
        setError(null);
        setIsLoading(false);
      })
      .catch((cause: unknown) => {
        // An abort is this component unmounting, not an outcome -- see the
        // note in `lib/api/errors.ts`.
        if (isAbortError(cause)) return;
        setError(cause);
        setIsLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <section className="my-schedule">
      <h2 className="page__section-title">My upcoming schedule</h2>
      <Body schedule={schedule} error={error} isLoading={isLoading} />
    </section>
  );
}

function Body({
  schedule,
  error,
  isLoading,
}: {
  schedule: MyScheduleData | null;
  error: unknown;
  isLoading: boolean;
}) {
  if (isLoading) return <LoadingNotice what="your schedule" />;

  if (error !== null) {
    const apiError = asApiError(error);
    return apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />;
  }

  if (schedule === null) return <UnexpectedErrorNotice />;

  if (schedule.assignments.length === 0) {
    // Not an error and not an empty-state apology: having nothing scheduled is
    // the ordinary condition of most volunteers most of the time.
    return (
      <div className="notice notice--info">
        <p className="notice__title">Nothing scheduled</p>
        <p>You have no upcoming commitments. This page will fill in once you do.</p>
      </div>
    );
  }

  const days = groupByDay(schedule.assignments);

  return (
    <>
      <ul className="list-reset my-schedule__days">
        {days.map((day) => (
          <li className="card my-schedule__day" key={day.date}>
            <h3 className="my-schedule__date">{formatEventDate(day.date)}</h3>
            <ul className="list-reset">
              {day.assignments.map((assignment) => (
                <li className="my-schedule__item" key={assignment.assignment_id}>
                  <span className="my-schedule__role">{assignment.ministry_role_name}</span>
                  <span className="my-schedule__ministry">{assignment.ministry_name}</span>
                  {assignment.event_name !== null && (
                    <span className="my-schedule__event">{assignment.event_name}</span>
                  )}
                  {!assignment.is_confirmed && (
                    <span className="badge">Not confirmed yet</span>
                  )}
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>

      {hasUnconfirmed(schedule.assignments) && (
        // Shown only when it applies. Somebody whose schedule is entirely
        // finalized should not be told what a draft is.
        <p className="pilot-note">
          Entries marked <strong>Not confirmed yet</strong> come from a schedule the
          ministry is still working on. They may change before it is finalized.
        </p>
      )}
    </>
  );
}
