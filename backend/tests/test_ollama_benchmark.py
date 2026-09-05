"""Tests for the corrected, frozen M3 forced-Ollama benchmark contract."""

import json
from datetime import date
from pathlib import Path

import pytest

from app.query.compiler import compile_financial_query
from app.schemas.financial_query import (
    Aggregation,
    ComparisonSpec,
    DateRange,
    FinancialQuery,
    GroupByDimension,
    Intent,
    Metric,
    QueryFilters,
    QueryRefusal,
    QueryRefusalReason,
    refusal,
)
from app.understanding.rules import understand_question
from evaluation.benchmark_ollama import (
    CONTEXTUAL_FALLBACK_SEGMENT,
    EXPECTED_SEGMENT_SIZES,
    FROZEN_CORPUS_SHA256,
    ORIGINAL_4B_RESULT_FILE,
    ORIGINAL_4B_RESULT_SHA256,
    ORIGINAL_CORPUS_FILE,
    ORIGINAL_CORPUS_SHA256,
    PARITY_SEGMENT,
    SAFETY_REFUSAL_SEGMENT,
    context_for,
    load_cases,
    nearest_rank_percentile,
    output_from_row,
    score_output,
    sha256_bytes,
    summarize,
    write_result,
)


def query(
    *,
    intent: Intent = Intent.TRANSACTION_SUMMARY,
    metric: Metric = Metric.TRANSACTION_AMOUNT,
    aggregation: Aggregation = Aggregation.SUM,
    filters: QueryFilters | None = None,
    date_range: DateRange | None = None,
    group_by: list[GroupByDimension] | None = None,
    limit: int | None = None,
    comparison: ComparisonSpec | None = None,
) -> FinancialQuery:
    return FinancialQuery(
        intent=intent,
        metric=metric,
        aggregation=aggregation,
        filters=filters or QueryFilters(transaction_type="debit"),
        date_range=date_range or DateRange(start=date(2026, 8, 1), end=date(2026, 9, 1)),
        group_by=group_by or [],
        limit=limit,
        comparison=comparison,
    )


def query_case(expected: FinancialQuery) -> dict:
    dumped = expected.model_dump(mode="json")
    dumped["date_range"] = {"start": dumped["date_range"]["start"], "end": dumped["date_range"]["end"]}
    return {"expected_query": dumped}


