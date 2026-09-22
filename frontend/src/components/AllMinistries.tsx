"use client";

/**
 * Every ministry in the church, for an administrator (Task 79 §4).
 *
 * **The gap this closes was one the app used to report to its own users.** The
 * administrator home screen said, in as many words, that no view listed every
 * ministry an administrator could manage. This is that view.
 *
 * **Oversight is a read, and this component renders only reads.** Each row
 * links to the ministry's existing screens, which an administrator may open.
 * What it deliberately does not render is any operational control — no "add
 * somebody", no "generate", no "archive". Running a ministry belongs to
 * whoever leads it, and an administrator who happens to head one reaches those
 * controls through "Ministries you lead", exactly as any other head does.
 *
 * **Nothing is invented to fill a column.** A ministry with no head, no
 * members or no scheduling period says so. The backend sends only facts it
 * stores, and a plausible-looking blank would be worse than an honest dash.
 */

import { useEffect, useState } from "react";
import Link from "next/link";

import {
  EmptyNotice,
  ErrorNotice,
  LoadingNotice,
  UnexpectedErrorNotice,
  asApiError,
} from "@/components/Feedback";
import { getMinistries } from "@/lib/api/client";
import { isAbortError } from "@/lib/api/errors";
import type { MinistryList, MinistryOverview } from "@/lib/api/types";

export function AllMinistries() {
  const [data, setData] = useState<MinistryList | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    getMinistries(controller.signal)
      .then((result) => {
        setData(result);
        setError(null);
        setIsLoading(false);
      })
      .catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setError(cause);
        setIsLoading(false);
      });
    return () => controller.abort();
  }, []);

  return (
    <section className="section" aria-labelledby="all-ministries-heading">
      <h2 className="page__section-title" id="all-ministries-heading">
        All ministries
      </h2>
      <p className="small muted">
        Every ministry in the church. You can open any of them to see how they are set up —
        the people who run each one make its scheduling decisions.
      </p>
      <Body data={data} error={error} isLoading={isLoading} />
    </section>
  );
}

function Body({
  data,
  error,
  isLoading,
}: {
  data: MinistryList | null;
  error: unknown;
  isLoading: boolean;
}) {
  if (isLoading) return <LoadingNotice what="ministries" />;

  if (error !== null) {
    const apiError = asApiError(error);
    return apiError !== null ? <ErrorNotice error={apiError} /> : <UnexpectedErrorNotice />;
  }

  if (data === null) return <UnexpectedErrorNotice />;

  if (data.ministries.length === 0) {
    return (
      <EmptyNotice>
        <p className="notice__title">No ministries yet</p>
        <p>Nothing has been set up in this church yet.</p>
      </EmptyNotice>
    );
  }

  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">Ministry</th>
            <th scope="col">Led by</th>
            <th scope="col">People</th>
            <th scope="col">Current period</th>
            <th scope="col">
              <span className="visually-hidden">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {data.ministries.map((ministry) => (
            <MinistryRow key={ministry.ministry_id} ministry={ministry} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MinistryRow({ ministry }: { ministry: MinistryOverview }) {
  const isActive = ministry.deactivated_at === null;

  return (
    <tr>
      <th scope="row">
        {ministry.name}
        {/* "Archived", never "Deleted": past schedules still name it. */}
        {!isActive && <span className="badge badge--warning">Archived</span>}
      </th>
      <td>
        {ministry.heads.length === 0 ? (
          // Not a blank: "nobody leads this" is the actionable fact, and it is
          // the state in which nobody can roster it at all.
          <span className="muted">No head yet</span>
        ) : (
          ministry.heads.map((head) => head.display_name).join(", ")
        )}
      </td>
      <td>{ministry.active_member_count}</td>
      <td>
        <PeriodCell ministry={ministry} />
      </td>
      <td>
        <Link href={`/ministries/${ministry.ministry_id}/periods`}>View</Link>
      </td>
    </tr>
  );
}

/**
 * Where this ministry has got to.
 *
 * Three honest states, and none of them is guessed: no period at all, the
 * period today falls inside, or the most recently started one — which is
 * labelled as ended rather than called "current", because saying "current"
 * about a quarter that finished in March would be wrong by one word in the
 * place it matters.
 */
function PeriodCell({ ministry }: { ministry: MinistryOverview }) {
  const period = ministry.period;
  if (period === null) return <span className="muted">Not set up yet</span>;

  return (
    <>
      <span>{period.name}</span>
      {!period.is_current && <span className="badge">Ended</span>}
      {period.latest_version_status === null ? (
        <span className="muted"> · no schedule yet</span>
      ) : (
        <span className="muted">
          {" "}
          · v{period.latest_version_number} {period.latest_version_status.toLowerCase()}
        </span>
      )}
    </>
  );
}
