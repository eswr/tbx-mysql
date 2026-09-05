"""Ollama-based LLM for query understanding fallback."""

import json
import logging
from typing import Optional

import httpx

from app.schemas.financial_query import FinancialQuery, QueryRefusal, QueryRefusalReason, refusal as mk_refusal

logger = logging.getLogger(__name__)


QUERY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "transaction_summary",
                "transaction_list",
                "top_descriptions",
                "monthly_trend",
                "comparison",
                "account_balance",
                "account_list",
                "bank_balance",
                "bank_account_count",
                "reference_lookup",
            ],
        },
        "metric": {
            "type": "string",
            "enum": ["transaction_amount", "transaction_count", "balance"],
        },
        "aggregation": {
            "type": "string",
            "enum": ["sum", "count", "avg", "max", "min", "none"],
        },
        "filters": {
            "type": "object",
            "properties": {
                "bank_code": {"type": ["string", "null"]},
                "account_id": {"type": ["string", "null"]},
                "transaction_type": {"type": ["string", "null"], "enum": ["credit", "debit", None]},
                "description_contains": {"type": ["string", "null"]},
                "reference_id": {"type": ["string", "null"]},
                "utr_number": {"type": ["string", "null"]},
                "min_amount": {"type": ["number", "null"]},
                "max_amount": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
        "date_range_type": {
            "type": "string",
            "enum": [
                "calendar_month",
                "last_n_months",
                "custom",
                "all_time",
                "month_before_previous",
                "this_month",
                "this_week",
                "last_week",
                "last_n_days",
                "yesterday",
                "today",
                "this_year",
                "last_year",
            ],
        },
        "month": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "group_by": {
            "type": "array",
            "items": {"type": "string", "enum": ["bank", "account", "transaction_type", "month"]},
        },
        "limit": {"type": ["integer", "null"]},
        "refusal": {
            "type": ["object", "null"],
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["unsupported_metric", "ambiguous", "invalid_structure", "no_data", "capability"],
                },
                "message": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["refusal"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are Artha, a financial query assistant.

Users ask questions about:
- Transaction amounts and counts (debits/credits)
- Account balances
- Transaction descriptions, references, and dates
- Spending by bank, account, or time period

IMPORTANT RULES:
1. Never invent data. If a query is unsupported (vendor payables, invoices, reconciliation, tax, payroll, forecasts), return a refusal.
2. Always provide a date range. If missing, ask for clarification (refusal: ambiguous).
3. When the user says "spend", they mean debit transactions.
4. When the user says "received" or "incoming", they mean credit transactions.
5. Reference numbers and UTRs are distinct. "Reference" = transaction_reference_id; only "UTR" = utr_number.
6. Never compute dates yourself; let the application resolve them.
7. For "last month", "August", "last 7 days", set the date_range_type and let the application compute the exact dates.
8. If ambiguous (e.g., "How much did I spend?" with no period), return a refusal asking for clarification.

Respond ONLY with a JSON object matching the provided schema. If you cannot parse the question, return a refusal.
"""


async def understand_with_ollama(
    question: str,
    base_url: str,
    model: str,
    timeout: float = 30.0,
) -> FinancialQuery | QueryRefusal | None:
    """
    Call Ollama with constrained JSON schema to understand a question.
    
    Args:
        question: User's natural language question
        base_url: Ollama base URL (e.g., "http://127.0.0.1:11434")
        model: Model name (e.g., "qwen3.5:0.8b")
        timeout: Request timeout in seconds
    
    Returns:
        FinancialQuery, QueryRefusal, or None (parse failed)
    """
    
    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "format": "json",
        "temperature": 0,
        "num_predict": 512,
        "stream": False,
    }
    
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            
            data = response.json()
            content = data.get("message", {}).get("content", "")
            
            # Extract JSON from content
            json_str = _extract_json(content)
            if not json_str:
                logger.warning(f"No JSON in Ollama response: {content[:100]}")
                return None
            
            parsed = json.loads(json_str)
            
            # Check if it's a refusal
            if parsed.get("refusal"):
                return QueryRefusal(
                    reason=QueryRefusalReason(parsed["refusal"]["reason"]),
                    message=parsed["refusal"]["message"],
                )
            
            # Otherwise, construct FinancialQuery
            # Experimental adapter construction remains outside the production rule path.
            return None  # Placeholder
    
    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to Ollama at {base_url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Ollama request failed: {e}")
        return None


def _extract_json(text: str) -> str | None:
    """Extract JSON object from text, handling markdown code blocks."""
    import json
    
    # Try direct parse first
    try:
        json.loads(text)
        return text
    except:
        pass
    
    # Look for ```json ... ``` block
    if "```json" in text:
        start = text.index("```json") + len("```json")
        if "```" in text[start:]:
            end = text.index("```", start)
            candidate = text[start:end].strip()
            try:
                json.loads(candidate)
                return candidate
            except:
                pass
    
    # Look for plain ``` ... ``` block
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part and part[0] == "{":
                try:
                    json.loads(part)
                    return part
                except:
                    pass
    
    # Try to find a leading { and extract to matching }
    if "{" in text:
        start = text.index("{")
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except:
                        pass
    
    return None
