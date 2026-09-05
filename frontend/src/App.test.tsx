import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import App from "./App";
import { capabilitiesResponse, chatResponse, healthResponse } from "./test/factories";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

it("discovers health then capabilities and enables chat", async () => {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.endsWith("/api/health")) return jsonResponse(healthResponse());
      if (url.endsWith("/api/capabilities")) return jsonResponse(capabilitiesResponse());
      throw new Error(`Unexpected URL: ${url}`);
    }),
  );

  render(<App />);
  expect(await screen.findByText(/healthy · mysql/i)).toHaveTextContent("8,000 transactions");
  expect(calls.map((url) => url.split("/").at(-1))).toEqual(["health", "capabilities"]);
  expect(screen.getByLabelText(/Ask a question/)).toBeEnabled();
});

it("disables chat while degraded and Retry re-enables it", async () => {
  let healthCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/health")) {
        healthCalls += 1;
        return jsonResponse(healthCalls === 1 ? healthResponse({ status: "degraded", database: "error" }) : healthResponse());
      }
      if (url.endsWith("/api/capabilities")) return jsonResponse(capabilitiesResponse());
      throw new Error(`Unexpected URL: ${url}`);
    }),
  );
  const user = userEvent.setup();
  render(<App />);

  expect(await screen.findByRole("alert")).toHaveTextContent("chat is disabled");
  expect(screen.getByLabelText(/Ask a question/)).toBeDisabled();
  expect(screen.getByRole("button", { name: "How many accounts per bank?" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByText(/healthy · mysql/i)).toBeVisible();
  expect(screen.getByLabelText(/Ask a question/)).toBeEnabled();
  expect(healthCalls).toBe(2);
});

it("restores, sends, persists, and clears the conversation id", async () => {
  sessionStorage.setItem("artha.conversation_id", "restored-id");
  let chatBody: { conversation_id?: string } | null = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return jsonResponse(healthResponse());
      if (url.endsWith("/api/capabilities")) return jsonResponse(capabilitiesResponse());
      if (url.endsWith("/api/chat")) {
        chatBody = JSON.parse(String(init?.body)) as { conversation_id?: string };
        return jsonResponse(chatResponse({ conversation_id: "returned-id" }));
      }
      throw new Error(`Unexpected URL: ${url}`);
    }),
  );
  const user = userEvent.setup();
  render(<App />);

  expect(screen.getByText(/resumed/)).toBeVisible();
  expect(screen.getByRole("button", { name: "New conversation" })).toBeEnabled();
  await screen.findByText(/healthy · mysql/i);
  await user.type(screen.getByLabelText(/Ask a question/), "What about July?");
  await user.click(screen.getByRole("button", { name: "Send" }));
  await screen.findByText("The result is ₹10.00.");
  expect(chatBody).toMatchObject({ conversation_id: "restored-id" });
  expect(sessionStorage.getItem("artha.conversation_id")).toBe("returned-id");

  await user.click(screen.getByRole("button", { name: "New conversation" }));
  expect(sessionStorage.getItem("artha.conversation_id")).toBeNull();
  expect(screen.queryByText(/resumed/)).not.toBeInTheDocument();
});

it("revalidates health after a network-class chat failure", async () => {
  let healthCalls = 0;
  let chatCalls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/health")) {
        healthCalls += 1;
        return jsonResponse(healthResponse());
      }
      if (url.endsWith("/api/capabilities")) return jsonResponse(capabilitiesResponse());
      if (url.endsWith("/api/chat")) {
        chatCalls += 1;
        throw new TypeError("network offline");
      }
      throw new Error(`Unexpected URL: ${url}`);
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText(/healthy · mysql/i);
  await user.type(screen.getByLabelText(/Ask a question/), "How much did I spend?");
  await user.click(screen.getByRole("button", { name: "Send" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the Artha backend");
  await waitFor(() => expect(healthCalls).toBe(2));
  expect(chatCalls).toBe(1);
});
