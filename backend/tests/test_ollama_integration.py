"""Ollama boundary, strict normalization, and API fallback tests."""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.conversation import ConversationContext, InMemoryConversationStore
from app.main import app, get_conversation_store, get_executor
from app.query.base import Capabilities
from app.query.execution import FinancialQueryExecutor
from app.query.mysql_engine import MySQLQueryEngine
from app.config import UTRMode
from app.schemas.financial_query import (
    Aggregation,
    DateRange,
    FinancialQuery,
    Intent,
    Metric,
    QueryFilters,
    QueryRefusal,
    QueryRefusalReason,
)
from app.understanding.normalize import normalize_llm_output
from app.understanding.ollama import (
    OllamaResult,
    build_query_json_schema,
    capabilities_from_discovery,
    understand_with_ollama,
)


def full_capabilities() -> Capabilities:
    return Capabilities(
        tables=["bank", "account", "transaction"],
        columns={
            "bank": ["bank_code", "bank_name"],
            "account": ["account_id", "available_balance", "bank_code"],
            "transaction": [
                "transaction_id",
                "account_id",
                "transaction_date",
                "transaction_type",
                "description",
                "transaction_amount",
                "transaction_reference_id",
                "utr_number",
            ],
        },
        utr_mode="plaintext",
    )


def valid_query(**updates):
    value = {
        "intent": "transaction_summary",
        "metric": "transaction_amount",
        "aggregation": "sum",
        "filters": {"transaction_type": "debit"},
        "date_range_type": "calendar_month",
        "month": "August",
        "year": 2026,
        "group_by": [],
        "limit": None,
        "comparison": None,
        "refusal": None,
    }
    value.update(updates)
    return value


def ollama_response(content: str, *, model: str = "actual-model") -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "model": model,
        "message": {"content": content},
        "prompt_eval_count": 25,
        "eval_count": 50,
    }
    return response


def fake_ollama_client(response: MagicMock, captured: dict | None = None):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            if captured is not None:
                captured.update({"url": url, "json": json})
            return response

    return FakeClient


class FakeOllamaEngine:
    def __init__(self):
        self.calls = []

    async def discover_capabilities(self):
        return full_capabilities()

    async def execute_snapshot(self, plans, oracle_sqls):
        self.calls.append(plans)
        return {
            "plan_rows": [[{"value": Decimal("1000.00")}], [{"matched_count": 5}]],
            "oracle_rows": [],
            "sql": ["SELECT result", "SELECT count"],
        }


@pytest.fixture
def ollama_api_dependencies():
    engine = FakeOllamaEngine()
    store = InMemoryConversationStore()
    app.dependency_overrides[get_executor] = lambda: FinancialQueryExecutor(engine)
    app.dependency_overrides[get_conversation_store] = lambda: store
    yield engine, store
    app.dependency_overrides.clear()


@pytest.fixture
async def ollama_api_client(ollama_api_dependencies):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_rules_hit_never_calls_ollama(ollama_api_client):
    with patch("app.understanding.ollama.understand_with_ollama", new_callable=AsyncMock) as understand:
        response = await ollama_api_client.post("/api/chat", json={"question": "How much did I spend in August 2026?"})
    assert response.json()["meta"]["llm_calls"] == 0
    understand.assert_not_called()


@pytest.mark.asyncio
async def test_rules_miss_with_ollama_disabled_returns_ambiguity(ollama_api_client):
    with patch("app.main.settings") as settings:
        settings.ARTHA_OLLAMA_ENABLED = False
        response = await ollama_api_client.post("/api/chat", json={"question": "xyz_unparseable_question_xyz"})
    assert response.json()["refusal"]["reason"] == "ambiguous"
    assert response.json()["meta"]["llm_calls"] == 0


