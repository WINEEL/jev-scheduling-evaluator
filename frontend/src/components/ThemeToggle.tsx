"use client";

/**
 * The one control that switches between light (the default) and dark.
 *
 * The inline bootstrap script in the root layout already set `data-theme`
 * on `<html>`, before this component -- or any of React -- ever ran, so a
 * hard page load never shows the wrong theme even for a moment. This
 * component's own state reads that same attribute straight off the DOM in
 * its `useState` initializer, which the server-rendered pass cannot do
 * (there is no DOM yet, so it falls back to "light" there); the resulting
 * one-element text difference between server and client is exactly what
 * `suppressHydrationWarning` on the button exists for, and is the
 * React-documented shape for a theme toggle like this one.
 */

import { useState } from "react";

import { THEME_STORAGE_KEY, type Theme } from "@/lib/theme";

function currentTheme(): Theme {
  if (typeof document === "undefined") return "light";
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.style.colorScheme = theme;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);

  function handleClick() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    applyTheme(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Best-effort only -- a browser that refuses storage still gets a
      // working toggle for the rest of this session, it just will not be
      // remembered next time.
    }
  }

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={handleClick}
      aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
      suppressHydrationWarning
    >
      {theme === "dark" ? "🌙 Dark" : "☀️ Light"}
    </button>
  );
}
