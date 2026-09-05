"""
Normalize and validate LLM JSON output into FinancialQuery or QueryRefusal.

Never raises exceptions; all errors result in structured QueryRefusal.
Implements strict 11-step validation pipeline with fail-closed safety.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.schemas.financial_query import (
    Aggregation,
    DateRangeType,
    FinancialQuery,
    GroupByDimension,
    Intent,
    Metric,
    QueryFilters,
    QueryRefusal,
    QueryRefusalReason,
    refusal as mk_refusal,
)
from app.understanding.dates import resolve_date_range, today_ist

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActiveQueryCapabilities:
    """Semantic operations that the active engine can actually execute."""

    intents: frozenset[Intent]
    filters: frozenset[str]
    group_by: frozenset[GroupByDimension]


DEFAULT_ACTIVE_CAPABILITIES = ActiveQueryCapabilities(
    intents=frozenset(
        {
            Intent.TRANSACTION_SUMMARY,
            Intent.TRANSACTION_LIST,
            Intent.COMPARISON,
            Intent.ACCOUNT_BALANCE,
            Intent.REFERENCE_LOOKUP,
        }
    ),
    filters=frozenset(QueryFilters.model_fields),
    group_by=frozenset(),
)


# SQL injection patterns to block
SQL_KEYWORDS = r"\b(drop|alter|delete|insert|update|create|truncate|union|select|exec|execute)\b"
INJECTION_PATTERNS = r"(--|;|\*|--|\/\*|\*\/|xp_|sp_)"


def normalize_llm_output(
    llm_json: Any,
    reference_date: date | None = None,
    active_capabilities: ActiveQueryCapabilities = DEFAULT_ACTIVE_CAPABILITIES,
) -> FinancialQuery | QueryRefusal:
    """
    Coerce and validate LLM JSON into FinancialQuery or QueryRefusal.

    Implements 11-step validation pipeline:
    1. Refusal check
    2. Extra fields detection
    3. Required fields validation
    4. Enum validation
    5. Date range resolution
    6. Filter coercion
    7. Safety checks
    8. Group-by coercion
    9. Limit coercion
    10. Semantic coherence
    11. FinancialQuery construction

    Args:
        llm_json: Raw JSON from Ollama (potentially malformed)
        reference_date: For date range resolution (default: today_ist())

    Returns:
        FinancialQuery if valid and safe
        QueryRefusal (INVALID_STRUCTURE, AMBIGUOUS, or CAPABILITY) otherwise

    Raises:
        Never. All errors → QueryRefusal.
    """
    if reference_date is None:
        reference_date = today_ist()

    if not isinstance(llm_json, dict):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Model response must be a JSON object.")

    allowed_fields = {
        "intent",
        "metric",
        "aggregation",
        "filters",
        "date_range_type",
        "month",
        "year",
        "group_by",
        "limit",
        "comparison",
        "refusal",
    }
    extra_fields = set(llm_json) - allowed_fields
    if extra_fields:
        logger.warning("LLM output contains unexpected fields: %s", sorted(extra_fields))
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Response contains unexpected fields.")

    # Step 1: Refusal check
    if llm_json.get("refusal") is not None:
        refusal_obj = llm_json.get("refusal")
        if set(llm_json) != {"refusal"}:
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "A refusal cannot contain query fields.")
        return _validate_and_return_refusal(refusal_obj)

    # Step 2: Extra fields check
    if "refusal" not in llm_json:
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Response must include refusal=null for a query.")

    # Step 3: Required fields validation
    intent = llm_json.get("intent")
    metric = llm_json.get("metric")
    aggregation = llm_json.get("aggregation")
    date_range_type = llm_json.get("date_range_type")

    missing = [
        field
        for field in ("intent", "metric", "aggregation", "filters", "date_range_type", "group_by")
        if field not in llm_json
    ]
    if missing:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            f"Response is missing required fields: {', '.join(missing)}.",
        )

    # Step 4: Enum validation
    try:
        intent_enum = Intent(intent)
    except (ValueError, KeyError, TypeError):
        logger.warning(f"Unknown intent: {intent}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Unknown intent: {intent}")

    if intent_enum not in active_capabilities.intents:
        return mk_refusal(
            QueryRefusalReason.CAPABILITY,
            f"Intent is not supported by the active data capabilities: {intent_enum.value}.",
        )

    try:
        metric_enum = Metric(metric)
    except (ValueError, KeyError, TypeError):
        logger.warning(f"Unknown metric: {metric}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Unknown metric: {metric}")

    try:
        aggregation_enum = Aggregation(aggregation)
    except (ValueError, KeyError, TypeError):
        logger.warning(f"Unknown aggregation: {aggregation}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Unknown aggregation: {aggregation}")

    try:
        date_range_type_enum = DateRangeType(date_range_type)
    except (ValueError, KeyError, TypeError):
        logger.warning(f"Unknown date_range_type: {date_range_type}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Unknown date_range_type: {date_range_type}")

    # Step 5: Date range resolution
    try:
        month = llm_json.get("month")
        year = llm_json.get("year")
        if month is not None and not isinstance(month, str):
            raise ValueError("month must be a string or null")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            raise ValueError("year must be an integer or null")
        if date_range_type_enum != DateRangeType.CALENDAR_MONTH and (month is not None or year is not None):
            raise ValueError("month and year are only valid for calendar_month")
        if date_range_type_enum == DateRangeType.CALENDAR_MONTH and month is None and year is not None:
            raise ValueError("year cannot be supplied without month")
        date_range = resolve_date_range(date_range_type_enum, context_date=reference_date, month=month, year=year)
    except Exception as e:
        logger.warning(f"Failed to resolve date range: {e}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Could not resolve date range: {str(e)}")

    # Step 6: Filter coercion
    filters_raw = llm_json.get("filters", {})
    filters = _coerce_filters(filters_raw, active_capabilities.filters)
    if isinstance(filters, QueryRefusal):
        return filters

    # Step 7: Safety checks (on all extracted values)
    safety_check = _perform_safety_checks(intent_enum, metric_enum, aggregation_enum, filters, date_range_type_enum)
    if safety_check is not None:
        return safety_check

    # Step 8: Group-by coercion
    group_by_raw = llm_json.get("group_by", [])
    group_by = _coerce_group_by(group_by_raw, active_capabilities.group_by)
    if isinstance(group_by, QueryRefusal):
        return group_by

    # Step 9: Limit coercion
    limit = _coerce_limit(llm_json.get("limit"))
    if isinstance(limit, QueryRefusal):
        return limit

    comparison = _coerce_comparison(llm_json.get("comparison"))
    if isinstance(comparison, QueryRefusal):
        return comparison

    used_filter_fields = {key for key, value in filters_raw.items() if value is not None}
    if intent_enum == Intent.ACCOUNT_BALANCE:
        ignored_filters = used_filter_fields - {"bank_code"}
        if ignored_filters:
            return mk_refusal(
                QueryRefusalReason.INVALID_STRUCTURE,
                f"Filters are not applicable to account_balance: {', '.join(sorted(ignored_filters))}.",
            )
    if limit is not None and aggregation_enum != Aggregation.NONE:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "limit is only executable for list queries.",
        )

    # Step 10 & 11: Semantic coherence + construction
    try:
        query = FinancialQuery(
            intent=intent_enum,
            metric=metric_enum,
            aggregation=aggregation_enum,
            filters=filters,
            date_range=date_range,
            group_by=group_by,
            limit=limit,
            comparison=comparison,
        )
        logger.info(f"Successfully normalized LLM output to query: {intent_enum}")
        return query
    except Exception as e:
        logger.warning(f"FinancialQuery construction failed: {e}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Query validation failed: {str(e)}")


def _validate_and_return_refusal(refusal_dict: Any) -> QueryRefusal:
    """Validate refusal structure and return QueryRefusal."""
    if not isinstance(refusal_dict, dict):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Refusal must be an object.")

    if set(refusal_dict) != {"reason", "message"}:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "Refusal must contain exactly reason and message.",
        )

    reason = refusal_dict.get("reason")
    message = refusal_dict.get("message")

    if not reason or not message:
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Refusal must have reason and message.")

    try:
        reason_enum = QueryRefusalReason(reason)
    except (ValueError, KeyError, TypeError):
        logger.warning(f"Unknown refusal reason: {reason}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Unknown refusal reason: {reason}")

    if not isinstance(message, str) or not message.strip():
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Refusal message must be a non-empty string.")

    return QueryRefusal(reason=reason_enum, message=message)


def _coerce_filters(filters_raw: Any, active_filters: frozenset[str]) -> QueryFilters | QueryRefusal:
    """
    Coerce raw filter dict into QueryFilters.

    Returns:
        QueryFilters if valid
        QueryRefusal if unsafe or malformed
    """
    if not isinstance(filters_raw, dict):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Filters must be an object.")

    unknown = set(filters_raw) - set(QueryFilters.model_fields)
    if unknown:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            f"Unknown filter fields: {', '.join(sorted(unknown))}.",
        )

    unsupported = {key for key, value in filters_raw.items() if value is not None and key not in active_filters}
    if unsupported:
        return mk_refusal(
            QueryRefusalReason.CAPABILITY,
            f"Filters are not supported by the active data capabilities: {', '.join(sorted(unsupported))}.",
        )

    coerced: dict[str, Any] = {}

    # bank_code
    bank_code = filters_raw.get("bank_code")
    if bank_code is not None:
        if not isinstance(bank_code, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "bank_code must be a string or null.")
        bank_code_str = bank_code.strip()
        if bank_code_str and _is_unsafe_identifier(bank_code_str):
            logger.error(f"Unsafe bank_code detected: {bank_code_str}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid bank code.")
        coerced["bank_code"] = bank_code_str

    # bank_name
    bank_name = filters_raw.get("bank_name")
    if bank_name is not None:
        if not isinstance(bank_name, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "bank_name must be a string or null.")
        bank_name_str = bank_name.strip()
        if bank_name_str and _is_unsafe_text(bank_name_str):
            logger.error(f"Unsafe bank_name detected: {bank_name_str}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid bank name.")
        coerced["bank_name"] = bank_name_str

    # account_id
    account_id = filters_raw.get("account_id")
    if account_id is not None:
        if not isinstance(account_id, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "account_id must be a string or null.")
        account_id_str = account_id.strip()
        if account_id_str and _is_unsafe_identifier(account_id_str):
            logger.error(f"Unsafe account_id detected: {account_id_str}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid account ID.")
        coerced["account_id"] = account_id_str

    # transaction_type
    txn_type = filters_raw.get("transaction_type")
    if txn_type is not None:
        if txn_type not in ("credit", "debit", None):
            logger.warning(f"Invalid transaction_type: {txn_type}")
            return mk_refusal(
                QueryRefusalReason.INVALID_STRUCTURE, "transaction_type must be 'credit', 'debit', or null."
            )
        coerced["transaction_type"] = txn_type

    # description_contains
    desc = filters_raw.get("description_contains")
    if desc is not None:
        if not isinstance(desc, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "description_contains must be a string or null.")
        desc_str = desc.strip()
        if desc_str and _is_unsafe_text(desc_str):
            logger.error("Unsafe description_contains detected")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid description filter.")
        coerced["description_contains"] = desc_str

    # reference_id
    ref_id = filters_raw.get("reference_id")
    if ref_id is not None:
        if not isinstance(ref_id, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "reference_id must be a string or null.")
        ref_id_str = ref_id.strip()
        if ref_id_str and _is_unsafe_identifier(ref_id_str):
            logger.error("Unsafe reference_id detected")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid reference ID.")
        coerced["reference_id"] = ref_id_str

    # utr_number
    utr = filters_raw.get("utr_number")
    if utr is not None:
        if not isinstance(utr, str):
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "utr_number must be a string or null.")
        utr_str = utr.strip()
        if utr_str and _is_unsafe_identifier(utr_str):
            logger.error("Unsafe utr_number detected")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Invalid UTR number.")
        coerced["utr_number"] = utr_str

    # min_amount
    min_amt = filters_raw.get("min_amount")
    if min_amt is not None:
        try:
            coerced["min_amount"] = Decimal(str(min_amt))
        except Exception as e:
            logger.warning(f"Failed to coerce min_amount: {e}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "min_amount must be a valid number.")

    # max_amount
    max_amt = filters_raw.get("max_amount")
    if max_amt is not None:
        try:
            coerced["max_amount"] = Decimal(str(max_amt))
        except Exception as e:
            logger.warning(f"Failed to coerce max_amount: {e}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "max_amount must be a valid number.")

    # min_amount_operator and max_amount_operator already have defaults in schema
    min_op = filters_raw.get("min_amount_operator", ">=")
    if min_op not in (">=", ">"):
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "min_amount_operator must be '>' or '>='.",
        )
    coerced["min_amount_operator"] = min_op
    if "min_amount_operator" in filters_raw and min_amt is None:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "min_amount_operator requires min_amount.",
        )

    max_op = filters_raw.get("max_amount_operator", "<=")
    if max_op not in ("<=", "<"):
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "max_amount_operator must be '<' or '<='.",
        )
    coerced["max_amount_operator"] = max_op
    if "max_amount_operator" in filters_raw and max_amt is None:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "max_amount_operator requires max_amount.",
        )

    try:
        return QueryFilters(**coerced)
    except Exception as e:
        logger.warning(f"QueryFilters construction failed: {e}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Filter validation failed: {str(e)}")


def _perform_safety_checks(
    intent: Intent,
    metric: Metric,
    aggregation: Aggregation,
    filters: QueryFilters,
    date_range_type: DateRangeType,
) -> QueryRefusal | None:
    """
    Perform safety checks on extracted values.

    Returns:
        QueryRefusal if unsafe pattern detected
        None if safe
    """
    # Check if any filter value contains SQL keywords or dangerous patterns
    filter_dict = filters.model_dump(exclude_none=True)
    for key, value in filter_dict.items():
        if value is None:
            continue
        value_str = str(value)

        if re.search(SQL_KEYWORDS, value_str, re.IGNORECASE):
            logger.error(f"SQL keyword detected in filter {key}: {value_str[:50]}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Query contains unsafe SQL syntax.")

        if re.search(INJECTION_PATTERNS, value_str):
            logger.error(f"Injection pattern detected in filter {key}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "Query contains unsafe syntax.")

    return None


def _is_unsafe_identifier(value: str) -> bool:
    """Check if string looks like a SQL identifier or contains injection patterns."""
    if not value:
        return False

    # Block SQL keywords and reserved words in identifiers
    dangerous_keywords = {
        "accounts",
        "schema",
        "table",
        "column",
        "database",
        "user",
        "password",
        "admin",
        "root",
        "exec",
        "execute",
        "select",
        "delete",
        "drop",
        "insert",
        "update",
        "truncate",
        "union",
        "where",
        "from",
        "join",
        "or",
        "and",
        "--",
        "/*",
        "*/",
        ";",
        "'",
        '"',
        "`",
        "%",
    }

    value_lower = value.lower()
    for keyword in dangerous_keywords:
        if keyword in value_lower:
            return True

    # Check for SQL injection patterns
    if re.search(SQL_KEYWORDS, value, re.IGNORECASE):
        return True
    if re.search(INJECTION_PATTERNS, value):
        return True

    return False


def _is_unsafe_text(value: str) -> bool:
    """Check if text contains SQL injection patterns."""
    if not value:
        return False

    if re.search(SQL_KEYWORDS, value, re.IGNORECASE):
        return True
    if re.search(INJECTION_PATTERNS, value):
        return True

    return False


def _coerce_group_by(
    group_by_raw: Any,
    active_group_by: frozenset[GroupByDimension],
) -> list[GroupByDimension] | QueryRefusal:
    """
    Coerce raw group_by list into GroupByDimension enums.

    Refuses invalid or inactive dimensions; never silently removes them.
    """
    if not isinstance(group_by_raw, list):
        logger.warning(f"group_by is not a list: {type(group_by_raw)}")
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "group_by must be an array.")

    result = []
    for item in group_by_raw:
        try:
            dimension = GroupByDimension(item)
        except (ValueError, KeyError, TypeError):
            logger.warning(f"Invalid group_by dimension: {item}")
            return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, f"Invalid group_by dimension: {item}.")
        if dimension not in active_group_by:
            return mk_refusal(
                QueryRefusalReason.CAPABILITY,
                f"Grouping is not supported by the active data capabilities: {dimension.value}.",
            )
        result.append(dimension)

    if len(set(result)) != len(result):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "group_by dimensions must be unique.")

    return result


def _coerce_limit(limit_raw: Any) -> int | None | QueryRefusal:
    """
    Coerce raw limit to int with bounds checking.

    Supplied malformed or out-of-range values are refused rather than defaulted.
    """
    if limit_raw is None:
        return None

    if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "limit must be an integer.")
    if limit_raw < 1 or limit_raw > 1000:
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "limit must be between 1 and 1000.")
    return limit_raw


def _coerce_comparison(comparison_raw: Any):
    """Accept only the comparison shape implemented by the frozen compiler."""
    if comparison_raw is None:
        return None
    if not isinstance(comparison_raw, dict):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "comparison must be an object or null.")
    if set(comparison_raw) != {"against"}:
        return mk_refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "comparison must contain exactly the supported against field.",
        )
    if comparison_raw["against"] != "previous_month":
        return mk_refusal(
            QueryRefusalReason.CAPABILITY,
            "The supplied comparison is not supported by the active executor.",
        )
    return comparison_raw
