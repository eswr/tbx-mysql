from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.query.compiler import compile_financial_query, validate_allowlisted_plan
from app.query.execution import FinancialQueryExecutor
from app.query.duckdb_engine import DuckDBQueryEngine
from app.query.mysql_engine import MySQLQueryEngine
from app.query.logical_plan import LogicalPlan, Predicate
from app.schemas.financial_query import (
    Aggregation,
    DateRange,
    FinancialQuery,
    Intent,
    Metric,
    QueryFilters,
    QueryRefusalReason,
    refusal,
)
from app.understanding.rules import understand_question
from evaluation.run_eval import (
    LOCKED_REGRESSION_SHA256,
    SUITE_FILES,
    _sha256,
    load_eval_suites,
    passes_release_gates,
    run_eval,
    score_case,
    write_result,
)


def query(*, aggregation=Aggregation.SUM, filters=None, intent=Intent.TRANSACTION_SUMMARY):
    return FinancialQuery(
        intent=intent,
        metric=Metric.TRANSACTION_COUNT if aggregation == Aggregation.COUNT else Metric.TRANSACTION_AMOUNT,
        aggregation=aggregation,
        filters=filters or QueryFilters(transaction_type="debit"),
        date_range=DateRange(start=date(2026, 8, 1), end=date(2026, 9, 1)),
        limit=20 if aggregation == Aggregation.NONE else None,
    )


class FakeEngine:
    def __init__(self, result_value=Decimal("10"), matched_count=1, oracle_value=Decimal("10"), rows=None):
        self.result_value = result_value
        self.matched_count = matched_count
        self.oracle_value = oracle_value
        self.rows = rows
        self.calls = 0

    async def execute_snapshot(self, plans, oracle_sqls):
        self.calls += 1
        result_rows = self.rows if self.rows is not None else [{"value": self.result_value}]
        oracle_rows = [[(self.oracle_value,)]] if oracle_sqls else []
        return {
            "plan_rows": [result_rows, [{"matched_count": self.matched_count}]],
            "oracle_rows": oracle_rows,
            "sql": ["SELECT result", "SELECT count"],
        }


@pytest.mark.asyncio
async def test_sparse_matched_refusal_passes_without_unrelated_fields():
    engine = FakeEngine()
    executor = FinancialQueryExecutor(engine)

    def parser(question, context):
        return refusal(QueryRefusalReason.AMBIGUOUS, "period required")

    result = await score_case(
        {"id": "r", "category": "refusal", "question": "spend?", "expected_refusal_reason": "ambiguous"},
        executor,
        parser,
    )
    assert result["passed"]
    assert result["checks"] == {"refusal_reason_match": True, "no_execution": True}
    assert engine.calls == 0


@pytest.mark.asyncio
async def test_refusal_reason_mismatch_fails():
    executor = FinancialQueryExecutor(FakeEngine())

    def parser(question, context):
        return refusal(QueryRefusalReason.UNSUPPORTED_METRIC, "unsupported")

    result = await score_case(
        {"id": "r", "category": "refusal", "question": "spend?", "expected_refusal_reason": "ambiguous"},
        executor,
        parser,
    )
    assert not result["passed"]


@pytest.mark.asyncio
async def test_sparse_correct_query_passes_and_wrong_number_fails():
    case = {"id": "q", "category": "number", "question": "spend August", "oracle_sql": "SELECT 10"}
    good = await score_case(case, FinancialQueryExecutor(FakeEngine()), lambda q, c: query())
    bad = await score_case(case, FinancialQueryExecutor(FakeEngine(oracle_value=Decimal("11"))), lambda q, c: query())
    assert good["passed"]
    assert not bad["passed"]
    assert bad["checks"]["numeric_match"] is False
    assert good["checks"]["fresh_execution"] is True


