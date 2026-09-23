import type { Metadata } from "next";

import "./globals.css";

/**
 * The document shell, and nothing else.
 *
 * There is no header, no navigation and no authentication gate, because there
 * is one page and nothing on it to protect. Light and dark both come from the
 * viewer's own `prefers-color-scheme` — no toggle, no stored preference, no
 * bootstrap script that has to run before React exists.
 */
export const metadata: Metadata = {
  title: "Soft-constraint evaluation with Jev",
  description:
    "Three synthetic scheduling drafts judged by a System One model, with the final status decided by deterministic Python.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <main className="page">{children}</main>
      </body>
    </html>
  );
}
