"""
Semantic query layer: database-independent FinancialQuery and supporting types.

These models represent the *intent* of a user's question, not the SQL to fetch it.
They enforce semantic coherence (e.g., balance intents require balance metric).
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class DateRangeType(str, Enum):
    """Type of date range specification."""
    CALENDAR_MONTH = "calendar_month"
    LAST_N_MONTHS = "last_n_months"
    CUSTOM = "custom"
    ALL_TIME = "all_time"
    MONTH_BEFORE_PREVIOUS = "month_before_previous"
    THIS_MONTH = "this_month"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    LAST_N_DAYS = "last_n_days"
    YESTERDAY = "yesterday"
    TODAY = "today"
    THIS_YEAR = "this_year"
    LAST_YEAR = "last_year"


class Intent(str, Enum):
    """User's high-level intent."""
    TRANSACTION_SUMMARY = "transaction_summary"  # "How much did I spend in Aug?"
    TRANSACTION_LIST = "transaction_list"  # "Show me debit txns above 50k"
    TOP_DESCRIPTIONS = "top_descriptions"  # "Top spending categories"
    MONTHLY_TREND = "monthly_trend"  # "Spending by month"
    COMPARISON = "comparison"  # "Aug vs Jul spend"
    ACCOUNT_BALANCE = "account_balance"  # "What's my balance?"
    ACCOUNT_LIST = "account_list"  # "Show my accounts"
    BANK_BALANCE = "bank_balance"  # "Which bank holds the most?"
    BANK_ACCOUNT_COUNT = "bank_account_count"  # "How many accounts per bank?"
    REFERENCE_LOOKUP = "reference_lookup"  # "Find txn with ref #1234"


class Metric(str, Enum):
    """What to measure."""
    TRANSACTION_AMOUNT = "transaction_amount"
    TRANSACTION_COUNT = "transaction_count"
    BALANCE = "balance"


class Aggregation(str, Enum):
    """How to aggregate metric."""
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    MAX = "max"
    MIN = "min"
    NONE = "none"  # List-style: no aggregation, just return rows


class GroupByDimension(str, Enum):
    """Dimensions to group by."""
    BANK = "bank"
    ACCOUNT = "account"
    TRANSACTION_TYPE = "transaction_type"
    MONTH = "month"


class SortDirection(str, Enum):
    """Sort order for results."""
    ASC = "asc"
    DESC = "desc"


class QueryFilters(BaseModel):
    """Filters to apply to transactions."""
    model_config = {"extra": "forbid"}
    
    bank_code: str | None = None
    bank_name: str | None = None
    account_id: str | None = None
    transaction_type: Literal["credit", "debit"] | None = None
    description_contains: str | None = None
    reference_id: str | None = None
    utr_number: str | None = None
    min_amount: Decimal | None = Field(None, ge=0)
    min_amount_operator: Literal[">", ">="] = ">="
    max_amount: Decimal | None = Field(None, ge=0)
    max_amount_operator: Literal["<", "<="] = "<="
    
    @field_validator("min_amount", "max_amount", mode="before")
    @classmethod
    def coerce_decimal(cls, v):
        if v is not None:
            return Decimal(str(v))
        return v
    
    @model_validator(mode="after")
    def amount_range_valid(self):
        if self.min_amount is not None and self.max_amount is not None:
            if self.min_amount > self.max_amount:
                raise ValueError("min_amount must be <= max_amount")
        return self


class DateRange(BaseModel):
    """Date range specification."""
    start: date
    end: date
    label: str | None = None  # Human-readable: "August 2026", "last 7 days", etc.
    
    @model_validator(mode="after")
    def start_before_end(self):
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self


class ComparisonSpec(BaseModel):
    """Specification for period-over-period comparison."""
    against: Literal["previous_period", "previous_month", "previous_year", "named_month"]
    month: str | None = None  # For named_month: "August", "Aug", month name
    year: int | None = None  # For named_month: 2026


