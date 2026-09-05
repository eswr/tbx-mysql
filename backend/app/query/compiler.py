"""Allowlisted compilation from semantic FinancialQuery objects to logical plans."""

from dataclasses import dataclass
from datetime import date

from app.query.logical_plan import LogicalPlan, Predicate, Sort
from app.schemas.financial_query import (
    Aggregation,
    FinancialQuery,
    GroupByDimension,
    Intent,
)


TRANSACTION_COLUMNS = {
    "transaction_id": "t.transaction_id",
    "account_id": "t.account_id",
    "transaction_date": "t.transaction_date",
    "transaction_type": "t.transaction_type",
    "description": "t.description",
    "transaction_amount": "t.transaction_amount",
    "reference_id": "t.transaction_reference_id",
    "utr_number": "t.utr_number",
}
ACCOUNT_COLUMNS = {
    "account_id": "a.account_id",
    "available_balance": "a.available_balance",
    "bank_code": "a.bank_code",
}
FILTER_COLUMNS = {
    "bank_code": "a.bank_code",
    "bank_name": "b.bank_name",
    "account_id": "t.account_id",
    "transaction_type": "t.transaction_type",
    "description_contains": "t.description",
    "reference_id": "t.transaction_reference_id",
    "utr_number": "t.utr_number",
    "min_amount": "t.transaction_amount",
    "max_amount": "t.transaction_amount",
}
ALLOWED_OPERATORS = {"=", ">", ">=", "<", "<=", "ILIKE"}
TRANSACTION_JOINS = {
    "account": ("account", "a", "t.account_id = a.account_id"),
    "bank": ("bank", "b", "a.bank_code = b.bank_code"),
}
ALLOWED_TABLES = {"transaction": "t", "account": "a"}


class QueryCompilationError(ValueError):
    """The semantic query cannot be compiled into an allowlisted plan."""


# Grouping dimensions the compiler can render. Each maps to the ordered key columns
# projected alongside the aggregate, plus the column whose distinct count gives the
# number of groups (used as `matched_count` for grouped results).
# MONTH is deliberately absent: it needs a dialect-specific bucket expression, which
# would leak dialect knowledge into the compiler.
GROUP_BY_COLUMNS: dict[GroupByDimension, tuple[tuple[str, ...], str]] = {
    GroupByDimension.BANK: (("a.bank_code", "b.bank_name"), "a.bank_code"),
    GroupByDimension.ACCOUNT: (("a.account_id",), "a.account_id"),
    GroupByDimension.TRANSACTION_TYPE: (("t.transaction_type",), "t.transaction_type"),
}

ALLOWED_SELECT_COLUMNS = {
    "COUNT(*) AS value",
    "SUM(t.transaction_amount) AS value",
    "AVG(t.transaction_amount) AS value",
    "MAX(t.transaction_amount) AS value",
    "MIN(t.transaction_amount) AS value",
    "COUNT(*) AS matched_count",
    "SUM(a.available_balance) AS value",
    "t.transaction_id",
    "t.account_id",
    "t.transaction_date",
    "t.transaction_type",
    "t.description",
    "t.transaction_amount",
    "t.transaction_reference_id",
    "t.utr_number",
    # Group key projections
    "a.bank_code",
    "a.account_id",
    "b.bank_name",
    # Group cardinality counts
    "COUNT(DISTINCT a.bank_code) AS matched_count",
    "COUNT(DISTINCT a.account_id) AS matched_count",
    "COUNT(DISTINCT t.transaction_type) AS matched_count",
}
# `value` is the aggregate alias; MySQL permits ordering by a select alias.
ALLOWED_SORT_COLUMNS = {
    "t.transaction_date",
    "t.transaction_id",
    "a.bank_code",
    "a.account_id",
    "t.transaction_type",
    "value",
}


@dataclass(frozen=True)
class PlanSet:
    result_plan: LogicalPlan
    count_plan: LogicalPlan
    comparison_result_plan: LogicalPlan | None = None
    comparison_count_plan: LogicalPlan | None = None


def _group_dimensions(query: FinancialQuery) -> list[GroupByDimension]:
    """Return the supported grouping dimensions, or raise for one we cannot render."""
    if len(query.group_by) > 1:
        raise QueryCompilationError("Multiple group_by dimensions are not supported")
    dimensions = []
    for dimension in query.group_by:
        if dimension not in GROUP_BY_COLUMNS:
            raise QueryCompilationError(f"Unsupported group_by dimension: {dimension.value}")
        dimensions.append(dimension)
    return dimensions


def _group_columns(dimensions: list[GroupByDimension]) -> list[str]:
    columns: list[str] = []
    for dimension in dimensions:
        for column in GROUP_BY_COLUMNS[dimension][0]:
            if column not in columns:
                columns.append(column)
    return columns


def _group_count_column(dimensions: list[GroupByDimension]) -> str:
    """Distinct-count the first dimension: matched_count becomes the number of groups."""
    return GROUP_BY_COLUMNS[dimensions[0]][1]


