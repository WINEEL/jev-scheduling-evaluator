/**
 * Who sees the People screen, and what they may do on it (Task 79).
 *
 * Every rule here is enforced again in FastAPI, and the backend's own suites
 * prove that. What these tests protect is the *other* half: that the UI tells
 * the truth -- no navigation to a screen that would only show a refusal, and
 * no button that promises an act the API will reject.
 *
 * Three actors run through the whole file, because the rules are almost
 * entirely about telling them apart:
 *
 * - an **Admin**, who may change the church-wide record;
 * - a **ministry head**, who may change memberships of ministries they lead
 *   and nothing else;
 * - a **volunteer**, who sees none of it.
 */

import { describe, expect, it } from "vitest";

import type { DirectoryPerson, PersonMembership } from "./api/types";
import {
  DEACTIVATE_PERSON_LABEL,
  REACTIVATE_PERSON_LABEL,
  activeMinistryNames,
  authorityLabel,
  CHURCH_STATUS_ORDER,
  RECORDED_SERVING_LABEL,
  canChangeChurchStatus,
  canCreatePerson,
  canManageAnyMinistry,
  canManageMinistry,
  canManagePersonRecord,
  canRemoveMembership,
  canSeeAllMinistries,
  canViewPeople,
  churchStatusLabel,
  recordedServingText,
  headedMinistryNames,
  isMembershipActive,
  isPersonActive,
  managedMinistries,
  ministriesAvailableFor,
  removeFromMinistryLabel,
} from "./peopleAccess";

const AV = 100;
const KIDS = 200;
const SETUP = 300;

const ADMIN = { is_admin: true, headed_ministries: [] };
const ADMIN_WHO_LEADS = {
  is_admin: true,
  headed_ministries: [{ ministry_id: AV, name: "AV" }],
};
const AV_HEAD = {
  is_admin: false,
  headed_ministries: [{ ministry_id: AV, name: "AV" }],
};
const TWO_MINISTRY_HEAD = {
  is_admin: false,
  headed_ministries: [
    { ministry_id: AV, name: "AV" },
    { ministry_id: KIDS, name: "Kids" },
  ],
};
const VOLUNTEER = { is_admin: false, headed_ministries: [] };

function membership(overrides: Partial<PersonMembership> = {}): PersonMembership {
  return {
    ministry_membership_id: 700,
    ministry_id: AV,
    ministry_name: "AV",
    is_ministry_head: false,
    deactivated_at: null,
    notes: null,
    joined_on: null,
    ...overrides,
  };
}

function person(overrides: Partial<DirectoryPerson> = {}): DirectoryPerson {
  return {
    person_id: 1,
    display_name: "Sam Taylor",
    is_admin: false,
    church_membership_status: "UNKNOWN",
    deactivated_at: null,
    memberships: [],
    recorded_serving_total: 0,
    ...overrides,
  };
}

// -- who sees the screen at all --------------------------------------------

describe("who may see the People directory", () => {
  it("an admin may", () => {
    expect(canViewPeople(ADMIN)).toBe(true);
  });

  it("a ministry head may — the directory is church-wide on purpose", () => {
    // This is how a head finds an existing person instead of creating a
    // second copy of one, so restricting them to their own team would
    // guarantee the duplicates the whole design exists to prevent.
    expect(canViewPeople(AV_HEAD)).toBe(true);
  });

  it("a volunteer may not", () => {
    expect(canViewPeople(VOLUNTEER)).toBe(false);
  });

  it("heading any ministry is enough — which one does not matter", () => {
    expect(canViewPeople({ is_admin: false, headed_ministries: [{ ministry_id: SETUP, name: "Setup" }] })).toBe(true);
  });
});

// -- the church-wide record ------------------------------------------------

describe("who may change the church-wide person record", () => {
  it("an admin may", () => {
    expect(canManagePersonRecord(ADMIN)).toBe(true);
  });

  it("a ministry head may not, however many ministries they lead", () => {
    // The single most important refusal in this task: deactivating somebody
    // church-wide takes them out of every ministry's pool, including ones the
    // head has nothing to do with.
    expect(canManagePersonRecord(AV_HEAD)).toBe(false);
    expect(canManagePersonRecord(TWO_MINISTRY_HEAD)).toBe(false);
  });

  it("a volunteer may not", () => {
    expect(canManagePersonRecord(VOLUNTEER)).toBe(false);
  });

  it("an admin who also heads a ministry is still an admin", () => {
    expect(canManagePersonRecord(ADMIN_WHO_LEADS)).toBe(true);
  });
});

