import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { DatabaseStatusState } from "../hooks/useDatabaseStatus";
import { capabilitiesResponse, healthResponse } from "../test/factories";
import { DatabaseStatus, DatabaseStatusAlert } from "./DatabaseStatus";

function state(overrides: Partial<DatabaseStatusState> = {}): DatabaseStatusState {
  return {
    phase: "healthy",
    health: healthResponse(),
    capabilities: capabilitiesResponse(),
    capabilitiesError: null,
    error: null,
    ...overrides,
  };
}

it("renders healthy engine, counts, and date range", () => {
  render(<DatabaseStatus status={state()} />);
  expect(screen.getByText(/healthy · mysql/i)).toHaveTextContent("8,000 transactions");
  expect(screen.getByText(/healthy · mysql/i)).toHaveTextContent("25 accounts");
  expect(screen.getByText(/healthy · mysql/i)).toHaveTextContent(/05.*Sep.*2025.*05.*Sep.*2026/i);
});

it("shows expandable warnings and hides the chip when empty", async () => {
  const user = userEvent.setup();
  const { rerender } = render(
    <DatabaseStatus status={state({ capabilities: capabilitiesResponse({ warnings: ["First", "Second"] }) })} />,
  );
  await user.click(screen.getByText("⚠ 2"));
  expect(screen.getByText("First")).toBeVisible();
  rerender(<DatabaseStatus status={state()} />);
  expect(screen.queryByText(/⚠/)).not.toBeInTheDocument();
});

it.each(["degraded", "unreachable"] as const)("renders the %s alert", (phase) => {
  const retry = vi.fn();
  render(
    <DatabaseStatusAlert
      status={state({ phase, capabilities: null, error: "connection failed" })}
      onRetry={retry}
    />,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("Database unreachable — chat is disabled.");
  expect(screen.getByRole("alert")).toHaveTextContent("connection failed");
});

it("reports a non-blocking capabilities failure", () => {
  render(
    <DatabaseStatus
      status={state({ capabilities: null, capabilitiesError: "capabilities endpoint failed" })}
    />,
  );
  expect(screen.getByText("Capabilities unavailable")).toBeVisible();
  expect(screen.getByText(/healthy · mysql/i)).toBeVisible();
});
