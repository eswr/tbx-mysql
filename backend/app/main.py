"""Artha FastAPI application."""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import get_settings
from app.conversation import ConversationContext, InMemoryConversationStore
from app.query.execution import FinancialQueryExecutor, GroundedResult
from app.query.mysql_engine import MySQLQueryEngine
from app.schemas.financial_query import (
    Aggregation,
    FinancialQuery,
    QueryRefusal,
    QueryRefusalReason,
    refusal,
)
from app.schemas.query_result import Confidence, Evidence, EvidenceRow, HowCalculated
from app.understanding.rules import understand_question

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Validate database connectivity without preventing degraded startup."""
    if not await get_engine().ping():
        logger.warning("Failed to connect to database on startup")
    yield


app = FastAPI(title="Artha", version="0.1.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global engine (lazy-loaded)
_engine: MySQLQueryEngine | None = None
_executor: FinancialQueryExecutor | None = None
_conversation_store = InMemoryConversationStore()


def get_engine() -> MySQLQueryEngine:
    """Get or create the query engine."""
    global _engine
    if _engine is None:
        _engine = MySQLQueryEngine(settings.ARTHA_DATABASE_URL)
    return _engine


def get_executor() -> FinancialQueryExecutor:
    """Get the production executor backed by the configured MySQL engine."""
    global _executor
    if _executor is None:
        _executor = FinancialQueryExecutor(get_engine())
    return _executor


def get_conversation_store() -> InMemoryConversationStore:
    return _conversation_store


class HealthResponse(BaseModel):
    status: str
    database: str
    backend: str = "mysql"


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check."""
    engine = get_engine()
    db_ok = await engine.ping()
    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        database="ok" if db_ok else "error",
    )


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    interpretation: dict[str, Any] | None = None
    calculation: str | None = None
    matched_count: int | None = None
    evidence: Evidence | None = None
    confidence: Confidence = Field(default_factory=Confidence)
    refusal: QueryRefusal | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class BankCapability(BaseModel):
    code: str
    name: str


class CapabilitiesResponse(BaseModel):
    tables: list[str]
    columns: dict[str, list[str]]
    debit_sign: str
    debit_sign_confidence: str
    date_granularity: str
    utr_mode: str
    banks: list[BankCapability]
    transaction_count: int
    account_count: int
    date_range_start: datetime | None
    date_range_end: datetime | None
    warnings: list[str]


def _engine_name(executor: FinancialQueryExecutor) -> str:
    return "mysql" if isinstance(executor.engine, MySQLQueryEngine) else executor.engine.__class__.__name__.lower()


def _operation(query: FinancialQuery) -> str:
    if query.intent.value == "account_balance":
        return "SUM(available_balance)"
    if query.aggregation == Aggregation.NONE:
        return "SELECT(transactions)"
    column = "*" if query.aggregation == Aggregation.COUNT else "transaction_amount"
    return f"{query.aggregation.value.upper()}({column})"


def _filters(query: FinancialQuery) -> dict[str, Any]:
    return query.filters.model_dump(mode="json", exclude_none=True)


def _date_range_text(query: FinancialQuery) -> str:
    inclusive_end = query.date_range.end - timedelta(days=1)
    return f"{query.date_range.start.isoformat()} to {inclusive_end.isoformat()}"


def _evidence_rows(rows: list[dict[str, Any]]) -> list[EvidenceRow] | None:
    if not rows or "transaction_id" not in rows[0]:
        return None
    records = []
    for row in rows[:15]:
        records.append(
            EvidenceRow(
                transaction_id=str(row["transaction_id"]),
                account_id=str(row["account_id"]),
                transaction_date=row["transaction_date"].isoformat()
                if isinstance(row["transaction_date"], (date, datetime))
                else str(row["transaction_date"]),
                transaction_type=row["transaction_type"],
                description=row["description"],
                transaction_amount=row["transaction_amount"],
                transaction_reference_id=row.get("transaction_reference_id"),
                utr_number=row.get("utr_number"),
            )
        )
    return records


