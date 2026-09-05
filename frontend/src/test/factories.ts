import type { CapabilitiesResponse, ChatResponse, HealthResponse } from "../api/types";

export function healthResponse(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return { status: "healthy", database: "ok", backend: "mysql", ...overrides };
}

export function capabilitiesResponse(overrides: Partial<CapabilitiesResponse> = {}): CapabilitiesResponse {
  return {
    tables: ["bank", "account", "transaction"],
    columns: {},
    debit_sign: "positive",
    debit_sign_confidence: "detected",
    date_granularity: "datetime",
    utr_mode: "plaintext",
    banks: [],
    transaction_count: 8000,
    account_count: 25,
    date_range_start: "2025-09-05T00:00:00",
    date_range_end: "2026-09-05T00:00:00",
    warnings: [],
    ...overrides,
  };
}

export function chatResponse(overrides: Partial<ChatResponse> = {}): ChatResponse {
  return {
    answer: "The result is ₹10.00.",
    conversation_id: "conversation-123",
    interpretation: null,
    calculation: "SUM(transaction_amount)",
    matched_count: 1,
    evidence: null,
    confidence: { level: "high", basis: ["database-grounded"] },
    refusal: null,
    meta: { engine: "mysql" },
    ...overrides,
  };
}
