"""Privacy-preserving Ollama fallback for semantic query understanding."""

import copy
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from app.conversation import ConversationContext
from app.query.base import Capabilities
from app.schemas.financial_query import (
    FinancialQuery,
    Intent,
    QueryRefusal,
    QueryRefusalReason,
    refusal,
)
from app.understanding.dates import today_ist
from app.understanding.normalize import (
    ActiveQueryCapabilities,
    DEFAULT_ACTIVE_CAPABILITIES,
    normalize_llm_output,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OllamaMetadata:
    """Inference metadata retained through the API and evaluation layers."""

    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class OllamaResult:
    output: FinancialQuery | QueryRefusal
    metadata: OllamaMetadata


@dataclass(frozen=True)
class SensitiveReplacement:
    value: str
    filter_field: str | None


SYSTEM_PROMPT = """You are Artha, a financial query parser.
Return only JSON matching the supplied schema. You parse requests; a database executes them later.
Do not refuse because you lack account data and never return no_data; emit a query for every supported, specific request.
Use semantic conversation context only to resolve an underspecified follow-up.
"spend" means debit; "received" or "incoming" means credit.
Reference IDs and UTRs are distinct fields. Do not compute dates; select a date_range_type.
For a supported query, fill every query field and set refusal to null.
For an ambiguous or unsupported request, return an object containing only refusal.
Example for "How much did I spend last month?":
{"intent":"transaction_summary","metric":"transaction_amount","aggregation":"sum","filters":{"transaction_type":"debit"},"date_range_type":"calendar_month","month":null,"year":null,"group_by":[],"limit":null,"comparison":null,"refusal":null}
"""


def capabilities_from_discovery(discovered: Capabilities) -> ActiveQueryCapabilities:
    """Intersect discovered columns with the frozen compiler's executable surface."""
    tables = set(discovered.tables)
    columns = {table: set(values) for table, values in discovered.columns.items()}
    transaction = columns.get("transaction", set())
    account = columns.get("account", set())
    bank = columns.get("bank", set())

    intents: set[Intent] = set()
    filters: set[str] = set()
    transaction_core = {"transaction_date", "transaction_type", "transaction_amount"}
    if "transaction" in tables and transaction_core <= transaction:
        intents.update({Intent.TRANSACTION_SUMMARY, Intent.COMPARISON})
        filters.update({"transaction_type", "min_amount", "min_amount_operator", "max_amount", "max_amount_operator"})
        if "account_id" in transaction:
            filters.add("account_id")
        if "description" in transaction:
            filters.add("description_contains")
        if "transaction_reference_id" in transaction:
            filters.add("reference_id")
        if "utr_number" in transaction and discovered.utr_mode == "plaintext":
            filters.add("utr_number")

        list_columns = {
            "transaction_id",
            "account_id",
            "transaction_date",
            "transaction_type",
            "description",
            "transaction_amount",
            "transaction_reference_id",
            "utr_number",
        }
        if list_columns <= transaction:
            intents.add(Intent.TRANSACTION_LIST)
            intents.add(Intent.REFERENCE_LOOKUP)

        if "account_id" in transaction and "account" in tables and {"account_id", "bank_code"} <= account:
            filters.add("bank_code")
            if "bank" in tables and {"bank_code", "bank_name"} <= bank:
                filters.add("bank_name")

    if "account" in tables and {"available_balance", "bank_code"} <= account:
        intents.add(Intent.ACCOUNT_BALANCE)
        filters.add("bank_code")

    # The frozen compiler does not execute grouped plans.
    return ActiveQueryCapabilities(
        intents=frozenset(intents),
        filters=frozenset(filters),
        group_by=frozenset(),
    )


def build_query_json_schema(active: ActiveQueryCapabilities) -> dict[str, Any]:
    """Build the Ollama schema from active, executable capabilities."""
    filter_properties: dict[str, Any] = {
        "bank_code": {"type": ["string", "null"]},
        "bank_name": {"type": ["string", "null"]},
        "account_id": {"type": ["string", "null"]},
        "transaction_type": {"type": ["string", "null"], "enum": ["credit", "debit", None]},
        "description_contains": {"type": ["string", "null"]},
        "reference_id": {"type": ["string", "null"]},
        "utr_number": {"type": ["string", "null"]},
        "min_amount": {"type": ["number", "string", "null"]},
        "min_amount_operator": {"type": "string", "enum": [">", ">="]},
        "max_amount": {"type": ["number", "string", "null"]},
        "max_amount_operator": {"type": "string", "enum": ["<", "<="]},
    }
    filter_properties = {key: value for key, value in filter_properties.items() if key in active.filters}
    group_values = sorted(item.value for item in active.group_by)
    properties = {
        "intent": {"type": "string", "enum": sorted(item.value for item in active.intents)},
        "metric": {"type": "string", "enum": ["transaction_amount", "transaction_count", "balance"]},
        "aggregation": {"type": "string", "enum": ["sum", "count", "avg", "max", "min", "none"]},
        "filters": {"type": "object", "properties": filter_properties, "additionalProperties": False},
        "date_range_type": {
            "type": "string",
            "enum": [
                "calendar_month",
                "last_n_months",
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
            "items": {"type": "string", "enum": group_values},
            "maxItems": len(group_values),
            "uniqueItems": True,
        },
        "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000},
        "comparison": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {"against": {"const": "previous_month"}},
                    "required": ["against"],
                    "additionalProperties": False,
                },
            ]
        },
        "refusal": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "enum": [
                                "unsupported_metric",
                                "unsupported_field",
                                "ambiguous",
                                "invalid_structure",
                                "capability",
                            ],
                        },
                        "message": {"type": "string"},
                    },
                    "required": ["reason", "message"],
                    "additionalProperties": False,
                },
            ]
        },
    }
    query_required = [
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
    ]
    query_properties = dict(properties)
    query_properties["refusal"] = {"type": "null"}
    refusal_property = properties["refusal"]["anyOf"][1]
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": query_properties,
                "required": query_required,
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"refusal": refusal_property},
                "required": ["refusal"],
                "additionalProperties": False,
            },
        ]
    }


