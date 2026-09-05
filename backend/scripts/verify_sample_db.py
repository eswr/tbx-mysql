#!/usr/bin/env python3
"""
Standalone smoke test for artha_sample database.
Verifies row counts, foreign keys, and computed totals.
NOT part of the frozen evaluation corpus.

Usage:
    uv run python backend/scripts/verify_sample_db.py
"""

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymysql
from pymysql.cursors import DictCursor

# Add parent to path so we can import from backend
sys.path.insert(0, str(Path(__file__).parent.parent))


def connect_to_mysql(db_url: str = "mysql://artha:artha@127.0.0.1:3306/artha_sample"):
    """Connect to MySQL database."""
    parsed = urlparse(db_url if db_url.startswith("mysql://") else f"mysql://{db_url}")

    try:
        return pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "artha",
            password=parsed.password or "artha",
            database=parsed.path.lstrip("/") if parsed.path else "artha_sample",
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
    except Exception as e:
        print(f"ERROR: Failed to connect to MySQL: {e}")
        return None


def query_scalar(cursor: DictCursor, sql: str) -> Any:
    """Execute a query and return the first scalar result."""
    cursor.execute(sql)
    result = cursor.fetchone()
    if result:
        return list(result.values())[0]
    return None


def assert_equals(actual, expected, description: str) -> bool:
    """Assert equality with decimal tolerance for floating point."""
    if isinstance(actual, (float, Decimal)) and isinstance(expected, (float, Decimal)):
        # For decimals, use small tolerance for floating point errors
        diff = abs(float(actual) - float(expected))
        if diff < 0.01:
            print(f"  ✓ {description}: {actual}")
            return True
        else:
            print(f"  ✗ {description}: expected {expected}, got {actual} (diff: {diff})")
            return False
    else:
        if actual == expected:
            print(f"  ✓ {description}: {actual}")
            return True
        else:
            print(f"  ✗ {description}: expected {expected}, got {actual}")
            return False


