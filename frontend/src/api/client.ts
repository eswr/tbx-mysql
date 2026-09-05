import type { CapabilitiesResponse, ChatResponse, HealthResponse } from "./types";

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, init);
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(`Could not reach the Artha backend at ${BASE_URL}.`);
  }

  if (!response.ok) {
    throw new ApiError(await readErrorDetail(response), response.status);
  }
  return (await response.json()) as T;
}

export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health", { signal });
}

export function fetchCapabilities(signal?: AbortSignal): Promise<CapabilitiesResponse> {
  return request<CapabilitiesResponse>("/api/capabilities", { signal });
}

/**
 * Ask a grounded question. Passing the previous `conversationId` is what makes
 * follow-ups such as "What about July?" resolve against the stored context.
 */
export async function askQuestion(
  question: string,
  conversationId: string | null,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  return request<ChatResponse>("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: conversationId }),
      signal,
    });
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const { detail } = body as { detail: unknown };
      if (typeof detail === "string") return detail;
    }
  } catch {
    // Fall through to the status-only message.
  }
  return `The backend returned ${response.status} ${response.statusText}.`;
}
