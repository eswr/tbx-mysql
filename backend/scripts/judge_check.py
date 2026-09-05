#!/usr/bin/env python3
"""Verify a running judge-backed Artha API without writing to its database."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import get_settings


SMOKE_QUESTION = "How many accounts per bank?"


class JudgeCheckError(RuntimeError):
    """A judge preflight check failed."""


def _request_json(base_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = Request(f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 -- URL is the operator-selected local API
            if response.status < 200 or response.status >= 300:
                raise JudgeCheckError(f"{path} returned HTTP {response.status}")
            decoded = json.load(response)
    except HTTPError as exc:
        raise JudgeCheckError(f"{path} returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise JudgeCheckError(f"could not reach {path}: {exc.reason if isinstance(exc, URLError) else exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JudgeCheckError(f"{path} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise JudgeCheckError(f"{path} returned an unexpected JSON value")
    return decoded


def _display(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def run_check(base_url: str) -> list[str]:
    """Run health, capability, and smoke checks; return printable summary lines."""
    health = _request_json(base_url, "/api/health")
    status = health.get("status")
    if status != "healthy":
        raise JudgeCheckError(f"backend health is {_display(status)} (database={_display(health.get('database'))})")

    capabilities = _request_json(base_url, "/api/capabilities")
    debit_sign = capabilities.get("debit_sign")
    debit_confidence = capabilities.get("debit_sign_confidence")
    utr_mode = capabilities.get("utr_mode")
    if debit_sign not in {"positive", "negative"}:
        raise JudgeCheckError("capabilities did not provide a safe debit-sign convention")
    if utr_mode not in {"plaintext", "opaque"}:
        raise JudgeCheckError("capabilities did not provide a safe UTR mode")

    expected_debit_sign = get_settings().ARTHA_DEBIT_SIGN.value
    if expected_debit_sign != debit_sign:
        raise JudgeCheckError(
            f"configured debit sign ({expected_debit_sign}) conflicts with detected judge data ({debit_sign})"
        )

    smoke = _request_json(base_url, "/api/chat", {"question": SMOKE_QUESTION})
    if smoke.get("refusal") is not None or not (smoke.get("evidence") or {}).get("grounded"):
        raise JudgeCheckError("read-only smoke query was not successfully grounded")

    banks = capabilities.get("banks") or []
    bank_labels = [f"{bank.get('code')} ({bank.get('name')})" for bank in banks if isinstance(bank, dict)]
    warnings = capabilities.get("warnings") or []
    lines = [
        "Judge database preflight: PASS",
        f"Backend / engine: {_display(health.get('backend'))} / {_display((smoke.get('meta') or {}).get('engine'))}",
        f"Tables: {', '.join(map(str, capabilities.get('tables') or [])) or '—'}",
        f"Rows: {_display(capabilities.get('transaction_count'))} transactions; {_display(capabilities.get('account_count'))} accounts",
        f"Banks: {', '.join(bank_labels) or '—'}",
        f"Date range: {_display(capabilities.get('date_range_start'))} to {_display(capabilities.get('date_range_end'))}",
        f"Debit sign: {debit_sign} ({_display(debit_confidence)})",
        f"UTR mode: {utr_mode}",
        f"Warnings: {'; '.join(map(str, warnings)) if warnings else 'none'}",
        f'Smoke: PASS — "{SMOKE_QUESTION}" — {_display(smoke.get("answer"))}',
    ]
    return lines


def main() -> int:
    if not os.environ.get("ARTHA_DATABASE_URL", "").strip():
        print("ERROR: ARTHA_DATABASE_URL is required; the value will not be printed", file=sys.stderr)
        return 2
    base_url = os.environ.get("ARTHA_API_BASE_URL", "http://127.0.0.1:8000")
    try:
        lines = run_check(base_url)
    except JudgeCheckError as exc:
        print(f"Judge database preflight: FAIL — {exc}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
