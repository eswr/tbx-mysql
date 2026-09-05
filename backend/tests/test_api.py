"""End-to-end API contract tests with the database boundary replaced."""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.conversation import InMemoryConversationStore
from app.main import app, get_conversation_store, get_engine, get_executor
from app.query.base import BankInfo, Capabilities
from app.query.execution import FinancialQueryExecutor
from app.query.mysql_engine import MySQLQueryEngine


class RecordingEngine:
    def __init__(self):
        self.calls = []

    async def execute_snapshot(self, plans, oracle_sqls):
        self.calls.append(plans)
        start = plans[0].predicates[0].value
        if start == date(2026, 8, 1):
            value, matched_count = Decimal("1250.50"), 3
        else:
            value, matched_count = Decimal("900.00"), 2
        return {
            "plan_rows": [[{"value": value}], [{"matched_count": matched_count}]],
            "oracle_rows": [],
            "sql": ["SELECT result", "SELECT count"],
        }


class CapabilityEngine:
    async def discover_capabilities(self):
        return Capabilities(
            tables=["bank", "account", "transaction"],
            columns={"transaction": ["transaction_id"]},
            banks=[BankInfo(code="HDFC", name="HDFC BANK LIMITED")],
            transaction_count=8000,
            account_count=25,
        )


@pytest.fixture
def api_dependencies():
    engine = RecordingEngine()
    store = InMemoryConversationStore()
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(engine)
    app.dependency_overrides[get_conversation_store] = lambda: store
    app.dependency_overrides[get_engine] = lambda: CapabilityEngine()
    yield engine
    app.dependency_overrides.clear()


