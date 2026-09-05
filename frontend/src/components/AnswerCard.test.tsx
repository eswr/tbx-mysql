import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import type { Evidence } from "../api/types";
import { chatResponse } from "../test/factories";
import { AnswerCard } from "./AnswerCard";

function evidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    how_calculated: {
      date_range: "2026-08-01 to 2026-08-31",
      operation: "SUM(transaction_amount)",
      records_matched: 2,
      filters_applied: {},
      sql: null,
      cache_hit: false,
    },
    source: "transaction",
    grounded: true,
    breakdown: null,
    records: null,
    records_truncated: false,
    comparison_of: null,
    ...overrides,
  };
}

it("renders a hard refusal with suggestions and supported capabilities", () => {
  render(
    <AnswerCard
      response={chatResponse({
        answer: "Unsupported request",
        matched_count: null,
        refusal: {
          reason: "capability",
          message: "Unsupported request",
          suggestions: ["Ask for a balance"],
          supported_capabilities: ["Metrics: balance"],
        },
      })}
    />,
  );
  expect(screen.getByText("capability")).toBeVisible();
  expect(screen.getByText("Ask for a balance")).toBeVisible();
  expect(screen.getByText("What is supported")).toBeVisible();
});

it("keeps grounded evidence for no_data", () => {
  render(
    <AnswerCard
      response={chatResponse({
        answer: "No records matched.",
        matched_count: 0,
        evidence: evidence({ how_calculated: { ...evidence().how_calculated, records_matched: 0 } }),
        refusal: { reason: "no_data", message: "No records matched.", suggestions: [], supported_capabilities: null },
      })}
    />,
  );
  expect(screen.getByText(/grounded in 0 records/i)).toBeVisible();
  expect(screen.getByText(/view how this was calculated/i)).toBeVisible();
});

it("uses group wording and displays the actual engine", async () => {
  const user = userEvent.setup();
  render(
    <AnswerCard
      response={chatResponse({
        matched_count: 2,
        evidence: evidence({
          source: "account",
          breakdown: [
            { key: "HDFC", label: "HDFC Bank", value: "9007199254740993.01", count: null },
            { key: "SBIN", label: "State Bank of India", value: "2", count: null },
          ],
        }),
        meta: { engine: "duckdb" },
      })}
    />,
  );
  expect(screen.getByText(/grounded in 2 groups/i)).toBeVisible();
  await user.click(screen.getByText(/view how this was calculated/i));
  expect(screen.getByText("Groups matched:")).toBeVisible();
  expect(screen.getByText(/Source: DUCKDB — account table/)).toBeVisible();
  expect(screen.getByText("9007199254740993.01")).toBeVisible();
});

it("formats comparison decimal strings without losing precision", async () => {
  const user = userEvent.setup();
  render(
    <AnswerCard
      response={chatResponse({
        evidence: evidence({ comparison_of: { current: "9007199254740993.01", comparison: "1.2" } }),
      })}
    />,
  );
  await user.click(screen.getByText(/view how this was calculated/i));
  expect(screen.getByText("₹9,00,71,99,25,47,40,993.01")).toBeVisible();
  expect(screen.getByText("₹1.20")).toBeVisible();
});
