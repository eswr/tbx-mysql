#!/usr/bin/env python3
"""Database URL safety helpers for commands that can modify data."""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from urllib.parse import SplitResult, urlsplit, urlunsplit


LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


def redact_url(value: str) -> str:
    """Return a URL safe for logs, with all user information removed."""
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return "<redacted-database-url>"
        hostname = parsed.hostname or "unknown-host"
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(SplitResult(parsed.scheme, f"{hostname}{port}", parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted-database-url>"


def is_local_database_url(value: str) -> bool:
    """Accept only loopback database hosts."""
    try:
        hostname = urlsplit(value).hostname
    except (TypeError, ValueError):
        return False
    if not hostname:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized in LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_local_for_destructive_action(db_url: str, judge_url: str | None = None) -> None:
    """Reject destructive work if either active URL could identify a remote DB."""
    if not is_local_database_url(db_url):
        raise ValueError(f"refusing destructive database action: DB_URL is not local ({redact_url(db_url)})")
    if judge_url and not is_local_database_url(judge_url):
        raise ValueError(
            "refusing destructive database action while ARTHA_DATABASE_URL points to a non-local host "
            f"({redact_url(judge_url)})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["require-judge-url", "guard-destructive"])
    parser.add_argument("--db-url")
    args = parser.parse_args()

    try:
        if args.command == "require-judge-url":
            if not os.environ.get("ARTHA_DATABASE_URL", "").strip():
                raise ValueError("ARTHA_DATABASE_URL is required; export it before running a judge target")
        else:
            if not args.db_url:
                raise ValueError("DB_URL is required for a destructive database action")
            require_local_for_destructive_action(args.db_url, os.environ.get("ARTHA_DATABASE_URL"))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
