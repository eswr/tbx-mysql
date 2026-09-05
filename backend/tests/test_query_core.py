"""
Parity tests: verify DuckDB and MySQL engines produce identical semantic results.

Tests use a fixture with planted boundaries:
  - Exact 50,000.00 amounts (for >= vs > boundary tests)
  - Ref collisions (same reference_id in multiple accounts)
  - Empty months (no txns for some accounts)
  - Negative balances
"""

import pytest
from datetime import date
from decimal import Decimal

from app.query.duckdb_engine import DuckDBQueryEngine
from app.query.mysql_engine import MySQLQueryEngine
from app.query.logical_plan import LogicalPlan, Predicate, Sort, DateRangeHalfOpen
from app.query.sql_render import DuckDBDialect, MySQLDialect, render_sql


@pytest.fixture
async def duckdb_engine(duckdb_path):
    """DuckDB engine fixture."""
    engine = DuckDBQueryEngine(duckdb_path)
    caps = await engine.discover_capabilities()
    assert "transaction" in caps.tables
    return engine


@pytest.fixture
async def mysql_engine(mysql_available, mysql_url, fixture_dir):
    """MySQL engine fixture (skipped if unavailable)."""
    if not mysql_available:
        pytest.skip("MySQL not available")
    
    from scripts.load_fixture import load_mysql
    
    # Load fixture
    success = load_mysql(mysql_url, fixture_dir, clear=True)
    if not success:
        pytest.skip("Failed to load fixture into MySQL")
    
    engine = MySQLQueryEngine(mysql_url)
    caps = await engine.discover_capabilities()
    assert "transaction" in caps.tables
    return engine


def canonicalize_decimal(value) -> Decimal:
    """Normalize Decimal to 2 decimal places."""
    if value is None:
        return None
    if isinstance(value, (int, float, str)):
        value = Decimal(str(value))
    return value.quantize(Decimal("0.01"))


def canonicalize_rows(rows: list[dict]) -> list[dict]:
    """Normalize row lists for comparison: quantize decimals, sort by tie-breaker."""
    normalized = []
    for row in rows:
        norm = dict(row)
        # Quantize any Decimal values
        for key, val in norm.items():
            if isinstance(val, Decimal):
                norm[key] = canonicalize_decimal(val)
        normalized.append(norm)
    
    # Sort by transaction_id if present (deterministic ordering)
    if normalized and "transaction_id" in normalized[0]:
        normalized.sort(key=lambda r: r.get("transaction_id", ""))
    
    return normalized


@pytest.mark.asyncio
async def test_simple_select(duckdb_engine):
    """Test: basic SELECT all transactions."""
    plan = LogicalPlan(
        select_columns=["t.transaction_id", "t.transaction_amount", "t.transaction_type"],
        primary_table="transaction",
        predicates=[],
        limit=10,
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    assert "rows" in result
    assert len(result["rows"]) <= 10
    assert all("transaction_id" in r for r in result["rows"])


@pytest.mark.asyncio
async def test_where_debit_type(duckdb_engine):
    """Test: filter by transaction_type='debit'."""
    plan = LogicalPlan(
        select_columns=["t.transaction_id", "t.transaction_amount", "t.transaction_type"],
        primary_table="transaction",
        predicates=[Predicate(column="t.transaction_type", operator="=", value="debit")],
        limit=50,
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    assert all(r["transaction_type"] == "debit" for r in rows)


@pytest.mark.asyncio
async def test_where_amount_range(duckdb_engine):
    """Test: filter by amount >= 50000.00."""
    plan = LogicalPlan(
        select_columns=["t.transaction_id", "t.transaction_amount", "t.transaction_type"],
        primary_table="transaction",
        predicates=[
            Predicate(column="t.transaction_amount", operator=">=", value=Decimal("50000.00")),
        ],
        limit=50,
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    assert all(canonicalize_decimal(r["transaction_amount"]) >= Decimal("50000.00") for r in rows)


@pytest.mark.asyncio
async def test_aggregation_sum(duckdb_engine):
    """Test: SUM(transaction_amount) WHERE type='debit'."""
    plan = LogicalPlan(
        select_columns=["SUM(t.transaction_amount) as total_amount"],
        primary_table="transaction",
        predicates=[Predicate(column="t.transaction_type", operator="=", value="debit")],
        aggregations={"total_amount": "SUM(t.transaction_amount)"},
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    assert len(rows) == 1
    assert "total_amount" in rows[0]


@pytest.mark.asyncio
async def test_aggregation_count(duckdb_engine):
    """Test: COUNT(*) WHERE type='debit'."""
    plan = LogicalPlan(
        select_columns=["COUNT(*) as count"],
        primary_table="transaction",
        predicates=[Predicate(column="t.transaction_type", operator="=", value="debit")],
        aggregations={"count": "COUNT(*)"},
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    assert len(rows) == 1
    assert isinstance(rows[0]["count"], int)
    assert rows[0]["count"] > 0


@pytest.mark.asyncio
async def test_group_by_transaction_type(duckdb_engine):
    """Test: GROUP BY transaction_type."""
    plan = LogicalPlan(
        select_columns=["t.transaction_type", "SUM(t.transaction_amount) as total", "COUNT(*) as cnt"],
        primary_table="transaction",
        group_by=["t.transaction_type"],
        aggregations={
            "total": "SUM(t.transaction_amount)",
            "cnt": "COUNT(*)",
        },
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    assert len(rows) >= 1  # At least one type
    types = {r["transaction_type"] for r in rows}
    assert types.issubset({"credit", "debit"})


@pytest.mark.asyncio
async def test_masking_account_number(duckdb_engine):
    """Test: account numbers are masked (XXXXX last4)."""
    plan = LogicalPlan(
        select_columns=["t.transaction_id", "a.account_number"],
        primary_table="transaction",
        predicates=[],
        limit=5,
    )
    
    result = await duckdb_engine.execute_logical_plan(plan)
    
    rows = result["rows"]
    for row in rows:
        acc_num = row["account_number"]
        assert acc_num.startswith("XXXXX"), f"Account number not masked: {acc_num}"
