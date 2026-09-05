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

from app.config import ConversationStoreType, get_settings
from app.conversation import (
    ConversationContext,
    ConversationStore,
    InMemoryConversationStore,
    SQLiteConversationStore,
)
from app.query.compiler import QueryCompilationError
from app.query.execution import FinancialQueryExecutor, GroundedResult
from app.query.mysql_engine import MySQLQueryEngine
from app.schemas.financial_query import (
    Aggregation,
    FinancialQuery,
    GroupByDimension,
    Intent,
    Metric,
    QueryRefusal,
    QueryRefusalReason,
    refusal,
)
from app.schemas.query_result import Breakdown, Confidence, Evidence, EvidenceRow, HowCalculated
from app.understanding.rules import understand_question

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Validate database connectivity without preventing degraded startup."""
    conversation_store = get_conversation_store()
    if isinstance(conversation_store, SQLiteConversationStore):
        conversation_store.initialize()
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
_conversation_store: ConversationStore | None = None
_conversation_store_config: tuple[ConversationStoreType, str | None] | None = None


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


def get_conversation_store() -> ConversationStore:
    """Get the configured conversation store, replacing it if configuration changes."""
    global _conversation_store, _conversation_store_config
    current_settings = get_settings()
    db_path = (
        current_settings.ARTHA_SQLITE_DB_PATH
        if current_settings.ARTHA_CONVERSATION_STORE == ConversationStoreType.SQLITE
        else None
    )
    config = (current_settings.ARTHA_CONVERSATION_STORE, db_path)
    if _conversation_store is None or _conversation_store_config != config:
        if current_settings.ARTHA_CONVERSATION_STORE == ConversationStoreType.MEMORY:
            _conversation_store = InMemoryConversationStore()
        else:
            assert db_path is not None
            _conversation_store = SQLiteConversationStore(db_path)
        _conversation_store_config = config
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
    if query.intent in {Intent.ACCOUNT_BALANCE, Intent.BANK_BALANCE}:
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


# Result-set column that carries the group key, per grouping dimension.
_BREAKDOWN_KEY_COLUMNS = {
    GroupByDimension.BANK: "bank_code",
    GroupByDimension.ACCOUNT: "account_id",
    GroupByDimension.TRANSACTION_TYPE: "transaction_type",
}


def _breakdown(query: FinancialQuery, rows: list[dict[str, Any]]) -> list[Breakdown] | None:
    """Project grouped result rows into breakdown entries for the evidence panel."""
    if not query.group_by or not rows or "value" not in rows[0]:
        return None
    key_column = _BREAKDOWN_KEY_COLUMNS.get(query.group_by[0])
    if key_column is None or key_column not in rows[0]:
        return None
    entries = []
    for row in rows:
        value = row["value"]
        if value is None:
            continue
        entries.append(
            Breakdown(
                key=str(row[key_column]),
                label=str(row["bank_name"]) if row.get("bank_name") is not None else None,
                value=Decimal(str(value)),
            )
        )
    return entries or None


def _format_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    return str(value)


def _format_group_value(query: FinancialQuery, value: Decimal) -> str:
    if query.aggregation == Aggregation.COUNT:
        return f"{int(value):,}"
    return f"₹{_format_value(value)}"


def _grouped_answer(query: FinancialQuery, result: GroundedResult) -> str | None:
    """
    A grouped result has no single scalar, so `result.value` is None. Name the
    leading groups instead of formatting that None into the sentence.
    """
    entries = _breakdown(query, result.rows)
    if not entries:
        return None
    dimension = query.group_by[0].value.replace("_", " ")
    if query.intent == Intent.BANK_ACCOUNT_COUNT:
        subject = "Accounts"
    elif query.metric == Metric.BALANCE:
        subject = "Balance"
    else:
        subject = "Totals"
    shown = entries[:4]
    named = ", ".join(f"{entry.label or entry.key} ({_format_group_value(query, entry.value)})" for entry in shown)
    remainder = max(0, result.matched_count - len(shown))
    suffix = f", and {remainder} more" if remainder > 0 else ""
    return f"{subject} by {dimension}: {named}{suffix}."


def _answer(query: FinancialQuery, result: GroundedResult) -> str:
    period = query.date_range.label or _date_range_text(query)
    if query.group_by:
        grouped = _grouped_answer(query, result)
        if grouped is not None:
            return grouped
    if query.aggregation == Aggregation.NONE:
        return f"I found {result.matched_count} matching transaction(s) for {period}."
    if query.intent == Intent.ACCOUNT_COUNT:
        return f"The total number of accounts is {int(result.value):,}."
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
    llm_calls: int = 0,
    ollama_metadata: dict[str, Any] | None = None,
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
        source="account"
        if query.intent in {Intent.ACCOUNT_BALANCE, Intent.ACCOUNT_COUNT, Intent.BANK_BALANCE, Intent.BANK_ACCOUNT_COUNT}
        else "transaction",
        grounded=True,
        breakdown=_breakdown(query, result.rows),
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
            "llm_calls": llm_calls,
            "ollama_model": ollama_metadata.get("model") if ollama_metadata else None,
            "ollama_latency_ms": round(ollama_metadata["latency_ms"], 3) if ollama_metadata else None,
            "ollama_prompt_tokens": ollama_metadata.get("prompt_tokens") if ollama_metadata else None,
            "ollama_completion_tokens": ollama_metadata.get("completion_tokens") if ollama_metadata else None,
            "ollama_total_tokens": ollama_metadata.get("total_tokens") if ollama_metadata else None,
            "understanding_ms": round(understanding_ms, 3),
            "query_ms": round(query_ms, 3),
        },
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    executor: FinancialQueryExecutor = Depends(get_executor),
    conversation_store: ConversationStore = Depends(get_conversation_store),
):
    """Interpret, execute, and return a database-grounded financial answer."""
    from app.understanding.dates import today_ist

    conv_id = req.conversation_id or str(uuid.uuid4())
    started = time.perf_counter()
    llm_calls = 0
    ollama_metadata: dict[str, Any] | None = None
    context = conversation_store.get(conv_id)

    # Step 1: Try rules-based understanding
    parsed = understand_question(req.question, context=context)

    # Step 2: Only an explicit rules miss may cross the Ollama boundary.
    if parsed is None and settings.ARTHA_OLLAMA_ENABLED:
        try:
            from app.understanding.ollama import understand_with_ollama

            discovered = await executor.engine.discover_capabilities()
            ollama_result = await understand_with_ollama(
                question=req.question,
                reference_date=today_ist(),
                context=context,
                discovered_capabilities=discovered,
                base_url=settings.ARTHA_OLLAMA_BASE_URL,
                model=settings.ARTHA_OLLAMA_MODEL,
                timeout=settings.ARTHA_OLLAMA_TIMEOUT,
            )
            parsed = ollama_result.output
            ollama_metadata = asdict(ollama_result.metadata)
            llm_calls = 1
        except Exception as e:
            logger.error(f"Ollama fallback failed: {e}")
            parsed = None

    understanding_ms = (time.perf_counter() - started) * 1000
    engine_name = _engine_name(executor)

    # Step 3: If still no result, return structured refusal
    if not isinstance(parsed, FinancialQuery):
        structured = parsed or refusal(QueryRefusalReason.AMBIGUOUS, "I could not interpret that question.")
        return ChatResponse(
            answer=structured.message,
            conversation_id=conv_id,
            confidence=Confidence(level="low", basis=["not-executed", "structured-refusal"]),
            refusal=structured,
            meta={
                "engine": engine_name,
                "llm_calls": llm_calls,
                "ollama_model": ollama_metadata.get("model") if ollama_metadata else None,
                "ollama_latency_ms": round(ollama_metadata["latency_ms"], 3) if ollama_metadata else None,
                "ollama_prompt_tokens": ollama_metadata.get("prompt_tokens") if ollama_metadata else None,
                "ollama_completion_tokens": ollama_metadata.get("completion_tokens") if ollama_metadata else None,
                "ollama_total_tokens": ollama_metadata.get("total_tokens") if ollama_metadata else None,
                "understanding_ms": round(understanding_ms, 3),
                "query_ms": 0.0,
            },
        )

    # Step 4: Execute query (existing deterministic path)
    query_started = time.perf_counter()
    try:
        result = await executor.execute(parsed)
    except QueryCompilationError as exc:
        # The compiler fails closed on anything outside the allowlist. Surface that as a
        # structured capability refusal rather than a 500.
        logger.warning(f"Compilation refused for {parsed.intent.value}: {exc}")
        structured = refusal(
            QueryRefusalReason.CAPABILITY,
            "I understood the question but cannot compute that shape of answer yet.",
            ["Try asking about totals, counts, balances, or transactions for a specific period"],
        )
        return ChatResponse(
            answer=structured.message,
            conversation_id=conv_id,
            interpretation=parsed.model_dump(mode="json"),
            confidence=Confidence(level="low", basis=["not-executed", "structured-refusal"]),
            refusal=structured,
            meta={
                "engine": engine_name,
                "llm_calls": llm_calls,
                "understanding_ms": round(understanding_ms, 3),
                "query_ms": round((time.perf_counter() - query_started) * 1000, 3),
            },
        )
    query_ms = (time.perf_counter() - query_started) * 1000
    conversation_store.put(conv_id, ConversationContext.from_query(parsed))
    return _success_response(
        conv_id,
        parsed,
        result,
        engine_name,
        understanding_ms,
        query_ms,
        llm_calls=llm_calls,
        ollama_metadata=ollama_metadata,
    )


@app.get("/api/capabilities", response_model=CapabilitiesResponse)
async def capabilities(engine: MySQLQueryEngine = Depends(get_engine)):
    """Return capabilities discovered from the configured production database."""
    return CapabilitiesResponse.model_validate(asdict(await engine.discover_capabilities()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