// -- ministry-scoped writes ------------------------------------------------

describe("which ministries an actor may change", () => {
  it("a head manages the ones they lead", () => {
    expect(canManageMinistry(AV_HEAD, AV)).toBe(true);
  });

  it("a head does not manage anybody else's", () => {
    expect(canManageMinistry(AV_HEAD, KIDS)).toBe(false);
  });

  it("a head of two manages both and no third", () => {
    expect(canManageMinistry(TWO_MINISTRY_HEAD, AV)).toBe(true);
    expect(canManageMinistry(TWO_MINISTRY_HEAD, KIDS)).toBe(true);
    expect(canManageMinistry(TWO_MINISTRY_HEAD, SETUP)).toBe(false);
  });

  it("an admin who leads nothing manages none of them", () => {
    // **The Task 79 change.** Rostering a team belongs to whoever runs it;
    // being an administrator means seeing every ministry, not running them.
    // The backend refuses them too, so a control shown here would only lead
    // to a 403.
    expect(canManageMinistry(ADMIN, SETUP)).toBe(false);
    expect(canManageMinistry(ADMIN, AV)).toBe(false);
  });

  it("an admin who heads a ministry manages that one and no other", () => {
    // And through the head membership, not the admin flag -- which is why the
    // second assertion is false.
    expect(canManageMinistry(ADMIN_WHO_LEADS, AV)).toBe(true);
    expect(canManageMinistry(ADMIN_WHO_LEADS, SETUP)).toBe(false);
  });

  it("a volunteer manages none", () => {
    expect(canManageMinistry(VOLUNTEER, AV)).toBe(false);
    expect(canManageAnyMinistry(VOLUNTEER)).toBe(false);
  });

  it("an admin who heads nothing manages no ministry at all", () => {
    // Both questions now agree, because the answer is the same one: they lead
    // nothing, so there is no roster of theirs to change. They can still see
    // every ministry, and appoint somebody to lead one.
    expect(canManageAnyMinistry(ADMIN)).toBe(false);
    expect(managedMinistries(ADMIN)).toEqual([]);
  });

  it("a head's managed list is exactly the ministries they lead", () => {
    expect(managedMinistries(TWO_MINISTRY_HEAD).map((m) => m.ministry_id)).toEqual([AV, KIDS]);
  });
});

// -- removing a membership -------------------------------------------------

describe("who may remove which membership", () => {
  it("a head may remove an ordinary member of their own ministry", () => {
    expect(canRemoveMembership(AV_HEAD, membership())).toBe(true);
  });

  it("a head may not remove a member of another ministry", () => {
    expect(canRemoveMembership(AV_HEAD, membership({ ministry_id: KIDS }))).toBe(false);
  });

  it("a head may not remove a co-head, even of their own ministry", () => {
    // Removal necessarily revokes head authority, and revoking is admin-only.
    // Hiding the button is what keeps the screen from promising something the
    // API would refuse.
    expect(canRemoveMembership(AV_HEAD, membership({ is_ministry_head: true }))).toBe(false);
  });

  it("an admin who leads the ministry may remove a head of it", () => {
    // Both halves of the rule at once: removing somebody from a team needs
    // the team's head, and removing a *head* additionally needs an
    // administrator. ADMIN_WHO_LEADS is the only actor who is both.
    expect(
      canRemoveMembership(ADMIN_WHO_LEADS, membership({ is_ministry_head: true })),
    ).toBe(true);
  });

  it("an admin who leads nothing may not remove anybody", () => {
    // Taking authority away is theirs; taking somebody off a team is not.
    expect(canRemoveMembership(ADMIN, membership())).toBe(false);
    expect(canRemoveMembership(ADMIN, membership({ is_ministry_head: true }))).toBe(false);
  });

  it("a volunteer may remove nobody", () => {
    expect(canRemoveMembership(VOLUNTEER, membership())).toBe(false);
  });
});

// -- which ministries an add offers ----------------------------------------