def main():
    """Run verification tests."""
    print("=" * 50)
    print("Verifying artha_sample database")
    print("=" * 50)

    conn = connect_to_mysql()
    if not conn:
        return 1

    cursor = conn.cursor()
    passed = 0
    failed = 0

    try:
        # Row count assertions
        print("\nRow count assertions:")

        # Test 1: Bank count
        bank_count = query_scalar(cursor, "SELECT COUNT(*) FROM bank;")
        if assert_equals(bank_count, 10, "Bank count"):
            passed += 1
        else:
            failed += 1

        # Test 2: Account count
        account_count = query_scalar(cursor, "SELECT COUNT(*) FROM account;")
        if assert_equals(account_count, 10, "Account count"):
            passed += 1
        else:
            failed += 1

        # Test 3: Transaction count
        transaction_count = query_scalar(cursor, "SELECT COUNT(*) FROM `transaction`;")
        if assert_equals(transaction_count, 10, "Transaction count"):
            passed += 1
        else:
            failed += 1

        print("\nSmoke test assertions (computed totals):")

        # Test 4: Total debit amount
        total_debit = query_scalar(
            cursor, "SELECT SUM(transaction_amount) FROM `transaction` WHERE transaction_type='debit';"
        )
        if assert_equals(total_debit, Decimal("249806.00"), "Total debit amount"):
            passed += 1
        else:
            failed += 1

        # Test 5: Total credit amount
        total_credit = query_scalar(
            cursor, "SELECT SUM(transaction_amount) FROM `transaction` WHERE transaction_type='credit';"
        )
        if assert_equals(total_credit, Decimal("296810.00"), "Total credit amount"):
            passed += 1
        else:
            failed += 1

        # Test 6: Net cash movement
        net_movement = query_scalar(
            cursor,
            "SELECT (SUM(CASE WHEN transaction_type='credit' THEN transaction_amount ELSE 0 END) - "
            "SUM(CASE WHEN transaction_type='debit' THEN transaction_amount ELSE 0 END)) FROM `transaction`;",
        )
        if assert_equals(net_movement, Decimal("47004.00"), "Net cash movement (credit - debit)"):
            passed += 1
        else:
            failed += 1

        # Test 7: Total available balance
        total_balance = query_scalar(cursor, "SELECT SUM(available_balance) FROM account;")
        if assert_equals(total_balance, Decimal("-81229672.84"), "Total available balance"):
            passed += 1
        else:
            failed += 1

        # Test 8: HDFC available balance
        hdfc_balance = query_scalar(cursor, "SELECT SUM(available_balance) FROM account WHERE bank_code='HDFC';")
        if assert_equals(hdfc_balance, Decimal("-252302939.33"), "HDFC available balance"):
            passed += 1
        else:
            failed += 1

        # Test 9: HDFC transaction count
        hdfc_txn_count = query_scalar(
            cursor,
            "SELECT COUNT(*) FROM `transaction` t "
            "JOIN account a ON t.account_id = a.account_id "
            "WHERE a.bank_code='HDFC';",
        )
        if assert_equals(hdfc_txn_count, 7, "HDFC transaction count"):
            passed += 1
        else:
            failed += 1

        # Test 10: HDFC debit total
        hdfc_debit = query_scalar(
            cursor,
            "SELECT SUM(transaction_amount) FROM `transaction` t "
            "JOIN account a ON t.account_id = a.account_id "
            "WHERE a.bank_code='HDFC' AND t.transaction_type='debit';",
        )
        if assert_equals(hdfc_debit, Decimal("240455.00"), "HDFC debit amount"):
            passed += 1
        else:
            failed += 1

        # Test 11: HDFC credit total
        hdfc_credit = query_scalar(
            cursor,
            "SELECT SUM(transaction_amount) FROM `transaction` t "
            "JOIN account a ON t.account_id = a.account_id "
            "WHERE a.bank_code='HDFC' AND t.transaction_type='credit';",
        )
        if assert_equals(hdfc_credit, Decimal("260000.00"), "HDFC credit amount"):
            passed += 1
        else:
            failed += 1

        # Test 12: 2026-06-24 debit total
        date_debit = query_scalar(
            cursor,
            "SELECT SUM(transaction_amount) FROM `transaction` "
            "WHERE DATE(transaction_date)='2026-06-24' AND transaction_type='debit';",
        )
        if assert_equals(date_debit, Decimal("169299.00"), "2026-06-24 debit total"):
            passed += 1
        else:
            failed += 1

        # Test 13: Reference S5314253 matches
        ref_count = query_scalar(
            cursor, "SELECT COUNT(*) FROM `transaction` WHERE transaction_reference_id='S5314253';"
        )
        if assert_equals(ref_count, 1, "Reference S5314253 matches"):
            passed += 1
        else:
            failed += 1

        print("\nForeign key assertions:")

        # Test 14: All accounts have valid bank_codes
        orphan_accounts = query_scalar(
            cursor,
            "SELECT COUNT(*) FROM account a WHERE NOT EXISTS (SELECT 1 FROM bank b WHERE b.bank_code = a.bank_code);",
        )
        if assert_equals(orphan_accounts, 0, "All accounts have valid bank_codes"):
            passed += 1
        else:
            failed += 1

        # Test 15: All transactions have valid account_ids
        orphan_transactions = query_scalar(
            cursor,
            "SELECT COUNT(*) FROM `transaction` t "
            "WHERE NOT EXISTS (SELECT 1 FROM account a WHERE a.account_id = t.account_id);",
        )
        if assert_equals(orphan_transactions, 0, "All transactions have valid account_ids"):
            passed += 1
        else:
            failed += 1

        print("\n" + "=" * 50)
        print(f"Verification Summary: {passed} passed, {failed} failed")
        print("=" * 50)

        if failed > 0:
            print(f"ERROR: {failed} assertions failed")
            return 1

        print("\n✓ artha_sample database verification completed successfully!")
        return 0

    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