_LABELED_IDENTIFIER = re.compile(
    r"(?i)\b(account(?:\s*(?:number|no\.?|id))?|a/c|acct|utr(?:\s*(?:number|no\.?))?|"
    r"reference(?:\s*(?:id|number|no\.?))?|ref(?:\s*(?:id|no\.?))?|transaction\s*id)"
    r"(\s*[:#=-]?\s*)([A-Z0-9][A-Z0-9+/_=-]{3,})"
)
_SENSITIVE_PATTERNS = [
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"),
    re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}(?:[ -]?\d{4})?\b"),
    re.compile(r"(?<![\w\[])[A-Z0-9]{10,}(?![\w\]])"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
]


def redact_sensitive_identifiers(text: str) -> tuple[str, dict[str, SensitiveReplacement]]:
    """Replace sensitive identifiers with typed placeholders before HTTP serialization."""
    replacements: dict[str, SensitiveReplacement] = {}
    counts: dict[str, int] = {}

    def replace_value(value: str, kind: str, filter_field: str | None = None) -> str:
        for placeholder, replacement in replacements.items():
            if replacement.value == value and replacement.filter_field == filter_field:
                return placeholder
        counts[kind] = counts.get(kind, 0) + 1
        placeholder = f"[{kind}_{counts[kind]}]"
        replacements[placeholder] = SensitiveReplacement(value=value, filter_field=filter_field)
        return placeholder

    def labeled(match: re.Match[str]) -> str:
        label, separator, value = match.groups()
        lowered = label.lower()
        if lowered.startswith("utr"):
            kind = "UTR"
            filter_field = "utr_number"
        elif "account" in lowered or lowered in {"a/c", "acct"}:
            kind = "ACCOUNT_ID"
            filter_field = "account_id"
        elif lowered.startswith("transaction"):
            kind = "SENSITIVE_ID"
            filter_field = None
        else:
            kind = "REFERENCE_ID"
            filter_field = "reference_id"
        return f"{label}{separator}{replace_value(value, kind, filter_field)}"

    redacted = _LABELED_IDENTIFIER.sub(labeled, text)
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(lambda match: replace_value(match.group(0), "SENSITIVE_ID"), redacted)
    return redacted, replacements


def semantic_context(context: ConversationContext | None) -> dict[str, Any] | None:
    """Return compact semantics only—never prior values, evidence, amounts, or identifiers."""
    if context is None:
        return None
    safe_filters = {
        key: value
        for key, value in {
            "bank_code": context.filters.bank_code,
            "bank_name": context.filters.bank_name,
            "transaction_type": context.filters.transaction_type,
        }.items()
        if value is not None
    }
    return {
        "intent": context.intent.value,
        "metric": context.metric.value,
        "aggregation": context.aggregation.value,
        "filters": safe_filters,
        "date_range": {
            "start": context.date_range.start.isoformat(),
            "end_exclusive": context.date_range.end.isoformat(),
            "label": context.date_range.label,
        },
        "group_by": [item.value for item in context.group_by],
        "comparison": context.comparison.model_dump(mode="json") if context.comparison else None,
    }


_PLACEHOLDER = re.compile(r"\[?((?:ACCOUNT_ID|REFERENCE_ID|UTR|SENSITIVE_ID)_\d+)\]?")


def _placeholder_key(value: str) -> str | None:
    match = _PLACEHOLDER.fullmatch(value)
    return f"[{match.group(1)}]" if match else None


