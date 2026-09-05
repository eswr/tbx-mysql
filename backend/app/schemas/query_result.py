"""
Query result schema: what engines return after executing a query.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class HowCalculated(BaseModel):
    """Metadata about how a result was computed."""
    date_range: str  # "August 2026" or "2026-08-01 to 2026-08-31"
    operation: str  # "SUM(transaction_amount)", "COUNT(*)", etc.
    records_matched: int  # Total rows matched before LIMIT
    filters_applied: dict[str, Any] = Field(default_factory=dict)  # Logged filters (sensitive data masked)
    sql: str | None = None  # Debug: the compiled SQL (optional)
    cache_hit: bool = False


class Breakdown(BaseModel):
    """One row of a grouped result (e.g., spending by bank)."""
    key: str  # The group key: bank_code, account_id, month label, etc.
    value: Decimal  # The aggregated metric: sum, count, avg, etc.
    count: int | None = None  # For summaries, often include count alongside sum


class EvidenceRow(BaseModel):
    """One transaction row in the evidence set."""
    transaction_id: str
    account_id: str
    transaction_date: str  # ISO date or datetime
    transaction_type: str  # "credit" or "debit"
    description: str
    transaction_amount: Decimal
    transaction_reference_id: str | None = None
    utr_number: str | None = None  # Masked


class Evidence(BaseModel):
    """Proof of a result: how it was calculated and sample rows."""
    how_calculated: HowCalculated
    source: str  # "transaction", "account_balance", etc.
    grounded: bool  # True = derived from data; False = could not be grounded
    breakdown: list[Breakdown] | None = None  # For grouped queries
    records: list[EvidenceRow] | None = None  # Sample rows (≤ 15, masked)
    records_truncated: bool = False  # True if more rows exist beyond the limit
    comparison_of: dict[str, Any] | None = None  # For comparisons: {period1: value, period2: value, pct_change: ...}


class Confidence(BaseModel):
    """Confidence assessment of the result."""
    level: Literal["high", "medium", "low"] = "high"  # high=rule-derived; medium=LLM-parsed; low=error/warning
    basis: list[str] = Field(default_factory=list)  # Why this confidence: ["rule-parsed", "all-fields-valid", ...]


class QueryResult(BaseModel):
    """Complete result from executing a FinancialQuery."""
    summary: str  # Human-facing answer: "You spent ₹X in August across N transactions"
    interpretation: dict[str, Any] | None = None  # The parsed query: intent, metric, filters, date_range
    calculation: str | None = None  # How the number was computed: "SUM(debit transactions in Aug 2026)"
    matched_count: int | None = None  # Total rows matching the query (before LIMIT)
    breakdown: list[Breakdown] | None = None  # For grouped results
    evidence: Evidence | None = None  # Full proof
    confidence: Confidence = Field(default_factory=Confidence)
    meta: dict[str, Any] = Field(default_factory=dict)  # engine, latency_ms, llm_calls, etc.
