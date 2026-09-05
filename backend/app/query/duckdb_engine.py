"""DuckDB query engine for local development and testing."""

import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from app.query.base import Capabilities, BankInfo, QueryEngine
from app.query.masking import mask_record
from app.query.sql_render import DuckDBDialect, render_sql
from app.query.logical_plan import LogicalPlan, Predicate, DateRangeHalfOpen

logger = logging.getLogger(__name__)


class DuckDBQueryEngine(QueryEngine):
    """Query engine backed by DuckDB."""
    
    def __init__(self, db_path: str = "artha.duckdb"):
        self.db_path = Path(db_path)
        self.conn = duckdb.connect(str(self.db_path))
        self.dialect = DuckDBDialect()
    
    async def discover_capabilities(self) -> Capabilities:
        """Probe the DuckDB database."""
        caps = Capabilities()
        
        try:
            # Check tables
            tables_result = self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
            caps.tables = [row[0] for row in tables_result]
            
            # Check columns per table
            for table in caps.tables:
                cols_result = self.conn.execute(
                    f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}'"
                ).fetchall()
                caps.columns[table] = [row[0] for row in cols_result]
            
            # Probe debit sign convention
            try:
                min_debit = self.conn.execute(
                    "SELECT MIN(transaction_amount) FROM transaction WHERE transaction_type='debit'"
                ).fetchall()
                if min_debit and min_debit[0][0] is not None:
                    if min_debit[0][0] < 0:
                        caps.debit_sign = "negative"
                    else:
                        caps.debit_sign = "positive"
            except Exception as e:
                logger.warning(f"Failed to probe debit sign: {e}")
            
            # Probe date granularity
            try:
                sample = self.conn.execute(
                    "SELECT transaction_date FROM transaction LIMIT 1"
                ).fetchall()
                if sample:
                    # DuckDB returns timestamp or date
                    ts = sample[0][0]
                    if hasattr(ts, "time") and ts.time() != datetime.min.time():
                        caps.date_granularity = "datetime"
                    else:
                        caps.date_granularity = "date"
            except Exception as e:
                logger.warning(f"Failed to probe date granularity: {e}")
            
            # Get bank list
            try:
                banks_result = self.conn.execute(
                    "SELECT bank_code, bank_name FROM bank ORDER BY bank_code"
                ).fetchall()
                caps.banks = [BankInfo(code=row[0], name=row[1]) for row in banks_result]
            except Exception as e:
                logger.warning(f"Failed to load banks: {e}")
            
            # Get counts and date range
            try:
                counts = self.conn.execute(
                    "SELECT COUNT(*) FROM bank, COUNT(*) FROM account, COUNT(*) FROM transaction"
                ).fetchall()
                caps.account_count = counts[1][0]
                # transaction count is separate
            except:
                pass
            
            try:
                date_range = self.conn.execute(
                    "SELECT MIN(transaction_date), MAX(transaction_date) FROM transaction"
                ).fetchall()
                if date_range and date_range[0][0]:
                    caps.date_range_start = date_range[0][0]
                    caps.date_range_end = date_range[0][1]
            except Exception as e:
                logger.warning(f"Failed to get date range: {e}")
            
            caps.utr_mode = "plaintext"  # DuckDB: always plaintext
            
        except Exception as e:
            logger.error(f"Capability discovery failed: {e}")
            caps.warnings.append(f"Discovery error: {e}")
        
        return caps
    
    async def execute_logical_plan(self, plan: LogicalPlan) -> dict:
        """Execute a LogicalPlan."""
        compiled = render_sql(plan, self.dialect)
        
        logger.debug(f"DuckDB SQL: {compiled.sql}")
        logger.debug(f"Params: {compiled.params}")
        
        result = self.conn.execute(compiled.sql, compiled.params).fetchall()
        columns = [desc[0] for desc in self.conn.description]
        
        rows = [dict(zip(columns, row)) for row in result]
        
        # Apply masking
        rows = [mask_record(r) for r in rows]
        
        return {
            "rows": rows,
            "sql": compiled.sql,
            "dialect": "duckdb",
        }
    
    async def execute_transaction_list(
        self,
        predicates: list,
        order_by: list,
        limit: int,
    ) -> dict:
        """Execute a transaction list query."""
        # TODO: implement
        pass
    
    async def execute_aggregation(
        self,
        aggregations: dict,
        group_by: list,
        predicates: list,
        order_by: list | None = None,
        limit: int | None = None,
    ) -> dict:
        """Execute an aggregation query."""
        # TODO: implement
        pass
    
    async def ping(self) -> bool:
        """Test connectivity."""
        try:
            self.conn.execute("SELECT 1").fetchall()
            return True
        except:
            return False
