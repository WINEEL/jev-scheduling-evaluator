import { describe, expect, it } from "vitest";

import { parsePositiveIntegerField, positiveIntegerFieldText } from "./positiveIntegerField";

describe("parsePositiveIntegerField", () => {
  it("treats blank as valid and meaning no value, never zero", () => {
    expect(parsePositiveIntegerField("")).toEqual({ isValid: true, value: null });
    expect(parsePositiveIntegerField("   ")).toEqual({ isValid: true, value: null });
  });

  it("accepts whole numbers of 1 or more", () => {
    expect(parsePositiveIntegerField("1")).toEqual({ isValid: true, value: 1 });
    expect(parsePositiveIntegerField("5")).toEqual({ isValid: true, value: 5 });
    expect(parsePositiveIntegerField(" 12 ")).toEqual({ isValid: true, value: 12 });
  });

  it("rejects zero -- blank already means 'no value'", () => {
    expect(parsePositiveIntegerField("0").isValid).toBe(false);
  });

  it("rejects negative numbers", () => {
    expect(parsePositiveIntegerField("-1").isValid).toBe(false);
  });

  it("rejects non-integers", () => {
    expect(parsePositiveIntegerField("1.5").isValid).toBe(false);
  });

  it("rejects text that is not a number", () => {
    expect(parsePositiveIntegerField("two").isValid).toBe(false);
    expect(parsePositiveIntegerField("1a").isValid).toBe(false);
  });
});

describe("positiveIntegerFieldText", () => {
  it("is the inverse of parsing: null becomes blank, a number becomes its digits", () => {
    expect(positiveIntegerFieldText(null)).toBe("");
    expect(positiveIntegerFieldText(4)).toBe("4");
  });

  it("round-trips through parsePositiveIntegerField", () => {
    for (const value of [null, 1, 4, 12]) {
      const text = positiveIntegerFieldText(value);
      expect(parsePositiveIntegerField(text)).toEqual({ isValid: true, value });
    }
  });
});
