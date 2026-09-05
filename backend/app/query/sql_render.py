"""
Render LogicalPlan to dialect-specific SQL.

Supports MySQL and DuckDB with dialect hooks for:
  - Parameter placeholder style (%(name)s vs $name)
  - LIKE normalization (LOWER vs lower)
  - Date/time functions (DATE_FORMAT vs strftime)
  - Case sensitivity (LIKE vs ILIKE)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.query.logical_plan import LogicalPlan, Predicate


@dataclass
class CompiledQuery:
    """A compiled SQL query with parameters."""

    sql: str
    params: dict[str, str | int | Decimal | date | None]
    dialect: str  # "mysql" or "duckdb"


class Dialect(ABC):
    """Abstract SQL dialect."""

    name: str
    param_placeholder: str  # "%(name)s" or "$name"

    @abstractmethod
    def param_name(self, name: str) -> str:
        """Format a parameter placeholder."""
        pass

    @abstractmethod
    def like_clause(self, column: str, pattern: str, case_insensitive: bool) -> str:
        """Generate a LIKE clause."""
        pass

    @abstractmethod
    def date_to_string(self, column: str, format_str: str) -> str:
        """Convert a date/datetime column to a string."""
        pass


class MySQLDialect(Dialect):
    """MySQL 8.4 dialect."""

    name = "mysql"
    param_placeholder = "%(name)s"

    def param_name(self, name: str) -> str:
        return f"%({name})s"

    def like_clause(self, column: str, pattern: str, case_insensitive: bool) -> str:
        if case_insensitive:
            return f"LOWER({column}) LIKE LOWER({self.param_name('pattern')})"
        return f"{column} LIKE {self.param_name('pattern')}"

    def date_to_string(self, column: str, format_str: str) -> str:
        # MySQL DATE_FORMAT: %Y=%Y, %m=%m, %d=%d
        return f"DATE_FORMAT({column}, '{format_str}')"

    def month_bucket(self, column: str) -> tuple[str, str]:
        """Return (month_expr, year_expr) for grouping by month."""
        return (
            f"DATE_FORMAT({column}, '%Y-%m')",  # "2026-08"
            f"DATE_FORMAT({column}, '%Y')",  # "2026"
        )


class DuckDBDialect(Dialect):
    """DuckDB dialect."""

    name = "duckdb"
    param_placeholder = "$name"

    def param_name(self, name: str) -> str:
        return f"${name}"

    def like_clause(self, column: str, pattern: str, case_insensitive: bool) -> str:
        if case_insensitive:
            return f"{column} ILIKE {self.param_name('pattern')}"
        return f"{column} LIKE {self.param_name('pattern')}"

    def date_to_string(self, column: str, format_str: str) -> str:
        # DuckDB strftime: %Y=%Y, %m=%m, %d=%d
        return f"strftime({column}, '{format_str}')"

    def month_bucket(self, column: str) -> tuple[str, str]:
        """Return (month_expr, year_expr) for grouping by month."""
        return (
            f"strftime({column}, '%Y-%m')",  # "2026-08"
            f"strftime({column}, '%Y')",  # "2026"
        )


def render_predicate(
    pred: Predicate,
    dialect: Dialect,
    params: dict,
    param_counter: int,
) -> tuple[str, int]:
    """
    Render a single predicate to SQL fragment.

    Returns: (sql_fragment, new_param_counter)
    """
    col = pred.column

    if pred.operator == "=":
        param_name = f"p{param_counter}"
        params[param_name] = pred.value
        return f"{col} = {dialect.param_name(param_name)}", param_counter + 1

    elif pred.operator == "!=":
        param_name = f"p{param_counter}"
        params[param_name] = pred.value
        return f"{col} != {dialect.param_name(param_name)}", param_counter + 1

    elif pred.operator in (">", ">=", "<", "<="):
        param_name = f"p{param_counter}"
        params[param_name] = pred.value
        return f"{col} {pred.operator} {dialect.param_name(param_name)}", param_counter + 1

    elif pred.operator == "LIKE":
        param_name = f"p{param_counter}"
        params[param_name] = pred.value
        placeholder = dialect.param_name(param_name)
        if pred.case_insensitive:
            sql = f"LOWER({col}) LIKE LOWER({placeholder})" if dialect.name == "mysql" else f"{col} ILIKE {placeholder}"
        else:
            sql = f"{col} LIKE {placeholder}"
        return sql, param_counter + 1

    elif pred.operator == "ILIKE":
        # Normalize to LIKE with case_insensitive=True
        param_name = f"p{param_counter}"
        params[param_name] = pred.value
        placeholder = dialect.param_name(param_name)
        sql = f"LOWER({col}) LIKE LOWER({placeholder})" if dialect.name == "mysql" else f"{col} ILIKE {placeholder}"
        return sql, param_counter + 1

    elif pred.operator == "IN":
        values = pred.value
        if not isinstance(values, list):
            raise ValueError("IN predicate requires a list value")
        placeholders = [dialect.param_name(f"p{param_counter + i}") for i in range(len(values))]
        for i, val in enumerate(values):
            params[f"p{param_counter + i}"] = val
        return f"{col} IN ({', '.join(placeholders)})", param_counter + len(values)

    elif pred.operator == "NOT IN":
        values = pred.value
        if not isinstance(values, list):
            raise ValueError("NOT IN predicate requires a list value")
        placeholders = [dialect.param_name(f"p{param_counter + i}") for i in range(len(values))]
        for i, val in enumerate(values):
            params[f"p{param_counter + i}"] = val
        return f"{col} NOT IN ({', '.join(placeholders)})", param_counter + len(values)

    else:
        raise ValueError(f"Unknown operator: {pred.operator}")


def render_sql(plan: LogicalPlan, dialect: Dialect) -> CompiledQuery:
    """
    Render a LogicalPlan to SQL.

    Raises:
        ValueError: if the plan is malformed
    """
    params: dict[str, str | int | Decimal | date | None] = {}
    param_counter = 0

    # SELECT clause
    select_clause = ", ".join(plan.select_columns)

    # FROM / JOIN (fixed structure)
    primary_table = (
        f"`{plan.primary_table}`"
        if dialect.name == "mysql" and plan.primary_table == "transaction"
        else plan.primary_table
    )
    from_clause = f"{primary_table} {plan.primary_alias}"

    # Add standard joins if needed
    # We always have: t (transaction) -> a (account) -> b (bank)
    for table, alias, condition in plan.joins:
        from_clause += f" JOIN {table} {alias} ON {condition}"

    # WHERE clause
    where_parts = []
    for pred in plan.predicates:
        sql, param_counter = render_predicate(pred, dialect, params, param_counter)
        where_parts.append(sql)

    where_clause = " AND ".join(where_parts) if where_parts else ""

    # GROUP BY clause
    group_by_clause = ""
    if plan.group_by:
        group_by_clause = f"GROUP BY {', '.join(plan.group_by)}"

    # ORDER BY clause
    order_by_clause = ""
    if plan.order_by:
        order_parts = [f"{sort.column} {sort.direction}" for sort in plan.order_by]
        order_by_clause = f"ORDER BY {', '.join(order_parts)}"

    # LIMIT clause
    limit_clause = ""
    if plan.limit:
        limit_clause = f"LIMIT {plan.limit}"

    # Assemble
    sql = f"SELECT {select_clause} FROM {from_clause}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    if group_by_clause:
        sql += f" {group_by_clause}"
    if order_by_clause:
        sql += f" {order_by_clause}"
    if limit_clause:
        sql += f" {limit_clause}"

    return CompiledQuery(sql=sql, params=params, dialect=dialect.name)


def validate_sql_select_only(sql: str, dialect_name: str = "mysql"):
    """
    Validate that SQL is a single SELECT statement (via sqlglot).

    Raises:
        ValueError: if SQL contains non-SELECT statements or injections
    """
    import re
    import sqlglot

    # sqlglot validates SQL structure, not DB-driver placeholder syntax.
    parse_sql = re.sub(r"%\([A-Za-z0-9_]+\)s", "?", sql)
    parse_sql = re.sub(r"\$[A-Za-z0-9_]+", "?", parse_sql)
    if dialect_name == "duckdb":
        parse_sql = parse_sql.replace("`", '"')

    try:
        parsed = sqlglot.parse_one(parse_sql, dialect=dialect_name)
    except Exception as e:
        raise ValueError(f"SQL parse error: {e}")

    # Must be a single SELECT
    if not isinstance(parsed, sqlglot.exp.Select):
        raise ValueError(f"Expected SELECT statement, got {type(parsed).__name__}")

    # Walk the tree and reject dangerous constructs
    for node in parsed.walk():
        node_type = type(node).__name__
        if node_type in (
            "Insert",
            "Update",
            "Delete",
            "Drop",
            "Create",
            "Command",
            "Copy",
            "Alter",
            "Exec",
            "Execute",
            "Show",
            "Use",
        ):
            raise ValueError(f"SQL injection attempt: {node_type} not allowed")

    return True
