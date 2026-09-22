/**
 * The browser's half of sign-in (Task 76).
 *
 * There is very little to test here, and that is the design: every security
 * decision is made on the server. What remains is a URL and one sentence.
 *
 * The privacy pass turned the refusal messages into a single full stop, so
 * these tests now assert the *absence* of explanation as much as its presence.
 */

import { describe, expect, it } from "vitest";

import { ACCESS_UNAVAILABLE_MESSAGE, SIGN_IN_PATH, wasSignInRefused } from "./auth";

describe("sign-in URL", () => {
  it("is same-origin, so the browser never learns the backend's address", () => {
    expect(SIGN_IN_PATH.startsWith("/api/backend/")).toBe(true);
    expect(SIGN_IN_PATH).not.toContain("http");
  });

  it("points at the backend's login route", () => {
    expect(SIGN_IN_PATH).toBe("/api/backend/api/v1/auth/google/login");
  });
});

describe("refusal reporting", () => {
  it("says nothing when there was no refusal", () => {
    expect(wasSignInRefused(null)).toBe(false);
    expect(wasSignInRefused(undefined)).toBe(false);
    expect(wasSignInRefused("")).toBe(false);
  });

  it("reports a refusal for every reason the backend can send", () => {
    for (const reason of [
      "not_linked",
      "email_not_verified",
      "person_inactive",
      "invalid_response",
      "not_configured",
    ]) {
      expect(wasSignInRefused(reason), reason).toBe(true);
    }
  });

  it("still reports a refusal for a code this build does not recognise", () => {
    // The backend said sign-in failed. Rendering a normal signed-out page
    // because the code was unfamiliar would hide that.
    expect(wasSignInRefused("something_new")).toBe(true);
  });

  it("is the same message whatever the reason, so nothing can be inferred", () => {
    // The whole point of the privacy pass: an unlinked account, a deactivated
    // person and a replayed callback must be indistinguishable from outside.
    expect(ACCESS_UNAVAILABLE_MESSAGE).toBe("Access unavailable.");
  });

  it("the public message explains nothing about the organisation", () => {
    const forbidden = [
      "administrator", "admin", "link", "email", "account", "ministry",
      "ministries", "schedul", "volunteer", "leader", "role", "deactivat",
      "verify", "verified", "configured",
    ];
    for (const word of forbidden) {
      expect(ACCESS_UNAVAILABLE_MESSAGE.toLowerCase()).not.toContain(word);
    }
  });

  it("exposes no per-reason wording for a component to start explaining with", () => {
    // `wasSignInRefused` returns a boolean on purpose. A function that handed
    // back the reason would invite exactly the disclosure this removed.
    expect(typeof wasSignInRefused("not_linked")).toBe("boolean");
  });
});