def _format_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    return str(value)


def _answer(query: FinancialQuery, result: GroundedResult) -> str:
    period = query.date_range.label or _date_range_text(query)
    if query.aggregation == Aggregation.NONE:
        return f"I found {result.matched_count} matching transaction(s) for {period}."
    if query.aggregation == Aggregation.COUNT:
        return f"I found {_format_value(result.value)} matching transaction(s) for {period}."
    if query.intent.value == "account_balance":
        return f"Your available balance is ₹{_format_value(result.value)}."
    if result.comparison_matched_count is not None:
        return f"The result is ₹{_format_value(result.value)}, compared with ₹{_format_value(result.comparison_value)}."
    return f"The result is ₹{_format_value(result.value)} across {result.matched_count} transaction(s) for {period}."


def _success_response(
    conversation_id: str,
    query: FinancialQuery,
    result: GroundedResult,
    engine_name: str,
    understanding_ms: float,
    query_ms: float,
) -> ChatResponse:
    no_data_refusal = None
    if result.no_data:
        no_data_refusal = refusal(
            QueryRefusalReason.NO_DATA,
            "No records matched the interpreted question.",
            ["Try a wider date range or fewer filters"],
        )
    records = _evidence_rows(result.rows)
    evidence = Evidence(
        how_calculated=HowCalculated(
            date_range=_date_range_text(query),
            operation=_operation(query),
            records_matched=result.matched_count,
            filters_applied=_filters(query),
        ),
        source="account" if query.intent.value == "account_balance" else "transaction",
        grounded=True,
        records=records,
        records_truncated=records is not None and result.matched_count > len(records),
        comparison_of={
            "current": result.value,
            "comparison": result.comparison_value,
        }
        if result.comparison_matched_count is not None
        else None,
    )
    return ChatResponse(
        answer=no_data_refusal.message if no_data_refusal else _answer(query, result),
        conversation_id=conversation_id,
        interpretation=query.model_dump(mode="json"),
        calculation=_operation(query),
        matched_count=result.matched_count,
        evidence=evidence,
        confidence=Confidence(level="high", basis=["rule-parsed", "schema-validated", "database-grounded"]),
        refusal=no_data_refusal,
        meta={
            "engine": engine_name,
            "llm_calls": 0,
            "understanding_ms": round(understanding_ms, 3),
            "query_ms": round(query_ms, 3),
        },
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    executor: FinancialQueryExecutor = Depends(get_executor),
    conversation_store: InMemoryConversationStore = Depends(get_conversation_store),
):
    """Interpret, execute, and return a database-grounded financial answer."""
    conv_id = req.conversation_id or str(uuid.uuid4())
    started = time.perf_counter()
    parsed = understand_question(req.question, context=conversation_store.get(conv_id))
    understanding_ms = (time.perf_counter() - started) * 1000
    engine_name = _engine_name(executor)
    if not isinstance(parsed, FinancialQuery):
        structured = parsed or refusal(QueryRefusalReason.AMBIGUOUS, "I could not interpret that question.")
        return ChatResponse(
            answer=structured.message,
            conversation_id=conv_id,
            confidence=Confidence(level="low", basis=["not-executed", "structured-refusal"]),
            refusal=structured,
            meta={
                "engine": engine_name,
                "llm_calls": 0,
                "understanding_ms": round(understanding_ms, 3),
                "query_ms": 0.0,
            },
        )

    query_started = time.perf_counter()
    result = await executor.execute(parsed)
    query_ms = (time.perf_counter() - query_started) * 1000
    conversation_store.put(conv_id, ConversationContext.from_query(parsed))
    return _success_response(conv_id, parsed, result, engine_name, understanding_ms, query_ms)


@app.get("/api/capabilities", response_model=CapabilitiesResponse)
async def capabilities(engine: MySQLQueryEngine = Depends(get_engine)):
    """Return capabilities discovered from the configured production database."""
    return CapabilitiesResponse.model_validate(asdict(await engine.discover_capabilities()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
