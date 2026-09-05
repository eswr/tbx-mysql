"""Safety and API checks for the live-demo judge database workflow."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import judge_check
from scripts.db_safety import is_local_database_url, redact_url


ROOT = Path(__file__).parents[2]
LOCAL_DB_URL = "mysql://artha:artha@127.0.0.1:3306/artha"
JUDGE_DB_URL = "mysql://judge-user:super-secret@judge.example.com:3306/finance"


def _make(*targets: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = ["make", "-s", *targets, f"PY={sys.executable}"]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)


def _healthy_responses(base_url: str, path: str, payload=None):
    if path == "/api/health":
        return {"status": "healthy", "database": "ok", "backend": "mysql"}
    if path == "/api/capabilities":
        return {
            "tables": ["bank", "account", "transaction"],
            "banks": [{"code": "HDFC", "name": "HDFC Bank"}],
            "transaction_count": 8000,
            "account_count": 25,
            "date_range_start": "2025-09-05T00:00:00",
            "date_range_end": "2026-09-05T00:00:00",
            "debit_sign": "positive",
            "debit_sign_confidence": "detected",
            "utr_mode": "opaque",
            "warnings": ["UTR lookup disabled"],
        }
    assert path == "/api/chat"
    assert payload == {"question": judge_check.SMOKE_QUESTION}
    return {
        "answer": "HDFC Bank: 25 accounts.",
        "refusal": None,
        "evidence": {"grounded": True},
        "meta": {"engine": "mysql"},
    }


def test_judge_target_requires_database_url():
    env = os.environ.copy()
    env.pop("ARTHA_DATABASE_URL", None)

    result = _make("judge-check", env=env)

    assert result.returncode != 0
    assert "ARTHA_DATABASE_URL is required" in result.stderr


def test_database_url_redaction_removes_credentials():
    redacted = redact_url(f"{JUDGE_DB_URL}?password=another-secret#token")

    assert redacted == "mysql://judge.example.com:3306/finance"
    assert "judge-user" not in redacted
    assert "super-secret" not in redacted
    assert "another-secret" not in redacted
    assert not is_local_database_url(JUDGE_DB_URL)
    assert is_local_database_url(LOCAL_DB_URL)
    assert is_local_database_url("mysql://user:pass@[::1]:3306/artha")


def test_judge_check_healthy_prints_demo_summary(monkeypatch):
    monkeypatch.setattr(judge_check, "_request_json", _healthy_responses)

    lines = judge_check.run_check("http://127.0.0.1:8000")

    summary = "\n".join(lines)
    assert "Judge database preflight: PASS" in summary
    assert "mysql / mysql" in summary
    assert "8000 transactions; 25 accounts" in summary
    assert "Debit sign: positive (detected)" in summary
    assert "UTR mode: opaque" in summary
    assert "Smoke: PASS" in summary


def test_judge_check_degraded_fails(monkeypatch):
    monkeypatch.setattr(
        judge_check,
        "_request_json",
        lambda base_url, path, payload=None: {"status": "degraded", "database": "error", "backend": "mysql"},
    )

    with pytest.raises(judge_check.JudgeCheckError, match="health is degraded"):
        judge_check.run_check("http://127.0.0.1:8000")


def test_judge_check_unreachable_fails(monkeypatch):
    def unreachable(base_url, path, payload=None):
        raise judge_check.JudgeCheckError("could not reach /api/health")

    monkeypatch.setattr(judge_check, "_request_json", unreachable)

    with pytest.raises(judge_check.JudgeCheckError, match="could not reach"):
        judge_check.run_check("http://127.0.0.1:8000")


@pytest.mark.parametrize("target", ["seed", "db-reset"])
def test_destructive_targets_refuse_when_judge_url_is_non_local(target):
    env = {**os.environ, "ARTHA_DATABASE_URL": JUDGE_DB_URL}

    result = _make(target, f"DB_URL={LOCAL_DB_URL}", env=env)

    assert result.returncode != 0
    assert "refusing destructive database action" in result.stderr
    assert "super-secret" not in result.stdout + result.stderr


def test_make_all_pins_database_to_local_db_url():
    env = {**os.environ, "ARTHA_DATABASE_URL": JUDGE_DB_URL}
    result = subprocess.run(
        ["make", "--dry-run", "all", f"DB_URL={LOCAL_DB_URL}", f"PY={sys.executable}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0
    assert JUDGE_DB_URL not in output
    assert "super-secret" not in output
    assert output.count(f'--mysql-url "{LOCAL_DB_URL}"') == 2
    assert "backend/scripts/load_fixture.py" not in output