def row(*, passed: bool, segment: str, latency: float, refusal_reason: str | None = None) -> dict:
    is_refusal = refusal_reason is not None
    return {
        "passed": passed,
        "segment": segment,
        "output_type": "QueryRefusal" if is_refusal else "FinancialQuery",
        "output": {"reason": refusal_reason} if is_refusal else {"intent": "transaction_summary"},
        "metadata": {
            "latency_ms": latency,
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
    }


def test_corpus_is_frozen_with_explicit_segments_and_executable_parity():
    cases = load_cases()
    assert {segment: sum(case["segment"] == segment for case in cases) for segment in EXPECTED_SEGMENT_SIZES} == {
        PARITY_SEGMENT: 24,
        SAFETY_REFUSAL_SEGMENT: 6,
        CONTEXTUAL_FALLBACK_SEGMENT: 3,
    }
    assert sha256_bytes(Path("evaluation/forced_ollama.json").read_bytes()) == FROZEN_CORPUS_SHA256
    assert sha256_bytes(ORIGINAL_CORPUS_FILE.read_bytes()) == ORIGINAL_CORPUS_SHA256
    assert sha256_bytes(ORIGINAL_4B_RESULT_FILE.read_bytes()) == ORIGINAL_4B_RESULT_SHA256
    for case in cases:
        if case["segment"] == PARITY_SEGMENT:
            compile_financial_query(FinancialQuery.model_validate(case["expected_query"]))


@pytest.mark.parametrize("case", [c for c in load_cases() if c["segment"] == PARITY_SEGMENT], ids=lambda c: c["id"])
def test_every_forced_parity_case_matches_the_deterministic_parser(case):
    """Every parity case must be reproducible by the rules and executable by the compiler."""
    output = understand_question(case["question"], reference_date=date(2026, 9, 5))
    assert isinstance(output, FinancialQuery), f"{case['id']} was not parsed into a query"
    passed, failures = score_output(output, case)
    assert passed, f"{case['id']} diverges from its expected query: {failures}"
    compile_financial_query(output)


def test_parity_segment_covers_all_twenty_four_cases():
    parity = [case for case in load_cases() if case["segment"] == PARITY_SEGMENT]
    assert len(parity) == 24
    assert {case["id"] for case in parity} == {f"parity_{index:02d}" for index in range(1, 25)}


def test_description_filter_is_scored_case_insensitively():
    """`description_contains` compiles to ILIKE, so casing must not change the score."""
    expected = query(filters=QueryFilters(transaction_type="debit", description_contains="SELECTION ELECTRONICS"))
    case = query_case(expected)
    for variant in ("Selection Electronics", "SELECTION ELECTRONICS", "selection electronics"):
        candidate = expected.model_copy(
            update={"filters": expected.filters.model_copy(update={"description_contains": variant})}
        )
        passed, failures = score_output(candidate, case)
        assert passed, f"{variant!r} should score identically: {failures}"
    trimmed = expected.model_copy(
        update={"filters": expected.filters.model_copy(update={"description_contains": "  Selection Electronics  "})}
    )
    assert score_output(trimmed, case)[0]
    wrong = expected.model_copy(
        update={"filters": expected.filters.model_copy(update={"description_contains": "Reliance"})}
    )
    passed, failures = score_output(wrong, case)
    assert not passed
    assert any("description_contains" in failure for failure in failures)


def test_amount_operators_are_compared_only_when_their_bound_is_present():
    """The compiler emits an amount operator only alongside its bound, so scoring follows."""
    unbounded = query(filters=QueryFilters(transaction_type="debit"))
    case = query_case(unbounded)
    noisy_operators = unbounded.model_copy(
        update={
            "filters": unbounded.filters.model_copy(update={"min_amount_operator": ">", "max_amount_operator": "<"})
        }
    )
    passed, failures = score_output(noisy_operators, case)
    assert passed, failures
    bounded = query(filters=QueryFilters(transaction_type="debit", min_amount="50000", min_amount_operator=">"))
    passed, failures = score_output(
        bounded.model_copy(update={"filters": bounded.filters.model_copy(update={"min_amount_operator": ">="})}),
        query_case(bounded),
    )
    assert not passed
    assert any("min_amount_operator" in failure for failure in failures)


def test_context_is_parsed_from_each_case_instead_of_hardcoded():
    case = {
        "context": {
            "intent": "transaction_summary",
            "metric": "transaction_count",
            "aggregation": "count",
            "filters": {"transaction_type": "credit", "bank_code": "ICIC"},
            "date_range": {"start": "2026-05-01", "end": "2026-06-01", "label": "May 2026"},
            "group_by": [],
            "limit": None,
            "comparison": None,
        }
    }
    context = context_for(case)
    assert context is not None
    assert context.metric == Metric.TRANSACTION_COUNT
    assert context.aggregation == Aggregation.COUNT
    assert context.filters.transaction_type == "credit"
    assert context.filters.bank_code == "ICIC"
    assert context.date_range == DateRange(start=date(2026, 5, 1), end=date(2026, 6, 1), label="May 2026")


@pytest.mark.parametrize(
    ("mutation", "failure_fragment"),
    [
        (
            lambda value: value.model_copy(
                update={"date_range": DateRange(start=date(2026, 8, 2), end=date(2026, 9, 1))}
            ),
            "date_range",
        ),
        (lambda value: value.model_copy(update={"aggregation": Aggregation.COUNT}), "aggregation"),
        (lambda value: value.model_copy(update={"comparison": None}), "comparison"),
        (
            lambda value: value.model_copy(
                update={
                    "filters": value.filters.model_copy(update={"min_amount": "50000", "min_amount_operator": ">="})
                }
            ),
            "min_amount_operator",
        ),
        (
            lambda value: value.model_copy(update={"filters": value.filters.model_copy(update={"bank_code": "HDFC"})}),
            "bank_code",
        ),
        (lambda value: value.model_copy(update={"group_by": [GroupByDimension.BANK]}), "group_by"),
    ],
)
def test_wrong_query_semantics_fail(mutation, failure_fragment):
    expected = query(
        intent=Intent.COMPARISON,
        filters=QueryFilters(transaction_type="debit", min_amount="50000", min_amount_operator=">"),
        comparison=ComparisonSpec(against="previous_month"),
    )
    passed, failures = score_output(mutation(expected), query_case(expected))
    assert not passed
    assert any(failure_fragment in failure for failure in failures)


def test_wrong_capability_response_fails():
    case = {"expected_refusal_reason": "capability"}
    passed, failures = score_output(refusal(QueryRefusalReason.UNSUPPORTED_METRIC, "unsupported"), case)
    assert not passed
    assert failures == ["refusal reason want capability, got unsupported_metric"]
    passed, failures = score_output(query(), case)
    assert not passed
    assert failures == ["expected refusal, got query"]


def test_summary_aggregates_metrics_and_applies_both_gates():
    rows = [row(passed=True, segment=PARITY_SEGMENT, latency=float(index + 1)) for index in range(24)]
    rows += [row(passed=True, segment=SAFETY_REFUSAL_SEGMENT, latency=float(index)) for index in range(25, 31)]
    rows += [row(passed=True, segment=CONTEXTUAL_FALLBACK_SEGMENT, latency=float(index)) for index in range(31, 34)]
    rows[0] = row(passed=False, segment=PARITY_SEGMENT, latency=1.0)
    rows[24] = row(passed=True, segment=SAFETY_REFUSAL_SEGMENT, latency=25.0, refusal_reason="unsupported_metric")
    summary = summarize(rows, model="test")
    assert summary["gate"]["passed"]
    assert summary["accuracy"]["overall"]["passed"] == 32
    assert summary["accuracy"]["forced_parity"]["passed"] == 23
    assert summary["accuracy"]["fallback_safety"]["passed"] == 9
    assert summary["accuracy"]["safety_refusal"]["passed"] == 6
    assert summary["latency_ms"] == {"mean": 17.0, "p50": 17.0, "p95": 32.0}
    assert summary["tokens"] == {"prompt": 330, "completion": 66, "total": 396}
    assert summary["counts"] == {"refusals": 1, "malformed_invalid_structure": 0}


def test_safety_failure_rejects_model_and_counts_malformed_output():
    rows = [row(passed=True, segment=PARITY_SEGMENT, latency=1.0) for _ in range(24)]
    rows += [row(passed=True, segment=SAFETY_REFUSAL_SEGMENT, latency=1.0) for _ in range(6)]
    rows += [row(passed=True, segment=CONTEXTUAL_FALLBACK_SEGMENT, latency=1.0) for _ in range(3)]
    rows[24] = row(passed=False, segment=SAFETY_REFUSAL_SEGMENT, latency=1.0, refusal_reason="invalid_structure")
    summary = summarize(rows, model="test")
    assert summary["accuracy"]["overall"]["passed"] == 32
    assert summary["accuracy"]["safety_refusal"]["passed"] == 5
    assert not summary["gate"]["passed"]
    assert summary["counts"] == {"refusals": 1, "malformed_invalid_structure": 1}


@pytest.mark.parametrize(
    "artifact_name",
    ["results_ollama_qwen3_5_4b_rescored_20260905.json", "results_ollama_granite3_3_8b_rescored_20260905.json"],
)
def test_rescored_artifacts_are_offline_and_keep_raw_outputs(artifact_name):
    """Rescoring reuses stored generations verbatim and never invokes a model."""
    rescored = json.loads((Path("evaluation") / artifact_name).read_text())
    source = json.loads((Path("evaluation") / rescored["reproducibility"]["rescored_from"]).read_text())
    assert rescored["reproducibility"]["rescoring"]["invoked_model"] is False
    assert rescored["reproducibility"]["rescored_from_sha256"] == sha256_bytes(
        (Path("evaluation") / rescored["reproducibility"]["rescored_from"]).read_bytes()
    )
    assert len(rescored["results"]) == 33
    for new_row, old_row in zip(rescored["results"], source["results"], strict=True):
        assert new_row["output"] == old_row["output"]
        assert new_row["metadata"] == old_row["metadata"]
        assert new_row["previous_passed"] == old_row["passed"]
    assert not rescored["summary"]["gate"]["passed"]
    assert rescored["summary"]["selected"] is False


def test_rescoring_recomputes_verdicts_without_touching_stored_output():
    case = {"id": "parity_14", "segment": PARITY_SEGMENT}
    expected = query(filters=QueryFilters(transaction_type="debit", description_contains="SELECTION ELECTRONICS"))
    stored = expected.model_copy(
        update={"filters": expected.filters.model_copy(update={"description_contains": "Selection Electronics"})}
    )
    rebuilt = output_from_row({"output_type": "FinancialQuery", "output": stored.model_dump(mode="json")})
    assert isinstance(rebuilt, FinancialQuery)
    assert rebuilt.filters.description_contains == "Selection Electronics"
    assert score_output(rebuilt, {**case, **query_case(expected)})[0]
    refused = output_from_row(
        {"output_type": "QueryRefusal", "output": refusal(QueryRefusalReason.CAPABILITY, "x").model_dump(mode="json")}
    )
    assert isinstance(refused, QueryRefusal)
    assert refused.reason is QueryRefusalReason.CAPABILITY


def test_nearest_rank_percentile_and_no_overwrite(tmp_path):
    assert nearest_rank_percentile(list(range(1, 34)), 0.95) == 32
    output = tmp_path / "result.json"
    write_result(output, {"first": True})
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        write_result(output, {"second": True})
    assert json.loads(output.read_text()) == {"first": True}
