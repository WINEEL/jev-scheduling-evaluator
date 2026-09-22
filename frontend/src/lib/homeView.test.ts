/**
 * Which sections of home each kind of account gets (Task 79).
 *
 * The product's access story in one table. Two cases matter most:
 *
 * - a **volunteer** must get their schedule and *nothing else*, because every
 *   other section is a management screen and an empty one would imply controls
 *   they do not have;
 * - an **administrator who also heads a ministry** must get both their own
 *   ministries and the church-wide list. The old single-view function could
 *   only return one of those, which is why it was replaced.
 */

import { describe, expect, it } from "vitest";

import { hasManagementSections, homeSectionsFor } from "./homeView";

const SETUP = { ministry_id: 1, name: "Setup" };
const AV = { ministry_id: 2, name: "AV" };

const volunteer = { is_admin: false, headed_ministries: [] };
const head = { is_admin: false, headed_ministries: [SETUP] };
const multiHead = { is_admin: false, headed_ministries: [SETUP, AV] };
const admin = { is_admin: true, headed_ministries: [] };
const adminHead = { is_admin: true, headed_ministries: [SETUP] };

describe("homeSectionsFor", () => {
  it("gives everybody their own schedule", () => {
    for (const actor of [volunteer, head, multiHead, admin, adminHead]) {
      expect(homeSectionsFor(actor).schedule).toBe(true);
    }
  });

  it("gives a volunteer nothing but their schedule", () => {
    const sections = homeSectionsFor(volunteer);

    expect(sections.ledMinistries).toBe(false);
    expect(sections.allMinistries).toBe(false);
    expect(sections.administration).toBe(false);
    expect(hasManagementSections(volunteer)).toBe(false);
  });

  it("gives a ministry head the ministries they lead", () => {
    expect(homeSectionsFor(head).ledMinistries).toBe(true);
    expect(homeSectionsFor(multiHead).ledMinistries).toBe(true);
  });

  it("does not give a ministry head the church-wide list", () => {
    // They reach the ministries they lead through their own memberships; a
    // church-wide inventory is a different thing, and the API refuses them.
    expect(homeSectionsFor(head).allMinistries).toBe(false);
    expect(homeSectionsFor(multiHead).allMinistries).toBe(false);
  });

  it("does not give a ministry head the administration section", () => {
    expect(homeSectionsFor(head).administration).toBe(false);
  });

  it("gives an administrator the church-wide list and administration", () => {
    const sections = homeSectionsFor(admin);

    expect(sections.allMinistries).toBe(true);
    expect(sections.administration).toBe(true);
  });

  it("gives an administrator who heads nothing no led-ministries section", () => {
    // Nothing to put in it, and the church-wide list is where they go instead.
    expect(homeSectionsFor(admin).ledMinistries).toBe(false);
  });

  it("gives an administrator who also heads a ministry both", () => {
    // The case the previous single-view function could not express: it had to
    // pick one, and picking either dropped something the person needs.
    const sections = homeSectionsFor(adminHead);

    expect(sections.ledMinistries).toBe(true);
    expect(sections.allMinistries).toBe(true);
    expect(sections.administration).toBe(true);
  });

  it("decides from authority and membership alone", () => {
    // Same authority, different ministry names -> same sections. Nothing here
    // may depend on what a ministry is called.
    const a = homeSectionsFor({ is_admin: false, headed_ministries: [SETUP] });
    const b = homeSectionsFor({ is_admin: false, headed_ministries: [{ ministry_id: 9, name: "Kids" }] });

    expect(a).toEqual(b);
  });
});

describe("hasManagementSections", () => {
  it("is true for anybody with a section beyond their schedule", () => {
    expect(hasManagementSections(head)).toBe(true);
    expect(hasManagementSections(admin)).toBe(true);
    expect(hasManagementSections(adminHead)).toBe(true);
  });

  it("is false for a volunteer", () => {
    expect(hasManagementSections(volunteer)).toBe(false);
  });
});