@pytest.mark.asyncio
async def test_rules_ambiguity_does_not_fall_back_to_ollama(ollama_api_client):
    with (
        patch("app.main.settings") as settings,
        patch("app.understanding.ollama.understand_with_ollama", new_callable=AsyncMock) as understand,
    ):
        settings.ARTHA_OLLAMA_ENABLED = True
        response = await ollama_api_client.post("/api/chat", json={"question": "What is your name?"})
    assert response.json()["refusal"]["reason"] == "ambiguous"
    assert response.json()["meta"]["llm_calls"] == 0
    understand.assert_not_called()


def test_valid_model_query_normalizes():
    result = normalize_llm_output(valid_query(), reference_date=date(2026, 9, 5))
    assert isinstance(result, FinancialQuery)
    assert result.date_range == DateRange(start=date(2026, 8, 1), end=date(2026, 9, 1), label="August 2026")


@pytest.mark.parametrize(
    "mutation",
    [
        {"filters": {"merchant_secret": "x"}},
        {"group_by": ["not_a_dimension"]},
        {"limit": "20"},
        {"limit": 0},
        {"limit": 1001},
        {"filters": {"min_amount": 5, "min_amount_operator": "="}},
        {"filters": {"max_amount": 5, "max_amount_operator": ">"}},
        {"comparison": {"against": "previous_year"}},
        {"comparison": {"against": "previous_month", "year": 2025}},
    ],
)
def test_malformed_supplied_fields_fail_closed(mutation):
    result = normalize_llm_output(valid_query(**mutation))
    assert isinstance(result, QueryRefusal)
    assert result.reason in {QueryRefusalReason.INVALID_STRUCTURE, QueryRefusalReason.CAPABILITY}


def test_supported_comparison_survives_normalization():
    result = normalize_llm_output(valid_query(intent="comparison", comparison={"against": "previous_month"}))
    assert isinstance(result, FinancialQuery)
    assert result.comparison is not None
    assert result.comparison.against == "previous_month"


def test_grouping_known_but_not_executable_is_capability_refusal():
    result = normalize_llm_output(valid_query(group_by=["bank"]))
    assert isinstance(result, QueryRefusal)
    assert result.reason == QueryRefusalReason.CAPABILITY


def test_missing_or_mixed_model_shapes_are_invalid_structure():
    missing = normalize_llm_output({"refusal": None})
    mixed = normalize_llm_output({"refusal": {"reason": "ambiguous", "message": "x"}, "intent": "comparison"})
    assert isinstance(missing, QueryRefusal)
    assert isinstance(mixed, QueryRefusal)
    assert missing.reason == QueryRefusalReason.INVALID_STRUCTURE
    assert mixed.reason == QueryRefusalReason.INVALID_STRUCTURE


def test_capability_schema_uses_discovered_columns_and_frozen_executor_surface():
    discovered = Capabilities(
        tables=["transaction"],
        columns={"transaction": ["transaction_date", "transaction_type", "transaction_amount"]},
    )
    active = capabilities_from_discovery(discovered)
    schema = build_query_json_schema(active)
    query_schema = schema["oneOf"][0]
    assert query_schema["properties"]["intent"]["enum"] == ["comparison", "transaction_summary"]
    assert query_schema["properties"]["group_by"]["items"]["enum"] == []
    assert set(query_schema["properties"]["filters"]["properties"]) == {
        "transaction_type",
        "min_amount",
        "min_amount_operator",
        "max_amount",
        "max_amount_operator",
    }


def test_unsupported_discovered_intent_cannot_normalize():
    active = capabilities_from_discovery(Capabilities(tables=[], columns={}))
    result = normalize_llm_output(valid_query(), active_capabilities=active)
    assert isinstance(result, QueryRefusal)
    assert result.reason == QueryRefusalReason.CAPABILITY


def test_configured_opaque_utr_mode_removes_utr_capability():
    discovered = full_capabilities()
    discovered.utr_mode = "opaque"
    active = capabilities_from_discovery(discovered)
    assert "utr_number" not in active.filters
    assert MySQLQueryEngine("mysql://unused", utr_mode=UTRMode.OPAQUE).utr_mode == "opaque"


