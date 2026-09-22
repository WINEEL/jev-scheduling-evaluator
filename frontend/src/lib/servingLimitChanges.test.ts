import { describe, expect, it } from "vitest";

import { servingLimitChange } from "./servingLimitChanges";

describe("servingLimitChange", () => {
  it("blank where nothing was ever set is not a change -- blank means no limit", () => {
    expect(servingLimitChange(1, "", null)).toBeNull();
  });

  it("blank clearing an existing limit is a clear", () => {
    expect(servingLimitChange(1, "", 3)).toEqual({ kind: "clear", membershipId: 1 });
  });

  it("a positive whole number is editable into a set", () => {
    expect(servingLimitChange(1, "3", null)).toEqual({
      kind: "set", membershipId: 1, maxAssignments: 3,
    });
    expect(servingLimitChange(1, "5", 3)).toEqual({
      kind: "set", membershipId: 1, maxAssignments: 5,
    });
  });

  it("a number matching the saved value is not a change", () => {
    expect(servingLimitChange(1, "3", 3)).toBeNull();
  });

  it("an invalid draft is never a change", () => {
    expect(servingLimitChange(1, "0", 3)).toBeNull();
    expect(servingLimitChange(1, "-2", 3)).toBeNull();
    expect(servingLimitChange(1, "many", 3)).toBeNull();
  });
});
