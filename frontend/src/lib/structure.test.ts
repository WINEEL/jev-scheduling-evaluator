/**
 * What this frontend must *not* contain.
 *
 * These read the application source and assert absences: no baked-in ids, no
 * path to the database, and -- since Task 80 -- no lifecycle action beyond the
 * two the backend actually offers. Where a claim can be checked by behaviour
 * or by parsing rather than by searching prose, it is: a comment mentioning
 * "successor" should not fail a build, and a `<button>Un-finalize` in a
 * component must.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join } from "node:path";

import { describe, expect, it, vi } from "vitest";

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

/**
 * Source with comments and JSX text stripped down to *code*, so an
 * explanatory comment cannot fail an assertion about behaviour.
 */
function codeOf(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

const FILES = sourceFiles();

it("finds the application source it is asserting about", () => {
  expect(FILES.length).toBeGreaterThan(8);
  expect(FILES.some((path) => path.endsWith("client.ts"))).toBe(true);
});

// -- 26-29: nothing baked in -----------------------------------------------

describe("no hard-coded domain values", () => {
  it("26. no Person id is written into the source", () => {
    // The only id this app may use comes from the environment, read in one
    // place, and that place names the variable rather than a value.
    const devAuth = codeOf(join(SOURCE_ROOT, "lib/devAuth.ts"));
    expect(devAuth).toContain("CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID");
    expect(devAuth).not.toMatch(/X-Dev-Actor-Person-Id"\s*\]?\s*[:=]\s*["'`]\d+/);

    for (const path of FILES) {
      const code = codeOf(path);
      // No assignment of a bare number to anything actor/person shaped.
      expect(code).not.toMatch(/\b(personId|person_id|actorId|actorPersonId)\s*[:=]\s*\d+/);
    }
  });

  it("27. no Ministry id is written into the source", () => {
    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toMatch(/\b(ministryId|ministry_id)\s*[:=]\s*\d+/);
    }
  });

  it("28. no target-per-person default is baked in", async () => {
    // Behaviour first: an unspecified target is sent as null, not as a number
    // this app chose. Setup's target of ~3 is a ministry's decision.
    const sent: unknown[] = [];
    const { generateSchedule } = await import("./api/client");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        sent.push(JSON.parse(String(init?.body)));
        return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
      }),
    );

    await generateSchedule(21, {});
    vi.unstubAllGlobals();

    expect(sent[0]).toMatchObject({ target_assignments_per_candidate: null });

    // And no literal target is written anywhere in the source.
    for (const path of FILES) {
      expect(codeOf(path)).not.toMatch(/target_assignments_per_candidate\s*[:=]\s*[1-9]/);
      expect(codeOf(path)).not.toMatch(/defaultTarget\s*[:=]\s*\d/);
    }
  });

  it("29. no ministry role ids are written into the source", () => {
    for (const path of FILES) {
      const code = codeOf(path);
      // Variety roles are always sent as null; no id list is ever constructed.
      expect(code).not.toMatch(/role_variety_role_ids\s*[:=]\s*\[/);
      expect(code).not.toMatch(/\broleId\s*[:=]\s*\d+/);
    }
  });
});

// -- 30-31: the lifecycle UI, and its limits --------------------------------

