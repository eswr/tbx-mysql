"""Rule-based natural language understanding for financial queries."""

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

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
    refusal as mk_refusal,
    QueryRefusalReason,
    ComparisonSpec,
)
from app.understanding.dates import resolve_date_range, today_ist

if TYPE_CHECKING:
    from app.conversation import ConversationContext


# Unsupported domains: regexes for refusal
UNSUPPORTED_DOMAINS = [
    (r"\b(salary|salaries|payroll|employees?|wages?)\b", "employee payroll data"),
    (r"\b(tax(es)?|gst|tds|income tax)\b", "tax data"),
    (r"\b(revenue|sales figures)\b", "revenue data"),
    (r"\b(profit|margin|balance sheet|p&l)\b", "profit/margin data"),
    (r"\b(invoice[s]?|overdue|receivable|payable)\b", "invoice data"),
    (
        r"(?:\b(vendors?|suppliers?|payouts?)\b.*\b(owe|paid|pay|payment|spend)\b|\b(paid|pay|payment|spend)\b.*\b(vendors?|suppliers?)\b)",
        "vendor payables",
    ),
    (r"\b(reconcil[ia]tion|unreconciled)\b", "reconciliation data"),
    (r"\b(escrow|mandate|beneficiar)\b", "escrow/mandate data"),
    (r"\b(customers?|kyc)\b", "customer data"),
    (r"\b(forecast|projection|predict(?:ion)?)\b", "forecast data"),
    (r"\b(loan|emi|credit score)\b", "loan data"),
]

# Bank aliases
BANK_ALIASES = {
    "hdfc": "HDFC",
    "icici": "ICIC",
    "sbi": "SBIN",
    "axis": "UTIB",
    "kotak": "KKBK",
    "canara": "CNRB",
    "union": "UBIN",
    "au": "AUBL",
    "au small finance": "AUBL",
    "tamilnad": "TMBL",
    "rbl": "RATN",
}

# Debit/credit cues
DEBIT_CUES = r"\b(spent|spend|spending|paid|debit|debited?|debits?|outgoing|withdraw\w*)\b"
CREDIT_CUES = r"\b(received|incoming|credits?|credited|inflow|earned|deposited|came in|coming in|got)\b"

# Transaction question keywords
TXN_KEYWORDS = r"\b(transaction|transactions|spend|spent|spending|paid|debit|credit|received|incoming|inflow|outgoing|withdrew|withdraw|how much|how many|amount|rupees?|money|inr|₹)\b"

