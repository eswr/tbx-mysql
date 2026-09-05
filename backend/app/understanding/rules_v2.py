"""Improved rule-based understanding v2."""

import re
from decimal import Decimal
from datetime import timedelta

from app.schemas.financial_query import (
    FinancialQuery,
    Intent,
    Metric,
    Aggregation,
    GroupByDimension,
    QueryFilters,
    DateRange,
    DateRangeType,
    QueryRefusal,
    QueryRefusalReason,
    refusal as mk_refusal,
)
from app.understanding.dates import resolve_date_range, today_ist

# Simple refusal patterns
UNSUPPORTED = [
    (r"\b(payroll|salary|wages|employees?)\b", "payroll"),
    (r"\b(tax|gst|tds)\b", "tax"),
    (r"\b(invoice|overdue|receivable|payable)\b", "invoices"),
    (r"\b(reconcil|unreconciled)\b", "reconciliation"),
    (r"\b(forecast|prediction)\b", "forecasts"),
]


def parse_q(question: str) -> FinancialQuery | QueryRefusal | None:
    """Simple rule-based parser."""
    q = question.lower()

    # Unsupported
    for pattern, domain in UNSUPPORTED:
        if re.search(pattern, q):
            return mk_refusal(QueryRefusalReason.UNSUPPORTED_METRIC, f"No {domain} data.")

    # Balance
    if re.search(r"\b(balance|my balance|account balance)", q):
        if re.search(r"\b(how much|total)\b", q):
            return FinancialQuery(
                intent=Intent.ACCOUNT_BALANCE,
                metric=Metric.BALANCE,
                aggregation=Aggregation.SUM,
                filters=QueryFilters(),
                date_range=DateRange(start=today_ist(), end=today_ist() + timedelta(days=1)),
            )

    # Count how many accounts
    if re.search(r"\b(how many accounts|accounts per bank)\b", q):
        return FinancialQuery(
            intent=Intent.BANK_ACCOUNT_COUNT,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.COUNT,
            filters=QueryFilters(),
            date_range=DateRange(start=today_ist(), end=today_ist() + timedelta(days=1)),
            group_by=[GroupByDimension.BANK],
        )

    # List accounts
    if re.search(r"\b(my accounts|show.*accounts)\b", q):
        return FinancialQuery(
            intent=Intent.ACCOUNT_LIST,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.NONE,
            filters=QueryFilters(),
            date_range=DateRange(start=today_ist(), end=today_ist() + timedelta(days=1)),
            limit=25,
        )

    # Reference lookup
    if "reference" in q or "ref" in q or "utr" in q:
        match = re.search(r"([A-Za-z0-9]{6,20})", question)
        if match:
            ref = match.group(1)
            return FinancialQuery(
                intent=Intent.REFERENCE_LOOKUP,
                metric=Metric.TRANSACTION_COUNT,
                aggregation=Aggregation.NONE,
                filters=QueryFilters(reference_id=ref),
                date_range=DateRange(start=today_ist(), end=today_ist() + timedelta(days=1)),
            )
        return mk_refusal(QueryRefusalReason.AMBIGUOUS, "Need a reference or UTR number.")

    # Transaction-related (spend, debits, etc.)
    if re.search(r"\b(spend|spent|spending|debit|received|credit|transaction)\b", q):
        # Infer type
        txn_type = None
        if re.search(r"\b(spend|spent|spending|debit|paid)\b", q):
            txn_type = "debit"
        elif re.search(r"\b(received|credit|incoming)\b", q):
            txn_type = "credit"

        # Metric
        if re.search(r"\b(how many|count)\b", q):
            metric = Metric.TRANSACTION_COUNT
            agg = Aggregation.COUNT
        else:
            metric = Metric.TRANSACTION_AMOUNT
            agg = Aggregation.SUM

        # Intent
        if re.search(r"\b(show|list|display|find)\b", q):
            intent = Intent.TRANSACTION_LIST
        elif re.search(r"\b(monthly|by month|per month)\b", q):
            intent = Intent.MONTHLY_TREND
        else:
            intent = Intent.TRANSACTION_SUMMARY

        # Date: must have one
        date_spec = _extract_date(q)
        if not date_spec:
            return mk_refusal(
                QueryRefusalReason.AMBIGUOUS, "Which period? (e.g., 'August', 'last month', 'last 7 days')"
            )

        dr = resolve_date_range(date_spec, today_ist())

        # Bank filter
        bank_code = None
        if "hdfc" in q:
            bank_code = "HDFC"
        elif "icici" in q or "icic" in q:
            bank_code = "ICIC"
        elif re.search(r"\b(sbi|state bank)\b", q):
            bank_code = "SBIN"
        elif re.search(r"\baxis\b", q):
            bank_code = "UTIB"

        # Amount thresholds
        min_amount = None
        match = re.search(r"(?:above|over|more than|greater than|> )([\d,]+)", q)
        if match:
            min_amount = Decimal(match.group(1).replace(",", ""))

        filters = QueryFilters(
            transaction_type=txn_type,
            bank_code=bank_code,
            min_amount=min_amount,
        )

        group_by_list = []
        if intent == Intent.MONTHLY_TREND:
            group_by_list = [GroupByDimension.MONTH]

        return FinancialQuery(
            intent=intent,
            metric=metric,
            aggregation=agg,
            filters=filters,
            date_range=dr,
            group_by=group_by_list,
            limit=20 if intent == Intent.TRANSACTION_LIST else None,
        )

    # Off-topic
    return mk_refusal(QueryRefusalReason.AMBIGUOUS, "Ask about spending, balances, or transactions.")


def _extract_date(q: str) -> DateRangeType | None:
    """Extract date range type from question."""
    if "august" in q or " aug " in q:
        return DateRangeType.CALENDAR_MONTH
    if "july" in q or " jul " in q:
        return DateRangeType.CALENDAR_MONTH
    if "last month" in q:
        return DateRangeType.CALENDAR_MONTH
    if "this month" in q:
        return DateRangeType.THIS_MONTH
    if re.search(r"last \d+ days?", q):
        return DateRangeType.LAST_N_DAYS
    if re.search(r"last \d+ months?", q):
        return DateRangeType.LAST_N_MONTHS
    if "last week" in q:
        return DateRangeType.LAST_WEEK
    if "this week" in q:
        return DateRangeType.THIS_WEEK
    if "yesterday" in q:
        return DateRangeType.YESTERDAY
    if "today" in q:
        return DateRangeType.TODAY
    return None
