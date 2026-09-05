"""MySQL query engine for production and parity testing."""

import asyncio
import logging
from datetime import datetime
from urllib.parse import urlparse

import pymysql
from pymysql.cursors import DictCursor

from app.config import UTRMode, get_settings
from app.query.base import Capabilities, BankInfo, QueryEngine
from app.query.masking import mask_record
from app.query.sql_render import MySQLDialect, render_sql, validate_sql_select_only
from app.query.logical_plan import LogicalPlan

logger = logging.getLogger(__name__)


class MySQLQueryEngine(QueryEngine):
    """Query engine backed by MySQL."""

    def __init__(self, db_url: str, utr_mode: UTRMode | str | None = None):
        self.db_url = db_url
        self.dialect = MySQLDialect()
        settings = get_settings()
        self.conn_kwargs = self._parse_url(db_url)
        self.max_execution_time_ms = settings.ARTHA_MYSQL_MAX_EXECUTION_TIME_MS
        configured_utr_mode = utr_mode or settings.ARTHA_UTR_MODE
        self.utr_mode = (
            configured_utr_mode.value
            if isinstance(configured_utr_mode, UTRMode)
            else UTRMode(configured_utr_mode).value
        )

    def _parse_url(self, db_url: str) -> dict:
        """Parse MySQL URL and return connection kwargs."""
        if not db_url.startswith("mysql://"):
            db_url = f"mysql://{db_url}"

        parsed = urlparse(db_url)
        return {
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 3306,
            "user": parsed.username or "root",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") if parsed.path else "artha",
            "charset": "utf8mb4",
            "cursorclass": DictCursor,
            "autocommit": True,
            "connect_timeout": get_settings().ARTHA_MYSQL_CONNECT_TIMEOUT,
            "read_timeout": get_settings().ARTHA_MYSQL_READ_TIMEOUT,
            "write_timeout": get_settings().ARTHA_MYSQL_WRITE_TIMEOUT,
        }

    def _get_connection(self):
        """Get a new connection."""
        return pymysql.connect(**self.conn_kwargs)

    async def discover_capabilities(self) -> Capabilities:
        """Probe the MySQL database."""
        return await asyncio.to_thread(self._discover_capabilities_sync)

    def _discover_capabilities_sync(self) -> Capabilities:
        """Probe MySQL without blocking the application's async event loop."""
        caps = Capabilities(utr_mode=self.utr_mode)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={self.max_execution_time_ms}")
            # Check tables
            cursor.execute(
                f"SELECT table_name FROM information_schema.tables WHERE table_schema='{self.conn_kwargs['database']}'"
            )
            caps.tables = [next(iter(row.values())) for row in cursor.fetchall()]

            # Check columns per table
            for table in caps.tables:
                cursor.execute(
                    f"SELECT column_name FROM information_schema.columns WHERE table_schema='{self.conn_kwargs['database']}' AND table_name='{table}'"
                )
                caps.columns[table] = [next(iter(row.values())) for row in cursor.fetchall()]

            # Probe debit sign convention
            try:
                cursor.execute("SELECT transaction_amount FROM `transaction` WHERE transaction_type='debit' LIMIT 1")
                result = cursor.fetchone()
                if result and result["transaction_amount"] is not None:
                    if result["transaction_amount"] < 0:
                        caps.debit_sign = "negative"
                    else:
                        caps.debit_sign = "positive"
            except Exception as e:
                logger.warning(f"Failed to probe debit sign: {e}")

            # Probe date granularity
            try:
                cursor.execute("SELECT transaction_date FROM `transaction` LIMIT 1")
                result = cursor.fetchone()
                if result:
                    ts = result["transaction_date"]
                    if isinstance(ts, datetime) and ts.time() != datetime.min.time():
                        caps.date_granularity = "datetime"
                    else:
                        caps.date_granularity = "date"
            except Exception as e:
                logger.warning(f"Failed to probe date granularity: {e}")

            # Get bank list (authoritative)
            try:
                cursor.execute("SELECT bank_code, bank_name FROM bank ORDER BY bank_code")
                caps.banks = [BankInfo(code=row["bank_code"], name=row["bank_name"]) for row in cursor.fetchall()]
            except Exception as e:
                logger.warning(f"Failed to load banks: {e}")

            # Get counts
            try:
                cursor.execute("SELECT COUNT(*) as cnt FROM account")
                caps.account_count = cursor.fetchone()["cnt"]

                cursor.execute("SELECT COUNT(*) as cnt FROM `transaction`")
                # COUNT(*) returns one result row even when the table is empty;
                # use the scalar count in that row so an empty table stays 0.
                caps.transaction_count = cursor.fetchone()["cnt"]
            except Exception as e:
                logger.warning(f"Failed to get counts: {e}")

            # Get date range
            try:
                cursor.execute("SELECT MIN(transaction_date) as start, MAX(transaction_date) as end FROM `transaction`")
                result = cursor.fetchone()
                if result and result["start"]:
                    caps.date_range_start = result["start"]
                    caps.date_range_end = result["end"]
            except Exception as e:
                logger.warning(f"Failed to get date range: {e}")

        except Exception as e:
            logger.error(f"Capability discovery failed: {e}")
            caps.warnings.append(f"Discovery error: {e}")

        finally:
            cursor.close()
            conn.close()

        return caps

    async def execute_logical_plan(self, plan: LogicalPlan) -> dict:
        """Execute a LogicalPlan."""
        return await asyncio.to_thread(self._execute_logical_plan_sync, plan)

    def _execute_logical_plan_sync(self, plan: LogicalPlan) -> dict:
        """Execute one logical plan without blocking the async event loop."""
        compiled = render_sql(plan, self.dialect)

        # Validate
        validate_sql_select_only(compiled.sql, "mysql")

        logger.debug(f"MySQL SQL: {compiled.sql}")
        logger.debug(f"Params: {compiled.params}")

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # Set session options for safety and consistency
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={self.max_execution_time_ms}")

            cursor.execute(compiled.sql, compiled.params)
            rows = cursor.fetchall()

            # Apply masking
            rows = [mask_record(r) for r in rows]

            return {
                "rows": rows,
                "sql": compiled.sql,
                "dialect": "mysql",
            }

        finally:
            cursor.close()
            conn.close()

    async def execute_snapshot(self, plans: list[LogicalPlan], oracle_sqls: list[str] | None = None) -> dict:
        """Execute result, evidence count, and oracle reads in one read-only consistent snapshot."""
        return await asyncio.to_thread(self._execute_snapshot_sync, plans, oracle_sqls)

    def _execute_snapshot_sync(self, plans: list[LogicalPlan], oracle_sqls: list[str] | None = None) -> dict:
        """Execute a consistent snapshot without blocking the async event loop."""
        compiled_plans = [render_sql(plan, self.dialect) for plan in plans]
        for compiled in compiled_plans:
            validate_sql_select_only(compiled.sql, "mysql")
        for sql in oracle_sqls or []:
            validate_sql_select_only(sql, "mysql")

        conn = self._get_connection()
        cursor = conn.cursor()
        plan_rows = []
        oracle_rows = []
        try:
            cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={self.max_execution_time_ms}")
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            for compiled in compiled_plans:
                cursor.execute(compiled.sql, compiled.params)
                plan_rows.append([mask_record(row) for row in cursor.fetchall()])
            for sql in oracle_sqls or []:
                cursor.execute(sql)
                rows = cursor.fetchall()
                oracle_rows.append([tuple(row.values()) for row in rows])
        finally:
            conn.rollback()
            cursor.close()
            conn.close()
        return {"plan_rows": plan_rows, "oracle_rows": oracle_rows, "sql": [item.sql for item in compiled_plans]}

    async def execute_transaction_list(
        self,
        predicates: list,
        order_by: list,
        limit: int,
    ) -> dict:
        """Legacy API retained for interface compatibility."""
        raise NotImplementedError("Use execute_logical_plan or execute_snapshot")

    async def execute_aggregation(
        self,
        aggregations: dict,
        group_by: list,
        predicates: list,
        order_by: list | None = None,
        limit: int | None = None,
    ) -> dict:
        """Legacy API retained for interface compatibility."""
        raise NotImplementedError("Use execute_logical_plan or execute_snapshot")

    async def ping(self) -> bool:
        """Test connectivity."""
        return await asyncio.to_thread(self._ping_sync)

    def _ping_sync(self) -> bool:
        """Ping MySQL without blocking the async event loop."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            conn.close()
            return True
        except Exception as e:
            logger.warning(f"MySQL ping failed: {e}")
            return False
