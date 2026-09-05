#!/usr/bin/env python3
"""Database-grounded deterministic evaluation harness for Artha."""

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
EVALUATION_DIR = Path(__file__).parent
SUITE_FILES = {
    "regression": EVALUATION_DIR / "regression.json",
    "generalization": EVALUATION_DIR / "generalization.json",
    "holdout": EVALUATION_DIR / "holdout.json",
}
EXPECTED_SUITE_SIZES = {"regression": 26, "generalization": 55, "holdout": 20}
RULES_FILE = BACKEND / "app" / "understanding" / "rules.py"
FIXTURE_FILE = BACKEND / "fixtures" / "generate_fixture.py"
LOCKED_REGRESSION_SHA256 = "fc5c6c569892a1ca7ab497ae35fea91c825e1ad99ac70f50a85a71cf89990dff"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _combined_cases_sha256(paths: dict[str, Path]) -> str:
    digest = hashlib.sha256()
    for name in ("regression", "generalization", "holdout"):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(paths[name].read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_legacy_case(case: dict[str, Any]) -> None:
    refusal = case.get("expected_refusal_reason")
    if refusal and refusal != "no_data" and any(
        key in case for key in ("oracle_sql", "expected_intent", "expected_metric")
    ):
        raise ValueError(f"Pre-execution refusal case {case['id']} has answer expectations")
    if "followup" in case and case.get("category") != "multi_turn":
        raise ValueError(f"Follow-up case {case['id']} must be multi_turn")


def _validate_turn_case(case: dict[str, Any]) -> None:
    turns = case.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError(f"Case {case['id']} must contain a non-empty turns array")
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn.get("question"), str) or not turn["question"].strip():
            raise ValueError(f"Case {case['id']} turn {index} needs a question")
        if not isinstance(turn.get("conversation_id"), str) or not turn["conversation_id"]:
            raise ValueError(f"Case {case['id']} turn {index} needs a conversation_id")
        refusal = turn.get("expected_refusal_reason")
        if refusal and refusal != "no_data":
            if "oracle_sql" in turn:
                raise ValueError(f"Pre-execution refusal {case['id']} turn {index} must not have oracle SQL")
        elif not turn.get("oracle_sql"):
            raise ValueError(f"Answer turn {case['id']} turn {index} requires independent oracle SQL")
        if turn.get("oracle_mode") not in {"scalar", "matched_count", "ordered_ids"}:
            raise ValueError(f"Case {case['id']} turn {index} needs a valid oracle_mode")