def _joins_for(query: FinancialQuery) -> list[tuple[str, str, str]]:
    """Joins needed by filters and grouping. `bank` implies `account` from a transaction root."""
    group_columns = _group_columns(_group_dimensions(query))
    needs_account = (
        query.filters.bank_code is not None
        or query.filters.bank_name is not None
        or any(column.startswith(("a.", "b.")) for column in group_columns)
    )
    if not needs_account:
        return []
    joins = [TRANSACTION_JOINS["account"]]
    if query.filters.bank_name is not None or any(column.startswith("b.") for column in group_columns):
        joins.append(TRANSACTION_JOINS["bank"])
    return joins


def _transaction_predicates(query: FinancialQuery) -> list[Predicate]:
    f = query.filters
    predicates = [
        Predicate("t.transaction_date", ">=", query.date_range.start),
        Predicate("t.transaction_date", "<", query.date_range.end),
    ]
    equality_values = {
        "bank_code": f.bank_code,
        "bank_name": f.bank_name,
        "account_id": f.account_id,
        "transaction_type": f.transaction_type,
        "reference_id": f.reference_id,
        "utr_number": f.utr_number,
    }
    for field, value in equality_values.items():
        if value is not None:
            predicates.append(Predicate(FILTER_COLUMNS[field], "=", value))
    if f.description_contains is not None:
        predicates.append(Predicate(FILTER_COLUMNS["description_contains"], "ILIKE", f"%{f.description_contains}%"))
    if f.min_amount is not None:
        predicates.append(Predicate(FILTER_COLUMNS["min_amount"], f.min_amount_operator, f.min_amount))
    if f.max_amount is not None:
        predicates.append(Predicate(FILTER_COLUMNS["max_amount"], f.max_amount_operator, f.max_amount))
    return predicates


def _transaction_result_plan(
    query: FinancialQuery, date_start: date | None = None, date_end: date | None = None
) -> LogicalPlan:
    if date_start is not None and date_end is not None:
        query = query.model_copy(
            update={"date_range": query.date_range.model_copy(update={"start": date_start, "end": date_end})}
        )
    predicates = _transaction_predicates(query)
    joins = _joins_for(query)
    if query.aggregation == Aggregation.COUNT:
        columns = ["COUNT(*) AS value"]
    elif query.aggregation == Aggregation.SUM:
        columns = ["SUM(t.transaction_amount) AS value"]
    elif query.aggregation == Aggregation.AVG:
        columns = ["AVG(t.transaction_amount) AS value"]
    elif query.aggregation == Aggregation.MAX:
        columns = ["MAX(t.transaction_amount) AS value"]
    elif query.aggregation == Aggregation.MIN:
        columns = ["MIN(t.transaction_amount) AS value"]
    elif query.aggregation == Aggregation.NONE:
        columns = [
            "t.transaction_id",
            "t.account_id",
            "t.transaction_date",
            "t.transaction_type",
            "t.description",
            "t.transaction_amount",
            "t.transaction_reference_id",
            "t.utr_number",
        ]
    else:  # pragma: no cover - enums make this defensive
        raise QueryCompilationError(f"Unsupported aggregation: {query.aggregation}")

    # A grouped aggregate projects its key columns and orders by the aggregate,
    # largest first, so the answer can name the leading groups.
    dimensions = _group_dimensions(query)
    if dimensions and query.aggregation != Aggregation.NONE:
        group_columns = _group_columns(dimensions)
        return LogicalPlan(
            select_columns=[*group_columns, *columns],
            primary_table="transaction",
            primary_alias="t",
            joins=joins,
            predicates=predicates,
            group_by=group_columns,
            order_by=[Sort("value", "DESC"), Sort(_group_count_column(dimensions), "ASC")],
            limit=query.limit,
        )

    return LogicalPlan(
        select_columns=columns,
        primary_table="transaction",
        primary_alias="t",
        joins=joins,
        predicates=predicates,
        order_by=[Sort("t.transaction_date", "DESC"), Sort("t.transaction_id", "ASC")]
        if query.aggregation == Aggregation.NONE
        else [],
        limit=query.limit if query.aggregation == Aggregation.NONE else None,
    )


def _transaction_count_plan(
    query: FinancialQuery, date_start: date | None = None, date_end: date | None = None
) -> LogicalPlan:
    if date_start is not None and date_end is not None:
        query = query.model_copy(
            update={"date_range": query.date_range.model_copy(update={"start": date_start, "end": date_end})}
        )
    # For a grouped result, "records matched" means the number of groups, not rows.
    dimensions = _group_dimensions(query)
    count_column = (
        f"COUNT(DISTINCT {_group_count_column(dimensions)}) AS matched_count"
        if dimensions and query.aggregation != Aggregation.NONE
        else "COUNT(*) AS matched_count"
    )
    return LogicalPlan(
        select_columns=[count_column],
        primary_table="transaction",
        primary_alias="t",
        joins=_joins_for(query),
        predicates=_transaction_predicates(query),
    )