class FinancialQuery(BaseModel):
    """
    A database-independent financial query with semantic coherence.
    
    Engines (DuckDB, MySQL) compile this into dialect-specific SQL.
    """
    model_config = {"extra": "forbid"}
    
    intent: Intent
    metric: Metric
    aggregation: Aggregation
    filters: QueryFilters = Field(default_factory=QueryFilters)
    date_range: DateRange
    group_by: list[GroupByDimension] = Field(default_factory=list)
    limit: int | None = Field(None, ge=1, le=1000)
    comparison: ComparisonSpec | None = None
    
    @model_validator(mode="after")
    def semantic_coherence(self):
        """Enforce coherence between intent, metric, aggregation, and group_by."""
        
        # Balance intents require balance metric
        if self.intent in (Intent.ACCOUNT_BALANCE, Intent.BANK_BALANCE):
            if self.metric != Metric.BALANCE:
                raise ValueError(f"Intent {self.intent} requires metric=balance")
        
        # Non-balance intents must not use balance metric
        if self.metric == Metric.BALANCE:
            if self.intent not in (Intent.ACCOUNT_BALANCE, Intent.BANK_BALANCE, Intent.ACCOUNT_LIST):
                raise ValueError(f"Balance metric only valid for balance intents, not {self.intent}")
        
        # Bank account count requires transaction count
        if self.intent == Intent.BANK_ACCOUNT_COUNT:
            if self.metric != Metric.TRANSACTION_COUNT:
                raise ValueError("bank_account_count requires metric=transaction_count")
        
        # Reference lookup must be a single transaction, no aggregation
        if self.intent == Intent.REFERENCE_LOOKUP:
            if self.aggregation != Aggregation.NONE:
                raise ValueError("reference_lookup must have aggregation=none")
        
        # Top descriptions intent requires a description filter
        if self.intent == Intent.TOP_DESCRIPTIONS:
            if not self.filters.description_contains:
                raise ValueError("top_descriptions requires description_contains filter")
        
        # Monthly trend requires month grouping
        if self.intent == Intent.MONTHLY_TREND:
            if GroupByDimension.MONTH not in self.group_by:
                raise ValueError("monthly_trend requires group_by=[month]")
        
        # Comparison requires comparison spec
        if self.comparison is not None:
            if self.intent not in (Intent.TRANSACTION_SUMMARY, Intent.MONTHLY_TREND, Intent.COMPARISON):
                raise ValueError("comparison only valid for transaction_summary, monthly_trend, or comparison intent")
        
        # List intents default limit
        if self.intent in (Intent.TRANSACTION_LIST, Intent.ACCOUNT_LIST):
            if self.limit is None:
                self.limit = 20
        
        return self


class QueryRefusalReason(str, Enum):
    """Why a query was refused."""
    UNSUPPORTED_METRIC = "unsupported_metric"  # Metric requires data we don't have (e.g., vendor)
    UNSUPPORTED_FIELD = "unsupported_field"  # Filter requires a field we don't have
    AMBIGUOUS = "ambiguous"  # Need clarification (e.g., "spend" without date)
    INVALID_STRUCTURE = "invalid_structure"  # Validation failed
    NO_DATA = "no_data"  # Query is valid but returned zero rows
    CAPABILITY = "capability"  # Feature requires optional adapter (e.g., payout reconciliation)


class QueryRefusal(BaseModel):
    """A structured refusal."""
    reason: QueryRefusalReason
    message: str
    suggestions: list[str] = Field(default_factory=list)
    supported_capabilities: list[str] | None = None  # What we do support


def supported_capabilities() -> dict:
    """Single source of truth: what queries we support."""
    return {
        "metrics": ["transaction_amount", "transaction_count", "balance"],
        "filters": [
            "bank_code",
            "bank_name",
            "account_id",
            "transaction_type (credit|debit)",
            "description_contains",
            "reference_id",
            "utr_number",
            "min_amount",
            "max_amount",
        ],
        "intents": [
            "transaction_summary",
            "transaction_list",
            "top_descriptions",
            "monthly_trend",
            "comparison",
            "account_balance",
            "account_list",
            "bank_balance",
            "bank_account_count",
            "reference_lookup",
        ],
        "group_by": ["bank", "account", "transaction_type", "month"],
    }


def refusal(
    reason: QueryRefusalReason,
    message: str,
    suggestions: list[str] | None = None,
) -> QueryRefusal:
    """Create a structured refusal."""
    caps = supported_capabilities()
    return QueryRefusal(
        reason=reason,
        message=message,
        suggestions=suggestions or [],
        supported_capabilities=[
            f"Metrics: {', '.join(caps['metrics'])}",
            f"Filters: {', '.join(caps['filters'])}",
            f"Intents: {', '.join(caps['intents'])}",
        ],
    )