describe("the ministries an add control offers", () => {
  it("excludes ones they are already active in", () => {
    const subject = person({ memberships: [membership({ ministry_id: AV })] });
    expect(ministriesAvailableFor(TWO_MINISTRY_HEAD, subject).map((m) => m.ministry_id)).toEqual([KIDS]);
  });

  it("offers a ministry they were removed from, so rejoining is possible", () => {
    // Adding them back restores the original membership and the serving
    // history hanging off it -- which is the reason to offer it.
    const subject = person({
      memberships: [membership({ ministry_id: AV, deactivated_at: "2026-01-01T00:00:00Z" })],
    });
    expect(ministriesAvailableFor(AV_HEAD, subject).map((m) => m.ministry_id)).toEqual([AV]);
  });

  it("offers an admin nothing to choose from, because nothing lists ministries", () => {
    expect(ministriesAvailableFor(ADMIN, person())).toEqual([]);
  });

  it("offers a volunteer nothing", () => {
    expect(ministriesAvailableFor(VOLUNTEER, person())).toEqual([]);
  });
});

// -- how a row reads -------------------------------------------------------

describe("what a directory row says", () => {
  const subject = person({
    memberships: [
      membership({ ministry_id: AV, ministry_name: "AV", is_ministry_head: true }),
      membership({ ministry_id: KIDS, ministry_name: "Kids" }),
      membership({
        ministry_id: SETUP,
        ministry_name: "Setup",
        deactivated_at: "2026-01-01T00:00:00Z",
      }),
    ],
  });

  it("lists only the ministries they are currently in", () => {
    expect(activeMinistryNames(subject)).toEqual(["AV", "Kids"]);
  });

  it("names only the ones they currently lead", () => {
    expect(headedMinistryNames(subject)).toEqual(["AV"]);
  });

  it("a removed head membership counts as neither", () => {
    const stoodDown = person({
      memberships: [
        membership({
          ministry_name: "AV",
          is_ministry_head: true,
          deactivated_at: "2026-01-01T00:00:00Z",
        }),
      ],
    });
    expect(activeMinistryNames(stoodDown)).toEqual([]);
    expect(headedMinistryNames(stoodDown)).toEqual([]);
  });

  it("calls a head a ministry head", () => {
    expect(authorityLabel(subject)).toBe("Ministry head");
  });

  it("calls an administrator an administrator, even when they also lead", () => {
    expect(authorityLabel({ ...subject, is_admin: true })).toBe("Administrator");
  });

  it("says nothing about an ordinary member", () => {
    expect(authorityLabel(person({ memberships: [membership()] }))).toBeNull();
  });

  it("reports the person's own active state separately from each membership's", () => {
    // Two different facts. A person can be active in the church with every
    // membership removed, or deactivated church-wide with memberships intact.
    const departed = person({ deactivated_at: "2026-01-01T00:00:00Z" });
    expect(isPersonActive(departed)).toBe(false);
    expect(isMembershipActive(membership())).toBe(true);
    expect(isMembershipActive(membership({ deactivated_at: "2026-01-01T00:00:00Z" }))).toBe(false);
  });
});

// -- the wording -----------------------------------------------------------

describe("the wording of the two removals", () => {
  it("removing from a ministry names the ministry", () => {
    expect(removeFromMinistryLabel("Setup")).toBe("Remove from Setup");
  });

  it("neither removal is ever called a deletion", () => {
    // Task 79 is explicit: do not present either operation as "Delete" while
    // data and history remain -- and they always do.
    for (const label of [
      removeFromMinistryLabel("AV"),
      DEACTIVATE_PERSON_LABEL,
      REACTIVATE_PERSON_LABEL,
    ]) {
      expect(label.toLowerCase()).not.toContain("delete");
      expect(label.toLowerCase()).not.toContain("remove permanently");
    }
  });

  it("the church-wide act is not called a removal, and vice versa", () => {
    // They are different acts with different scopes, and using one word for
    // both is how a ministry head ends up deactivating somebody church-wide
    // when they meant to take them off one rota.
    expect(DEACTIVATE_PERSON_LABEL).toBe("Deactivate person");
    expect(removeFromMinistryLabel("AV")).not.toContain("Deactivate");
  });
});

// -- where the duplicate rule actually lives -------------------------------

describe("the duplicate-name rule is the server's, not the browser's", () => {
  it("this module offers no client-side duplicate check", async () => {
    // Deliberate. A browser-side warning could only know about the people the
    // current search happened to load, so its silence would be unreliable
    // while its warnings looked authoritative -- and a control that sometimes
    // warns teaches people to trust it when it does not.
    //
    // `create_person` in the backend refuses every exact same-name creation
    // until a human acknowledges it, on every request and from every client.
    // The screen shows that refusal and offers the acknowledgement; it does
    // not predict it.
    const exported: Record<string, unknown> = await import("./peopleAccess");
    expect(Object.keys(exported)).not.toContain("duplicateNameWarning");
    for (const name of Object.keys(exported)) {
      expect(name.toLowerCase()).not.toContain("duplicate");
    }
  });
});


