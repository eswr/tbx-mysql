"""
Logical query plan: a database-neutral AST representation of a query.

Engines compile this into dialect-specific SQL (DuckDB, MySQL).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from app.schemas.financial_query import Aggregation, GroupByDimension


class JoinType(str, Enum):
    """SQL join type."""
    INNER = "INNER"
    LEFT = "LEFT"


@dataclass
class Predicate:
    """A simple boolean predicate for WHERE clause."""
    column: str
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "LIKE", "ILIKE", "IN", "NOT IN"]
    value: str | int | Decimal | list | None
    case_insensitive: bool = False  # For LIKE, whether to normalize case


@dataclass
class Sort:
    """A sort specification."""
    column: str
    direction: Literal["ASC", "DESC"]


@dataclass
class LogicalPlan:
    """A complete logical query plan."""
    
    # SELECT clause
    select_columns: list[str]  # ["t.transaction_amount", "b.bank_name", ...]
    
    # FROM/JOIN structure (fixed: t JOIN a JOIN b)
    primary_table: str  # Always "transaction" aliased as "t"
    joins: list[tuple[str, str, str]] = field(default_factory=list)  # (table, alias, ON condition)
    
    # WHERE clause
    predicates: list[Predicate] = field(default_factory=list)
    
    # GROUP BY
    group_by: list[str] | None = None  # ["b.bank_code", ...] or None for no grouping
    
    # ORDER BY (always explicit, with deterministic tie-breaker)
    order_by: list[Sort] = field(default_factory=list)
    
    # Aggregations (only used in grouped queries)
    aggregations: dict[str, str] | None = None  # {"amount": "SUM(t.transaction_amount)", "count": "COUNT(*)"}
    
    # LIMIT
    limit: int | None = None
    
    # Helpers
    @property
    def is_aggregation(self) -> bool:
        """Is this a grouped aggregation query?"""
        return self.group_by is not None and len(self.group_by) > 0
    
    @property
    def is_count_query(self) -> bool:
        """Is this a COUNT(*) query (for matched_count)?"""
        return len(self.select_columns) == 1 and "COUNT(*)" in self.select_columns[0]


@dataclass
class DateRangeHalfOpen:
    """A date range as a half-open interval [start, end+1)."""
    start: date
    end_exclusive: date  # One day past the actual end (e.g., Sep 1 for Aug 2026)
    label: str | None = None
    
    def to_inclusive_pair(self) -> tuple[date, date]:
        """Convert to inclusive [start, end] for human readability."""
        from datetime import timedelta
        return self.start, self.end_exclusive - timedelta(days=1)


def build_base_plan(
    select_columns: list[str],
    predicates: list[Predicate] | None = None,
    limit: int | None = None,
) -> LogicalPlan:
    """Build a simple SELECT FROM transaction query."""
    return LogicalPlan(
        select_columns=select_columns,
        primary_table="transaction",
        predicates=predicates or [],
        limit=limit,
    )


def build_aggregation_plan(
    select_columns: list[str],
    aggregations: dict[str, str],
    group_by: list[str],
    predicates: list[Predicate] | None = None,
    order_by: list[Sort] | None = None,
    limit: int | None = None,
) -> LogicalPlan:
    """Build a grouped aggregation query."""
    return LogicalPlan(
        select_columns=select_columns,
        primary_table="transaction",
        predicates=predicates or [],
        group_by=group_by,
        aggregations=aggregations,
        order_by=order_by or [],
        limit=limit,
    )
