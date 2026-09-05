"""
Generate deterministic, schema-faithful fixtures for Artha.

Key properties:
  - Positive debit amounts (per TBX schema doc)
  - TIMESTAMP(6) with real times
  - All-caps bank names
  - Ciphertext-style UTRs (some NULL)
  - Planted boundaries: exact 50,000.00 amounts, ref collisions, empty months, negative balances
"""

import csv
import random
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid5, UUID
import base64

NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

CANONICAL_BANKS = [
    ("HDFC", "HDFC BANK LIMITED"),
    ("ICIC", "ICICI BANK LIMITED"),
    ("SBIN", "STATE BANK OF INDIA"),
    ("UTIB", "AXIS BANK LIMITED"),
    ("KKBK", "KOTAK MAHINDRA BANK LIMITED"),
    ("CNRB", "CANARA BANK"),
    ("UBIN", "UNION BANK OF INDIA"),
    ("AUBL", "AU SMALL FINANCE BANK LIMITED"),
    ("TMBL", "TAMILNAD MERCANTILE BANK LIMITED"),
    ("RATN", "RBL BANK LIMITED"),
]

MERCHANTS = [
    "SELECTION ELECTRONICS",
    "RELIANCEDIGITAL RETAIL LTD",
    "BAJAJ FINANCE LTD",
    "SELECTRICITY TWO PRIVATE LIMITED",
    "PARESH VIKRANT GHASE",
    "GAUTAM SINGH",
    "HDFC ERGO",
    "AXIS BANK",
    "ICICI BANK",
]

def generate_utr(seed_val: int) -> str | None:
    """Generate a ciphertext-style UTR (base64-like) or None with 55% density."""
    if seed_val % 100 < 45:  # 45% are NULL
        return None
    rng = random.Random(seed_val)
    size = rng.choice([56, 64])
    # Base64-ish: mostly alphanumerics + / + + and =
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    return "".join(rng.choice(chars) for _ in range(size))


def generate_reference_id(seed_val: int) -> str:
    """Generate reference ID: 10-digit numeric, S-prefixed, or bank-prefixed."""
    rng = random.Random(seed_val)
    choice = rng.randint(0, 2)
    if choice == 0:  # Numeric
        return str(rng.randint(1000000000, 9999999999))
    elif choice == 1:  # S-prefixed
        return f"S{rng.randint(1000000, 9999999)}"
    else:  # Bank-prefixed
        bank_code = rng.choice([b[0] for b in CANONICAL_BANKS])
        return f"{bank_code}H{rng.randint(10000000000, 99999999999)}"