def load_eval_suites(
    *,
    include_holdout: bool = False,
    suite_files: dict[str, Path] | None = None,
    enforce_sizes: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    paths = suite_files or SUITE_FILES
    names = ["regression", "generalization"] + (["holdout"] if include_holdout else [])
    suites: dict[str, list[dict[str, Any]]] = {}
    all_ids: list[str] = []
    for name in names:
        cases = json.loads(paths[name].read_text())
        if not isinstance(cases, list):
            raise ValueError(f"{name} suite must be a JSON array")
        if enforce_sizes and len(cases) != EXPECTED_SUITE_SIZES[name]:
            raise ValueError(f"{name} suite must contain {EXPECTED_SUITE_SIZES[name]} cases, got {len(cases)}")
        for case in cases:
            if "id" not in case or "category" not in case:
                raise ValueError(f"Every {name} case needs id and category")
            if "turns" in case:
                _validate_turn_case(case)
            elif name == "regression":
                _validate_legacy_case(case)
            else:
                raise ValueError(f"New case {case['id']} must use the turns format")
        suites[name] = cases
        all_ids.extend(case["id"] for case in cases)
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("Evaluation case ids must be unique across selected suites")
    return suites


def load_eval_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Compatibility loader used by older tests and callers."""
    selected = path or SUITE_FILES["regression"]
    cases = json.loads(selected.read_text())
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Evaluation case ids must be unique")
    for case in cases:
        _validate_turn_case(case) if "turns" in case else _validate_legacy_case(case)
    return cases


def parse_with_rules(question: str, context: ConversationContext | None = None) -> FinancialQuery | QueryRefusal | None:
    from app.understanding.rules import understand_question

    with patch("app.understanding.rules.today_ist", return_value=REFERENCE_DATE):
        return understand_question(question, context=context, reference_date=REFERENCE_DATE)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, float):
        return str(Decimal(str(value)).normalize())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _filter_match(actual: Any, expected: dict[str, Any]) -> bool:
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


def _new_score(case: dict[str, Any]) -> dict[str, Any]:
    question = case.get("question") or case.get("turns", [{}])[0].get("question", "")
    return {
        "case_id": case["id"],
        "category": case["category"],
        "question": question,
        "latency_ms": 0.0,
        "checks": {},
        "passed": False,
        "score": 0.0,
        "notes": "",
        "observed": [],
    }


def _add_query_checks(checks: dict[str, bool], expected: dict[str, Any], query: FinancialQuery, prefix: str = "") -> None:
    def get(name: str):
        key = f"expected_{name}" if prefix.startswith("turn_") else f"expected_{prefix}{name}"
        return (key, expected[key]) if key in expected else (None, None)

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
    key, value = get("group_by")
    if key:
        checks[f"{prefix}group_by_match"] = [item.value for item in query.group_by] == value
    key, value = get("limit")
    if key:
        checks[f"{prefix}limit_match"] = query.limit == value
    start_key, start = get("date_start")
    end_key, end = get("date_end")
    if start_key or end_key:
        checks[f"{prefix}date_match"] = bool(start_key and end_key) and query.date_range.start.isoformat() == start and query.date_range.end.isoformat() == end
    key, value = get("comparison_against")
    if key:
        checks[f"{prefix}comparison_match"] = query.comparison is not None and query.comparison.against == value


def _oracle_check(mode: str, query: FinancialQuery, grounded: GroundedResult) -> tuple[bool, dict[str, Any]]:
    if mode == "ordered_ids":
        actual = [row["transaction_id"] for row in grounded.rows]
        oracle_rows = (grounded.oracle_rows or [[]])[0]
        expected = [row[0] for row in oracle_rows]
    elif mode == "matched_count":
        actual = [grounded.matched_count]
        expected = _oracle_numeric(grounded)
    else:
        actual = _actual_numeric(query, grounded)
        expected = _oracle_numeric(grounded)
    return actual == expected, {"actual": actual, "oracle": expected}


async def _score_answer_turn(
    expected: dict[str, Any],
    question: str,
    parser: Callable,
    executor: FinancialQueryExecutor,
    context: ConversationContext | None,
    checks: dict[str, bool],
    prefix: str = "",
) -> tuple[FinancialQuery | None, GroundedResult | None, str, dict[str, Any]]:
    before = executor.execution_count
    result = parser(question, context)
    checks[f"{prefix}refusal_absent"] = not isinstance(result, QueryRefusal)
    checks[f"{prefix}query_present"] = isinstance(result, FinancialQuery)
    if not isinstance(result, FinancialQuery):
        checks[f"{prefix}fresh_execution"] = executor.execution_count == before
        reason = result.reason.value if isinstance(result, QueryRefusal) else "none"
        return None, None, f"Unexpected parse outcome: {reason}", {"parse": reason}
    _add_query_checks(checks, expected, result, prefix)
    oracle_key = "oracle_sql" if not prefix or prefix.startswith("turn_") else f"{prefix}oracle_sql"
    sqls = _oracle_sqls(expected.get(oracle_key))
    try:
        grounded = await executor.execute(result, sqls)
    except Exception as exc:
        checks[f"{prefix}execution_success"] = False
        checks[f"{prefix}fresh_execution"] = executor.execution_count == before + 1
        return result, None, f"Execution failed: {exc}", {"error": str(exc)}
    checks[f"{prefix}execution_success"] = True
    checks[f"{prefix}fresh_execution"] = executor.execution_count == before + 1
    mode = expected.get("oracle_mode", "scalar" if result.aggregation.value != "none" else "matched_count")
    if sqls:
        matched, observed = _oracle_check(mode, result, grounded)
        check_name = "oracle_match" if prefix.startswith("turn_") else "numeric_match"
        checks[f"{prefix}{check_name}"] = matched
    else:
        observed = {"actual": _actual_numeric(result, grounded), "oracle": []}
    range_key = "expected_matched_count_range" if prefix.startswith("turn_") else f"expected_{prefix}matched_count_range"
    if range_key in expected:
        low, high = expected[range_key]
        checks[f"{prefix}matched_count_match"] = low <= grounded.matched_count <= high
    observed.update({"intent": result.intent.value, "matched_count": grounded.matched_count, "no_data": grounded.no_data})
    return result, grounded, "", observed


async def _score_structured_case(case: dict[str, Any], executor: FinancialQueryExecutor, parser: Callable) -> dict[str, Any]:
    score = _new_score(case)
    checks = score["checks"]
    stores: dict[str, ConversationContext] = {}
    started = time.perf_counter()
    for index, turn in enumerate(case["turns"], 1):
        prefix = f"turn_{index}_"
        conversation_id = turn["conversation_id"]
        context = stores.get(conversation_id)
        expected_refusal = turn.get("expected_refusal_reason")
        if expected_refusal and expected_refusal != "no_data":
            before = executor.execution_count
            result = parser(turn["question"], context)
            checks[f"{prefix}refusal_reason_match"] = isinstance(result, QueryRefusal) and result.reason.value == expected_refusal
            checks[f"{prefix}no_execution"] = executor.execution_count == before
            score["observed"].append({"turn": index, "conversation_id": conversation_id, "refusal": result.reason.value if isinstance(result, QueryRefusal) else None})
            continue
        query, grounded, note, observed = await _score_answer_turn(turn, turn["question"], parser, executor, context, checks, prefix)
        score["observed"].append({"turn": index, "conversation_id": conversation_id, **observed})
        if note:
            score["notes"] = note
        if grounded is not None:
            if expected_refusal == "no_data":
                checks[f"{prefix}post_execution_no_data"] = grounded.no_data and grounded.matched_count == 0
            else:
                oracle_values = observed.get("oracle", [])
                if oracle_values and all(value is None for value in oracle_values):
                    checks[f"{prefix}oracle_confirmed_no_data"] = grounded.no_data
                else:
                    checks[f"{prefix}answer_not_no_data"] = not grounded.no_data
        if query is not None and grounded is not None:
            stores[conversation_id] = ConversationContext.from_query(query)
    score["latency_ms"] = (time.perf_counter() - started) * 1000
    applicable = list(checks.values())
    score["score"] = sum(applicable) / len(applicable) if applicable else 0.0
    score["passed"] = bool(applicable) and all(applicable)
    return score


async def score_case(case: dict[str, Any], executor: FinancialQueryExecutor, parser: Callable = parse_with_rules) -> dict[str, Any]:
    if "turns" in case:
        return await _score_structured_case(case, executor, parser)
    score = _new_score(case)
    checks = score["checks"]
    started = time.perf_counter()
    expected_refusal = case.get("expected_refusal_reason")
    if expected_refusal and expected_refusal != "no_data":
        before = executor.execution_count
        result = parser(case["question"], None)
        checks["refusal_reason_match"] = isinstance(result, QueryRefusal) and result.reason.value == expected_refusal
        checks["no_execution"] = executor.execution_count == before
        score["notes"] = f"Refusal: {result.reason.value}" if isinstance(result, QueryRefusal) else "Expected refusal, got answer"
    else:
        query, grounded, note, observed = await _score_answer_turn(case, case["question"], parser, executor, None, checks)
        score["observed"].append(observed)
        score["notes"] = note
        if expected_refusal == "no_data":
            checks["post_execution_no_data"] = grounded is not None and grounded.no_data and grounded.matched_count == 0
        elif grounded is not None:
            checks["answer_not_no_data"] = not grounded.no_data
        if case.get("followup") and query is not None and grounded is not None:
            store = InMemoryConversationStore()
            store.put(case["id"], ConversationContext.from_query(query))
            follow_query, follow_grounded, follow_note, follow_observed = await _score_answer_turn(
                case, case["followup"], parser, executor, store.get(case["id"]), checks, "followup_"
            )
            score["observed"].append(follow_observed)
            if follow_note:
                score["notes"] = follow_note
            if follow_grounded is not None:
                checks["followup_answer_not_no_data"] = not follow_grounded.no_data
            if follow_query is not None and follow_grounded is not None:
                store.put(case["id"], ConversationContext.from_query(follow_query))
    score["latency_ms"] = (time.perf_counter() - started) * 1000
    applicable = list(checks.values())
    score["score"] = sum(applicable) / len(applicable) if applicable else 0.0
    score["passed"] = bool(applicable) and all(applicable)
    return score


def _suite_summary(scores: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, float] = {}
    for category in sorted({item["category"] for item in scores}):
        selected = [item for item in scores if item["category"] == category]
        by_category[category] = sum(item["passed"] for item in selected) / len(selected)
    refusal_scores = [item for item in scores if any(key.endswith(("no_execution", "refusal_reason_match")) for key in item["checks"])]
    return {
        "total": len(scores),
        "passed": sum(item["passed"] for item in scores),
        "accuracy": sum(item["passed"] for item in scores) / len(scores),
        "safety_refusal_accuracy": (sum(item["passed"] for item in refusal_scores) / len(refusal_scores)) if refusal_scores else None,
        "scores_by_category": by_category,
        "cases": scores,
    }


async def run_eval(
    engine_name: str,
    db_target: str,
    parser: Callable = parse_with_rules,
    *,
    include_holdout: bool = False,
) -> dict[str, Any]:
    suites = load_eval_suites(include_holdout=include_holdout)
    engine = DuckDBQueryEngine(db_target) if engine_name == "duckdb" else MySQLQueryEngine(db_target)
    if not await engine.ping():
        raise RuntimeError(f"{engine_name} is unavailable")
    suite_results: dict[str, dict[str, Any]] = {}
    all_scores: list[dict[str, Any]] = []
    for name, cases in suites.items():
        executor = FinancialQueryExecutor(engine)
        scores = [await score_case(case, executor, parser) for case in cases]
        suite_results[name] = _suite_summary(scores)
        all_scores.extend(scores)
    latencies = sorted(item["latency_ms"] for item in all_scores)
    combined = _suite_summary(all_scores)
    combined.pop("cases")
    return {
        "provider": "rules",
        "engine": engine_name,
        "reference_date": REFERENCE_DATE.isoformat(),
        "fixture_seed": 42,
        "fixture_version": 2,
        "rules_sha256": _sha256(RULES_FILE),
        "fixture_sha256": _sha256(FIXTURE_FILE),
        "case_sha256": {name: _sha256(path) for name, path in SUITE_FILES.items()},
        "combined_cases_sha256": _combined_cases_sha256(SUITE_FILES),
        "selected_suites": list(suites),
        "suites": suite_results,
        "combined": combined,
        "p50_latency_ms": latencies[len(latencies) // 2],
        "p95_latency_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


def write_result(path: Path, result: dict[str, Any], *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing evaluation artifact: {path}")
    path.write_text(json.dumps(result, indent=2, default=str) + "\n")


def passes_release_gates(result: dict[str, Any]) -> bool:
    suites = result["suites"]
    regression = suites["regression"]
    generalization = suites["generalization"]
    non_holdout_total = regression["total"] + generalization["total"]
    non_holdout_passed = regression["passed"] + generalization["passed"]
    safety_scores = [
        suite["safety_refusal_accuracy"]
        for suite in suites.values()
        if suite["safety_refusal_accuracy"] is not None
    ]
    return (
        regression["accuracy"] == 1.0
        and all(score == 1.0 for score in safety_scores)
        and non_holdout_passed / non_holdout_total >= 0.95
        and ("holdout" not in suites or suites["holdout"]["accuracy"] >= 0.90)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["duckdb", "mysql"], default="duckdb")
    parser.add_argument("--duckdb-path", default="artha.duckdb")
    parser.add_argument("--mysql-url", default="mysql://artha:artha@127.0.0.1:3306/artha")
    parser.add_argument("--provider", choices=["rules"], default="rules")
    parser.add_argument("--include-holdout", action="store_true", help="Explicitly unlock the frozen holdout suite")
    parser.add_argument("--output")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    target = args.duckdb_path if args.engine == "duckdb" else args.mysql_url
    result = asyncio.run(run_eval(args.engine, target, include_holdout=args.include_holdout))
    for name, suite in result["suites"].items():
        print(f"{args.engine}/{name}: {suite['passed']}/{suite['total']} ({suite['accuracy']:.1%})")
    combined = result["combined"]
    print(f"{args.engine}/combined: {combined['passed']}/{combined['total']} ({combined['accuracy']:.1%})")
    if args.output:
        write_result(Path(args.output), result, overwrite=args.overwrite)
        print(f"Results written to {args.output}")
    return 0 if passes_release_gates(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