@pytest.mark.asyncio
async def test_http_payload_redacts_raw_sensitive_identifiers():
    raw_values = [
        "12345",
        "HDFC202609051234567890",
        "REF-9988776655",
        "ABCDE1234F",
        "550e8400-e29b-41d4-a716-446655440000",
        "person@example.com",
    ]
    question = (
        "account 12345 UTR HDFC202609051234567890 reference REF-9988776655 "
        "PAN ABCDE1234F txn 550e8400-e29b-41d4-a716-446655440000 email person@example.com"
    )
    with patch(
        "httpx.AsyncClient.post",
        return_value=ollama_response(json.dumps({"refusal": {"reason": "ambiguous", "message": "Need details"}})),
    ) as post:
        result = await understand_with_ollama(question, base_url="http://test", model="requested")
    serialized_payload = json.dumps(post.call_args.kwargs["json"])
    assert isinstance(result, OllamaResult)
    assert all(raw not in serialized_payload for raw in raw_values)
    assert "[ACCOUNT_ID_1]" in serialized_payload
    assert "[UTR_1]" in serialized_payload


@pytest.mark.asyncio
async def test_redacted_identifier_is_rehydrated_only_after_http_response():
    model_query = valid_query(
        intent="reference_lookup",
        metric="transaction_count",
        aggregation="none",
        filters={"utr_number": "UTR_1"},
        date_range_type="all_time",
        month=None,
        year=None,
        limit=1,
    )
    captured = {}
    with patch(
        "app.understanding.ollama.httpx.AsyncClient",
        fake_ollama_client(ollama_response(json.dumps(model_query)), captured),
    ):
        result = await understand_with_ollama(
            "Find UTR HDFC202609051234567890",
            discovered_capabilities=full_capabilities(),
            base_url="http://test",
            model="test",
        )
    assert "HDFC202609051234567890" not in json.dumps(captured["json"])
    assert isinstance(result.output, FinancialQuery)
    assert result.output.filters.utr_number == "HDFC202609051234567890"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        {"account_id": "UTR_1"},
        {"utr_number": "UTR_999"},
        {"description_contains": "UTR_1"},
        {"description_contains": "prefix UTR_1 suffix"},
    ],
)
async def test_sensitive_placeholder_rehydration_is_typed_and_fail_closed(filters):
    model_query = valid_query(filters=filters)
    with patch(
        "httpx.AsyncClient.post",
        return_value=ollama_response(json.dumps(model_query)),
    ):
        result = await understand_with_ollama(
            "Find UTR HDFC202609051234567890",
            discovered_capabilities=full_capabilities(),
            base_url="http://test",
            model="test",
        )
    assert isinstance(result.output, QueryRefusal)
    assert result.output.reason == QueryRefusalReason.INVALID_STRUCTURE


@pytest.mark.asyncio
async def test_second_turn_rules_miss_succeeds_via_ollama_with_safe_inherited_semantics(
    ollama_api_client, ollama_api_dependencies
):
    engine, store = ollama_api_dependencies
    first = await ollama_api_client.post(
        "/api/chat",
        json={"question": "How much did I spend in August 2026?", "conversation_id": "ctx-ollama"},
    )
    assert first.json()["meta"]["llm_calls"] == 0
    stored = store.get("ctx-ollama")
    assert stored is not None
    stored.filters.min_amount = Decimal("887766.55")
    stored.filters.account_id = "acct-secret-98765"
    store.put("ctx-ollama", stored)

    stored.filters.bank_code = "HDFC"
    store.put("ctx-ollama", stored)
    followup = "Please reprise that same lens, tersely."
    model_json = valid_query()
    captured = {}
    with (
        patch("app.main.settings") as settings,
        patch("app.main.understand_question", return_value=None),
        patch(
            "app.understanding.ollama.httpx.AsyncClient",
            fake_ollama_client(ollama_response(json.dumps(model_json)), captured),
        ),
    ):
        settings.ARTHA_OLLAMA_ENABLED = True
        settings.ARTHA_OLLAMA_BASE_URL = "http://test"
        settings.ARTHA_OLLAMA_MODEL = "requested"
        settings.ARTHA_OLLAMA_TIMEOUT = 3
        second = await ollama_api_client.post("/api/chat", json={"question": followup, "conversation_id": "ctx-ollama"})

    body = second.json()
    assert body["interpretation"]["date_range"]["start"] == "2026-08-01"
    assert body["interpretation"]["filters"]["transaction_type"] == "debit"
    assert body["meta"]["llm_calls"] == 1
    payload_text = json.dumps(captured["json"])
    context_message = captured["json"]["messages"][1]["content"]
    assert '"intent":"transaction_summary"' in context_message
    assert '"start":"2026-08-01"' in context_message
    assert '"filters":{"bank_code":"HDFC","transaction_type":"debit"}' in context_message
    assert "887766.55" not in payload_text
    assert "acct-secret-98765" not in payload_text
    assert "1000.00" not in payload_text
    assert len(engine.calls) == 2


