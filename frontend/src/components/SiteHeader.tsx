"use client";

/**
 * The application header, for signed-in visitors only.
 *
 * **Why this is a component rather than markup in `layout.tsx`.** The layout is
 * a server component, so anything it renders is serialized into the streamed
 * RSC payload *whether or not* the client ends up displaying it. With the
 * header written inline there, a signed-out visitor's page still carried a
 * description of it in the payload -- no data and no dashboard headings, but
 * more of the shell than a stranger needs. Moving it into a client component
 * that only `AuthGate` renders keeps the signed-out payload down to the landing
 * screen.
 *
 * Task 75's branding is unchanged: text only, because the repository carries no
 * logo asset and a drawn-here substitute would be a mark nobody approved.
 *
 * **It carries the app's only navigation link, and that link is conditional.**
 * Task 79's People directory is shown to an Admin or a ministry head and to
 * nobody else. Because this component renders only inside `AuthGate`, a
 * signed-out visitor's streamed payload contains none of it -- not the link,
 * not the word, not the route.
 */

import { useEffect } from "react";
import Link from "next/link";

import { AuthStatus } from "@/components/AuthStatus";
import { useActor } from "@/components/AuthGate";
import { ThemeToggle } from "@/components/ThemeToggle";
import { BRAND_APP_NAME, BRAND_CHURCH_NAME, BRAND_DOCUMENT_TITLE } from "@/lib/brand";
import { canViewPeople } from "@/lib/peopleAccess";

export function SiteHeader() {
  // Safe here, and only here: this component renders inside `AuthGate`, so
  // reaching it means the server has already said who is asking.
  const actor = useActor();

  // The document title is public, so it names only the church until somebody
  // is signed in (see `app/layout.tsx`). This component renders only inside
  // `AuthGate`, so reaching here means there is an actor and the fuller title
  // is no longer a disclosure.
  useEffect(() => {
    document.title = BRAND_DOCUMENT_TITLE;
  }, []);

  return (
    <header className="site-header">
      <div className="site-header__inner">
        <p className="site-header__title">
          <Link href="/">
            <span className="brand__church">{BRAND_CHURCH_NAME}</span>
            <span className="brand__app">{BRAND_APP_NAME}</span>
          </Link>
        </p>
        {/* Identity and sign-out sit here so they are reachable from every
            screen, not only from home. */}
        <div className="site-header__actions">
          {/* Task 79. Rendered only for an Admin or a ministry head -- a
              volunteer sees no People link, no People markup, and gets 403
              from the API if they type the URL anyway. The decision itself
              lives in `lib/peopleAccess.ts`, which is where it is tested;
              this is the one place it is rendered. */}
          {canViewPeople(actor) && (
            <Link className="site-header__nav-link" href="/people">
              People
            </Link>
          )}
          <AuthStatus />
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}