def _bank_grouped_account_plans(query: FinancialQuery, aggregate: str) -> PlanSet:
    """Group the `account` table by bank for balance and account-count questions."""
    if _group_dimensions(query) != [GroupByDimension.BANK]:
        raise QueryCompilationError(f"Intent {query.intent.value} requires group_by=[bank]")
    predicates: list[Predicate] = []
    if query.filters.bank_code is not None:
        predicates.append(Predicate("a.bank_code", "=", query.filters.bank_code))
    group_columns = ["a.bank_code", "b.bank_name"]
    joins = [TRANSACTION_JOINS["bank"]]
    result = LogicalPlan(
        select_columns=[*group_columns, aggregate],
        primary_table="account",
        primary_alias="a",
        joins=joins,
        predicates=predicates,
        group_by=group_columns,
        order_by=[Sort("value", "DESC"), Sort("a.bank_code", "ASC")],
        limit=query.limit,
    )
    count = LogicalPlan(
        select_columns=["COUNT(DISTINCT a.bank_code) AS matched_count"],
        primary_table="account",
        primary_alias="a",
        joins=joins,
        predicates=predicates,
    )
    return PlanSet(result, count)


def _previous_month(start: date) -> tuple[date, date]:
    end = date(start.year, start.month, 1)
    if start.month == 1:
        return date(start.year - 1, 12, 1), end
    return date(start.year, start.month - 1, 1), end


def compile_financial_query(query: FinancialQuery) -> PlanSet:
    """Compile only known semantic values; no identifier is sourced from input text."""
    _group_dimensions(query)
    if query.intent == Intent.ACCOUNT_BALANCE:
        predicates: list[Predicate] = []
        if query.filters.bank_code is not None:
            predicates.append(Predicate("a.bank_code", "=", query.filters.bank_code))
        result = LogicalPlan(["SUM(a.available_balance) AS value"], "account", primary_alias="a", predicates=predicates)
        count = LogicalPlan(["COUNT(*) AS matched_count"], "account", primary_alias="a", predicates=predicates)
        plans = PlanSet(result, count)
    elif query.intent == Intent.ACCOUNT_COUNT:
        result = LogicalPlan(["COUNT(*) AS value"], "account", primary_alias="a")
        count = LogicalPlan(["COUNT(*) AS matched_count"], "account", primary_alias="a")
        plans = PlanSet(result, count)
    elif query.intent == Intent.BANK_BALANCE:
        plans = _bank_grouped_account_plans(query, "SUM(a.available_balance) AS value")
    elif query.intent == Intent.BANK_ACCOUNT_COUNT:
        plans = _bank_grouped_account_plans(query, "COUNT(*) AS value")
    elif query.intent in {
        Intent.TRANSACTION_SUMMARY,
        Intent.TRANSACTION_LIST,
        Intent.REFERENCE_LOOKUP,
        Intent.COMPARISON,
    }:
        result = _transaction_result_plan(query)
        count = _transaction_count_plan(query)
        if query.comparison is None:
            plans = PlanSet(result, count)
        elif query.comparison.against == "previous_month":
            start, end = _previous_month(query.date_range.start)
            plans = PlanSet(
                result, count, _transaction_result_plan(query, start, end), _transaction_count_plan(query, start, end)
            )
        else:
            raise QueryCompilationError(f"Unsupported comparison: {query.comparison.against}")
    else:
        raise QueryCompilationError(f"Unsupported intent for execution: {query.intent.value}")
    for plan in (plans.result_plan, plans.count_plan, plans.comparison_result_plan, plans.comparison_count_plan):
        if plan is not None:
            validate_allowlisted_plan(plan)
    return plans


def validate_allowlisted_plan(plan: LogicalPlan) -> None:
    """Fail closed if a compiler regression introduces a non-allowlisted plan token."""
    if ALLOWED_TABLES.get(plan.primary_table) != plan.primary_alias:
        raise QueryCompilationError("Non-allowlisted primary table or alias")
    allowed_joins = set(TRANSACTION_JOINS.values())
    if any(join not in allowed_joins for join in plan.joins):
        raise QueryCompilationError("Non-allowlisted join")
    if any(column not in ALLOWED_SELECT_COLUMNS for column in plan.select_columns):
        raise QueryCompilationError("Non-allowlisted select column")
    if any(sort.column not in ALLOWED_SORT_COLUMNS or sort.direction not in {"ASC", "DESC"} for sort in plan.order_by):
        raise QueryCompilationError("Non-allowlisted sort")
    allowed_group_columns = {column for keys, _ in GROUP_BY_COLUMNS.values() for column in keys}
    if any(column not in allowed_group_columns for column in plan.group_by or []):
        raise QueryCompilationError("Non-allowlisted group by")
    allowed_predicate_columns = set(FILTER_COLUMNS.values()) | {"t.transaction_date", "a.bank_code"}
    for predicate in plan.predicates:
        if predicate.column not in allowed_predicate_columns or predicate.operator not in ALLOWED_OPERATORS:
            raise QueryCompilationError("Non-allowlisted predicate")