describe("no actions beyond the first-pass flow", () => {
  it("30. offers no successor-version or carry-forward action", () => {
    for (const path of FILES) {
      const code = codeOf(path).toLowerCase();
      expect(code).not.toContain("carry_forward");
      expect(code).not.toContain("carryforward");
      expect(code).not.toContain("create_successor");
      expect(code).not.toContain("createsuccessor");
    }
  });

  it("31. offers the two lifecycle transitions and no others", () => {
    // **Task 80 inverted this test.** Submitting for review and finalizing
    // are now the point of the review screen, so asserting their absence
    // would assert the feature away. What still must not exist is a third
    // transition: nothing un-finalizes a schedule, amends one, or creates a
    // successor, because the backend offers none of those and a control that
    // implied otherwise would promise a head something the domain refuses.
    const client = codeOf(join(SOURCE_ROOT, "lib/api/client.ts"));

    expect(client).toContain("/submit-for-review");
    expect(client).toContain("/finalize");

    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toMatch(/["'`][^"'`]*\/(unfinalize|un-finalize|amend|successor)["'`]/i);
      expect(code).not.toMatch(/>\s*(Un-?finalize|Amend|Create successor|Reopen)/i);
    }
  });

  it("31a. finalizing is never a single click", () => {
    // The one action in this app that volunteers immediately depend on, and
    // the one that cannot be undone. The screen asks before it happens, in
    // the same two-step shape the availability lock uses.
    const page = codeOf(join(SOURCE_ROOT, "app/schedule-versions/[versionId]/page.tsx"));

    expect(page).toContain("isConfirming");
    expect(page).toContain("FINALIZE_CONFIRM_TITLE");
    expect(page).toContain("FINALIZE_CONFIRM_BODY");
    // And the confirmation offers a way out.
    expect(page).toMatch(/>\s*Cancel\s*</);
  });

  it("31d. no lifecycle control is rendered from `is_admin`", () => {
    // Task 80. Whether a reader may act on a ministry is the server's answer,
    // carried as `can_operate`; deriving it in the browser from the admin
    // flag is exactly the mistake that would let an administrator be shown
    // controls the API refuses -- or hide them from one who also heads the
    // ministry.
    const page = codeOf(join(SOURCE_ROOT, "app/schedule-versions/[versionId]/page.tsx"));
    const lifecycle = codeOf(join(SOURCE_ROOT, "lib/lifecycle.ts"));

    expect(page).not.toContain("is_admin");
    expect(lifecycle).not.toContain("is_admin");
    expect(lifecycle).toContain("can_operate");
  });

  it("31e. every ministry screen gates its write controls on the server's answer", () => {
    // Task 80. An administrator may open any ministry and change nothing in
    // it, so each of these screens has to know which of its controls are
    // real. The only acceptable source for that is `can_operate` from the
    // response it already read — deriving it from `is_admin` in the browser
    // would put a second copy of an authorization rule somewhere it cannot be
    // kept in step with the first.
    const screens = [
      "app/ministries/[ministryId]/periods/page.tsx",
      "app/ministries/[ministryId]/roles/page.tsx",
      "app/ministries/[ministryId]/roles/[roleId]/qualifications/page.tsx",
      "app/ministries/[ministryId]/periods/[periodId]/staffing/page.tsx",
      "app/ministries/[ministryId]/periods/[periodId]/availability/page.tsx",
      "app/ministries/[ministryId]/periods/[periodId]/serving-limits/page.tsx",
      "app/ministries/[ministryId]/periods/[periodId]/scheduling-rules/page.tsx",
      "app/schedule-versions/[versionId]/page.tsx",
    ];

    for (const screen of screens) {
      const code = codeOf(join(SOURCE_ROOT, screen));
      expect(code, screen).toMatch(/can_operate|canOperate|anyCanOperate|lifecycleStateOf/);
      expect(code, screen).not.toContain("is_admin");
    }
  });

  it("31b. the API client exposes exactly the first-pass, role-, staffing-, qualification-, availability-, availability-lock-, serving-limit-, scheduling-rule-, people- and membership-management, sign-out and my-schedule calls", async () => {
    const client: Record<string, unknown> = await import("./api/client");
    const functions = Object.entries(client)
      .filter(([, value]) => typeof value === "function")
      .map(([name]) => name)
      .sort();

    expect(functions).toEqual([
      // Task 79. Adds an existing person to one ministry, named explicitly --
      // never inferred from who is signed in.
      "addPersonToMinistry",
      "clearAvailability",
      "clearMinInterveningEvents",
      "clearServingLimit",
      "clearStaffingRequirement",
      "createMinistryRole",
      // Task 79. The only way this app creates a person, and it carries an
      // explicit duplicate acknowledgement rather than merging anybody.
      "createPerson",
      "deactivateMinistryRole",
      // Task 79. Church-wide and reversible; note the absence of any
      // `deletePerson` beside it, here and in the backend.
      "deactivatePerson",
      // Task 80. REVIEW -> FINALIZED: the moment a schedule becomes real for
      // volunteers. Head-only server-side, and confirmed in the UI before it
      // is called. There is deliberately no `unfinalizeScheduleVersion`
      // beside it -- the backend offers none.
      "finalizeScheduleVersion",
      "generateSchedule",
      "getEventAvailability",
      "getEventStaffing",
      "getMe",
      // Task 79. Every ministry in the church, for an administrator's
      // oversight. Admin-only server-side, and a read: it hands nobody an
      // operational control.
      "getMinistries",
      "getMinistryRoles",
      // Task 77. Takes no arguments and cannot: the subject is the
      // authenticated actor, decided server-side.
      "getMySchedule",
      "getPeople",
      "getPeriodEvents",
      "getPerson",
      "getPersonMemberships",
      "getRoleQualifications",
      "getScheduleVersion",
      "getSchedulingPeriods",
      "getSchedulingRules",
      "getServingLimits",
      "lockAvailability",
      // Task 76. Sign-*in* is deliberately absent: it is a navigation to
      // Google, not a fetch, so it is an <a href> and not a client call.
      "logout",
      "reactivateMinistryRole",
      "reactivatePerson",
      // Task 79. One ministry only, and nothing is deleted.
      "removePersonFromMinistry",
      "setAvailability",
      // Task 79. Formal church membership: MEMBER, NON_MEMBER or UNKNOWN.
      // Admin-only, and a different fact from ministry membership.
      "setChurchMembershipStatus",
      "setMinInterveningEvents",
      // Task 79. Appoint somebody to lead a ministry, or revoke it.
      // Admin-only, person and ministry both explicit -- and the only way a
      // brand-new ministry can acquire its first head.
      "setMinistryHead",
      // Task 79. Links, replaces or removes the address somebody signs in
      // with. No part of OAuth is reimplemented in the browser.
      "setPersonAuthLink",
      "setRoleQualification",
      "setServingLimit",
      "setStaffingRequirement",
      "startFirstSchedule",
      // Task 80. DRAFT -> REVIEW. Tells nobody; marks the schedule ready to
      // be checked.
      "submitScheduleVersionForReview",
      "updateMinistryRole",
      "updatePerson",
    ]);
  });

  it("31c. every request the client can make is a GET, POST, PATCH, PUT, or DELETE -- and nothing else", () => {
    // PATCH (Task 53) renames a ministry role. PUT sets a staffing count
    // (Task 54), a qualification decision (Task 55), or an availability
    // answer (Task 56); DELETE clears a staffing requirement (Task 54) or an
    // availability answer (Task 56) -- both real rows removed entirely,
    // unlike a role or a qualification decision, neither of which is ever
    // deleted.
    const client = codeOf(join(SOURCE_ROOT, "lib/api/client.ts"));
    const methods = [...client.matchAll(/["'](GET|POST|PATCH|PUT|DELETE)["']/g)].map((m) => m[1]);
    expect(new Set(methods)).toEqual(new Set(["GET", "POST", "PATCH", "PUT", "DELETE"]));
  });
});

// -- 32-33: boundaries -----------------------------------------------------

describe("boundaries", () => {
  it("32. depends on no database or backend ORM package", async () => {
    const pkg: { dependencies?: Record<string, string>; devDependencies?: Record<string, string> } =
      JSON.parse(readFileSync(join(SOURCE_ROOT, "../package.json"), "utf8"));
    const names = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });

    for (const forbidden of ["pg", "postgres", "sqlalchemy", "prisma", "@prisma/client", "knex", "typeorm", "drizzle-orm"]) {
      expect(names).not.toContain(forbidden);
    }

    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toContain("DATABASE_URL");
      expect(code).not.toMatch(/postgres(ql)?:\/\//);
    }
  });

  it("32b. no component talks to the backend directly", () => {
    // Only the client and the proxy may call fetch; a component doing its own
    // would bypass error handling and, in the browser, the same-origin design.
    const allowed = ["lib/api/client.ts", "api/backend/[...path]/route.ts"];
    for (const path of FILES) {
      if (allowed.some((suffix) => path.endsWith(suffix))) continue;
      expect(codeOf(path)).not.toMatch(/\bfetch\s*\(/);
    }
  });

  it("32c. the browser is never given the backend's address", () => {
    // Client components import the client, which uses a same-origin path; the
    // backend URL is read only on the server.
    const client = codeOf(join(SOURCE_ROOT, "lib/api/client.ts"));
    expect(client).toContain('"/api/backend"');
    expect(client).not.toContain("CHURCH_SCHEDULING_API_URL");
    expect(client).not.toContain("NEXT_PUBLIC_");

    for (const path of FILES) {
      // No secret would ever be exposed through a NEXT_PUBLIC_ variable,
      // because none is defined at all.
      expect(codeOf(path)).not.toContain("NEXT_PUBLIC_");
    }
  });

  it("33. uses no `any` in application code", () => {
    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toMatch(/:\s*any\b/);
      expect(code).not.toMatch(/\bas\s+any\b/);
      expect(code).not.toMatch(/<any>/);
    }
  });

  it("33b. never silences the type checker", () => {
    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toContain("@ts-ignore");
      expect(code).not.toContain("@ts-nocheck");
      expect(code).not.toContain("eslint-disable");
    }
  });
});

describe("the compact matrix redesign (Task 72 continuation)", () => {
  const STAFFING = join(SOURCE_ROOT, "app/ministries/[ministryId]/periods/[periodId]/staffing/page.tsx");
  const AVAILABILITY = join(
    SOURCE_ROOT,
    "app/ministries/[ministryId]/periods/[periodId]/availability/page.tsx",
  );
  const SERVING_LIMITS = join(
    SOURCE_ROOT,
    "app/ministries/[ministryId]/periods/[periodId]/serving-limits/page.tsx",
  );
  const SCHEDULING_RULES = join(
    SOURCE_ROOT,
    "app/ministries/[ministryId]/periods/[periodId]/scheduling-rules/page.tsx",
  );

  it("34. staffing is one matrix built from the shared reshaping module, not one card per event", () => {
    const code = codeOf(STAFFING);
    expect(code).toContain("buildStaffingMatrix");
    expect(code).not.toContain("Show roles");
    expect(code).not.toContain("Hide roles");
  });

  it("34b. a staffing cell is edited directly and saved through one batched action", () => {
    const code = codeOf(STAFFING);
    expect(code).toContain("SaveChangesBar");
    // The old per-row "Set" button is gone; there is exactly one place that
    // sends a requirement count, and it is the batched save.
    expect(code).not.toMatch(/>\s*Set\s*</);
  });

  it("35. availability is one matrix built from the shared reshaping module, not one card per event", () => {
    const code = codeOf(AVAILABILITY);
    expect(code).toContain("buildAvailabilityMatrix");
    expect(code).not.toContain("Show members");
    expect(code).not.toContain("Hide members");
  });

  it("35b. each availability cell is a single compact control, not four buttons", () => {
    const code = codeOf(AVAILABILITY);
    expect(code).toMatch(/<select/);
    // STATES still drives the select's <option> list -- what must be gone is
    // rendering a <button> per state.
    expect(code).not.toMatch(/STATES\.map[\s\S]{0,120}<button/);
  });

  it("35c. clearing an answer back to no response remains possible", () => {
    const code = codeOf(AVAILABILITY);
    expect(code).toContain("clearAvailability");
    expect(code).toContain("availabilityStateFromSelectValue");
  });

  it("35d. the 'If need be' wording comes from the shared label, never a second literal copy", () => {
    const code = codeOf(AVAILABILITY);
    expect(code).not.toContain('"If need be"');
    expect(code).toContain("availabilityStateLabel");
  });

  it("36. serving limits are edited in one table with one batched save, not a Save/Clear pair per row", () => {
    const code = codeOf(SERVING_LIMITS);
    expect(code).toContain("servingLimitChange");
    expect(code).toContain("SaveChangesBar");
    expect(code).not.toContain("Save maximum");
  });

  it("36b. both matrix screens save through the same shared bar, not two conventions", () => {
    expect(codeOf(join(SOURCE_ROOT, "components/SaveChangesBar.tsx"))).toContain("Save changes");
  });

  it("37. the event-gap rule shows its product name, never the stored field name, in the page", () => {
    const code = codeOf(SCHEDULING_RULES);
    expect(code).toContain("EVENT_GAP_RULE_NAME");
    expect(code).not.toContain("Minimum events to skip between assignments");
    // The API contract is unchanged -- only the label and layout moved.
    expect(code).toContain("min_intervening_events");
    expect(code).toContain("setMinInterveningEvents");
    expect(code).toContain("clearMinInterveningEvents");
  });

  it("37b. lays the rule out as a Rule / Setting / Meaning table", () => {
    const code = codeOf(SCHEDULING_RULES);
    expect(code).toContain("rule-table");
    expect(code).toMatch(/Rule<\/th>/);
    expect(code).toMatch(/Setting<\/th>/);
    expect(code).toMatch(/Meaning<\/th>/);
  });

  it("38. every touched matrix input and select still carries an accessible label", () => {
    for (const path of [STAFFING, AVAILABILITY, SERVING_LIMITS, SCHEDULING_RULES]) {
      const code = codeOf(path);
      expect(code).toMatch(/aria-label=/);
    }
  });
});

describe("the theme foundation (Task 72 final continuation)", () => {
  const CSS = codeOf(join(SOURCE_ROOT, "app/globals.css"));
  const LAYOUT = codeOf(join(SOURCE_ROOT, "app/layout.tsx"));
  const THEME_TOGGLE = codeOf(join(SOURCE_ROOT, "components/ThemeToggle.tsx"));

  it("39. light is the unconditional default -- the base :root is not gated behind a system-preference media query", () => {
    // A `prefers-color-scheme` media query is fine for *offering* dark, but
    // must not be what decides the *default* :root palette any more.
    expect(CSS).not.toMatch(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{/);
    expect(CSS).toMatch(/:root\s*\{[^}]*color-scheme:\s*light/);
  });

  it("39b. dark is reachable only through an explicit attribute the toggle controls", () => {
    expect(CSS).toContain(':root[data-theme="dark"]');
  });

  it("40. the header renders a theme toggle", () => {
    // The header moved out of the layout into `components/SiteHeader.tsx` in
    // Task 76's final pass, so that a signed-out visitor's streamed payload
    // carries no application chrome. The toggle moved with it.
    expect(codeOf(join(SOURCE_ROOT, "components/SiteHeader.tsx"))).toContain("ThemeToggle");
  });

  it("40b. the toggle persists the choice to this browser's own storage, nothing server-side", () => {
    expect(THEME_TOGGLE).toContain("localStorage");
    expect(THEME_TOGGLE).not.toMatch(/fetch\s*\(/);
  });

  it("40c. the toggle offers exactly two states, not a third 'system' option", () => {
    expect(THEME_TOGGLE).toMatch(/light/i);
    expect(THEME_TOGGLE).toMatch(/dark/i);
    expect(THEME_TOGGLE.toLowerCase()).not.toContain("system");
  });

  it("41. a bootstrap script in <head> avoids a flash on hard reload, not a client-only effect", () => {
    expect(LAYOUT).toContain("themeBootstrapScript");
    expect(LAYOUT).toContain("<head>");
  });
});

describe("the schedule review redesign (Task 72 final continuation)", () => {
  const SCHEDULE_TABLE = join(SOURCE_ROOT, "components/ScheduleTable.tsx");
  const ASSIGNMENTS_LIST = join(SOURCE_ROOT, "components/AssignmentsByPersonList.tsx");
  const REVIEW_PAGE = join(SOURCE_ROOT, "app/schedule-versions/[versionId]/page.tsx");

  it("42. the schedule is a generic event x role matrix, not one card/table per event", () => {
    const code = codeOf(SCHEDULE_TABLE);
    expect(code).toContain("buildScheduleMatrix");
    expect(code).not.toContain('className="event"');
    // Generic: no ministry's role names appear in the component that renders
    // every ministry's schedule.
    for (const roleName of ["Setup Lead", "Setup 2", "Lead Teacher"]) {
      expect(code).not.toContain(roleName);
    }
  });

  it("42b. supports a role that is simply not required at an event", () => {
    const code = codeOf(SCHEDULE_TABLE);
    expect(code).toContain("cellIsNotRequired");
  });

  it("42c. an unfilled required position is visibly highlighted, not just listed", () => {
    const code = codeOf(SCHEDULE_TABLE);
    expect(code).toContain("cellHasUnfilled");
    expect(code).toContain("schedule-cell--unfilled");
  });

  it("43. assignments by person render as a table, not one card per person", () => {
    const code = codeOf(ASSIGNMENTS_LIST);
    expect(code).toMatch(/<table/);
    expect(code).not.toMatch(/className="card"/);
  });

  it("44. the Generate controls are still present and still offer the same two preferences", () => {
    const code = codeOf(REVIEW_PAGE);
    expect(code).toContain("Include people who have not answered");
    expect(code).toContain("target-per-person");
    expect(code).toContain("Generate schedule");
  });

  it("45. 'What happens next' is still present", () => {
    expect(codeOf(REVIEW_PAGE)).toContain("What happens next");
  });

  it("46. schedule checks and diagnostics are still present and never hidden", () => {
    const code = codeOf(REVIEW_PAGE);
    expect(code).toContain("Schedule checks");
    expect(code).toContain("finalization_readiness");
    expect(code).toContain("readinessLabel");
  });
});

// -- 47-52: the pilot UX freeze (Task 75) -----------------------------------

describe("the schedule review order (Task 75)", () => {
  const REVIEW_PAGE = join(SOURCE_ROOT, "app/schedule-versions/[versionId]/page.tsx");
  const SCHEDULE_TABLE = join(SOURCE_ROOT, "components/ScheduleTable.tsx");

  /** Where a section is rendered, not where its component is declared: every
   *  marker below is the JSX in the page component's own return. */
  function renderOrder(): Record<string, number> {
    const code = codeOf(REVIEW_PAGE);
    const markers = {
      summary: 'aria-labelledby="summary-heading"',
      generate: "<GenerateSection",
      schedule: 'aria-labelledby="schedule-heading"',
      byPerson: 'aria-labelledby="by-person-heading"',
      checks: 'aria-labelledby="checks-heading"',
      next: 'aria-labelledby="next-heading"',
    };
    const found: Record<string, number> = {};
    for (const [name, marker] of Object.entries(markers)) {
      const index = code.indexOf(marker);
      expect(index, `${name} section is missing from the review page`).toBeGreaterThan(-1);
      found[name] = index;
    }
    return found;
  }

  it("47. the schedule itself comes before the per-person breakdown of it", () => {
    const order = renderOrder();
    // The whole point of the reorder: "what is the schedule?" is answered
    // before "what is each person's statistical breakdown?".
    expect(order.schedule).toBeLessThan(order.byPerson);
  });

  it("47b. the six sections run summary, generate, schedule, by person, checks, next", () => {
    const order = renderOrder();
    expect(order.summary).toBeLessThan(order.generate);
    expect(order.generate).toBeLessThan(order.schedule);
    expect(order.schedule).toBeLessThan(order.byPerson);
    expect(order.byPerson).toBeLessThan(order.checks);
    expect(order.checks).toBeLessThan(order.next);
  });

  it("48. generated output is labelled a draft until it is not one", () => {
    const code = codeOf(REVIEW_PAGE);
    expect(code).toContain("Draft schedule");
    // Conditional on the version's own status, not a word printed regardless.
    expect(code).toContain("isDraft");
    expect(code).toContain("STATUS_DRAFT");
  });

  it("49. the schedule matrix is the page's one emphasized element", () => {
    expect(codeOf(SCHEDULE_TABLE)).toContain("matrix-scroll--feature");
    // ...and nothing else claims that treatment.
    for (const path of FILES) {
      if (path.endsWith("ScheduleTable.tsx")) continue;
      expect(codeOf(path)).not.toContain("matrix-scroll--feature");
    }
  });

  it("50. a wide matrix scrolls rather than collapsing into cards on a narrow screen", () => {
    const css = readFileSync(join(SOURCE_ROOT, "app/globals.css"), "utf8");
    expect(css).toMatch(/\.matrix-scroll\s*\{[^}]*overflow:\s*auto/);
    // No breakpoint turns a matrix row into a stacked block.
    expect(css).not.toMatch(/@media[^{]*\{[^}]*table\.matrix[^}]*display:\s*block/);
  });
});

describe("the pilot branding and status wording (Task 75)", () => {
  const CSS = readFileSync(join(SOURCE_ROOT, "app/globals.css"), "utf8");

  it("51. the palette carries the pilot greens and light stays the default", () => {
    expect(CSS).toMatch(/--accent:\s*#008030/);
    expect(CSS).toMatch(/--accent-bright:\s*#00be4b/i);
    expect(CSS).toMatch(/:root\s*\{[^}]*color-scheme:\s*light/);
  });

  it("51b. the header is text branding -- no logo asset is invented or fetched", () => {
    // Branding now appears in two places, both text-only: the signed-in header
    // and the signed-out landing screen.
    expect(codeOf(join(SOURCE_ROOT, "components/SiteHeader.tsx"))).toContain("BRAND_CHURCH_NAME");
    expect(codeOf(join(SOURCE_ROOT, "components/SignInLanding.tsx"))).toContain("BRAND_CHURCH_NAME");
    for (const path of FILES) {
      const code = codeOf(path);
      expect(code).not.toMatch(/<img\b/);
      expect(code).not.toMatch(/next\/image/);
      expect(code).not.toMatch(/https?:\/\/(?!localhost|127\.)/);
    }
  });

  it("52. no page claims the product is approved, production ready or fully validated", () => {
    const forbidden = [
      "production ready",
      "production-ready",
      "church approved",
      "church-approved",
      "fully validated",
      "officially",
    ];
    // Code and displayed text, not comments: the modules that exist to keep
    // these claims out of the product necessarily name them to do so.
    for (const path of FILES) {
      const text = codeOf(path).toLowerCase();
      for (const phrase of forbidden) {
        expect(text, `${path} claims "${phrase}"`).not.toContain(phrase);
      }
    }
  });

  it("52b. a ministry's pilot status is configuration, never a name written into the source", () => {
    const pilot = codeOf(join(SOURCE_ROOT, "lib/pilotStatus.ts"));
    expect(pilot).toContain("CHURCH_SCHEDULING_FRONTEND_PILOT_VALIDATED_MINISTRIES");
    // The ministries this pilot happens to use are not named anywhere in the
    // application source -- the list is read from the environment.
    for (const path of FILES) {
      const code = codeOf(path);
      for (const ministry of ['"Setup"', '"AV"', '"Kids"']) {
        expect(code, `${path} names a ministry`).not.toContain(ministry);
      }
    }
  });
});


// -- 34: the authenticated-only shell (Task 76 final pass) -----------------

describe("the app shell is gated behind authentication", () => {
  it("34a. exactly one 'Sign in with Google' control exists in the whole app", () => {
    // There were two: one in the header's AuthStatus and one in the home
    // page's panel, so a signed-out visitor saw the same call to action twice
    // in different styles. The header now renders nothing until there is
    // somebody to name in it.
    const occurrences = FILES.flatMap((path) =>
      [...codeOf(path).matchAll(/Sign in with Google/g)].map(() => path),
    ).filter((path) => !path.endsWith(".test.ts") && !path.endsWith(".test.tsx"));

    expect(occurrences).toHaveLength(1);
    expect(occurrences[0]).toContain("SignInLanding");
  });

  it("34b. the layout renders no application chrome of its own", () => {
    const layout = codeOf(join(SOURCE_ROOT, "app/layout.tsx"));

    expect(layout).toContain("<AuthGate>");
    // The header and the page wrapper belong to the gate. Left in this server
    // component they would be serialized into a signed-out visitor's streamed
    // payload even though the browser never displays them.
    expect(layout).not.toContain("<header");
    expect(layout).not.toContain("site-header");
    expect(layout).not.toContain("<main");

    const gate = codeOf(join(SOURCE_ROOT, "components/AuthGate.tsx"));
    expect(gate).toContain("<SiteHeader />");
    expect(gate).toContain('className="page"');
  });

  it("34c. only the gate asks who the current actor is", () => {
    // One /me for the page. Two components fetching it independently could
    // disagree, and each extra call is another chance to render a dashboard
    // before the answer arrives.
    const callers = FILES.filter(
      (path) => !path.endsWith(".test.ts") && !path.endsWith(".test.tsx"),
    )
      // client.ts is where getMe is *defined*, not a caller of it.
      .filter((path) => !path.endsWith("lib/api/client.ts"))
      .filter((path) => /\bgetMe\s*\(/.test(codeOf(path)));

    expect(callers).toHaveLength(1);
    expect(callers[0]).toContain("AuthGate");
  });

  it("34d. the landing screen renders no dashboard structure", () => {
    const landing = codeOf(join(SOURCE_ROOT, "components/SignInLanding.tsx"));

    for (const leak of ["Ministries you lead", "page__title", "site-header", "Scheduling periods"]) {
      expect(landing).not.toContain(leak);
    }
  });

  it("34e. the home page assumes an actor rather than handling a signed-out state", () => {
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));

    expect(home).toContain("useActor");
    expect(home).not.toContain("getMe");
    expect(home).not.toContain("unauthenticated");
    expect(home).not.toContain("SIGN_IN_PATH");
  });

  it("34f. a volunteer's home exposes no ministry-head or admin control", () => {
    // Task 79 removed the `VolunteerHome` function: home now composes
    // sections rather than choosing between whole screens, and a volunteer's
    // gates are all false (`homeView.test.ts`). So what is checked here is
    // the part of the page rendered *before any gate* -- which is all a
    // volunteer ever reaches.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));
    const gate = home.indexOf("sections.ledMinistries");
    expect(gate).toBeGreaterThan(0);
    const ungated = home.slice(0, gate);

    for (const leak of ["/periods", "/roles", "button--primary", "<Link"]) {
      expect(ungated).not.toContain(leak);
    }
  });

  it("34g. a ministry card on home offers one way in, and it is the periods list", () => {
    // Task 78. The card used to carry a second link, `Roles and qualifications`,
    // whose href was `/ministries/${id}/roles` -- character for character the
    // href of the scheduling workflow's third step. Two buttons, one screen.
    // Home is now the ministry and its periods; roles stay in the workflow,
    // which is where a head is looking when roles matter.
    //
    // Scoped to the led-ministries section since Task 79: home grew an
    // administration section with its own link, and a whole-file link count
    // would now be measuring two different things at once.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));
    const led = home.slice(
      home.indexOf("function LedMinistries"),
      home.indexOf("function Administration"),
    );

    expect(led).toContain("/periods");
    expect(led).not.toContain("/roles");
    // And exactly one link out of a card, not two.
    expect([...led.matchAll(/<Link\b/g)]).toHaveLength(1);
  });

  it("34h. the roles screen is still reachable, from the workflow", () => {
    // Removing the duplicate must not have removed the feature: the step
    // survives on the period card with the same ministry-level href.
    const periods = codeOf(join(SOURCE_ROOT, "app/ministries/[ministryId]/periods/page.tsx"));

    expect(periods).toContain("Roles and qualifications");
    expect(periods).toContain("/roles");
  });
});


// -- 35: what an unauthenticated stranger may learn ------------------------

describe("the public surface reveals nothing about the application", () => {
  /** Words that would tell a stranger what this is, or how access works. */
  const REVEALING = [
    "Volunteer Scheduling", "volunteer", "schedul", "ministr", "roster",
    "administrator", "admin", "link your", "email address", "leader", "role",
  ];

  it("35a. the landing screen names the church and nothing else", () => {
    const landing = codeOf(join(SOURCE_ROOT, "components/SignInLanding.tsx"));
    // Only the copy matters, not the prose in the file's own comments, so the
    // comments are stripped before looking.
    const copy = landing
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/\/\/.*$/gm, "");

    expect(copy).toContain("BRAND_CHURCH_NAME");
    expect(copy).not.toContain("BRAND_APP_NAME");
    for (const word of REVEALING) {
      expect(copy.toLowerCase(), word).not.toContain(word.toLowerCase());
    }
  });

  it("35b. the public document metadata names the church and carries no description", () => {
    // The <title> and meta description are served to everyone -- link
    // previews, crawlers, the tab strip -- long before anyone signs in.
    const layout = codeOf(join(SOURCE_ROOT, "app/layout.tsx"));
    const meta = layout.slice(
      layout.indexOf("export const metadata"),
      layout.indexOf("export default"),
    );

    expect(meta).toContain("BRAND_CHURCH_NAME");
    expect(meta).not.toContain("BRAND_DOCUMENT_TITLE");
    expect(meta).not.toMatch(/description:/);
  });

  it("35c. the full title is restored only inside the authenticated shell", () => {
    const header = codeOf(join(SOURCE_ROOT, "components/SiteHeader.tsx"));
    expect(header).toContain("BRAND_DOCUMENT_TITLE");
    expect(header).toContain("document.title");
  });

  it("35d. one refusal message serves every reason", () => {
    // An unlinked account, a deactivated person and a replayed callback must
    // be indistinguishable from outside.
    const auth = codeOf(join(SOURCE_ROOT, "lib/auth.ts"));

    expect(auth).toContain("ACCESS_UNAVAILABLE_MESSAGE");
    // No per-reason table may come back.
    expect(auth).not.toMatch(/not_linked\s*:/);
    expect(auth).not.toMatch(/email_not_verified\s*:/);
    expect(auth).not.toMatch(/Record<string, string>/);
  });
});


// -- 36: My upcoming schedule (Task 77) -------------------------------------

describe("my upcoming schedule", () => {
  it("36a. every signed-in role sees their own schedule, and sees it first", () => {
    // A ministry head serves on the rota they build; an admin is a person too.
    // "Where am I expected?" is the question anybody opening this page has.
    //
    // **One unconditional render since Task 79**, not one per view. Home now
    // composes sections rather than choosing between three whole screens, so
    // the schedule is rendered once, outside every gate -- which is a stronger
    // statement than three copies were: there is no branch that can omit it.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));

    expect([...home.matchAll(/<MySchedule \/>/g)]).toHaveLength(1);
    // And it precedes every management section.
    for (const section of ["sections.ledMinistries", "sections.allMinistries", "sections.administration"]) {
      expect(home.indexOf("<MySchedule />")).toBeLessThan(home.indexOf(section));
    }
  });

  it("36b. every management section on home is gated on the actor's sections", () => {
    // A volunteer's `homeSectionsFor` answers false to all three (pinned in
    // `homeView.test.ts`), so gating every one of them is what makes their
    // home the schedule and nothing else. There is no longer a
    // `VolunteerHome` function to inspect, because there is no longer a
    // volunteer *screen* -- only the absence of sections.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));

    for (const section of ["LedMinistries", "AllMinistries", "Administration"]) {
      // Each rendered exactly once, and each immediately behind its gate.
      expect([...home.matchAll(new RegExp(`<${section}\\b`, "g"))]).toHaveLength(1);
    }
    expect(home).toContain("sections.ledMinistries && (");
    expect(home).toContain("sections.allMinistries && <AllMinistries />");
    expect(home).toContain("sections.administration && <Administration />");
  });

  it("36c. the placeholder it replaced is gone", () => {
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));

    expect(home).not.toContain("Nothing to show you yet");
    expect(home).not.toContain("There is no volunteer view");
  });

  it("36d. the client call takes no subject and no endpoint accepts one", () => {
    const client = codeOf(join(SOURCE_ROOT, "lib/api/client.ts"));

    expect(client).toContain('"/api/v1/me/schedule"');
    // No person id is interpolated into the path, and none is passed.
    expect(client).not.toMatch(/me\/schedule[^"]*\$\{/);
    expect(client).toMatch(/getMySchedule\(signal\?: AbortSignal\)/);
  });

  it("36e. an unconfirmed commitment is labelled, never shown as settled", () => {
    const component = codeOf(join(SOURCE_ROOT, "components/MySchedule.tsx"));

    expect(component).toContain("is_confirmed");
    expect(component).toContain("Not confirmed yet");
  });
});


// -- 53: the people directory (Task 79) ------------------------------------

describe("the people directory is gated, compact and never a delete", () => {
  const header = () => codeOf(join(SOURCE_ROOT, "components/SiteHeader.tsx"));
  const directory = () => codeOf(join(SOURCE_ROOT, "app/people/page.tsx"));
  const detail = () => codeOf(join(SOURCE_ROOT, "app/people/[personId]/page.tsx"));

  it("53. the People navigation is rendered only for an admin or a ministry head", () => {
    // The gate is a call to the one tested decision function, not a condition
    // written inline here -- so there is exactly one place this rule lives in
    // the browser, and `peopleAccess.test.ts` is where it is proven.
    expect(header()).toContain("canViewPeople(actor)");
    expect(header()).toContain('href="/people"');
    expect(header()).not.toMatch(/is_admin\s*\?/);
  });

  it("53b. the nav link is inside the authenticated shell, not the public layout", () => {
    // A signed-out visitor's streamed payload must not carry the word.
    const layout = codeOf(join(SOURCE_ROOT, "app/layout.tsx"));
    expect(layout).not.toContain("/people");
    expect(layout).not.toContain("People");

    const landing = codeOf(join(SOURCE_ROOT, "components/SignInLanding.tsx"));
    expect(landing).not.toContain("/people");
    expect(landing.toLowerCase()).not.toContain("directory");
  });

  it("53c. both people screens check authorization before rendering anything", () => {
    for (const source of [directory(), detail()]) {
      expect(source).toContain("canViewPeople(actor)");
    }
    // And the detail screen does not even issue the request when refused.
    expect(detail()).toMatch(/if \(!hasValidId \|\| !canViewPeople\(actor\)\) return;/);
  });

  it("53d. home stays focused — the people table is not on it", () => {
    // Task 79 gave an administrator a link to the directory; it must stay a
    // link. The whole church's membership roll does not belong on a home
    // screen, and somebody opening this page is usually here for their own
    // schedule.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));

    expect(home).toContain("/people");
    // The table itself is not: home fetches nobody and renders no row.
    expect(home).not.toContain("getPeople");
    expect(home).not.toContain("DirectoryPerson");
    expect(home).not.toContain("<table");
  });

  it("53e. a volunteer's home is still free of every people control", () => {
    // Task 78's guard, re-stated for Task 79's composition: there is no
    // longer a volunteer *branch* to inspect, so what is checked instead is
    // that everything a volunteer must not see lives inside a gated section
    // -- and `homeView.test.ts` pins that a volunteer's gates are all false.
    const home = codeOf(join(SOURCE_ROOT, "components/HomeMinistries.tsx"));
    const ungated = home.slice(0, home.indexOf("sections.ledMinistries"));

    for (const leak of ["/people", "/periods", "People", "Directory", "button--primary"]) {
      expect(ungated).not.toContain(leak);
    }
    // The one thing rendered before any gate is their own schedule.
    expect(ungated).toContain("<MySchedule />");
  });

  it("53f. the all-ministries list is a read, and offers no operational control", () => {
    // Task 79 §4: an administrator may inspect every ministry and must not be
    // handed the controls that belong to whoever leads it.
    const all = codeOf(join(SOURCE_ROOT, "components/AllMinistries.tsx"));

    expect(all).toContain("getMinistries");
    for (const write of [
      "addPersonToMinistry",
      "removePersonFromMinistry",
      "generateSchedule",
      "createMinistryRole",
      "setStaffingRequirement",
      "setMinistryHead",
    ]) {
      expect(all).not.toContain(write);
    }
  });

  it("54. nothing in the people screens is called a delete", () => {
    // Task 79 is explicit: do not present either removal as "Delete" while
    // data and history remain -- and they always do.
    for (const source of [directory(), detail()]) {
      expect(source.toLowerCase()).not.toContain("delete");
    }
  });

  it("54b. the two removals are worded as the different acts they are", () => {
    // Both labels come from the shared module, so the wording exists once and
    // the screen cannot drift from what the tests pin.
    expect(detail()).toContain("removeFromMinistryLabel");
    expect(detail()).toContain("DEACTIVATE_PERSON_LABEL");
    // And neither wording is written a second time as a literal.
    expect(detail()).not.toMatch(/>\s*Remove from \w/);
    expect(detail()).not.toMatch(/>\s*Deactivate person/);
  });

  it("54c. each removal explains what survives it", () => {
    expect(detail()).toContain("REMOVE_FROM_MINISTRY_EXPLANATION");
    expect(detail()).toContain("DEACTIVATE_PERSON_EXPLANATION");
  });

  it("55. person and membership are separate sections, not one blurred record", () => {
    const source = detail();
    expect(source).toContain("Church-wide person");
    expect(source).toContain("Ministry memberships");
    // Two sections, each with its own labelled heading.
    expect(source).toContain('aria-labelledby="church-wide-heading"');
    expect(source).toContain('aria-labelledby="memberships-heading"');
  });

  it("55b. the church-wide controls are gated on the admin-only decision", () => {
    expect(detail()).toContain("canManagePersonRecord(actor)");
    // Removing one membership is a different question, asked separately.
    expect(detail()).toContain("canRemoveMembership(actor, membership)");
  });

  it("56. creating a person requires an explicit second decision", () => {
    const source = directory();
    // The acknowledgement is sent only from its own differently-worded
    // button; pressing the same control twice is a double-click, not a
    // decision.
    expect(source).toContain("acknowledge_duplicate_name");
    expect(source).toContain("submit(true)");
    expect(source).toContain("submit(false)");
    expect(source).toContain("Create anyway");
  });

  it("56b. nothing in the browser merges, fuzzy-matches or picks a person", () => {
    for (const path of FILES) {
      const code = codeOf(path).toLowerCase();
      for (const forbidden of ["fuzzy", "levenshtein", "similarity", "automerge", "mergeperson"]) {
        expect(code, path).not.toContain(forbidden);
      }
    }
  });

  it("57. the directory searches by name and shows the active/inactive state", () => {
    const source = directory();
    expect(source).toContain("getPeople");
    expect(source).toContain("includeInactive");
    expect(source).toContain("Active");
    expect(source).toContain("Inactive");
  });

  it("57b. the directory table has the columns Task 79 asked for", () => {
    const source = directory();
    for (const column of ["Name", "Church status", "Ministries", "Authority", "Status"]) {
      expect(source).toContain(`<th scope="col">${column}</th>`);
    }
    // The serving column's heading comes from the shared constant, never a
    // second literal, so the one term cannot drift between screens.
    expect(source).toContain("{RECORDED_SERVING_LABEL}");
  });

  it("57c. the directory shows a recorded-serving total, and a church status", () => {
    const source = directory();

    expect(source).toContain("recordedServingText(person.recorded_serving_total)");
    expect(source).toContain("churchStatusLabel(person.church_membership_status)");
  });

  it("57d. the serving number is never called attendance or 'times served'", () => {
    // Task 79 §13. The domain records who was *scheduled*; nothing records
    // who turned up, so no screen may claim otherwise.
    const access = readFileSync(join(SOURCE_ROOT, "lib/peopleAccess.ts"), "utf8");
    expect(access).toContain('RECORDED_SERVING_LABEL = "Recorded serving"');

    for (const path of FILES) {
      const code = codeOf(path).toLowerCase();
      for (const claim of ["times served", "attended", "actually served"]) {
        expect(code, `${path} claims ${claim}`).not.toContain(claim);
      }
    }

    // The one place the word 'attendance' may appear is the sentence that
    // denies it, kept beside the label so the two cannot drift apart.
    expect(access).toContain("It does not record attendance.");
  });

  it("57e. the person detail shows the per-ministry breakdown, and the total", () => {
    const source = detail();

    expect(source).toContain("person.recorded_serving_total");
    expect(source).toContain("serving.by_ministry");
    expect(source).toContain("entry.ministry_name");
    expect(source).toContain("entry.count");
  });

  it("57f. a same-Sunday cross-ministry conflict is reported, not folded into the number", () => {
    // §13: one person serves at most one ministry on a Sunday, so two is a
    // contradiction in the record rather than two services.
    const source = detail();

    expect(source).toContain("same_date_conflicts");
    expect(source).toContain("notice--warning");
  });

  it("57g. church membership status is Admin-only to change, and never inferred", () => {
    // §6. A ministry head reads it; only an administrator sets it. And it
    // lives in the church-wide section, deliberately far from the ministry
    // memberships table.
    const source = detail();

    expect(source).toContain("canChangeChurchStatus(actor) && (");
    expect(source).toContain("setChurchMembershipStatus");
    expect(source.indexOf("ChurchStatusControl")).toBeLessThan(
      source.indexOf("function MembershipsSection"),
    );
  });

  it("57h. an administrator who leads nothing gets no ministry roster control", () => {
    // Task 79 §1/§19: seeing every ministry is not running one. The decision
    // is `canManageMinistry`/`managedMinistries`, which `peopleAccess.test.ts`
    // pins to head memberships alone; what is checked here is that the
    // screens ask those questions rather than `is_admin`.
    const access = readFileSync(join(SOURCE_ROOT, "lib/peopleAccess.ts"), "utf8");
    const managing = access.slice(
      access.indexOf("export function canManageMinistry"),
      access.indexOf("export function canRemoveMembership"),
    );
    expect(managing).not.toContain("is_admin");

    const anyMinistry = access.slice(
      access.indexOf("export function canManageAnyMinistry"),
      access.indexOf("export function canManageMinistry"),
    );
    expect(anyMinistry).not.toContain("is_admin");
  });

  it("57i. appointing a ministry head is the one governance route, and it is Admin-only", () => {
    // §10: person, ministry and direction, all explicit. And it is what lets
    // a brand-new ministry acquire its first head, which is why it does not
    // address a membership.
    const source = detail();

    expect(source).toContain("canManagePersonRecord(actor) && (");
    expect(source).toContain("<AppointHeadElsewhere");
    expect(source).toContain("setMinistryHead(");
    expect(source).not.toContain("grantMinistryHead");
    expect(source).not.toContain("revokeMinistryHead");
  });

  it("58. no privacy field this domain does not have is collected or shown", () => {
    // Task 79 §9: gender, birthday, child, affinity and avoidance are
    // concepts from a different product. Nothing in this app asks for one,
    // renders one, or has a type carrying one.
    const forbidden = [
      "gender", "birthday", "birthdate", "birth_date", "date_of_birth",
      "affinity", "affinities", "avoidance", "avoidances", "is_child",
    ];
    for (const path of FILES) {
      const code = codeOf(path).toLowerCase();
      for (const field of forbidden) {
        expect(code, `${path} mentions ${field}`).not.toContain(field);
      }
    }
  });

  it("58b. the person DTOs carry exactly the fields the backend sends", () => {
    const types = readFileSync(join(SOURCE_ROOT, "lib/api/types.ts"), "utf8");
    const fieldsOf = (start: string, end: string) => {
      const block = types.slice(types.indexOf(start), types.indexOf(end));
      return [...block.matchAll(/^\s{2}(\w+)[?]?:/gm)].map((match) => match[1]).sort();
    };

    // The listing row. Task 79 added formal church membership and the
    // recorded-serving total, both facts the backend stores -- and **removed**
    // the contact details, which the table never showed and which the backend
    // no longer sends for every person in the church.
    expect(
      fieldsOf("export interface DirectoryPerson", "export interface PersonDetail"),
    ).toEqual([
      "church_membership_status", "deactivated_at", "display_name",
      "is_admin", "memberships", "person_id", "recorded_serving_total",
    ]);

    // The detail read adds exactly three: the two contact fields an
    // administrator managing sign-in access needs, and the per-ministry
    // serving breakdown. Still no gender, birthday, age, child flag, affinity
    // or avoidance anywhere -- the domain has none of them and nothing in
    // scheduling reads one.
    expect(
      fieldsOf("export interface PersonDetail", "export interface PeopleDirectory"),
    ).toEqual(["email", "phone", "serving"]);
  });

  it("58c. the directory listing never asks for anybody's contact details", () => {
    // Task 79 §5. The whole church's roll is readable by every ministry head;
    // a screen that shows no address has no business receiving one for every
    // person in the church. The fields are absent rather than null, so a row
    // cannot be read as "they have no address".
    const directorySource = directory();

    expect(directorySource).not.toContain("person.email");
    expect(directorySource).not.toContain("person.phone");
  });

  it("59. permission failures render through the shared error notice", () => {
    // A 403 must look like a refusal a person can read, not a blank screen or
    // a raw status code.
    for (const source of [directory(), detail()]) {
      expect(source).toContain("ErrorNotice");
      expect(source).toContain("asApiError");
      expect(source).not.toMatch(/status\s*===\s*403/);
    }
  });

  it("59b. neither screen inspects a status code to decide what to show", () => {
    // Components ask what *kind* of failure it was; `lib/api/errors.ts` is the
    // only place a status becomes a kind.
    for (const source of [directory(), detail()]) {
      expect(source).not.toMatch(/\.status\s*===\s*\d/);
    }
  });
});
