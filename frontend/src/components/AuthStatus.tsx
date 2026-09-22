"use client";

/**
 * The header's identity line: who you are, and the way out.
 *
 * **It no longer fetches anything, and it no longer offers a way in.** Both
 * were right when the header was visible to everybody; neither is now. The
 * actor comes from `AuthGate`'s context -- one `/me` for the whole page -- and
 * this component only ever renders inside that gate, which means there is
 * always somebody to name. A signed-out visitor never reaches this code at all,
 * so the second "Sign in with Google" button that used to live here is gone.
 *
 * It stays in the header rather than on the home page so that signing out is
 * reachable from every screen: a ministry head who has finished for the day
 * should not have to navigate home first.
 */

import { useActor } from "@/components/AuthGate";
import { signOut } from "@/lib/auth";

export function AuthStatus() {
  const actor = useActor();

  return (
    <div className="auth-status">
      <span className="auth-status__name">
        {actor.display_name}
        {actor.is_admin ? " · administrator" : ""}
      </span>
      <button className="button auth-status__action" type="button" onClick={() => void signOut()}>
        Sign out
      </button>
    </div>
  );
}
