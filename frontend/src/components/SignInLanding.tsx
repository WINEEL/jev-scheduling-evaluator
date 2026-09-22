"use client";

/**
 * Everything an unauthenticated visitor is allowed to see: the church's name
 * and one button.
 *
 * **What is deliberately absent, and why.** No application name, no
 * description of what this is for, no mention of ministries, scheduling,
 * volunteers, roles, or how access is arranged. A visitor who has not been
 * granted access should not be able to learn from this screen what the
 * application does or how somebody gets in -- that is information about the
 * organisation, and none of it is needed to sign in.
 *
 * The same rule governs the document's `<title>` and meta description, which
 * are just as public: see `app/layout.tsx`.
 *
 * A refusal is a full stop -- "Access unavailable." -- for every reason. The
 * backend still distinguishes them and still logs which occurred; only the
 * public wording is flattened. See `lib/auth.ts`.
 */

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";

import { BRAND_CHURCH_NAME } from "@/lib/brand";
import {
  ACCESS_UNAVAILABLE_MESSAGE,
  AUTH_ERROR_PARAM,
  SIGN_IN_PATH,
  wasSignInRefused,
} from "@/lib/auth";

export function SignInLanding({
  checking = false,
  unreachable = false,
}: {
  checking?: boolean;
  unreachable?: boolean;
}) {
  return (
    <main className="landing">
      <div className="landing__card">
        {/* Task 75 branding, reduced to the one line a stranger may see. Text
            only: the repository carries no logo asset, and a drawn-here
            substitute would be a mark nobody approved. */}
        <p className="landing__brand">
          <span className="brand__church">{BRAND_CHURCH_NAME}</span>
        </p>

        {checking ? (
          /* No button while the session is still resolving: one that appeared
             and then vanished would be worse than a beat of stillness. */
          <p className="landing__note" aria-live="polite">
            &nbsp;
          </p>
        ) : unreachable ? (
          <Unavailable />
        ) : (
          /* Reading the query string needs a boundary; the brand above renders
             either way. */
          <Suspense fallback={<SignInAction />}>
            <SignedOut />
          </Suspense>
        )}
      </div>
    </main>
  );
}

function SignedOut() {
  const refused = wasSignInRefused(useSearchParams().get(AUTH_ERROR_PARAM));

  return (
    <>
      {refused && <p className="landing__note">{ACCESS_UNAVAILABLE_MESSAGE}</p>}
      <SignInAction />
    </>
  );
}

function SignInAction() {
  // An anchor, not a button with a handler: this navigates to Google, and a
  // `fetch` would either follow the redirect invisibly or fail CORS. See
  // `lib/auth.ts`.
  return (
    <a className="button button--primary link-button landing__action" href={SIGN_IN_PATH}>
      Sign in with Google
    </a>
  );
}

/**
 * The API could not be reached. Worth distinguishing from a refusal *to the
 * person*, because retrying is the right advice here and is not elsewhere --
 * but it still says nothing about what the service is.
 */
function Unavailable() {
  return (
    <>
      <p className="landing__note">{ACCESS_UNAVAILABLE_MESSAGE}</p>
      <SignInAction />
    </>
  );
}
