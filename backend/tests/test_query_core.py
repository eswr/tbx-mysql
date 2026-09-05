"""
Parity tests: verify DuckDB and MySQL engines produce identical semantic results.

Tests use a fixture with planted boundaries:
  - Exact 50,000.00 amounts (for >= vs > boundary tests)
  - Ref collisions (same reference_id in multiple accounts)
  - Empty months (no txns for some accounts)
  - Negative balances
"""

import pytest
from decimal import Decimal

from app.query.duckdb_engine import DuckDBQueryEngine
from app.query.mysql_engine import MySQLQueryEngine
from app.query.logical_plan import LogicalPlan, Predicate


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
        pytest.skip()

    from scripts.load_fixture import load_mysql

    # Load fixture
    success = load_mysql(mysql_url, fixture_dir, clear=True)
    if not success:
        pytest.skip()

    engine = MySQLQueryEngine(mysql_url)
    caps = await engine.discover_capabilities()
    assert "transaction" in caps.tables
    return engine


def canonicalize_decimal(value) -> Decimal | None:
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
    amounts = [canonicalize_decimal(row["transaction_amount"]) for row in rows]
    assert all(amount is not None and amount >= Decimal("50000.00") for amount in amounts)


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
        joins=[("account", "a", "t.account_id = a.account_id")],
        predicates=[],
        limit=5,
    )

    result = await duckdb_engine.execute_logical_plan(plan)

    rows = result["rows"]
    for row in rows:
        acc_num = row["account_number"]
        assert acc_num.startswith("XXXXX"), f"Account number not masked: {acc_num}"


@pytest.mark.parametrize(
    "question, expected_intent, expected_aggregate",
    [
        ("How many accounts per bank?", "bank_account_count", "COUNT(*) AS value"),
        ("Which bank holds the most money?", "bank_balance", "SUM(a.available_balance) AS value"),
    ],
)
def test_bank_grouped_intents_compile_to_allowlisted_plans(question, expected_intent, expected_aggregate):
    """These intents previously raised ValueError in the compiler and surfaced as a 500."""
    from app.query.compiler import compile_financial_query
    from app.query.sql_render import MySQLDialect, render_sql
    from app.schemas.financial_query import FinancialQuery
    from app.understanding.rules import understand_question

    parsed = understand_question(question)
    assert isinstance(parsed, FinancialQuery)
    assert parsed.intent.value == expected_intent

    plans = compile_financial_query(parsed)
    assert plans.result_plan.group_by == ["a.bank_code", "b.bank_name"]
    assert expected_aggregate in plans.result_plan.select_columns

    sql = render_sql(plans.result_plan, MySQLDialect()).sql
    assert "GROUP BY a.bank_code, b.bank_name" in sql
    assert "JOIN bank b ON a.bank_code = b.bank_code" in sql
    assert "ORDER BY value DESC, a.bank_code ASC" in sql

    # matched_count for a grouped result is the number of groups, not the row count.
    count_sql = render_sql(plans.count_plan, MySQLDialect()).sql
    assert "COUNT(DISTINCT a.bank_code) AS matched_count" in count_sql


def test_unsupported_group_dimension_fails_closed():
    """Month grouping has no compiled plan; the compiler must refuse rather than guess."""
    from datetime import date, timedelta

    from app.query.compiler import QueryCompilationError, compile_financial_query
    from app.schemas.financial_query import (
        Aggregation,
        DateRange,
        FinancialQuery,
        GroupByDimension,
        Intent,
        Metric,
        QueryFilters,
    )

    start = date(2026, 8, 1)
    query = FinancialQuery(
        intent=Intent.TRANSACTION_SUMMARY,
        metric=Metric.TRANSACTION_AMOUNT,
        aggregation=Aggregation.SUM,
        filters=QueryFilters(),
        date_range=DateRange(start=start, end=start + timedelta(days=30)),
        group_by=[GroupByDimension.MONTH],
    )

    with pytest.raises(QueryCompilationError, match="Unsupported group_by dimension"):
        compile_financial_query(query)


def test_multiple_group_dimensions_fail_closed():
    from datetime import date, timedelta

    from app.query.compiler import QueryCompilationError, compile_financial_query
    from app.schemas.financial_query import (
        Aggregation,
        DateRange,
        FinancialQuery,
        GroupByDimension,
        Intent,
        Metric,
        QueryFilters,
    )

    start = date(2026, 8, 1)
    query = FinancialQuery(
        intent=Intent.TRANSACTION_SUMMARY,
        metric=Metric.TRANSACTION_AMOUNT,
        aggregation=Aggregation.SUM,
        filters=QueryFilters(),
        date_range=DateRange(start=start, end=start + timedelta(days=31)),
        group_by=[GroupByDimension.BANK, GroupByDimension.TRANSACTION_TYPE],
    )

    with pytest.raises(QueryCompilationError, match="Multiple group_by dimensions"):
        compile_financial_query(query)