def _contains_placeholder(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_placeholder(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_placeholder(item) for item in value)
    return isinstance(value, str) and _PLACEHOLDER.search(value) is not None


def restore_sensitive_placeholders(
    parsed: Any,
    replacements: dict[str, SensitiveReplacement],
) -> tuple[Any | None, QueryRefusal | None]:
    """Rehydrate only an exact, typed placeholder in its designated filter field."""
    restored = copy.deepcopy(parsed)
    if not isinstance(restored, dict):
        return restored, None
    filters = restored.get("filters")
    if filters is not None and not isinstance(filters, dict):
        return restored, None
    if isinstance(filters, dict):
        for field, value in filters.items():
            if not isinstance(value, str):
                continue
            placeholder = _placeholder_key(value)
            if placeholder is None:
                continue
            replacement = replacements.get(placeholder)
            if replacement is None or replacement.filter_field != field:
                return None, refusal(
                    QueryRefusalReason.INVALID_STRUCTURE,
                    f"Sensitive placeholder is invalid for filter field: {field}.",
                )
            filters[field] = replacement.value
    if _contains_placeholder(restored):
        return None, refusal(
            QueryRefusalReason.INVALID_STRUCTURE,
            "Sensitive placeholder appeared outside its designated filter field.",
        )
    return restored, None


async def understand_with_ollama(
    question: str,
    reference_date: date | None = None,
    context: ConversationContext | None = None,
    discovered_capabilities: Capabilities | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 30.0,
) -> OllamaResult:
    """Call Ollama and always return structured output plus inference metadata."""
    if reference_date is None:
        reference_date = today_ist()
    if base_url is None or model is None:
        from app.config import get_settings

        settings = get_settings()
        base_url = base_url or settings.ARTHA_OLLAMA_BASE_URL
        model = model or settings.ARTHA_OLLAMA_MODEL

    assert base_url is not None and model is not None
    active = (
        capabilities_from_discovery(discovered_capabilities) if discovered_capabilities else DEFAULT_ACTIVE_CAPABILITIES
    )
    redacted_question, replacements = redact_sensitive_identifiers(question)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    safe_context = semantic_context(context)
    if safe_context is not None:
        messages.append(
            {
                "role": "system",
                "content": "Previous semantic context: " + json.dumps(safe_context, separators=(",", ":")),
            }
        )
    messages.append({"role": "user", "content": redacted_question})
    payload = {
        "model": model,
        "messages": messages,
        "format": build_query_json_schema(active),
        "options": {"temperature": 0, "num_predict": 512},
        "think": False,
        "stream": False,
    }
    started = time.perf_counter()

    def failure(reason: QueryRefusalReason, message: str) -> OllamaResult:
        return OllamaResult(
            output=refusal(reason, message),
            metadata=OllamaMetadata(model=model, latency_ms=(time.perf_counter() - started) * 1000),
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url.rstrip('/')}/api/chat", json=payload)
            response.raise_for_status()
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError, TypeError):
                return failure(QueryRefusalReason.INVALID_STRUCTURE, "Ollama returned a malformed response envelope.")

            metadata = _extract_metadata(data, model, (time.perf_counter() - started) * 1000)
            content = data.get("message", {}).get("content")
            if not isinstance(content, str):
                return OllamaResult(
                    refusal(QueryRefusalReason.INVALID_STRUCTURE, "Ollama response did not contain JSON text."),
                    metadata,
                )
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return OllamaResult(
                    refusal(QueryRefusalReason.INVALID_STRUCTURE, "Ollama returned malformed model JSON."),
                    metadata,
                )
            parsed, placeholder_error = restore_sensitive_placeholders(parsed, replacements)
            if placeholder_error is not None:
                return OllamaResult(output=placeholder_error, metadata=metadata)
            output = normalize_llm_output(parsed, reference_date, active)
            return OllamaResult(output=output, metadata=metadata)
    except httpx.TimeoutException:
        logger.warning("Ollama request timed out after %ss", timeout)
        return failure(QueryRefusalReason.UPSTREAM_TIMEOUT, "Ollama timed out before interpreting the question.")
    except (httpx.ConnectError, httpx.NetworkError, httpx.HTTPStatusError) as exc:
        logger.error("Ollama is unavailable at %s: %s", base_url, exc)
        return failure(QueryRefusalReason.UPSTREAM_UNAVAILABLE, "Ollama is currently unavailable.")
    except httpx.HTTPError as exc:
        logger.error("Ollama request failed: %s", exc)
        return failure(QueryRefusalReason.UPSTREAM_UNAVAILABLE, "Ollama request failed.")


def _extract_metadata(response_data: dict[str, Any], requested_model: str, latency_ms: float) -> OllamaMetadata:
    prompt_tokens = response_data.get("prompt_eval_count")
    completion_tokens = response_data.get("eval_count")
    prompt_tokens = prompt_tokens if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) else None
    completion_tokens = (
        completion_tokens if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool) else None
    )
    total_tokens = (
        prompt_tokens + completion_tokens if prompt_tokens is not None and completion_tokens is not None else None
    )
    response_model = response_data.get("model")
    return OllamaMetadata(
        model=response_model if isinstance(response_model, str) and response_model else requested_model,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
