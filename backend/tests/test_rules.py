"""Quick test of rules parser."""

import pytest
from datetime import date
from app.conversation import ConversationContext
from app.understanding.rules import understand_question
from app.schemas.financial_query import FinancialQuery, QueryRefusal, Intent


def parse_q(question, context=None):
    return understand_question(question, context=context, reference_date=date(2026, 9, 5))

def test_spend_august():
    result = parse_q("How much did I spend in August 2026?")
    assert isinstance(result, FinancialQuery)
    assert result.intent == Intent.TRANSACTION_SUMMARY
    assert result.filters.transaction_type == "debit"

def test_balance():
    result = parse_q("What's my total balance?")
    assert isinstance(result, FinancialQuery)
    assert result.intent == Intent.ACCOUNT_BALANCE

def test_list_transactions():
    result = parse_q("Show me HDFC credit transactions")
    assert isinstance(result, QueryRefusal)
    assert result.reason.value == "ambiguous"

def test_unsupported_payroll():
    result = parse_q("How much do my employees earn?")
    assert isinstance(result, QueryRefusal)
    assert result.reason.value == "unsupported_metric"

def test_ambiguous_no_date():
    result = parse_q("Show HDFC transactions")
    # This should ask for date clarification
    assert isinstance(result, QueryRefusal)


@pytest.mark.parametrize(("phrase", "field", "operator"), [
    ("above 50000", "min_amount", ">"),
    ("over 50000", "min_amount", ">"),
    ("more than 50000", "min_amount", ">"),
    ("at least 50000", "min_amount", ">="),
    ("minimum 50000", "min_amount", ">="),
    ("below 50000", "max_amount", "<"),
    ("under 50000", "max_amount", "<"),
    ("less than 50000", "max_amount", "<"),
    ("at most 50000", "max_amount", "<="),
    ("no more than 50000", "max_amount", "<="),
    ("maximum 50000", "max_amount", "<="),
])
def test_amount_operator_language(phrase, field, operator):
    result = parse_q(f"How many transactions {phrase} in August?")
    assert isinstance(result, FinancialQuery)
    assert getattr(result.filters, field) == 50000
    assert getattr(result.filters, f"{field}_operator") == operator


def test_named_month_and_last_week_are_half_open():
    august = parse_q("How much did I spend in August 2026?")
    last_week = parse_q("Transactions last week?")
    assert (august.date_range.start, august.date_range.end) == (date(2026, 8, 1), date(2026, 9, 1))
    assert (last_week.date_range.start, last_week.date_range.end) == (date(2026, 8, 24), date(2026, 8, 31))


def test_followup_inherits_semantics_but_replaces_period():
    first = parse_q("How much did I spend last month?")
    context = ConversationContext.from_query(first)
    followup = parse_q("What about July?", context)
    assert followup.filters.transaction_type == "debit"
    assert (followup.date_range.start, followup.date_range.end) == (date(2026, 7, 1), date(2026, 8, 1))
