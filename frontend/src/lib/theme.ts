/**
 * The two-state theme this app supports, and where a visitor's choice lives.
 *
 * **Light is the default.** A visitor who has never chosen sees light,
 * whatever their operating system prefers -- a demo shown on someone else's
 * laptop or a projector should look the same regardless of whose dark-mode
 * setting it inherits. Only an explicit click on the toggle in the site
 * header, persisted to this browser's own `localStorage`, switches to dark.
 * There is no third "follow the system" state, no account-level preference,
 * and nothing is sent to the server.
 */

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "church-scheduling-theme";

export const THEME_ATTRIBUTE = "data-theme";

/** What a freshly-read `localStorage` value means. Anything but the exact
 *  string `"dark"` is light -- absence, `null`, garbage, and a stray
 *  `"system"` from some earlier idea all collapse to the one default. */
export function themeFromStoredValue(value: string | null): Theme {
  return value === "dark" ? "dark" : "light";
}

/**
 * The inline bootstrap script's own body, as a string -- written once here
 * so the copy embedded in `<head>` (before React exists) and the behaviour
 * `ThemeToggle` runs after hydration can never drift apart.
 *
 * Deliberately tiny, dependency-free, and defensive: `localStorage` can
 * throw (private browsing, a locked-down browser), and this must never be
 * the thing that breaks a page load, so every access is wrapped and any
 * failure is silently treated as "light".
 */
export function themeBootstrapScript(storageKey: string): string {
  return (
    "(function(){try{" +
    `if(localStorage.getItem(${JSON.stringify(storageKey)})==="dark"){` +
    'document.documentElement.setAttribute("data-theme","dark");' +
    'document.documentElement.style.colorScheme="dark";' +
    "}" +
    "}catch(e){}})();"
  );
}
