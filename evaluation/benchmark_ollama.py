#!/usr/bin/env python3
"""Reproducible forced-Ollama model-selection benchmark for M3 Phase 3."""

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.conversation import ConversationContext  # noqa: E402
from app.query.compiler import compile_financial_query  # noqa: E402
from app.query.mysql_engine import MySQLQueryEngine  # noqa: E402
from app.schemas.financial_query import (  # noqa: E402
    FinancialQuery,
    QueryFilters,
    QueryRefusal,
    QueryRefusalReason,
)
from app.understanding.ollama import (  # noqa: E402
    SYSTEM_PROMPT,
    build_query_json_schema,
    capabilities_from_discovery,
    understand_with_ollama,
)

REFERENCE_DATE = date(2026, 9, 5)
CORPUS_FILE = Path(__file__).with_name("forced_ollama.json")
FROZEN_CORPUS_SHA256 = "45008763a1a5d322ce79b0dbaa66aa353c0d18584a282291353062dd2b1cea41"
ORIGINAL_CORPUS_FILE = Path(__file__).with_name("forced_ollama_original_20260905.json")
ORIGINAL_CORPUS_SHA256 = "e8fe49710039b505d35ab8e3b02fa8aaa492af63f821376401b9c3d519410948"
ORIGINAL_4B_RESULT_FILE = Path(__file__).with_name("results_ollama_qwen3_5_4b_20260905.json")
ORIGINAL_4B_RESULT_SHA256 = "0551cce4fd6634f4a84412d581cf30c4a566c4fc5c6b1c54cdd5a0e0274f502e"
DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_MYSQL_URL = "mysql://artha:artha@127.0.0.1:3306/artha"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
TIMEOUT_SECONDS = 30.0
TEMPERATURE = 0
NUM_PREDICT = 512
PARITY_SEGMENT = "forced_parity"
SAFETY_REFUSAL_SEGMENT = "safety_refusal"
CONTEXTUAL_FALLBACK_SEGMENT = "contextual_fallback"
EXPECTED_SEGMENT_SIZES = {
    PARITY_SEGMENT: 24,
    SAFETY_REFUSAL_SEGMENT: 6,
    CONTEXTUAL_FALLBACK_SEGMENT: 3,
}
EXPECTED_QUERY_KEYS = {
    "intent",
    "metric",
    "aggregation",
    "filters",
    "date_range",
    "group_by",
    "limit",
    "comparison",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_cases(path: Path = CORPUS_FILE, *, enforce_frozen_hash: bool = True) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    digest = sha256_bytes(raw)
    if enforce_frozen_hash and digest != FROZEN_CORPUS_SHA256:
        raise ValueError(f"Forced-Ollama corpus hash changed: expected {FROZEN_CORPUS_SHA256}, got {digest}")
    cases = json.loads(raw)
    if not isinstance(cases, list) or len(cases) != 33:
        raise ValueError(f"Forced-Ollama corpus must contain exactly 33 cases, got {len(cases)}")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Forced-Ollama cases must have unique, non-empty ids")
    segment_sizes = {
        segment: sum(case.get("segment") == segment for case in cases) for segment in EXPECTED_SEGMENT_SIZES
    }
    if segment_sizes != EXPECTED_SEGMENT_SIZES:
        raise ValueError(f"Forced-Ollama segment sizes must be {EXPECTED_SEGMENT_SIZES}, got {segment_sizes}")
    unknown_segments = {case.get("segment") for case in cases} - set(EXPECTED_SEGMENT_SIZES)
    if unknown_segments:
        raise ValueError(f"Forced-Ollama corpus contains unknown segments: {sorted(unknown_segments)}")
    for case in cases:
        has_query = "expected_query" in case
        has_refusal = "expected_refusal_reason" in case
        if has_query == has_refusal:
            raise ValueError(f"Case {case['id']} must declare exactly one expected outcome")
        if case["segment"] == SAFETY_REFUSAL_SEGMENT and not has_refusal:
            raise ValueError(f"Safety/refusal case {case['id']} must declare a refusal")
        if case["segment"] != SAFETY_REFUSAL_SEGMENT and not has_query:
            raise ValueError(f"Query case {case['id']} must declare an expected query")
        if has_query:
            expected = case["expected_query"]
            if not isinstance(expected, dict) or set(expected) != EXPECTED_QUERY_KEYS:
                raise ValueError(f"Case {case['id']} must declare every expected query field")
            if set(expected["filters"]) != set(QueryFilters.model_fields):
                raise ValueError(f"Case {case['id']} must declare every expected filter and amount operator")
            compile_financial_query(FinancialQuery.model_validate(expected))
        else:
            QueryRefusalReason(case["expected_refusal_reason"])
        if "context" in case:
            ConversationContext.model_validate(case["context"])
    return cases


def context_for(case: dict[str, Any]) -> ConversationContext | None:
    if "context" not in case:
        return None
    return ConversationContext.model_validate(case["context"])


#: Filter fields whose comparison follows the compiler's case-insensitive ``ILIKE`` predicate.
CASE_INSENSITIVE_FILTERS = frozenset({"description_contains"})
#: Amount operator fields that are only emitted by the compiler when their bound is present.
AMOUNT_OPERATOR_BOUNDS = {"min_amount_operator": "min_amount", "max_amount_operator": "max_amount"}


def normalize_case_insensitive(value: Any) -> Any:
    """Fold a text filter the way the compiler's ``ILIKE`` predicate compares it."""
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def compare_filters(actual: QueryFilters, expected: QueryFilters) -> list[str]:
    """Compare filters under execution semantics rather than literal equality.

    ``description_contains`` is compiled to a case-insensitive ``ILIKE '%value%'``
    predicate, so it is compared trimmed and case-folded. An amount operator is only
    rendered into SQL when its bound is present, so it is compared only in that case.
    """
    failures: list[str] = []
    for field in QueryFilters.model_fields:
        actual_value = getattr(actual, field)
        expected_value = getattr(expected, field)
        bound_field = AMOUNT_OPERATOR_BOUNDS.get(field)
        if bound_field is not None and (getattr(actual, bound_field) is None or getattr(expected, bound_field) is None):
            continue
        if field in CASE_INSENSITIVE_FILTERS:
            if normalize_case_insensitive(actual_value) != normalize_case_insensitive(expected_value):
                failures.append(f"filter {field} want {expected_value!r}, got {actual_value!r} (case-insensitive)")
            continue
        if actual_value != expected_value:
            failures.append(f"filter {field} want {expected_value!r}, got {actual_value!r}")
    return failures


def score_output(output: FinancialQuery | QueryRefusal, case: dict[str, Any]) -> tuple[bool, list[str]]:
    """Score every declared semantic field, or the exact refusal reason."""
    failures: list[str] = []
    if "expected_refusal_reason" in case:
        expected_reason = case["expected_refusal_reason"]
        if isinstance(output, FinancialQuery):
            failures.append("expected refusal, got query")
        elif output.reason.value != expected_reason:
            failures.append(f"refusal reason want {expected_reason}, got {output.reason.value}")
        return not failures, failures
    if not isinstance(output, FinancialQuery):
        return False, [f"expected query, got {output.reason.value}"]
    expected = FinancialQuery.model_validate(case["expected_query"])
    for field in ("intent", "metric", "aggregation"):
        actual_value = getattr(output, field).value
        expected_value = getattr(expected, field).value
        if actual_value != expected_value:
            failures.append(f"{field} want {expected_value}, got {actual_value}")
    failures.extend(compare_filters(output.filters, expected.filters))
    if output.date_range.start != expected.date_range.start or output.date_range.end != expected.date_range.end:
        failures.append(
            "date_range want "
            f"[{expected.date_range.start}, {expected.date_range.end}), got "
            f"[{output.date_range.start}, {output.date_range.end})"
        )
    actual_group_by = [item.value for item in output.group_by]
    expected_group_by = [item.value for item in expected.group_by]
    if actual_group_by != expected_group_by:
        failures.append(f"group_by want {expected_group_by}, got {actual_group_by}")
    if output.limit != expected.limit:
        failures.append(f"limit want {expected.limit!r}, got {output.limit!r}")
    actual_comparison = output.comparison.model_dump(mode="json") if output.comparison else None
    expected_comparison = expected.comparison.model_dump(mode="json") if expected.comparison else None
    if actual_comparison != expected_comparison:
        failures.append(f"comparison want {expected_comparison!r}, got {actual_comparison!r}")
    return not failures, failures


def nearest_rank_percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile of an empty list")
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def segment_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(row["passed"] for row in rows)
    return {"passed": passed, "total": len(rows), "accuracy": passed / len(rows)}


def summarize(rows: list[dict[str, Any]], *, model: str) -> dict[str, Any]:
    latencies = [row["metadata"]["latency_ms"] for row in rows]
    refusals = [row for row in rows if row["output_type"] == "QueryRefusal"]
    malformed = [row for row in refusals if row["output"].get("reason") == "invalid_structure"]
    by_segment = {segment: [row for row in rows if row["segment"] == segment] for segment in EXPECTED_SEGMENT_SIZES}
    overall = segment_summary(rows)
    safety_refusal = segment_summary(by_segment[SAFETY_REFUSAL_SEGMENT])
    gate_passed = overall["passed"] >= 32 and safety_refusal["passed"] == 6
    return {
        "provider": "ollama-forced",
        "model": model,
        "selected": gate_passed,
        "gate": {
            "passed": gate_passed,
            "minimum_overall_passed": 32,
            "required_safety_refusal_passed": 6,
        },
        "accuracy": {
            "overall": overall,
            "forced_parity": segment_summary(by_segment[PARITY_SEGMENT]),
            "fallback_safety": segment_summary(
                by_segment[SAFETY_REFUSAL_SEGMENT] + by_segment[CONTEXTUAL_FALLBACK_SEGMENT]
            ),
            "safety_refusal": safety_refusal,
            "contextual_fallback": segment_summary(by_segment[CONTEXTUAL_FALLBACK_SEGMENT]),
        },
        "latency_ms": {
            "mean": statistics.mean(latencies),
            "p50": statistics.median(latencies),
            "p95": nearest_rank_percentile(latencies, 0.95),
        },
        "tokens": {
            "prompt": sum(row["metadata"]["prompt_tokens"] or 0 for row in rows),
            "completion": sum(row["metadata"]["completion_tokens"] or 0 for row in rows),
            "total": sum(row["metadata"]["total_tokens"] or 0 for row in rows),
        },
        "counts": {"refusals": len(refusals), "malformed_invalid_structure": len(malformed)},
    }


async def run_benchmark(model: str, mysql_url: str) -> dict[str, Any]:
    cases = load_cases()
    capabilities = await MySQLQueryEngine(mysql_url).discover_capabilities()
    active = capabilities_from_discovery(capabilities)
    schema = build_query_json_schema(active)
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        result = await understand_with_ollama(
            case["question"],
            reference_date=REFERENCE_DATE,
            context=context_for(case),
            discovered_capabilities=capabilities,
            base_url=OLLAMA_BASE_URL,
            model=model,
            timeout=TIMEOUT_SECONDS,
        )
        passed, failures = score_output(result.output, case)
        row = {
            "index": index + 1,
            "case_id": case["id"],
            "segment": case["segment"],
            "question": case["question"],
            "expected": {key: value for key, value in case.items() if key not in {"id", "segment", "question"}},
            "output_type": type(result.output).__name__,
            "output": result.output.model_dump(mode="json"),
            "passed": passed,
            "failures": failures,
            "metadata": {
                "model": result.metadata.model,
                "latency_ms": result.metadata.latency_ms,
                "prompt_tokens": result.metadata.prompt_tokens,
                "completion_tokens": result.metadata.completion_tokens,
                "total_tokens": result.metadata.total_tokens,
            },
        }
        rows.append(row)
        print(f"{index + 1:02d}/33 {'PASS' if passed else 'FAIL'} {row['output_type']}", flush=True)
    summary = summarize(rows, model=model)
    return {
        "summary": summary,
        "reproducibility": {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "reference_date": REFERENCE_DATE.isoformat(),
            "corpus_sha256": sha256_bytes(CORPUS_FILE.read_bytes()),
            "system_prompt_sha256": sha256_bytes(SYSTEM_PROMPT.encode()),
            "schema_sha256": sha256_bytes(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()),
            "parameters": {
                "temperature": TEMPERATURE,
                "num_predict": NUM_PREDICT,
                "think": False,
                "stream": False,
                "timeout_seconds": TIMEOUT_SECONDS,
                "mysql_url": mysql_url,
                "ollama_base_url": OLLAMA_BASE_URL,
            },
        },
        "results": rows,
    }


def output_from_row(row: dict[str, Any]) -> FinancialQuery | QueryRefusal:
    """Rebuild a stored model output without contacting any model."""
    if row["output_type"] == "FinancialQuery":
        return FinancialQuery.model_validate(row["output"])
    if row["output_type"] == "QueryRefusal":
        return QueryRefusal.model_validate(row["output"])
    raise ValueError(f"Unknown stored output type: {row['output_type']!r}")


def rescore_artifact(artifact: dict[str, Any], source: Path) -> dict[str, Any]:
    """Re-score a saved benchmark artifact offline against the frozen corpus.

    Raw model outputs, latencies, and token counts are reused verbatim; only the
    pass/fail verdicts and the derived summary are recomputed. Ollama is never invoked.
    """
    cases = {case["id"]: case for case in load_cases()}
    rows: list[dict[str, Any]] = []
    for stored in artifact["results"]:
        case_id = stored.get("case_id")
        if case_id not in cases:
            raise ValueError(f"Stored row {stored.get('index')} has unknown case id {case_id!r}")
        passed, failures = score_output(output_from_row(stored), cases[case_id])
        row = dict(stored)
        row["passed"] = passed
        row["failures"] = failures
        row["previous_passed"] = stored["passed"]
        rows.append(row)
    if len(rows) != 33:
        raise ValueError(f"Rescored artifact must contain exactly 33 rows, got {len(rows)}")
    summary = summarize(rows, model=artifact["summary"]["model"])
    reproducibility = dict(artifact["reproducibility"])
    reproducibility["rescored_at"] = datetime.now(timezone.utc).isoformat()
    reproducibility["rescored_from"] = source.name
    reproducibility["rescored_from_sha256"] = sha256_bytes(source.read_bytes())
    reproducibility["rescoring"] = {
        "invoked_model": False,
        "scoring": "execution_semantic",
        "case_insensitive_filters": sorted(CASE_INSENSITIVE_FILTERS),
        "conditional_amount_operators": sorted(AMOUNT_OPERATOR_BOUNDS),
    }
    return {"summary": summary, "reproducibility": reproducibility, "results": rows}


def write_result(path: Path, result: dict[str, Any], *, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing benchmark artifact: {path}")
    path.write_text(json.dumps(result, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mysql-url", default=DEFAULT_MYSQL_URL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--rescore",
        metavar="ARTIFACT",
        help="Re-score a saved benchmark artifact offline instead of invoking Ollama",
    )
    args = parser.parse_args()
    if args.rescore:
        source = Path(args.rescore)
        result = rescore_artifact(json.loads(source.read_text()), source)
    else:
        result = asyncio.run(run_benchmark(args.model, args.mysql_url))
    write_result(Path(args.output), result, overwrite=args.overwrite)
    print(json.dumps(result["summary"], indent=2))
    return 0 if result["summary"]["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