@pytest.fixture
async def api_client(api_dependencies):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_chat_returns_grounded_contract(api_client, api_dependencies):
    response = await api_client.post("/api/chat", json={"question": "How much did I spend in August 2026?"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "answer",
        "conversation_id",
        "interpretation",
        "calculation",
        "matched_count",
        "evidence",
        "confidence",
        "refusal",
        "meta",
    }
    assert body["interpretation"]["filters"]["transaction_type"] == "debit"
    assert body["interpretation"]["date_range"]["start"] == "2026-08-01"
    assert body["calculation"] == "SUM(transaction_amount)"
    assert body["matched_count"] == 3
    assert body["evidence"]["grounded"] is True
    assert body["evidence"]["how_calculated"]["records_matched"] == 3
    assert body["confidence"]["level"] == "high"
    assert body["refusal"] is None
    assert body["meta"]["llm_calls"] == 0
    assert len(api_dependencies.calls) == 1


@pytest.mark.asyncio
async def test_chat_multi_turn_reuses_context_and_replaces_period(api_client, api_dependencies):
    first = await api_client.post(
        "/api/chat",
        json={"question": "How much did I spend in August 2026?", "conversation_id": "conversation-1"},
    )
    followup = await api_client.post(
        "/api/chat",
        json={"question": "What about July?", "conversation_id": "conversation-1"},
    )

    assert first.status_code == followup.status_code == 200
    body = followup.json()
    assert body["conversation_id"] == "conversation-1"
    assert body["interpretation"]["filters"]["transaction_type"] == "debit"
    assert body["interpretation"]["date_range"] == {
        "start": "2026-07-01",
        "end": "2026-08-01",
        "label": "July 2026",
    }
    assert body["matched_count"] == 2
    assert len(api_dependencies.calls) == 2


@pytest.mark.asyncio
async def test_chat_refusal_is_structured_and_does_not_execute(api_client, api_dependencies):
    response = await api_client.post("/api/chat", json={"question": "Show me overdue invoices"})

    body = response.json()
    assert response.status_code == 200
    assert body["interpretation"] is None
    assert body["calculation"] is None
    assert body["matched_count"] is None
    assert body["evidence"] is None
    assert body["confidence"]["level"] == "low"
    assert body["refusal"]["reason"] == "unsupported_metric"
    assert len(api_dependencies.calls) == 0


@pytest.mark.asyncio
async def test_capabilities_returns_discovered_database_contract(api_client):
    response = await api_client.get("/api/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["tables"] == ["bank", "account", "transaction"]
    assert body["banks"] == [{"code": "HDFC", "name": "HDFC BANK LIMITED"}]
    assert body["transaction_count"] == 8000
    assert body["account_count"] == 25


@pytest.mark.asyncio
@pytest.mark.requires_mysql
async def test_chat_mysql_end_to_end_multi_turn(mysql_available, mysql_url, fixture_dir):
    if not mysql_available:
        pytest.fail()
    from scripts.load_fixture import load_mysql

    assert load_mysql(mysql_url, fixture_dir, clear=True)
    store = InMemoryConversationStore()
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(MySQLQueryEngine(mysql_url))
    app.dependency_overrides[get_conversation_store] = lambda: store
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/api/chat",
                json={"question": "How much did I spend in August 2026?", "conversation_id": "mysql-e2e"},
            )
            followup = await client.post(
                "/api/chat",
                json={"question": "What about July?", "conversation_id": "mysql-e2e"},
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == followup.status_code == 200
    assert first.json()["evidence"]["grounded"] is True
    assert first.json()["matched_count"] > 0
    assert followup.json()["interpretation"]["date_range"]["start"] == "2026-07-01"
    assert followup.json()["interpretation"]["filters"]["transaction_type"] == "debit"
    assert followup.json()["evidence"]["grounded"] is True
    assert followup.json()["matched_count"] > 0


class GroupedEngine:
    """Returns bank-grouped rows in the shape the compiler's grouped plan selects."""

    def __init__(self, rows=None, matched_count=3):
        self.calls = []
        self.rows = rows or [
            {"bank_code": "HDFC", "bank_name": "HDFC Bank", "value": 258},
            {"bank_code": "ICIC", "bank_name": "ICICI Bank", "value": 190},
            {"bank_code": "SBIN", "bank_name": "State Bank of India", "value": 153},
        ]
        self.matched_count = matched_count

    async def execute_snapshot(self, plans, oracle_sqls):
        self.calls.append(plans)
        return {
            "plan_rows": [self.rows, [{"matched_count": self.matched_count}]],
            "oracle_rows": [],
            "sql": ["SELECT grouped", "SELECT count"],
        }


class AccountCountEngine:
    def __init__(self, value=5000):
        self.calls = []
        self.value = value

    async def execute_snapshot(self, plans, oracle_sqls):
        self.calls.append(plans)
        return {
            "plan_rows": [[{"value": self.value}]],
            "oracle_rows": [],
            "sql": ["SELECT COUNT(*) AS value FROM account a"],
        }


@pytest.mark.asyncio
async def test_chat_total_account_count_returns_grounded_scalar_answer():
    engine = AccountCountEngine()
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(engine)
    app.dependency_overrides[get_conversation_store] = lambda: InMemoryConversationStore()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json={"question": "what is the total number of accounts?"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "The total number of accounts is 5,000."
    assert body["interpretation"]["intent"] == "account_count"
    assert body["interpretation"]["group_by"] == []
    assert body["calculation"] == "COUNT(*)"
    assert body["matched_count"] == 5000
    assert body["evidence"]["source"] == "account"
    assert body["evidence"]["grounded"] is True
    assert body["refusal"] is None
    assert len(engine.calls) == 1
    assert len(engine.calls[0]) == 1


@pytest.mark.asyncio
async def test_chat_grouped_question_returns_breakdown_evidence():
    engine = GroupedEngine()
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(engine)
    app.dependency_overrides[get_conversation_store] = lambda: InMemoryConversationStore()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json={"question": "How many accounts per bank?"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["group_by"] == ["bank"]
    assert body["evidence"]["grounded"] is True

    breakdown = body["evidence"]["breakdown"]
    assert breakdown is not None, "grouped results must populate evidence.breakdown"
    assert len(breakdown) == 3
    assert breakdown[0]["key"] == "HDFC"
    assert breakdown[0]["label"] == "HDFC Bank"
    assert Decimal(breakdown[0]["value"]) == Decimal("258")
    # matched_count is the number of groups for a grouped result.
    assert body["matched_count"] == 3
    assert body["answer"] == "Accounts by bank: HDFC Bank (258), ICICI Bank (190), State Bank of India (153)."
    assert body["calculation"] == "COUNT(*)"
    assert body["evidence"]["source"] == "account"


@pytest.mark.asyncio
async def test_chat_grouped_balance_answer_uses_total_group_count_for_remainder():
    rows = [
        {"bank_code": "ICIC", "bank_name": "ICICI Bank", "value": Decimal("6235647.36")},
        {"bank_code": "SBIN", "bank_name": "State Bank of India", "value": Decimal("5878751.11")},
        {"bank_code": "RATN", "bank_name": "RBL Bank", "value": Decimal("3323884.54")},
        {"bank_code": "TMBL", "bank_name": "Tamilnad Mercantile Bank", "value": Decimal("2774501.95")},
        {"bank_code": "KKBK", "bank_name": "Kotak Mahindra Bank", "value": Decimal("1312329.23")},
    ]
    engine = GroupedEngine(rows=rows, matched_count=7)
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(engine)
    app.dependency_overrides[get_conversation_store] = lambda: InMemoryConversationStore()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json={"question": "Which bank holds the most money?"})
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert body["answer"] == (
        "Balance by bank: ICICI Bank (₹6,235,647.36), State Bank of India (₹5,878,751.11), "
        "RBL Bank (₹3,323,884.54), Tamilnad Mercantile Bank (₹2,774,501.95), and 3 more."
    )
    assert body["matched_count"] == 7
    assert body["calculation"] == "SUM(available_balance)"
    assert body["evidence"]["source"] == "account"


@pytest.mark.asyncio
async def test_chat_ungrouped_question_has_no_breakdown(api_client):
    response = await api_client.post("/api/chat", json={"question": "How much did I spend in August 2026?"})

    assert response.status_code == 200
    assert response.json()["evidence"]["breakdown"] is None


@pytest.mark.asyncio
async def test_chat_uncompilable_query_refuses_instead_of_erroring():
    """A compilation failure must become a structured refusal, not an unhandled 500."""

    class ExplodingEngine:
        async def execute_snapshot(self, plans, oracle_sqls):  # pragma: no cover - never reached
            raise AssertionError("execution should not be attempted")

    class RefusingExecutor(FinancialQueryExecutor):
        async def execute(self, query, oracle_sql=None):
            from app.query.compiler import QueryCompilationError

            raise QueryCompilationError("Unsupported group_by dimension: month")

    app.dependency_overrides[get_executor] = lambda: RefusingExecutor(ExplodingEngine())
    app.dependency_overrides[get_conversation_store] = lambda: InMemoryConversationStore()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json={"question": "How much did I spend in August 2026?"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["refusal"]["reason"] == "capability"
    assert body["confidence"]["level"] == "low"
    assert body["evidence"] is None


@pytest.mark.asyncio
async def test_chat_does_not_convert_runtime_value_error_to_capability_refusal():
    class FailingExecutor(FinancialQueryExecutor):
        async def execute(self, query, oracle_sql=None):
            raise ValueError("database decoding failed")

    app.dependency_overrides[get_executor] = lambda: FailingExecutor(GroupedEngine())
    app.dependency_overrides[get_conversation_store] = lambda: InMemoryConversationStore()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(ValueError, match="database decoding failed"):
                await client.post("/api/chat", json={"question": "How many accounts per bank?"})
    finally:
        app.dependency_overrides.clear()
