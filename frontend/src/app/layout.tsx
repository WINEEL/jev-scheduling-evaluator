import type { Metadata } from "next";

import { AuthGate } from "@/components/AuthGate";
import { BRAND_CHURCH_NAME } from "@/lib/brand";
import { THEME_STORAGE_KEY, themeBootstrapScript } from "@/lib/theme";

import "./globals.css";

/**
 * **The document's metadata is a public surface, and is treated as one.**
 *
 * It is served to everybody -- an unauthenticated visitor, a link preview, a
 * crawler, the browser's history and tab strip -- long before anyone signs in.
 * It previously used organization-specific branding with a
 * description explaining that the app builds volunteer schedules, which said
 * more about the organisation than a stranger needs to know.
 *
 * So the public title is the church's name alone, and there is **no
 * description**: an absent one reveals nothing, whereas a vague one still
 * invites a guess. Signed-in users lose none of the branding -- `SiteHeader`
 * restores the fuller title once there is an actor, and the header itself
 * still shows both lines.
 */
export const metadata: Metadata = {
  title: BRAND_CHURCH_NAME,
};

/**
 * The document shell, and nothing else.
 *
 * **The application's chrome is not here.** Task 76's final pass moved the
 * header and the page wrapper into `AuthGate`, which renders them only once
 * `/me` has named an actor. Keeping them in this server component would have
 * meant a signed-out visitor's streamed payload still described the shell they
 * are not allowed to see.
 *
 * What stays is what must: the theme bootstrap, which has to run before React
 * exists, and the metadata.
 */
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <head>
        {/* Runs before anything paints, so a hard reload for a visitor who
            previously chose dark never flashes light first -- see
            `lib/theme.ts` and `components/ThemeToggle.tsx`. Inline and
            synchronous is the whole point: it must run before React exists,
            which rules out a component. */}
        <script dangerouslySetInnerHTML={{ __html: themeBootstrapScript(THEME_STORAGE_KEY) }} />
      </head>
      <body>
        <AuthGate>{children}</AuthGate>
      </body>
    </html>
  );
}
