#!/usr/bin/env python3
"""Database-grounded evaluation harness for Artha."""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.conversation import ConversationContext, InMemoryConversationStore
from app.query.duckdb_engine import DuckDBQueryEngine
from app.query.execution import FinancialQueryExecutor, GroundedResult
from app.query.mysql_engine import MySQLQueryEngine
from app.schemas.financial_query import FinancialQuery, QueryRefusal

REFERENCE_DATE = date(2026, 9, 5)
CASES_FILE = Path(__file__).parent / "cases.json"
RULES_FILE = BACKEND / "app" / "understanding" / "rules.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_eval_cases(path: Path = CASES_FILE) -> list[dict]:
    cases = json.loads(path.read_text())
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Evaluation case ids must be unique")
    for case in cases:
        refusal = case.get("expected_refusal_reason")
        if (
            refusal
            and refusal != "no_data"
            and any(key in case for key in ("oracle_sql", "expected_intent", "expected_metric"))
        ):
            raise ValueError(f"Pre-execution refusal case {case['id']} has answer expectations")
        if "followup" in case and case.get("category") != "multi_turn":
            raise ValueError(f"Follow-up case {case['id']} must be multi_turn")
    return cases


def parse_with_rules(question: str, context: ConversationContext | None = None) -> FinancialQuery | QueryRefusal | None:
    """Call rules with a fixed clock; context support is added only after baseline."""
    from app.understanding.rules import understand_question

    with patch("app.understanding.rules.today_ist", return_value=REFERENCE_DATE):
        return understand_question(question, context=context, reference_date=REFERENCE_DATE)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, float):
        return Decimal(str(value)).normalize()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _filter_match(actual: Any, expected: dict) -> bool:
    for key, expected_value in expected.items():
        actual_value = getattr(actual, key)
        if isinstance(actual_value, Decimal):
            if actual_value != Decimal(str(expected_value)):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _oracle_sqls(value: Any) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _actual_numeric(query: FinancialQuery, grounded: GroundedResult) -> list[Any]:
    if grounded.comparison_matched_count is not None:
        return [_canonical(grounded.value), _canonical(grounded.comparison_value)]
    if query.aggregation.value == "none":
        return [grounded.matched_count]
    return [_canonical(grounded.value)]


def _oracle_numeric(grounded: GroundedResult) -> list[Any]:
    return [_canonical(rows[0][0]) if rows else None for rows in grounded.oracle_rows or []]


def _new_score(case: dict) -> dict:
    return {
        "case_id": case["id"],
        "category": case["category"],
        "question": case["question"],
        "latency_ms": 0.0,
        "checks": {},
        "passed": False,
        "score": 0.0,
        "notes": "",
    }


def _add_query_checks(checks: dict[str, bool], case: dict, query: FinancialQuery, prefix: str = "") -> None:
    def get(name: str):
        key = f"expected_{prefix}{name}"
        return (key, case[key]) if key in case else (None, None)

    key, value = get("intent")
    if key:
        checks[f"{prefix}intent_match"] = query.intent.value == value
    key, value = get("metric")
    if key:
        checks[f"{prefix}metric_match"] = query.metric.value == value
    key, value = get("aggregation")
    if key:
        checks[f"{prefix}aggregation_match"] = query.aggregation.value == value
    key, value = get("filters")
    if key:
        checks[f"{prefix}filters_match"] = _filter_match(query.filters, value)
    start_key, start = get("date_start")
    end_key, end = get("date_end")
    if start_key or end_key:
        checks[f"{prefix}date_match"] = (
            bool(start_key and end_key)
            and query.date_range.start.isoformat() == start
            and query.date_range.end.isoformat() == end
        )
    key, value = get("comparison_against")
    if key:
        checks[f"{prefix}comparison_match"] = query.comparison is not None and query.comparison.against == value


async def _score_answer_turn(
    case: dict,
    question: str,
    parser: Callable,
    executor: FinancialQueryExecutor,
    context: ConversationContext | None,
    prefix: str = "",
):
    checks = case["_checks"]
    result = parser(question, context)
    checks[f"{prefix}refusal_absent"] = not isinstance(result, QueryRefusal)
    checks[f"{prefix}query_present"] = isinstance(result, FinancialQuery)
    if not isinstance(result, FinancialQuery):
        reason = result.reason.value if isinstance(result, QueryRefusal) else "none"
        return None, None, f"Unexpected parse outcome: {reason}"
    _add_query_checks(checks, case, result, prefix)
    oracle_key = f"{prefix}oracle_sql" if prefix else "oracle_sql"
    sqls = _oracle_sqls(case.get(oracle_key))
    try:
        grounded = await executor.execute(result, sqls)
    except Exception as exc:
        checks[f"{prefix}execution_success"] = False
        return result, None, f"Execution failed: {exc}"
    checks[f"{prefix}execution_success"] = True
    if sqls:
        checks[f"{prefix}numeric_match"] = _actual_numeric(result, grounded) == _oracle_numeric(grounded)
    range_key = f"expected_{prefix}matched_count_range"
    if range_key in case:
        low, high = case[range_key]
        checks[f"{prefix}matched_count_match"] = low <= grounded.matched_count <= high
    return result, grounded, ""


