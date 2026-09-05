"""Quick test of rules parser."""

import pytest
from app.understanding.rules_v2 import parse_q
from app.schemas.financial_query import FinancialQuery, QueryRefusal, Intent

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
    assert isinstance(result, FinancialQuery)
    assert result.intent == Intent.TRANSACTION_LIST or result.intent == Intent.TRANSACTION_SUMMARY

def test_unsupported_payroll():
    result = parse_q("How much do my employees earn?")
    assert isinstance(result, QueryRefusal)
    assert result.reason.value == "unsupported_metric"

def test_ambiguous_no_date():
    result = parse_q("Show HDFC transactions")
    # This should ask for date clarification
    assert isinstance(result, (FinancialQuery, QueryRefusal))