// -- church-wide oversight (Task 79 §4) -------------------------------------

describe("who sees the All ministries list", () => {
  it("an admin does", () => {
    expect(canSeeAllMinistries(ADMIN)).toBe(true);
    expect(canSeeAllMinistries(ADMIN_WHO_LEADS)).toBe(true);
  });

  it("a ministry head does not", () => {
    // They reach the ministries they lead through their own memberships. A
    // church-wide inventory is a different thing, and the API refuses them.
    expect(canSeeAllMinistries(AV_HEAD)).toBe(false);
    expect(canSeeAllMinistries(TWO_MINISTRY_HEAD)).toBe(false);
  });

  it("a volunteer does not", () => {
    expect(canSeeAllMinistries(VOLUNTEER)).toBe(false);
  });
});

// -- formal church membership status (Task 79 §6) ---------------------------

describe("who may change a church membership status", () => {
  it("an admin may", () => {
    expect(canChangeChurchStatus(ADMIN)).toBe(true);
  });

  it("a ministry head may not, even of a ministry this person serves in", () => {
    // Whether somebody is a member of the church is not a fact about one
    // team's rota.
    expect(canChangeChurchStatus(AV_HEAD)).toBe(false);
  });

  it("a volunteer may not", () => {
    expect(canChangeChurchStatus(VOLUNTEER)).toBe(false);
  });

  it("offers exactly the three statuses the backend accepts", () => {
    expect([...CHURCH_STATUS_ORDER]).toEqual(["MEMBER", "NON_MEMBER", "UNKNOWN"]);
  });

  it("gives Unknown its own word rather than a blank", () => {
    // Nobody having said is a real answer. A blank cell would read as missing
    // data somebody ought to fill in.
    expect(churchStatusLabel("UNKNOWN")).toBe("Unknown");
    expect(churchStatusLabel("MEMBER")).toBe("Member");
    expect(churchStatusLabel("NON_MEMBER")).toBe("Not a member");
  });

  it("is not derived from ministry participation in either direction", () => {
    // Two people with identical memberships and different statuses, and two
    // with identical statuses and different memberships: the label follows
    // the status alone.
    const serving = person({ memberships: [membership()], church_membership_status: "NON_MEMBER" });
    const notServing = person({ memberships: [], church_membership_status: "MEMBER" });

    expect(churchStatusLabel(serving.church_membership_status)).toBe("Not a member");
    expect(churchStatusLabel(notServing.church_membership_status)).toBe("Member");
  });
});

// -- recorded serving (Task 79 §12–§13) -------------------------------------

describe("how recorded serving reads", () => {
  it("uses one term, and it does not claim attendance", () => {
    expect(RECORDED_SERVING_LABEL).toBe("Recorded serving");
    expect(RECORDED_SERVING_LABEL.toLowerCase()).not.toContain("attend");
    expect(RECORDED_SERVING_LABEL.toLowerCase()).not.toContain("times served");
  });

  it("renders nought as a number, never as a dash", () => {
    // They have served no recorded Sundays, which is a fact; a dash would
    // read as "not counted".
    expect(recordedServingText(0)).toBe("0");
    expect(recordedServingText(27)).toBe("27");
  });
});


// -- who may create a person at all -----------------------------------------

describe("who may create a canonical person", () => {
  it("an admin may, even leading no ministry", () => {
    // They administer the church roll itself, so an unattached person is a
    // legitimate thing for them to create. The ministry chooser they are
    // shown is simply empty.
    expect(canCreatePerson(ADMIN)).toBe(true);
  });

  it("a ministry head may — for a team they lead", () => {
    expect(canCreatePerson(AV_HEAD)).toBe(true);
  });

  it("a volunteer may not", () => {
    expect(canCreatePerson(VOLUNTEER)).toBe(false);
  });

  it("is a wider question than managing a roster", () => {
    // The two must not be collapsed: an admin who leads nothing may create a
    // person and may not put anybody on a team.
    expect(canCreatePerson(ADMIN)).toBe(true);
    expect(canManageAnyMinistry(ADMIN)).toBe(false);
  });
});