def generate_fixture(
    n_accounts: int = 25,
    n_transactions_per_account: int = 320,
    seed: int = 42,
    scale: int = 1,
    window_months: int = 12,
):
    """
    Generate deterministic fixture.
    
    Args:
        n_accounts: Number of accounts to generate
        n_transactions_per_account: Transactions per account (scaled)
        seed: Random seed for determinism
        scale: Multiplier for row counts (for index performance testing)
        window_months: How many months back from "today" (2026-09-05)
    """
    random.seed(seed)
    
    # Use 2026-09-05 as "today" (per extended dataset standard)
    today = datetime(2026, 9, 5)
    window_start = today - timedelta(days=window_months * 30)
    
    banks = []
    accounts = []
    transactions = []
    
    # Generate banks (canonical, deterministic)
    for code, name in CANONICAL_BANKS:
        banks.append({"bank_code": code, "bank_name": name})
    
    # Generate accounts
    rng = random.Random(seed)
    for i in range(n_accounts * scale):
        entity_id = str(uuid5(NAMESPACE, f"entity-{i}"))
        account_id = str(uuid5(NAMESPACE, f"account-{i}"))
        
        bank_code, _ = rng.choice(CANONICAL_BANKS)
        account_number = f"{rng.randint(10000000000, 99999999999)}"  # 11-14 digits
        program_id = rng.choice([4, 21, 46])
        
        # Mix of positive and negative balances (per TBX samples)
        if rng.random() < 0.3:
            available_balance = Decimal(str(rng.randint(-200000000, -1000000) / 100))
        else:
            available_balance = Decimal(str(rng.randint(100000, 300000000) / 100))
        
        accounts.append({
            "account_id": account_id,
            "entity_id": entity_id,
            "account_number": account_number,
            "program_id": program_id,
            "available_balance": available_balance,
            "bank_code": bank_code,
        })
    
    # Generate transactions with planted boundaries
    txn_id = 0
    for account_idx, account in enumerate(accounts):
        account_id = account["account_id"]
        
        for j in range(n_transactions_per_account):
            txn_id += 1
            
            # Date: uniform random in window, with some clustering on early-month
            days_back = rng.randint(0, window_months * 30)
            txn_date = today - timedelta(days=days_back)
            txn_time = txn_date.replace(
                hour=rng.randint(0, 23),
                minute=rng.randint(0, 59),
                second=rng.randint(0, 59),
                microsecond=rng.randint(0, 999999)
            )
            
            # Type
            txn_type = "credit" if rng.random() < 0.35 else "debit"
            
            # Amount: positive per schema, with some planted boundaries
            if j == n_transactions_per_account - 1 and account_idx == 0:
                # Boundary case: exact 50,000 in first account
                amount = Decimal("50000.00")
            elif j == n_transactions_per_account - 2 and account_idx == 0:
                # Boundary case: 50,000.01 in first account
                amount = Decimal("50000.01")
            elif j == n_transactions_per_account - 5 and account_idx == 0:
                # Boundary case: 49,999.99 in first account
                amount = Decimal("49999.99")
            elif rng.random() < 0.05:
                # Small amounts
                amount = Decimal(str(rng.randint(100, 10000) / 100))
            elif rng.random() < 0.1:
                # Large amounts
                amount = Decimal(str(rng.randint(100000, 500000) / 100))
            else:
                # Normal amounts
                amount = Decimal(str(rng.randint(1000, 100000) / 100))
            
            # Description
            merchant = rng.choice(MERCHANTS)
            prefix = rng.choice(["NEFT", "UPI", "IMPS", "FT", "CHQ"])
            description = f"{prefix} - {rng.randint(1000000, 9999999)} - {merchant}"
            
            # Reference ID and UTR
            # Intentional collision: duplicate ref on some txns
            if j == n_transactions_per_account - 3 and account_idx == 0:
                reference_id = "COLLISION-REF-001"
            elif j == n_transactions_per_account - 4 and account_idx == 1:
                reference_id = "COLLISION-REF-001"  # Same ref, different account
            else:
                reference_id = generate_reference_id(txn_id + seed)
            
            utr = generate_utr(txn_id + seed)
            
            # Intentionally skip a month (September 2026) for some accounts
            if account_idx % 5 == 0 and txn_date.month == 9 and txn_date.year == 2026:
                continue
            
            transactions.append({
                "transaction_id": str(uuid5(NAMESPACE, f"txn-{txn_id}")),
                "account_id": account_id,
                "transaction_date": txn_time.isoformat(timespec='microseconds'),
                "transaction_type": txn_type,
                "description": description,
                "transaction_amount": amount,
                "transaction_reference_id": reference_id,
                "utr_number": utr,
            })
    
    return {
        "banks": banks,
        "accounts": accounts,
        "transactions": transactions,
    }


def write_csvs(data: dict, output_dir: str):
    """Write fixture data to CSV files."""
    import os
    os.makedirs(output_dir, exist_ok=True)
    
    # Banks
    with open(os.path.join(output_dir, "bank.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["bank_code", "bank_name"])
        writer.writeheader()
        writer.writerows(data["banks"])
    
    # Accounts
    with open(os.path.join(output_dir, "account.csv"), "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["account_id", "entity_id", "account_number", "program_id", "available_balance", "bank_code"]
        )
        writer.writeheader()
        writer.writerows(data["accounts"])
    
    # Transactions
    with open(os.path.join(output_dir, "transaction.csv"), "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "transaction_id",
                "account_id",
                "transaction_date",
                "transaction_type",
                "description",
                "transaction_amount",
                "transaction_reference_id",
                "utr_number",
            ]
        )
        writer.writeheader()
        writer.writerows(data["transactions"])


if __name__ == "__main__":
    import sys
    
    scale = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    
    print(f"Generating fixture (scale={scale}) to {output_dir}")
    data = generate_fixture(
        n_accounts=25,
        n_transactions_per_account=320,
        scale=scale,
        seed=42,
    )
    write_csvs(data, output_dir)
    
    print(f"  Banks: {len(data['banks'])}")
    print(f"  Accounts: {len(data['accounts'])}")
    print(f"  Transactions: {len(data['transactions'])}")
    print("Done.")