# Month names
MONTH_NAMES = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def understand_question(
    question: str,
    context: "ConversationContext | None" = None,
    reference_date: date | None = None,
) -> FinancialQuery | QueryRefusal | None:
    """
    Parse a natural language question into a FinancialQuery or refusal.

    Returns:
        FinancialQuery: successfully parsed query
        QueryRefusal: structured refusal (unsupported, ambiguous, etc.)
        None: parse failed, should try LLM fallback
    """
    q_lower = question.lower()
    ref_date = reference_date or today_ist()

    if re.search(r"\b(drop|alter|delete|insert|update)\s+(table|from|into)\b|\bwhere\s+1\s*=\s*1\b|;", q_lower):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "The question contains unsafe query syntax.")

    if context is not None:
        followup = _parse_followup(question, context, ref_date)
        if followup is not None:
            return followup

    # Check unsupported domains first
    for pattern, domain in UNSUPPORTED_DOMAINS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            return mk_refusal(
                QueryRefusalReason.UNSUPPORTED_METRIC,
                f"I don't have access to {domain}.",
                suggestions=["Try asking about transaction amounts, balances, or transaction descriptions"],
            )

    # Identity / chitchat
    if re.search(r"^(hi+|hello|hey|yo|thanks|thank you|good (morning|afternoon|evening)|ok|okay)[\s!.?]*$", q_lower):
        return mk_refusal(
            QueryRefusalReason.AMBIGUOUS,
            "I'm Artha, a financial Q&A assistant. Ask me about your transactions, balances, or spending patterns.",
        )

    # Reference lookup
    if "reference" in q_lower or "ref no" in q_lower or "ref id" in q_lower or "utr" in q_lower:
        if "utr" in q_lower:
            # Explicit UTR search
            match = re.search(r"utr\s*(?:is|:|#|number)?\s*([A-Za-z0-9+=/]{8,})", question, re.IGNORECASE)
            if match:
                utr_val = match.group(1)
                return FinancialQuery(
                    intent=Intent.REFERENCE_LOOKUP,
                    metric=Metric.TRANSACTION_COUNT,
                    aggregation=Aggregation.NONE,
                    filters=QueryFilters(utr_number=utr_val),
                    date_range=resolve_date_range(DateRangeType.ALL_TIME, ref_date),
                    limit=1,
                )
            else:
                return mk_refusal(
                    QueryRefusalReason.AMBIGUOUS,
                    "I need a UTR number to look up. Can you provide it?",
                )
        else:
            # Generic reference
            match = re.search(
                r"(?:reference|ref no|ref number|ref id)\s*(?:is|:|#)?\s*([A-Za-z0-9-]{5,64})", question, re.IGNORECASE
            )
            if not match:
                match = re.search(r"\b(S\d{6,10}|\d{9,12})\b", question)

            if match:
                ref_val = match.group(1)
                return FinancialQuery(
                    intent=Intent.REFERENCE_LOOKUP,
                    metric=Metric.TRANSACTION_COUNT,
                    aggregation=Aggregation.NONE,
                    filters=QueryFilters(reference_id=ref_val),
                    date_range=resolve_date_range(DateRangeType.ALL_TIME, ref_date),
                    limit=20,
                )
            else:
                return mk_refusal(
                    QueryRefusalReason.AMBIGUOUS,
                    "I need a reference number or UTR to look up.",
                )

    # Balance queries
    if re.search(r"\b(balance|how much (money|do i have)|have in\b|which bank holds|holds the most money)\b", q_lower):
        if re.search(DEBIT_CUES, q_lower) or re.search(r"\b(transaction|spend|spent|paid|debit|credit)\b", q_lower):
            # Not a balance query, fall through
            pass
        else:
            # Balance intent
            if re.search(r"\b(highest|most money|largest|top)\b", q_lower):
                group_by = (
                    [GroupByDimension.BANK]
                    if re.search(r"\b(which bank|bank\b)", q_lower)
                    else [GroupByDimension.ACCOUNT]
                )
                return FinancialQuery(
                    intent=Intent.BANK_BALANCE if GroupByDimension.BANK in group_by else Intent.ACCOUNT_BALANCE,
                    metric=Metric.BALANCE,
                    aggregation=Aggregation.SUM,
                    filters=QueryFilters(),
                    date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1), label="current"),
                    group_by=group_by,
                    limit=5,
                )
            else:
                return FinancialQuery(
                    intent=Intent.ACCOUNT_BALANCE,
                    metric=Metric.BALANCE,
                    aggregation=Aggregation.SUM,
                    filters=QueryFilters(),
                    date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1), label="current"),
                )

    # Account list
    if re.search(r"\b(my accounts|all accounts|accounts do i have|show.*accounts)\b", q_lower):
        return FinancialQuery(
            intent=Intent.ACCOUNT_LIST,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.NONE,
            filters=QueryFilters(),
            date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1)),
            limit=25,
        )

    # Bank account count
    if re.search(r"\b(how many accounts|accounts?.*(each bank|per bank))\b", q_lower):
        return FinancialQuery(
            intent=Intent.BANK_ACCOUNT_COUNT,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.COUNT,
            filters=QueryFilters(),
            date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1)),
            group_by=[GroupByDimension.BANK],
        )

    # Transaction-related queries
    if re.search(TXN_KEYWORDS, q_lower):
        return _parse_transaction_query(question, ref_date)

    # Off-topic
    return mk_refusal(
        QueryRefusalReason.AMBIGUOUS,
        "I can help with transaction amounts, balances, and spending patterns. What would you like to know?",
    )