async def score_case(case: dict, executor: FinancialQueryExecutor, parser: Callable = parse_with_rules) -> dict:
    score = _new_score(case)
    checks = score["checks"]
    case = {**case, "_checks": checks}
    started = time.perf_counter()
    expected_refusal = case.get("expected_refusal_reason")
    if expected_refusal and expected_refusal != "no_data":
        before = executor.execution_count
        result = parser(case["question"], None)
        checks["refusal_reason_match"] = isinstance(result, QueryRefusal) and result.reason.value == expected_refusal
        checks["no_execution"] = executor.execution_count == before
        score["notes"] = (
            f"Refusal: {result.reason.value}" if isinstance(result, QueryRefusal) else "Expected refusal, got answer"
        )
    else:
        query, grounded, note = await _score_answer_turn(case, case["question"], parser, executor, None)
        score["notes"] = note
        if expected_refusal == "no_data":
            checks["post_execution_no_data"] = grounded is not None and grounded.no_data and grounded.matched_count == 0
        elif grounded is not None:
            checks["answer_not_no_data"] = not grounded.no_data
        if case.get("followup") and query is not None and grounded is not None:
            store = InMemoryConversationStore()
            store.put(case["id"], ConversationContext.from_query(query))
            _, follow_grounded, follow_note = await _score_answer_turn(
                case, case["followup"], parser, executor, store.get(case["id"]), "followup_"
            )
            if follow_note:
                score["notes"] = follow_note
            if follow_grounded is not None:
                checks["followup_answer_not_no_data"] = not follow_grounded.no_data
    score["latency_ms"] = (time.perf_counter() - started) * 1000
    applicable = list(checks.values())
    score["score"] = sum(applicable) / len(applicable) if applicable else 0.0
    score["passed"] = bool(applicable) and all(applicable)
    return score


async def run_eval(engine_name: str, db_target: str, parser: Callable = parse_with_rules) -> dict:
    cases = load_eval_cases()
    engine = DuckDBQueryEngine(db_target) if engine_name == "duckdb" else MySQLQueryEngine(db_target)
    if not await engine.ping():
        raise RuntimeError(f"{engine_name} is unavailable")
    executor = FinancialQueryExecutor(engine)
    scores = [await score_case(case, executor, parser) for case in cases]
    by_category = {}
    for category in sorted({item["category"] for item in scores}):
        selected = [item for item in scores if item["category"] == category]
        by_category[category] = sum(item["passed"] for item in selected) / len(selected)
    latencies = sorted(item["latency_ms"] for item in scores)
    return {
        "provider": "rules",
        "engine": engine_name,
        "reference_date": REFERENCE_DATE.isoformat(),
        "fixture_seed": 42,
        "fixture_version": 1,
        "rules_sha256": _sha256(RULES_FILE),
        "cases_sha256": _sha256(CASES_FILE),
        "total": len(scores),
        "passed": sum(item["passed"] for item in scores),
        "accuracy": sum(item["passed"] for item in scores) / len(scores),
        "scores_by_category": by_category,
        "p50_latency_ms": latencies[len(latencies) // 2],
        "p95_latency_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "cases": scores,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["duckdb", "mysql"], default="duckdb")
    parser.add_argument("--duckdb-path", default="artha.duckdb")
    parser.add_argument("--mysql-url", default="mysql://artha:artha@127.0.0.1:3306/artha")
    parser.add_argument("--provider", choices=["rules"], default="rules")
    parser.add_argument("--output")
    args = parser.parse_args()
    target = args.duckdb_path if args.engine == "duckdb" else args.mysql_url
    result = asyncio.run(run_eval(args.engine, target))
    print(f"{result['engine']}: {result['passed']}/{result['total']} ({result['accuracy']:.1%})")
    for category, accuracy in result["scores_by_category"].items():
        print(f"  {category}: {accuracy:.1%}")
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str) + "\n")
        print(f"Results written to {args.output}")
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
