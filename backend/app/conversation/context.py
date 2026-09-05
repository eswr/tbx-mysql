"""Small semantic conversation context."""

from typing import Protocol

from pydantic import BaseModel

from app.schemas.financial_query import (
    Aggregation,
    ComparisonSpec,
    DateRange,
    FinancialQuery,
    GroupByDimension,
    Intent,
    Metric,
    QueryFilters,
)


class ConversationContext(BaseModel):
    intent: Intent
    metric: Metric
    aggregation: Aggregation
    filters: QueryFilters
    date_range: DateRange
    group_by: list[GroupByDimension]
    limit: int | None = None
    comparison: ComparisonSpec | None = None
    result_reference: str | None = None

    @classmethod
    def from_query(cls, query: FinancialQuery) -> "ConversationContext":
        return cls(**query.model_dump(), result_reference=None)


class ConversationStore(Protocol):
    """Storage boundary for semantic conversation state."""

    def get(self, conversation_id: str) -> ConversationContext | None: ...

    def put(self, conversation_id: str, context: ConversationContext) -> None: ...


class InMemoryConversationStore:
    def __init__(self):
        self._contexts: dict[str, ConversationContext] = {}

    def get(self, conversation_id: str) -> ConversationContext | None:
        return self._contexts.get(conversation_id)

    def put(self, conversation_id: str, context: ConversationContext) -> None:
        self._contexts[conversation_id] = context.model_copy(deep=True)
