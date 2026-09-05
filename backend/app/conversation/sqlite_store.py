"""Durable local storage for semantic conversation context."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.conversation.context import ConversationContext

logger = logging.getLogger(__name__)


class SQLiteConversationStore:
    """Persist validated conversation context without retaining query results."""

    def __init__(self, db_path: str, *, busy_timeout_ms: int = 5_000):
        self.db_path = Path(db_path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1_000)
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        """Create the parent directory and schema safely and idempotently."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_contexts (
                    conversation_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get(self, conversation_id: str) -> ConversationContext | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT context_json FROM conversation_contexts WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            context = ConversationContext.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError):
            logger.warning("Ignoring malformed conversation context for %r", conversation_id)
            return None
        if (
            context.intent.value == "reference_lookup"
            and context.filters.reference_id is None
            and context.filters.utr_number is None
        ):
            logger.warning("Ignoring conversation context without its non-durable identifier for %r", conversation_id)
            return None
        return context

    def put(self, conversation_id: str, context: ConversationContext) -> None:
        # result_reference is intentionally never durable. UTRs are sensitive
        # transaction identifiers and are also omitted from local state.
        payload = context.model_dump(mode="json", exclude={"result_reference"})
        payload["filters"]["utr_number"] = None
        context_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        updated_at = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_contexts (conversation_id, context_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    context_json = excluded.context_json,
                    updated_at = excluded.updated_at
                """,
                (conversation_id, context_json, updated_at),
            )
