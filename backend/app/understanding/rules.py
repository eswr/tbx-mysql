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
        r"(?:\b(vendors?|suppliers?|payouts?)\b.*\b(owe|paid|pay|payment|spend)\b|\b(owe|paid|pay|payment|spend)\b.*\b(vendors?|suppliers?)\b)",
        "vendor payables",
    ),
    (r"\b(reconciliation|reconcile|reconciled|reconciling|unreconciled)\b", "reconciliation data"),
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
    "state bank of india": "SBIN",
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
DEBIT_CUES = r"\b(spent|spend|spending|paid|payments?|debit|debited?|debits?|outgoing|outflow|withdraw\w*|went out|shell out|shelled out)\b"
CREDIT_CUES = r"\b(received|incoming|credits?|credited|inflow|earned|deposits?|deposited|came in|coming in|got)\b"
# Credit cues that unambiguously describe money *flow*, so they outrank a balance reading.
CREDIT_FLOW_CUES = r"\b(came in|coming in|received|credited|incoming|inflow|deposited)\b"

# Transaction question keywords
TXN_KEYWORDS = r"\b(transaction|transactions|count|sum|list|display|spend|spent|spending|paid|payments?|debits?|credits?|received|incoming|inflow|outgoing|outflow|deposits?|deposited|withdrew|withdrawals?|withdraw|how much|how many|amount|cash|rupees?|money|inr|₹)\b"

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

    if re.search(
        r"\b(drop|alter|delete|insert|update)\s+(table|from|into|[a-z_]+)\b|\bwhere\s+1\s*=\s*1\b|;|"
        r"\b(ignore|disregard)\b.{0,30}\b(instructions?|rules?|prompt)\b|\breveal\b.{0,30}\b(secrets?|credentials?)\b",
        q_lower,
    ):
        return mk_refusal(QueryRefusalReason.INVALID_STRUCTURE, "The question contains unsafe query syntax.")

    # Check unsupported domains first
    for pattern, domain in UNSUPPORTED_DOMAINS:
        if re.search(pattern, q_lower, re.IGNORECASE):
            return mk_refusal(
                QueryRefusalReason.UNSUPPORTED_METRIC,
                f"I don't have access to {domain}.",
                suggestions=["Try asking about transaction amounts, balances, or transaction descriptions"],
            )

    # Month and description grouping still have no compiled plan, so they stay refused.
    # Bank grouping ("accounts per bank") is now compiled, so it falls through below.
    if re.search(
        r"\b(monthly|by month|per month)\b|"
        r"\b(top transaction descriptions|descriptions by spend|top spend(?:ing)? categories)\b",
        q_lower,
    ):
        return mk_refusal(
            QueryRefusalReason.CAPABILITY,
            "Grouped monthly and description breakdowns are not available in the deterministic executor.",
        )

    if context is not None:
        followup = _parse_followup(question, context, ref_date)
        if followup is not None:
            return followup

    # Identity / chitchat
    if re.search(r"^(hi+|hello|hey|yo|thanks|thank you|good (morning|afternoon|evening)|ok|okay)[\s!.?]*$", q_lower):
        return mk_refusal(
            QueryRefusalReason.AMBIGUOUS,
            "I'm Artha, a financial Q&A assistant. Ask me about your transactions, balances, or spending patterns.",
        )

    # Reference lookup
    if re.search(r"\b(reference|ref(?:erence)?\s*(?:no|number|id)|ref\s*[#:]|utr)\b", q_lower):
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
                r"(?:reference|ref(?:erence)?\s*(?:no|number|id)|ref)\s*(?:is|:|#)?\s*([A-Za-z0-9-]{5,64})",
                question,
                re.IGNORECASE,
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
    if re.search(
        r"\b(balance|available across|total available|how much (money|do i have)|have in\b|which bank holds|holds the most money)\b",
        q_lower,
    ):
        if (
            re.search(DEBIT_CUES, q_lower)
            or re.search(CREDIT_FLOW_CUES, q_lower)
            or re.search(r"\b(transaction|spend|spent|paid|debit|credit)\b", q_lower)
        ):
            # Describes money movement, not a stored balance; fall through
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
                # A named bank scopes the balance; without it we sum every account.
                return FinancialQuery(
                    intent=Intent.ACCOUNT_BALANCE,
                    metric=Metric.BALANCE,
                    aggregation=Aggregation.SUM,
                    filters=QueryFilters(bank_code=_extract_bank_code(q_lower)),
                    date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1), label="current"),
                )

    # Keep grouped account counts more specific than total account counts.
    if re.search(r"\baccounts?\b.{0,40}\b(each bank|per bank)\b", q_lower):
        return FinancialQuery(
            intent=Intent.BANK_ACCOUNT_COUNT,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.COUNT,
            filters=QueryFilters(),
            date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1)),
            group_by=[GroupByDimension.BANK],
        )

    # Total account count. This is independent of transaction dates.
    if re.search(
        r"\bhow many accounts\b|"
        r"\b(?:total (?:number|count)|number|count) of accounts\b|"
        r"\baccount count\b|"
        r"\baccounts\b.{0,24}\b(?:are there|do i have|in total|altogether)\b",
        q_lower,
    ):
        return FinancialQuery(
            intent=Intent.ACCOUNT_COUNT,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.COUNT,
            filters=QueryFilters(),
            date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1)),
        )

    # Account list; "transactions from my SBI accounts" is a transaction question, not an account listing.
    if not re.search(r"\btransactions?\b", q_lower) and re.search(
        r"\b(my accounts|all accounts|show.*accounts|list.*accounts)\b", q_lower
    ):
        return FinancialQuery(
            intent=Intent.ACCOUNT_LIST,
            metric=Metric.TRANSACTION_COUNT,
            aggregation=Aggregation.NONE,
            filters=QueryFilters(),
            date_range=DateRange(start=ref_date, end=ref_date + timedelta(days=1)),
            limit=25,
        )

    # Keep unsupported grouping explicit instead of allowing it to fall through to
    # an ungrouped answer or the optional model fallback.
    if re.search(r"\bgroup(?:ed)? by\b|\bby (?:category|merchant|description)\b", q_lower):
        return mk_refusal(
            QueryRefusalReason.CAPABILITY,
            "That grouping dimension is not available in the deterministic executor.",
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
    if re.search(r"\b(how many|count(?: of)?|number of)\b", q_lower):
        metric = Metric.TRANSACTION_COUNT
        aggregation = Aggregation.COUNT
    else:
        metric = Metric.TRANSACTION_AMOUNT
        aggregation = Aggregation.SUM

    # Determine intent
    limit = None
    if re.search(r"\b(largest|biggest|top \d+|largest \d+|highest)\b", q_lower):
        # Superlatives ask for rows, not an aggregate.
        intent = Intent.TRANSACTION_LIST
        aggregation = Aggregation.NONE
        group_by = []
        limit_match = re.search(r"\b(?:top|largest|biggest)\s+(\d+)\b", q_lower)
        limit = int(limit_match.group(1)) if limit_match else 10
    elif (
        re.search(r"\btransactions?\b", q_lower)
        and not re.search(r"\b(how much|how many|count|total|spend|spent|spending)\b", q_lower)
        or re.search(r"^(?:show|list|display)\b", q_lower)
    ):
        intent = Intent.TRANSACTION_LIST
        metric = Metric.TRANSACTION_COUNT
        aggregation = Aggregation.NONE
        group_by = []
        limit = 20
    else:
        intent = Intent.TRANSACTION_SUMMARY
        group_by = []

    description_contains = _extract_description(question)

    # Date range. A listing or a merchant-scoped question is already narrowed, so an
    # absent period means "all time"; an unscoped aggregate stays ambiguous.
    date_range = _extract_date_range(question, reference_date)
    if date_range is None:
        if intent == Intent.TRANSACTION_LIST or description_contains is not None:
            date_range = resolve_date_range(DateRangeType.ALL_TIME, reference_date)
        else:
            return mk_refusal(
                QueryRefusalReason.AMBIGUOUS,
                "What period would you like? (e.g., 'last month', 'August', 'last 7 days', 'this year')",
            )
    # Amount thresholds
    min_amount = None
    min_operator = ">="
    max_amount = None
    max_operator = "<="
    match_min = re.search(r"(?:above|over|(?<!no )more than|exceeding)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
    if match_min:
        min_amount = Decimal(match_min.group(1).replace(",", ""))
        min_operator = ">"
    else:
        match_min = re.search(r"(?:at least|minimum(?: of)?)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
        if match_min:
            min_amount = Decimal(match_min.group(1).replace(",", ""))

    match_max = re.search(r"(?:below|under|less than)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
    if match_max:
        max_amount = Decimal(match_max.group(1).replace(",", ""))
        max_operator = "<"
    else:
        match_max = re.search(r"(?:at most|no more than|maximum(?: of)?)\s*[₹]?\s*([\d,]+(?:\.\d+)?)", q_lower)
        if match_max:
            max_amount = Decimal(match_max.group(1).replace(",", ""))

    filters = QueryFilters(
        transaction_type=txn_type,
        bank_code=_extract_bank_code(q_lower),
        account_id=_extract_account_id(question),
        description_contains=description_contains,
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
            limit=limit,
        )
        return query
    except Exception:
        return None


def _extract_date_range(question: str, reference_date: date) -> DateRange | None:
    """Extract and resolve a date range into a canonical half-open interval."""
    q_lower = question.lower()

    # Explicit ISO range; natural-language end dates are inclusive.
    match = re.search(r"\b(?:from|between)\s+(\d{4}-\d{2}-\d{2})\s+(?:to|and)\s+(\d{4}-\d{2}-\d{2})\b", q_lower)
    if match:
        try:
            start = date.fromisoformat(match.group(1))
            inclusive_end = date.fromisoformat(match.group(2))
            if start > inclusive_end:
                return None
            return DateRange(start=start, end=inclusive_end + timedelta(days=1), label=f"{start} to {inclusive_end}")
        except ValueError:
            return None

    # A single ISO date means that calendar day.
    match = re.search(r"\b(?:on\s+)?(\d{4}-\d{2}-\d{2})\b", q_lower)
    if match:
        try:
            start = date.fromisoformat(match.group(1))
            return DateRange(start=start, end=start + timedelta(days=1), label=str(start))
        except ValueError:
            return None

    # Month-name day, year.
    match = re.search(r"\b([a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b", q_lower)
    if match and match.group(1) in MONTH_NAMES:
        try:
            start = date(int(match.group(3)), MONTH_NAMES[match.group(1)], int(match.group(2)))
            return DateRange(start=start, end=start + timedelta(days=1), label=str(start))
        except ValueError:
            return None

    # Explicit month + year
    match = re.search(r"\b([a-z]+)\s+(\d{4})\b", q_lower)
    if match:
        month_name = match.group(1)
        year = int(match.group(2))
        if month_name in MONTH_NAMES:
            return resolve_date_range(DateRangeType.CALENDAR_MONTH, reference_date, month=month_name, year=year)

    # Bare month name
    for month_name in MONTH_NAMES:
        if re.search(rf"\b{re.escape(month_name)}\b", q_lower):
            return resolve_date_range(DateRangeType.CALENDAR_MONTH, reference_date, month=month_name)

    # Explicit ranges
    if "yesterday" in q_lower:
        return resolve_date_range(DateRangeType.YESTERDAY, reference_date)
    if "today" in q_lower:
        return resolve_date_range(DateRangeType.TODAY, reference_date)
    if re.search(r"\b(last week|past week|previous week)\b", q_lower):
        return resolve_date_range(DateRangeType.LAST_WEEK, reference_date)
    if re.search(r"\b(this week|current week)\b", q_lower):
        return resolve_date_range(DateRangeType.THIS_WEEK, reference_date)
    if re.search(r"\b(last month|previous month)\b", q_lower):
        return resolve_date_range(DateRangeType.CALENDAR_MONTH, reference_date)
    if re.search(r"\b(this month|current month)\b", q_lower):
        return resolve_date_range(DateRangeType.THIS_MONTH, reference_date)
    match = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+days?\b", q_lower)
    if match:
        days = int(match.group(1))
        if days < 1 or days > 3660:
            return None
        return DateRange(
            start=reference_date - timedelta(days=days - 1),
            end=reference_date + timedelta(days=1),
            label=f"last {days} days",
        )
    if re.search(r"\b(last \d+ months?|past \d+ months?)\b", q_lower):
        return resolve_date_range(DateRangeType.LAST_N_MONTHS, reference_date)
    if re.search(r"\b(this year|ytd|year to date)\b", q_lower):
        return resolve_date_range(DateRangeType.THIS_YEAR, reference_date)
    if re.search(r"\b(last year)\b", q_lower):
        return resolve_date_range(DateRangeType.LAST_YEAR, reference_date)
    if re.search(r"\b(all time|ever)\b", q_lower):
        return resolve_date_range(DateRangeType.ALL_TIME, reference_date)

    return None


# Explicit description cues, then a capitalised merchant name after "at".
DESCRIPTION_KEYWORD_PATTERN = re.compile(
    r"\b(?:containing|contains|matching|mentioning|described as|with description)\s+(.+)",
    re.IGNORECASE,
)
DESCRIPTION_MERCHANT_PATTERN = re.compile(r"\b[Aa]t\s+([A-Z][A-Za-z0-9&'.\-]*(?:\s+[A-Z][A-Za-z0-9&'.\-]*)*)")
# Tokens that end a description phrase because they introduce another clause.
DESCRIPTION_STOP_WORDS = frozenset(
    {
        "a",
        "above",
        "after",
        "all",
        "an",
        "and",
        "at",
        "before",
        "below",
        "between",
        "by",
        "during",
        "for",
        "from",
        "in",
        "last",
        "my",
        "of",
        "on",
        "or",
        "over",
        "since",
        "the",
        "this",
        "to",
        "under",
        "with",
    }
)
MAX_DESCRIPTION_TOKENS = 4


def _clean_description(raw: str) -> str | None:
    """Trim a captured phrase down to the merchant-like tokens the ILIKE filter needs."""
    tokens: list[str] = []
    for token in raw.split():
        stripped = token.strip(".,;:!?\"'()")
        if not stripped or stripped.lower() in DESCRIPTION_STOP_WORDS:
            break
        tokens.append(stripped)
        if len(tokens) >= MAX_DESCRIPTION_TOKENS:
            break
    value = " ".join(tokens)
    # A bare bank name is already handled by the bank_code filter.
    if not value or value.lower() in BANK_ALIASES:
        return None
    return value


def _extract_description(question: str) -> str | None:
    """Extract a free-text description filter; compiled to a case-insensitive ILIKE."""
    for pattern in (DESCRIPTION_KEYWORD_PATTERN, DESCRIPTION_MERCHANT_PATTERN):
        match = pattern.search(question)
        if match:
            value = _clean_description(match.group(1))
            if value is not None:
                return value
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
    date_range = _extract_date_range(question, reference_date)
    if date_range is None:
        return None
    comparison = None
    intent = context.intent
    if re.search(r"\bcompare\b", question, re.IGNORECASE):
        intent = Intent.COMPARISON
        comparison = ComparisonSpec(against="previous_month")
        date_range = context.date_range
    filters = context.filters.model_copy(deep=True)
    bank_code = _extract_bank_code(question)
    account_id = _extract_account_id(question)
    q_lower = question.lower()
    transaction_type = None
    if re.search(DEBIT_CUES, q_lower):
        transaction_type = "debit"
    elif re.search(CREDIT_CUES, q_lower):
        transaction_type = "credit"
    if bank_code is not None:
        filters.bank_code = bank_code
    if account_id is not None:
        filters.account_id = account_id
    if transaction_type is not None:
        filters.transaction_type = transaction_type
    return FinancialQuery(
        intent=intent,
        metric=context.metric,
        aggregation=context.aggregation,
        filters=filters,
        date_range=date_range,
        group_by=list(context.group_by),
        comparison=comparison,
        limit=context.limit if context.aggregation == Aggregation.NONE else None,
    )
