"""Grounded execution of compiled financial queries."""

from dataclasses import dataclass
from typing import Any

from app.query.compiler import compile_financial_query
from app.schemas.financial_query import Aggregation, FinancialQuery


@dataclass
class GroundedResult:
    rows: list[dict[str, Any]]
    matched_count: int
    no_data: bool
    value: Any = None
    comparison_value: Any = None
    comparison_matched_count: int | None = None
    sql: list[str] | None = None
    oracle_rows: list[list[Any]] | None = None


class FinancialQueryExecutor:
    def __init__(self, engine):
        self.engine = engine
        self.execution_count = 0

    async def execute(self, query: FinancialQuery, oracle_sql: list[str] | None = None) -> GroundedResult:
        plans = compile_financial_query(query)
        plan_list = [plans.result_plan, plans.count_plan]
        if plans.comparison_result_plan is not None:
            plan_list.extend([plans.comparison_result_plan, plans.comparison_count_plan])
        self.execution_count += 1
        bundle = await self.engine.execute_snapshot(plan_list, oracle_sql or [])
        result_rows = bundle["plan_rows"][0]
        matched_count = int(bundle["plan_rows"][1][0]["matched_count"])
        value = result_rows[0].get("value") if len(result_rows) == 1 and "value" in result_rows[0] else None
        is_detail = query.aggregation == Aggregation.NONE
        no_data = matched_count == 0 and (is_detail or query.aggregation != Aggregation.COUNT)
        comparison_value = None
        comparison_count = None
        if len(bundle["plan_rows"]) == 4:
            comparison_rows = bundle["plan_rows"][2]
            comparison_count = int(bundle["plan_rows"][3][0]["matched_count"])
            comparison_value = comparison_rows[0].get("value") if comparison_rows else None
        return GroundedResult(
            rows=result_rows,
            matched_count=matched_count,
            no_data=no_data,
            value=value,
            comparison_value=comparison_value,
            comparison_matched_count=comparison_count,
            sql=bundle["sql"],
            oracle_rows=bundle["oracle_rows"],
        )
