"""Conversation store persistence, isolation, and safety tests."""

import sqlite3
from datetime import date

from app.config import ConversationStoreType
from app.conversation import ConversationContext, InMemoryConversationStore, SQLiteConversationStore
from app.main import get_conversation_store
from app.schemas.financial_query import Aggregation, DateRange, FinancialQuery, Intent, Metric, QueryFilters
from app.understanding.rules import understand_question


def context(*, utr_number: str | None = None, result_reference: str | None = None) -> ConversationContext:
    query = FinancialQuery(
        intent=Intent.TRANSACTION_SUMMARY,
        metric=Metric.TRANSACTION_AMOUNT,
        aggregation=Aggregation.SUM,
        filters=QueryFilters(transaction_type="debit", utr_number=utr_number),
        date_range=DateRange(start=date(2026, 8, 1), end=date(2026, 9, 1), label="August 2026"),
    )
    return ConversationContext.from_query(query).model_copy(update={"result_reference": result_reference})


def test_context_survives_new_store_instance(tmp_path):
    db_path = tmp_path / "conversation.db"
    SQLiteConversationStore(str(db_path)).put("durable", context())

    loaded = SQLiteConversationStore(str(db_path)).get("durable")

    assert loaded == context()


def test_follow_up_semantics_work_after_reload(tmp_path):
    db_path = tmp_path / "conversation.db"
    SQLiteConversationStore(str(db_path)).put("follow-up", context())

    parsed = understand_question(
        "What about July?",
        context=SQLiteConversationStore(str(db_path)).get("follow-up"),
        reference_date=date(2026, 9, 5),
    )

    assert isinstance(parsed, FinancialQuery)
    assert parsed.filters.transaction_type == "debit"
    assert parsed.date_range.start == date(2026, 7, 1)
    assert parsed.date_range.end == date(2026, 8, 1)


def test_conversations_are_isolated_by_id(tmp_path):
    store = SQLiteConversationStore(str(tmp_path / "conversation.db"))
    first = context()
    second = context().model_copy(
        update={"date_range": DateRange(start=date(2026, 7, 1), end=date(2026, 8, 1), label="July 2026")}
    )
    store.put("first", first)
    store.put("second", second)

    assert store.get("first") == first
    assert store.get("second") == second
    assert store.get("missing") is None


def test_only_semantic_non_sensitive_context_is_persisted(tmp_path):
    db_path = tmp_path / "conversation.db"
    store = SQLiteConversationStore(str(db_path))
    store.put("private", context(utr_number="UTR-SECRET-123", result_reference="RESULT-1250.50"))

    persisted = db_path.read_bytes()
    assert b"UTR-SECRET-123" not in persisted
    assert b"RESULT-1250.50" not in persisted
    assert b"evidence" not in persisted
    loaded = store.get("private")
    assert loaded is not None
    assert loaded.filters.utr_number is None
    with sqlite3.connect(db_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(conversation_contexts)")]
    assert columns == ["conversation_id", "context_json", "updated_at"]


def test_memory_configuration_still_works(monkeypatch):
    monkeypatch.setenv("ARTHA_CONVERSATION_STORE", ConversationStoreType.MEMORY.value)

    store = get_conversation_store()

    assert isinstance(store, InMemoryConversationStore)
    store.put("memory", context())
    assert store.get("memory") == context()


def test_sqlite_configuration_uses_configured_path(monkeypatch, tmp_path):
    db_path = tmp_path / "configured" / "artha.db"
    monkeypatch.setenv("ARTHA_CONVERSATION_STORE", ConversationStoreType.SQLITE.value)
    monkeypatch.setenv("ARTHA_SQLITE_DB_PATH", str(db_path))

    store = get_conversation_store()

    assert isinstance(store, SQLiteConversationStore)
    assert store.db_path == db_path
    assert db_path.exists()


def test_malformed_persisted_state_fails_safely(tmp_path):
    db_path = tmp_path / "conversation.db"
    store = SQLiteConversationStore(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO conversation_contexts VALUES (?, ?, ?)",
            ("broken", '{"filters":{"transaction_type":"credit"}}', "2026-09-05T00:00:00+00:00"),
        )

    assert SQLiteConversationStore(str(db_path)).get("broken") is None
    parsed = understand_question("What about July?", context=store.get("broken"), reference_date=date(2026, 9, 5))
    assert not isinstance(parsed, FinancialQuery)


def test_sqlite_enables_wal_and_busy_timeout(tmp_path):
    db_path = tmp_path / "conversation.db"
    store = SQLiteConversationStore(str(db_path), busy_timeout_ms=4_321)

    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 4_321