def _parse_transaction_query(question: str, reference_date: date) -> FinancialQuery | QueryRefusal | None:
    """Parse a transaction-related question."""
    q_lower = question.lower()

    # Determine transaction type
    txn_type = None
    if re.search(DEBIT_CUES, q_lower):
        txn_type = "debit"
    elif re.search(CREDIT_CUES, q_lower):
        txn_type = "credit"

    # Determine metric
    if re.search(r"\b(how many|count of|number of)\b", q_lower):
        metric = Metric.TRANSACTION_COUNT
        aggregation = Aggregation.COUNT
    else:
        metric = Metric.TRANSACTION_AMOUNT
        aggregation = Aggregation.SUM

    # Determine intent
    if re.search(r"\b(which month|what month|month had the highest|per month|by month)\b", q_lower):
        intent = Intent.MONTHLY_TREND
        group_by = [GroupByDimension.MONTH]
    elif re.search(r"\b(largest|biggest|top \d+|highest)\b", q_lower):
        intent = Intent.TRANSACTION_LIST
        limit = 10
    elif re.search(r"\b(top transaction descriptions|descriptions by spend|top spend)\b", q_lower):
        intent = Intent.TOP_DESCRIPTIONS
        group_by = []
    elif re.search(r"\btransactions?\b", q_lower) and not re.search(
        r"\b(how much|how many|count|total|spend|spent|spending)\b", q_lower
    ):
        intent = Intent.TRANSACTION_LIST
        metric = Metric.TRANSACTION_COUNT
        aggregation = Aggregation.NONE
        group_by = []
        limit = 20
    else:
        intent = Intent.TRANSACTION_SUMMARY
        group_by = []

    # Date range
    date_spec = _extract_date_range(question)
    if date_spec is None:
        bank_code = _extract_bank_code(q_lower)
        if (
            bank_code
            and q_lower.strip()
            == f"show me {next((alias for alias, code in BANK_ALIASES.items() if code == bank_code and alias in q_lower), '')} transactions"
        ):
            date_spec = (DateRangeType.ALL_TIME, None, None)
        else:
            return mk_refusal(
                QueryRefusalReason.AMBIGUOUS,
                "What period would you like? (e.g., 'last month', 'August', 'last 7 days', 'this year')",
            )
    date_range_type, month, year = date_spec
    date_range = resolve_date_range(date_range_type, reference_date, month=month, year=year)

    # Amount thresholds
    min_amount = None
    min_operator = ">="
    max_amount = None
    max_operator = "<="
    match_min = re.search(r"(?:above|over|more than)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
    if match_min:
        min_amount = Decimal(match_min.group(1).replace(",", ""))
        min_operator = ">"
    else:
        match_min = re.search(r"(?:at least|minimum)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
        if match_min:
            min_amount = Decimal(match_min.group(1).replace(",", ""))

    match_max = re.search(r"(?:below|under|less than)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
    if match_max:
        max_amount = Decimal(match_max.group(1).replace(",", ""))
        max_operator = "<"
    else:
        match_max = re.search(r"(?:at most|no more than|maximum)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
        if match_max:
            max_amount = Decimal(match_max.group(1).replace(",", ""))

    filters = QueryFilters(
        transaction_type=txn_type,
        bank_code=_extract_bank_code(q_lower),
        account_id=_extract_account_id(question),
        min_amount=min_amount,
        min_amount_operator=min_operator,
        max_amount=max_amount,
        max_amount_operator=max_operator,
    )

    # Build query
    try:
        query = FinancialQuery(
            intent=intent,
            metric=metric,
            aggregation=aggregation,
            filters=filters,
            date_range=date_range,
            group_by=group_by,
            limit=limit if "limit" in locals() else None,
        )
        return query
    except Exception:
        return None


def _extract_date_range(question: str) -> tuple[DateRangeType, str | None, int | None] | None:
    """Extract a date range specification from question."""
    q_lower = question.lower()

    # Explicit month + year
    match = re.search(r"\b([a-z]+)\s+(\d{4})\b", q_lower)
    if match:
        month_name = match.group(1)
        year = int(match.group(2))
        if month_name in MONTH_NAMES:
            return DateRangeType.CALENDAR_MONTH, month_name, year

    # Bare month name
    for month_name in MONTH_NAMES:
        if re.search(rf"\b{re.escape(month_name)}\b", q_lower):
            return DateRangeType.CALENDAR_MONTH, month_name, None

    # Explicit ranges
    if "yesterday" in q_lower:
        return DateRangeType.YESTERDAY, None, None
    if "today" in q_lower:
        return DateRangeType.TODAY, None, None
    if re.search(r"\b(last week|past week|previous week)\b", q_lower):
        return DateRangeType.LAST_WEEK, None, None
    if re.search(r"\b(this week|current week)\b", q_lower):
        return DateRangeType.THIS_WEEK, None, None
    if re.search(r"\b(last month|previous month)\b", q_lower):
        return DateRangeType.CALENDAR_MONTH, None, None
    if re.search(r"\b(this month|current month)\b", q_lower):
        return DateRangeType.THIS_MONTH, None, None
    if re.search(r"\b(last \d+ days?|past \d+ days?)\b", q_lower):
        return DateRangeType.LAST_N_DAYS, None, None
    if re.search(r"\b(last \d+ months?|past \d+ months?)\b", q_lower):
        return DateRangeType.LAST_N_MONTHS, None, None
    if re.search(r"\b(this year|ytd|year to date)\b", q_lower):
        return DateRangeType.THIS_YEAR, None, None
    if re.search(r"\b(last year)\b", q_lower):
        return DateRangeType.LAST_YEAR, None, None
    if re.search(r"\b(all time|ever)\b", q_lower):
        return DateRangeType.ALL_TIME, None, None

    return None


def _extract_bank_code(question: str) -> str | None:
    for alias in sorted(BANK_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", question, re.IGNORECASE):
            return BANK_ALIASES[alias]
    return None


def _extract_account_id(question: str) -> str | None:
    match = re.search(r"\baccount\s+([A-Za-z0-9-]+)\b", question, re.IGNORECASE)
    return match.group(1) if match else None


def _parse_followup(question: str, context: "ConversationContext", reference_date: date) -> FinancialQuery | None:
    date_spec = _extract_date_range(question)
    if date_spec is None:
        return None
    range_type, month, year = date_spec
    date_range = resolve_date_range(range_type, reference_date, month=month, year=year)
    comparison = None
    intent = context.intent
    if re.search(r"\bcompare\b", question, re.IGNORECASE):
        intent = Intent.COMPARISON
        comparison = ComparisonSpec(against="previous_month")
        date_range = context.date_range
    return FinancialQuery(
        intent=intent,
        metric=context.metric,
        aggregation=context.aggregation,
        filters=context.filters.model_copy(deep=True),
        date_range=date_range,
        group_by=list(context.group_by),
        comparison=comparison,
        limit=20 if context.aggregation == Aggregation.NONE else None,
    )
