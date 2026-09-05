import { describe, expect, it } from "vitest";
import { formatAmount, formatCount, formatDate } from "./format";

describe("formatAmount", () => {
  it.each([
    ["0", "₹0.00"],
    ["12.3", "₹12.30"],
    ["12345678.99", "₹1,23,45,678.99"],
    ["-0012345.6", "-₹12,345.60"],
    ["+42.", "+₹42.00"],
    ["1.2345", "₹1.2345"],
    ["9007199254740993.01", "₹9,00,71,99,25,47,40,993.01"],
  ])("formats %s without precision loss", (input, expected) => {
    expect(formatAmount(input)).toBe(expected);
  });

  it("returns non-decimal input unchanged", () => {
    expect(formatAmount("not available")).toBe("not available");
  });
});

it("formats counts and dates", () => {
  expect(formatCount(1234567)).toBe("12,34,567");
  expect(formatDate("2026-09-05T00:00:00")).toMatch(/05.*Sep.*2026/i);
  expect(formatDate("invalid")).toBe("invalid");
});
