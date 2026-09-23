/**
 * What this frontend must not contain. Asserted by reading its own source.
 *
 * The project is a demonstration of a judgment, not an application: it has no
 * users, no sessions, no stored data and no second screen. Each test below
 * pins one of those absences, because the way a small demo stops being small
 * is by acquiring them one at a time without anybody deciding to.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join } from "node:path";

import { describe, expect, it } from "vitest";

const SOURCE_ROOT = new URL("../", import.meta.url).pathname;

/** Every application source file, excluding the tests that describe them. */
function sourceFiles(): string[] {
  const found: string[] = [];
  const walk = (directory: string) => {
    for (const entry of readdirSync(directory)) {
      const path = join(directory, entry);
      if (statSync(path).isDirectory()) {
        walk(path);
        continue;
      }
      if (![".ts", ".tsx"].includes(extname(path))) continue;
      if (path.endsWith(".test.ts") || path.endsWith(".test.tsx")) continue;
      found.push(path);
    }
  };
  walk(SOURCE_ROOT);
  return found;
}

/** Source with comments stripped, so prose about a term cannot fail a test. */
function codeOf(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const FILES = sourceFiles();

it("finds the source it is asserting about", () => {
  expect(FILES.length).toBeGreaterThan(4);
  expect(FILES.some((path) => path.endsWith("page.tsx"))).toBe(true);
});

describe("no authentication, anywhere", () => {
  it("names no sign-in, session or identity concept", () => {
    // There is nothing on this page to protect: every volunteer in it is
    // invented and no request reads stored data. A sign-in would be asking
    // somebody to prove who they are before showing them a fiction.
    const forbidden = [
      "signIn",
      "signOut",
      "session",
      "Session",
      "oauth",
      "OAuth",
      "credentials",
      "Authorization",
      "getMe",
    ];

    for (const path of FILES) {
      const source = codeOf(path);
      for (const term of forbidden) {
        expect(source, `${path} / ${term}`).not.toContain(term);
      }
    }
  });

  it("never sets or reads a cookie", () => {
    for (const path of FILES) {
      const source = codeOf(path).toLowerCase();
      expect(source, path).not.toContain("cookie");
    }
  });

  it("stores nothing in the browser", () => {
    // No theme preference, no remembered scenario, no analytics. A page whose
    // whole state is "which of three scenarios is selected" does not need to
    // survive a reload.
    for (const path of FILES) {
      const source = codeOf(path);
      expect(source, path).not.toContain("localStorage");
      expect(source, path).not.toContain("sessionStorage");
      expect(source, path).not.toContain("document.cookie");
    }
  });
});

describe("the API key never reaches the browser", () => {
  it("never reads it, and carries no value that could be one", () => {
    // The proxy talks to the backend; the backend talks to TypeSafe. The key
    // lives in the backend's environment and is never in a response, so there
    // is nothing for this app to hold.
    //
    // Naming the variable is fine and useful — the page tells somebody what
    // to set when an evaluation fails. What must never appear is a *read* of
    // it, which would put it in a server response or a client bundle, or a
    // literal that looks like a key.
    for (const path of FILES) {
      const source = readFileSync(path, "utf8");
      expect(source, path).not.toMatch(/env\s*[.[]\s*["']?TYPESAFE_API_KEY/);
      expect(source, path).not.toMatch(/apikey_[A-Za-z0-9]/);
    }
  });

  it("reaches the backend only through the same-origin proxy", () => {
    // A direct browser call would need the backend's address in client code
    // and a CORS policy on the backend. Neither exists.
    const client = codeOf(join(SOURCE_ROOT, "lib/client.ts"));

    expect(client).toContain('"/api/jev-demo"');
    expect(client).not.toContain("http://");
    expect(client).not.toContain("https://");
  });

  it("keeps the backend's address on the server", () => {
    // Read in the route handler from `process.env`, never exposed through a
    // NEXT_PUBLIC_ variable that would be inlined into the client bundle.
    for (const path of FILES) {
      expect(codeOf(path), path).not.toContain("NEXT_PUBLIC_");
    }

    const proxy = codeOf(join(SOURCE_ROOT, "app/api/jev-demo/[...path]/route.ts"));
    expect(proxy).toContain("process.env");
  });
});

describe("one page, one transport", () => {
  it("has exactly one route and one proxy", () => {
    const pages = FILES.filter((path) => path.endsWith("page.tsx"));
    const routes = FILES.filter((path) => path.endsWith("route.ts"));

    expect(pages.map((path) => path.split("/src/")[1])).toEqual(["app/page.tsx"]);
    expect(routes.map((path) => path.split("/src/")[1])).toEqual([
      "app/api/jev-demo/[...path]/route.ts",
    ]);
  });

  it("calls fetch only in the client and the proxy", () => {
    // Anywhere else would bypass the one place a response becomes either data
    // or a typed error.
    const allowed = ["lib/client.ts", "app/api/jev-demo/[...path]/route.ts"];

    for (const path of FILES) {
      if (allowed.some((suffix) => path.endsWith(suffix))) continue;
      expect(codeOf(path), path).not.toMatch(/\bfetch\s*\(/);
    }
  });

  it("the proxy carries no cookie, no redirect and no extra header", () => {
    const proxy = codeOf(join(SOURCE_ROOT, "app/api/jev-demo/[...path]/route.ts"));

    expect(proxy).not.toContain("set-cookie");
    expect(proxy).not.toContain("getSetCookie");
    expect(proxy).not.toContain("location");
    expect(proxy).not.toContain('redirect: "manual"');
  });

  it("the proxy offers only the two verbs the API has", () => {
    const proxy = codeOf(join(SOURCE_ROOT, "app/api/jev-demo/[...path]/route.ts"));
    const verbs = ["GET", "POST", "PATCH", "PUT", "DELETE"].filter((verb) =>
      new RegExp(`export async function ${verb}\\b`).test(proxy),
    );

    expect(verbs).toEqual(["GET", "POST"]);
  });
});

describe("the page shows the distinction the project is about", () => {
  const page = () => readFileSync(join(SOURCE_ROOT, "app/page.tsx"), "utf8");

  it("says its data is synthetic, in text a screenshot will carry", () => {
    // A picture of a scheduling judgment that does not say "invented" is a
    // picture somebody can mistake for a real roster.
    expect(page()).toContain("Synthetic demo data");
    expect(page()).toMatch(/invented/i);
  });

  it("shows all five judgments, the status and the policy reasons", () => {
    const source = codeOf(join(SOURCE_ROOT, "app/page.tsx"));

    for (const judgment of [
      "workload_fairness",
      "preference_satisfaction",
      "overall_quality",
      "overuse_concern",
      "human_review_warranted",
    ]) {
      expect(source, judgment).toContain(judgment);
    }
    expect(source).toContain("statusLabel");
    expect(source).toContain("reasonLabel");
    expect(source).toContain("confidenceBand");
  });

  it("decides no status of its own -- no threshold lives in the browser", () => {
    // The load-bearing claim. The status is computed in
    // `app/soft_constraints/policy.py`; a comparison against a probability
    // here would be a second copy of that policy, in the one place it could
    // silently disagree with the first.
    const sources = ["app/page.tsx", "lib/presentation.ts"].map((path) =>
      codeOf(join(SOURCE_ROOT, path)),
    );

    for (const source of sources) {
      expect(source).not.toMatch(/(probability|normalized)\s*[<>]=?\s*0\.\d/);
      expect(source).not.toContain("thresholds.review_probability");
    }
  });

  it("offers no way to type anything", () => {
    // No form, no editable roster, no request body: the draft is built
    // server-side from one of three fixed scenario names.
    const source = codeOf(join(SOURCE_ROOT, "app/page.tsx"));

    expect(source).toContain("evaluateScenario");
    expect(source).not.toContain("<form");
    expect(source).not.toContain("<input");
    expect(source).not.toContain("<textarea");
  });

  it("every style rule it adds is namespaced", () => {
    const css = readFileSync(join(SOURCE_ROOT, "app/globals.css"), "utf8");
    const demoBlock = css.slice(css.indexOf("/* -- the demo's own rules"));

    expect(demoBlock.length).toBeGreaterThan(200);
    for (const selector of demoBlock.matchAll(/^\.([a-zA-Z][\w-]*)/gm)) {
      expect(selector[1], selector[0]).toMatch(/^jev-/);
    }
  });
});
