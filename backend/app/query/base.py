"""
Base query engine: protocol and capability discovery.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel


@dataclass
class BankInfo:
    """Discovered bank."""

    code: str
    name: str


@dataclass
class Capabilities:
    """Engine capabilities discovered at runtime."""

    tables: list[str] = field(default_factory=list)  # ["bank", "account", "transaction"]
    columns: dict[str, list[str]] = field(default_factory=dict)  # table -> column names

    # Semantics
    debit_sign: str = "positive"  # or "negative"
    debit_sign_confidence: str = "detected"  # or "configured"

    date_granularity: str = "datetime"  # "date" or "datetime"
    utr_mode: str = "plaintext"  # "plaintext" or "opaque"

    banks: list[BankInfo] = field(default_factory=list)  # Authoritative bank list

    # Performance
    transaction_count: int = 0
    account_count: int = 0
    date_range_start: datetime | None = None
    date_range_end: datetime | None = None

    # Warnings
    warnings: list[str] = field(default_factory=list)


class QueryEngine(ABC):
    """Abstract interface for query engines."""

    @abstractmethod
    async def discover_capabilities(self) -> Capabilities:
        """Probe the database and return capabilities."""
        pass

    @abstractmethod
    async def execute_transaction_list(
        self,
        predicates: list,
        order_by: list,
        limit: int,
    ) -> dict:
        """Execute a transaction list query."""
        pass

    @abstractmethod
    async def execute_aggregation(
        self,
        aggregations: dict,
        group_by: list,
        predicates: list,
        order_by: list | None,
        limit: int | None,
    ) -> dict:
        """Execute an aggregation query."""
        pass

    @abstractmethod
    async def ping(self) -> bool:
        """Test connectivity."""
        pass


@dataclass
class AggregationResult(BaseModel):
    """Result of an aggregation query."""

    rows: list[dict[str, Any]]
    total_matched: int
    metadata: dict = field(default_factory=dict)


@dataclass
class ListResult(BaseModel):
    """Result of a list query."""

    rows: list[dict[str, Any]]
    total_matched: int
    metadata: dict = field(default_factory=dict)
