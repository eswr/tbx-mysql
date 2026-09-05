/**
 * Mirrors the backend `/api/chat` contract in `backend/app/main.py` and
 * `backend/app/schemas/`. Decimals are serialised by Pydantic as strings, so
 * every monetary field is a string here and is never parsed into a float.
 */

export type TransactionType = "credit" | "debit";

export type RefusalReason =
  | "unsupported_metric"
  | "unsupported_field"
  | "ambiguous"
  | "invalid_structure"
  | "no_data"
  | "capability"
  | "upstream_unavailable"
  | "upstream_timeout";

export interface QueryFilters {
  bank_code: string | null;
  bank_name: string | null;
  account_id: string | null;
  transaction_type: TransactionType | null;
  description_contains: string | null;
  reference_id: string | null;
  utr_number: string | null;
  min_amount: string | null;
  min_amount_operator: ">" | ">=";
  max_amount: string | null;
  max_amount_operator: "<" | "<=";
}

export interface DateRange {
  start: string;
  end: string;
  label: string | null;
}

export interface ComparisonSpec {
  against: "previous_period" | "previous_month" | "previous_year" | "named_month";
  month: string | null;
  year: number | null;
}

export interface Interpretation {
  intent: string;
  metric: string;
  aggregation: string;
  filters: QueryFilters;
  date_range: DateRange;
  group_by: string[];
  limit: number | null;
  comparison: ComparisonSpec | null;
}

export interface HowCalculated {
  date_range: string;
  operation: string;
  records_matched: number;
  filters_applied: Record<string, string | number | boolean | null>;
  sql: string | null;
  cache_hit: boolean;
}

/** Evidence rows are masked server-side; `utr_number` arrives already redacted. */
export interface EvidenceRow {
  transaction_id: string;
  account_id: string;
  transaction_date: string;
  transaction_type: TransactionType;
  description: string;
  transaction_amount: string;
  transaction_reference_id: string | null;
  utr_number: string | null;
}

export interface Evidence {
  how_calculated: HowCalculated;
  source: string;
  grounded: boolean;
  breakdown: unknown[] | null;
  records: EvidenceRow[] | null;
  records_truncated: boolean;
  comparison_of: Record<string, string | null> | null;
}

export interface Confidence {
  level: "high" | "medium" | "low";
  basis: string[];
}

export interface Refusal {
  reason: RefusalReason;
  message: string;
  suggestions: string[];
  supported_capabilities: string[] | null;
}

export interface ChatMeta {
  engine?: string;
  llm_calls?: number;
  ollama_model?: string | null;
  ollama_latency_ms?: number | null;
  understanding_ms?: number;
  query_ms?: number;
  [key: string]: unknown;
}

export interface ChatResponse {
  answer: string;
  conversation_id: string;
  interpretation: Interpretation | null;
  calculation: string | null;
  matched_count: number | null;
  evidence: Evidence | null;
  confidence: Confidence;
  refusal: Refusal | null;
  meta: ChatMeta;
}
