/**
 * The home route: a server component whose only job is to read the one piece
 * of configuration the browser cannot read for itself, and hand it to the
 * client component that renders the page.
 *
 * Which ministries have been validated with their ministry head is a pilot
 * fact the backend does not store (see `lib/pilotStatus.ts`). It is read from
 * the environment here, on the server, the same way the backend's address is --
 * never through a `NEXT_PUBLIC_` variable, which this app does not use at all.
 *
 * It no longer reads `?auth_error=`. A refused sign-in never reaches this page
 * now: `AuthGate` renders the landing screen instead of the shell, and the
 * landing reads the reason itself.
 */

import { HomeMinistries } from "@/components/HomeMinistries";
import { VALIDATED_MINISTRIES_ENV_VAR, parseValidatedMinistries } from "@/lib/pilotStatus";

/** Configuration is read per request, not frozen into a build. */
export const dynamic = "force-dynamic";

export default function HomePage() {
  const validated = parseValidatedMinistries(process.env[VALIDATED_MINISTRIES_ENV_VAR]);
  return <HomeMinistries validatedMinistries={validated} />;
}
