import { describe, expect, it } from "vitest";

import { THEME_STORAGE_KEY, themeBootstrapScript, themeFromStoredValue } from "./theme";

describe("themeFromStoredValue", () => {
  it("only the exact string 'dark' means dark", () => {
    expect(themeFromStoredValue("dark")).toBe("dark");
  });

  it("everything else -- absence, garbage, a stray 'system' -- is light", () => {
    for (const value of [null, "", "light", "system", "DARK", "true"]) {
      expect(themeFromStoredValue(value)).toBe("light");
    }
  });
});

describe("themeBootstrapScript", () => {
  /** A minimal stand-in for `document.documentElement` and `localStorage`,
   *  so the script's actual behaviour is exercised -- not just its text. */
  function runScript(script: string, storedValue: string | null) {
    const calls: { setAttribute: [string, string][] } = { setAttribute: [] };
    const documentElement = {
      setAttribute: (name: string, value: string) => calls.setAttribute.push([name, value]),
      style: {} as Record<string, string>,
    };
    const fakeLocalStorage = {
      getItem: (key: string) => (key === THEME_STORAGE_KEY ? storedValue : null),
    };
    const fn = new Function("document", "localStorage", script);
    fn({ documentElement }, fakeLocalStorage);
    return { calls, documentElement };
  }

  it("sets data-theme=dark and the native colour-scheme when dark was stored", () => {
    const { calls, documentElement } = runScript(themeBootstrapScript(THEME_STORAGE_KEY), "dark");
    expect(calls.setAttribute).toEqual([["data-theme", "dark"]]);
    expect(documentElement.style.colorScheme).toBe("dark");
  });

  it("does nothing when nothing was stored", () => {
    const { calls, documentElement } = runScript(themeBootstrapScript(THEME_STORAGE_KEY), null);
    expect(calls.setAttribute).toEqual([]);
    expect(documentElement.style.colorScheme).toBeUndefined();
  });

  it("does nothing for a stored value that is not exactly 'dark'", () => {
    const { calls } = runScript(themeBootstrapScript(THEME_STORAGE_KEY), "light");
    expect(calls.setAttribute).toEqual([]);
  });

  it("never throws when localStorage itself throws (private browsing, a locked-down browser)", () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error("access denied");
      },
    };
    const calls: string[] = [];
    const documentElement = { setAttribute: () => calls.push("called"), style: {} };
    const fn = new Function("document", "localStorage", themeBootstrapScript(THEME_STORAGE_KEY));
    expect(() => fn({ documentElement }, throwingStorage)).not.toThrow();
    expect(calls).toEqual([]);
  });

  it("embeds the exact storage key this app reads and writes elsewhere", () => {
    expect(themeBootstrapScript(THEME_STORAGE_KEY)).toContain(THEME_STORAGE_KEY);
  });
});