@pytest.mark.asyncio
async def test_ollama_result_and_api_preserve_all_metadata(ollama_api_client):
    with (
        patch("app.main.settings") as settings,
        patch("app.main.understand_question", return_value=None),
        patch(
            "app.understanding.ollama.httpx.AsyncClient",
            fake_ollama_client(ollama_response(json.dumps(valid_query()))),
        ),
    ):
        settings.ARTHA_OLLAMA_ENABLED = True
        settings.ARTHA_OLLAMA_BASE_URL = "http://test"
        settings.ARTHA_OLLAMA_MODEL = "requested"
        settings.ARTHA_OLLAMA_TIMEOUT = 3
        response = await ollama_api_client.post("/api/chat", json={"question": "opaque phrasing for fallback"})
    meta = response.json()["meta"]
    assert meta["ollama_model"] == "actual-model"
    assert meta["ollama_prompt_tokens"] == 25
    assert meta["ollama_completion_tokens"] == 50
    assert meta["ollama_total_tokens"] == 75
    assert meta["ollama_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_malformed_model_json_is_invalid_structure():
    with patch("httpx.AsyncClient.post", return_value=ollama_response("not JSON")):
        result = await understand_with_ollama("unclear", base_url="http://test", model="test")
    assert isinstance(result.output, QueryRefusal)
    assert result.output.reason == QueryRefusalReason.INVALID_STRUCTURE
    assert result.metadata.prompt_tokens == 25


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError("offline"), QueryRefusalReason.UPSTREAM_UNAVAILABLE),
        (httpx.ReadTimeout("slow"), QueryRefusalReason.UPSTREAM_TIMEOUT),
    ],
)
async def test_transport_failures_are_not_user_ambiguity(error, reason):
    with patch("httpx.AsyncClient.post", side_effect=error):
        result = await understand_with_ollama("unclear", base_url="http://test", model="test")
    assert isinstance(result.output, QueryRefusal)
    assert result.output.reason == reason
    assert result.output.reason != QueryRefusalReason.AMBIGUOUS


def test_context_serializer_excludes_every_financial_or_identifier_value():
    context = ConversationContext(
        intent=Intent.TRANSACTION_SUMMARY,
        metric=Metric.TRANSACTION_AMOUNT,
        aggregation=Aggregation.SUM,
        filters=QueryFilters(
            account_id="secret-account",
            reference_id="secret-reference",
            utr_number="secret-utr",
            min_amount=Decimal("12345.67"),
            transaction_type="debit",
        ),
        date_range=DateRange(start=date(2026, 8, 1), end=date(2026, 9, 1), label="August 2026"),
        group_by=[],
        result_reference="secret-evidence-reference",
    )
    from app.understanding.ollama import semantic_context

    serialized = json.dumps(semantic_context(context))
    for raw in ["secret-account", "secret-reference", "secret-utr", "12345.67", "secret-evidence-reference"]:
        assert raw not in serialized
    assert '"filters": {"transaction_type": "debit"}' in serialized