@pytest.mark.asyncio
async def test_parser_no_data_refusal_fails_without_execution():
    engine = FakeEngine(matched_count=0)
    executor = FinancialQueryExecutor(engine)

    def parser(question, context):
        return refusal(QueryRefusalReason.NO_DATA, "none")

    result = await score_case(
        {"id": "n", "category": "empty", "question": "details", "expected_refusal_reason": "no_data"}, executor, parser
    )
    assert not result["passed"]
    assert engine.calls == 0


@pytest.mark.asyncio
async def test_zero_count_is_grounded_but_empty_detail_is_no_data():
    count_engine = FakeEngine(result_value=0, matched_count=0, oracle_value=0)
    count_result = await score_case(
        {"id": "c", "category": "count", "question": "count", "oracle_sql": "SELECT 0"},
        FinancialQueryExecutor(count_engine),
        lambda q, c: query(aggregation=Aggregation.COUNT),
    )
    assert count_result["passed"]
    detail_engine = FakeEngine(matched_count=0, oracle_value=0, rows=[])
    detail_result = await score_case(
        {
            "id": "d",
            "category": "empty",
            "question": "details",
            "expected_refusal_reason": "no_data",
            "oracle_sql": "SELECT 0",
        },
        FinancialQueryExecutor(detail_engine),
        lambda q, c: query(aggregation=Aggregation.NONE, intent=Intent.TRANSACTION_LIST),
    )
    assert detail_result["passed"]


def test_expanded_suite_is_frozen_and_every_new_answer_has_an_oracle():
    suites = load_eval_suites(include_holdout=True)
    assert {name: len(cases) for name, cases in suites.items()} == {
        "regression": 26,
        "generalization": 55,
        "holdout": 20,
    }
    assert sum(len(cases) for cases in suites.values()) == 101
    assert _sha256(SUITE_FILES["regression"]) == LOCKED_REGRESSION_SHA256
    for name in ("generalization", "holdout"):
        for case in suites[name]:
            for turn in case["turns"]:
                if turn.get("expected_refusal_reason") not in {
                    "ambiguous",
                    "capability",
                    "invalid_structure",
                    "unsupported_metric",
                }:
                    assert turn["oracle_sql"]


def test_suite_loader_rejects_cross_suite_duplicate_ids(tmp_path):
    paths = {}
    for name in ("regression", "generalization", "holdout"):
        path = tmp_path / f"{name}.json"
        path.write_text(
            '[{"id":"duplicate","category":"x","turns":[{"conversation_id":"c","question":"x",'
            '"expected_refusal_reason":"ambiguous","oracle_mode":"scalar"}]}]'
        )
        paths[name] = path
    with pytest.raises(ValueError, match="unique across"):
        load_eval_suites(include_holdout=True, suite_files=paths, enforce_sizes=False)


def test_suite_loader_requires_oracle_for_new_answer(tmp_path):
    paths = {}
    for name in ("regression", "generalization", "holdout"):
        path = tmp_path / f"{name}.json"
        path.write_text("[]")
        paths[name] = path
    paths["generalization"].write_text(
        '[{"id":"missing","category":"x","turns":[{"conversation_id":"c","question":"count"}]}]'
    )
    with pytest.raises(ValueError, match="requires independent oracle SQL"):
        load_eval_suites(suite_files=paths, enforce_sizes=False)


def test_result_writer_refuses_to_overwrite(tmp_path):
    path = tmp_path / "result.json"
    write_result(path, {"first": True})
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        write_result(path, {"second": True})
    assert json.loads(path.read_text()) == {"first": True}


def test_release_gate_allows_ninety_percent_holdout():
    result = {
        "suites": {
            "regression": {"total": 26, "passed": 26, "accuracy": 1.0, "safety_refusal_accuracy": 1.0},
            "generalization": {"total": 55, "passed": 53, "accuracy": 53 / 55, "safety_refusal_accuracy": 1.0},
            "holdout": {"total": 20, "passed": 18, "accuracy": 0.9, "safety_refusal_accuracy": 1.0},
        }
    }
    assert passes_release_gates(result)


