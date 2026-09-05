"""Focused tests for MySQL capability discovery."""

from app.query.mysql_engine import MySQLQueryEngine


class EmptyDatabaseCursor:
    def __init__(self):
        self.executed = []
        self.result = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "SELECT table_name FROM information_schema.tables" in sql:
            self.result = [{"table_name": "transaction"}, {"table_name": "account"}, {"table_name": "bank"}]
        elif "SELECT column_name FROM information_schema.columns" in sql:
            self.result = []
        elif "transaction_type='debit'" in sql or "SELECT transaction_date" in sql:
            self.result = None
        elif "SELECT bank_code, bank_name" in sql:
            self.result = []
        elif "SELECT COUNT(*) as cnt FROM account" in sql:
            self.result = {"cnt": 0}
        elif "SELECT COUNT(*) as cnt FROM `transaction`" in sql:
            self.result = {"cnt": 0}
        elif "SELECT MIN(transaction_date)" in sql:
            self.result = {"start": None, "end": None}

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result

    def close(self):
        pass


class EmptyDatabaseConnection:
    def __init__(self):
        self.cursor_instance = EmptyDatabaseCursor()

    def cursor(self):
        return self.cursor_instance

    def close(self):
        pass


def test_empty_transaction_table_reports_zero_without_estimate_warning():
    connection = EmptyDatabaseConnection()
    engine = object.__new__(MySQLQueryEngine)
    engine.conn_kwargs = {"database": "artha"}
    engine.max_execution_time_ms = 10_000
    engine.utr_mode = "plaintext"
    engine._get_connection = lambda: connection

    capabilities = engine._discover_capabilities_sync()

    assert capabilities.transaction_count == 0
    assert capabilities.warnings == []
    assert any(sql == "SELECT COUNT(*) as cnt FROM `transaction`" for sql, _ in connection.cursor_instance.executed)
    assert not any("table_rows AS cnt" in sql for sql, _ in connection.cursor_instance.executed)
