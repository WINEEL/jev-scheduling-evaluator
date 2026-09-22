"use client";

/**
 * The one place the application decides whether to render itself at all.
 *
 * **Why this is a gate and not a redirect.** Every screen in this app shows a
 * real church's data -- who serves when, who is available, who leads what. None
 * of it should be reachable, and none of its *structure* should be visible,
 * before the server has confirmed who is asking. Before Task 76's final pass
 * the home page rendered "Ministries you lead" and then discovered it was
 * signed out, which told an anonymous visitor the shape of the product and made
 * the page look briefly like a dashboard that had failed to load.
 *
 * So: this component wraps the entire shell -- header included -- and renders
 * `children` only once `/me` has answered with an actor. Signed out, there is
 * no header, no navigation, no headings and no data; there is a landing screen.
 *
 * **It is also the app's only call to `/me`.** The header and the home page
 * both used to fetch it independently. They now read the actor from this
 * component's context, so one request settles the question for the whole page
 * and there is no window in which two components disagree about who is signed
 * in.
 *
 * **This is a user-experience boundary, not a security boundary**, and the
 * distinction matters. Hiding the shell protects nobody by itself -- the
 * browser could render whatever it liked. What actually protects the data is
 * that every endpoint requires the signed session cookie and returns 401
 * without it, which is enforced in FastAPI and verified against the deployed
 * service. This component makes the UI tell the truth; the API makes it safe.
 */

import { createContext, useContext, useEffect, useState } from "react";

import { SignInLanding } from "@/components/SignInLanding";
import { SiteHeader } from "@/components/SiteHeader";
import { getMe } from "@/lib/api/client";
import { ApiError, isAbortError } from "@/lib/api/errors";
import type { CurrentActor } from "@/lib/api/types";

const ActorContext = createContext<CurrentActor | null>(null);

/**
 * The signed-in actor, for anything rendered inside the gate.
 *
 * Throws rather than returning `null` when used outside it. A component that
 * renders church data has no meaningful "no actor" branch -- if one is ever
 * mounted outside the gate, that is a wiring mistake worth failing loudly for,
 * not a state to render around.
 */
export function useActor(): CurrentActor {
  const actor = useContext(ActorContext);
  if (actor === null) {
    throw new Error("useActor must be used inside <AuthGate>");
  }
  return actor;
}

type State =
  | { status: "checking" }
  | { status: "signed-in"; actor: CurrentActor }
  | { status: "signed-out" }
  | { status: "unreachable" };

export function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<State>({ status: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    getMe(controller.signal)
      .then((actor) => setState({ status: "signed-in", actor }))
      .catch((cause: unknown) => {
        // An abort is the component unmounting or a newer request replacing
        // this one -- not an outcome. Leaving the state alone keeps the
        // "checking" screen rather than announcing a result nobody got.
        if (isAbortError(cause)) return;

        // A 401 is the ordinary signed-out state, not a failure. Anything else
        // -- the API down, a bad gateway, DNS -- is reported as its own thing,
        // because telling somebody to sign in when the server is unreachable
        // sends them round a loop that cannot succeed.
        const isUnauthenticated =
          cause instanceof ApiError && cause.kind === "unauthenticated";
        setState({ status: isUnauthenticated ? "signed-out" : "unreachable" });
      });
    return () => controller.abort();
  }, []);

  if (state.status === "signed-in") {
    // The gate owns the entire authenticated shell -- header included -- so
    // there is exactly one place that decides whether the application is on
    // screen at all.
    return (
      <ActorContext.Provider value={state.actor}>
        <SiteHeader />
        <main className="page">{children}</main>
      </ActorContext.Provider>
    );
  }

  // Every other state renders the landing screen and nothing else. Note that
  // "checking" lands here too: the first paint is branding, never a dashboard
  // that might be taken away a moment later.
  return (
    <SignInLanding
      checking={state.status === "checking"}
      unreachable={state.status === "unreachable"}
    />
  );
}