@pytest.mark.asyncio
async def test_structured_multiturn_executes_every_turn_and_isolates_conversations():
    engine = FakeEngine()
    executor = FinancialQueryExecutor(engine)

    def parser(question, context):
        if question.startswith("start debit"):
            return query(filters=QueryFilters(transaction_type="debit"))
        if question.startswith("start credit"):
            return query(filters=QueryFilters(transaction_type="credit"))
        assert context is not None
        month = 7 if "July" in question else 5
        return query(filters=context.filters).model_copy(
            update={"date_range": DateRange(start=date(2026, month, 1), end=date(2026, month + 1, 1))}
        )

    turns = []
    for conversation_id, question, transaction_type in (
        ("a", "start debit", "debit"),
        ("b", "start credit", "credit"),
        ("a", "July", "debit"),
        ("b", "May", "credit"),
    ):
        turns.append(
            {
                "conversation_id": conversation_id,
                "question": question,
                "expected_filters": {"transaction_type": transaction_type},
                "oracle_mode": "scalar",
                "oracle_sql": "SELECT 10",
            }
        )
    result = await score_case({"id": "isolation", "category": "multi_turn", "turns": turns}, executor, parser)
    assert result["passed"]
    assert engine.calls == 4


@pytest.mark.asyncio
async def test_non_holdout_duckdb_report_has_independent_suites_and_hashes(duckdb_path):
    result = await run_eval("duckdb", duckdb_path)
    assert result["selected_suites"] == ["regression", "generalization"]
    assert result["suites"]["regression"]["passed"] == 26
    assert result["suites"]["generalization"]["passed"] >= 53
    assert result["combined"]["accuracy"] >= 0.95
    assert result["combined"]["safety_refusal_accuracy"] == 1.0
    assert set(result["case_sha256"]) == {"regression", "generalization", "holdout"}


def test_threshold_operators_compile_explicitly():
    strict = compile_financial_query(query(filters=QueryFilters(min_amount="50000", min_amount_operator=">")))
    inclusive = compile_financial_query(query(filters=QueryFilters(max_amount="50000", max_amount_operator="<=")))
    assert any(p.operator == ">" for p in strict.result_plan.predicates)
    assert any(p.operator == "<=" for p in inclusive.result_plan.predicates)


def test_non_allowlisted_plan_is_rejected():
    plan = LogicalPlan(["secret"], "secrets", primary_alias="x", predicates=[Predicate("x.password", "=", "guess")])
    with pytest.raises(ValueError, match="allowlisted"):
        validate_allowlisted_plan(plan)

    injected_select = LogicalPlan(["t.transaction_amount; DROP TABLE account"], "transaction", primary_alias="t")
    with pytest.raises(ValueError, match="allowlisted"):
        validate_allowlisted_plan(injected_select)


@pytest.mark.asyncio
async def test_strict_and_inclusive_bounds_on_planted_boundary(duckdb_path):
    executor = FinancialQueryExecutor(DuckDBQueryEngine(duckdb_path))
    broad = DateRange(start=date(2025, 1, 1), end=date(2027, 1, 1))

    async def count(filters):
        q = query(aggregation=Aggregation.COUNT, filters=filters).model_copy(update={"date_range": broad})
        return (await executor.execute(q)).value

    above = await count(QueryFilters(min_amount="50000", min_amount_operator=">"))
    at_least = await count(QueryFilters(min_amount="50000", min_amount_operator=">="))
    below = await count(QueryFilters(max_amount="50000", max_amount_operator="<"))
    at_most = await count(QueryFilters(max_amount="50000", max_amount_operator="<="))
    assert at_least == above + 1
    assert at_most == below + 1
    above_lower = await count(QueryFilters(min_amount="49999.99", min_amount_operator=">"))
    at_least_lower = await count(QueryFilters(min_amount="49999.99", min_amount_operator=">="))
    below_upper = await count(QueryFilters(max_amount="50000.01", max_amount_operator="<"))
    at_most_upper = await count(QueryFilters(max_amount="50000.01", max_amount_operator="<="))
    assert at_least_lower == above_lower + 1
    assert at_most_upper == below_upper + 1


@pytest.mark.asyncio
@pytest.mark.requires_mysql
async def test_duckdb_mysql_financial_query_parity(duckdb_path, mysql_available, mysql_url, fixture_dir):
    if not mysql_available:
        pytest.fail()
    from scripts.load_fixture import load_mysql

    assert load_mysql(mysql_url, fixture_dir, clear=True)
    q = query(filters=QueryFilters(transaction_type="debit", min_amount="50000", min_amount_operator=">"))
    duck = await FinancialQueryExecutor(DuckDBQueryEngine(duckdb_path)).execute(q)
    mysql = await FinancialQueryExecutor(MySQLQueryEngine(mysql_url)).execute(q)
    assert duck.value == mysql.value
    assert duck.matched_count == mysql.matched_count


@pytest.mark.asyncio
@pytest.mark.requires_mysql
async def test_duckdb_mysql_grouped_bank_query_parity(duckdb_path, mysql_available, mysql_url, fixture_dir):
    if not mysql_available:
        pytest.fail()
    from scripts.load_fixture import load_mysql

    assert load_mysql(mysql_url, fixture_dir, clear=True)
    duck_executor = FinancialQueryExecutor(DuckDBQueryEngine(duckdb_path))
    mysql_executor = FinancialQueryExecutor(MySQLQueryEngine(mysql_url))

    for question in ("How many accounts per bank?", "Which bank holds the most money?"):
        parsed = understand_question(question)
        assert isinstance(parsed, FinancialQuery)
        duck = await duck_executor.execute(parsed)
        mysql = await mysql_executor.execute(parsed)
        assert duck.rows == mysql.rows
        assert duck.matched_count == mysql.matched_count == 7


@pytest.mark.asyncio
@pytest.mark.requires_mysql
async def test_mysql_result_and_count_share_consistent_snapshot(mysql_available, mysql_url, fixture_dir):
    if not mysql_available:
        pytest.fail()
    from scripts.load_fixture import load_mysql

    assert load_mysql(mysql_url, fixture_dir, clear=True)
    engine = MySQLQueryEngine(mysql_url)
    original_get_connection = engine._get_connection
    setup = original_get_connection()
    with setup.cursor() as cursor:
        cursor.execute("SELECT account_id FROM account LIMIT 1")
        account_id = cursor.fetchone()["account_id"]
    setup.close()
    transaction_id = str(uuid4())
    reference_id = f"SNAP-{uuid4().hex[:16]}"

    class HookCursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.triggered = False

        def execute(self, *args, **kwargs):
            return self.cursor.execute(*args, **kwargs)

        def fetchall(self):
            rows = self.cursor.fetchall()
            if not self.triggered:
                self.triggered = True
                writer = original_get_connection()
                with writer.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO `transaction` (transaction_id, account_id, transaction_date, transaction_type, description, transaction_amount, transaction_reference_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            transaction_id,
                            account_id,
                            "2026-08-15 12:00:00",
                            "debit",
                            "snapshot probe",
                            Decimal("1.00"),
                            reference_id,
                        ),
                    )
                writer.close()
            return rows

        def close(self):
            self.cursor.close()

    class HookConnection:
        def __init__(self, connection):
            self.connection = connection

        def cursor(self):
            return HookCursor(self.connection.cursor())

        def rollback(self):
            return self.connection.rollback()

        def close(self):
            return self.connection.close()

    typed_engine: Any = engine
    typed_engine._get_connection = lambda: HookConnection(original_get_connection())
    q = query(
        aggregation=Aggregation.NONE, intent=Intent.REFERENCE_LOOKUP, filters=QueryFilters(reference_id=reference_id)
    )
    try:
        result = await FinancialQueryExecutor(engine).execute(q)
        assert result.rows == []
        assert result.matched_count == 0
    finally:
        cleanup = original_get_connection()
        with cleanup.cursor() as cursor:
            cursor.execute("DELETE FROM `transaction` WHERE transaction_id=%s", (transaction_id,))
        cleanup.close()
