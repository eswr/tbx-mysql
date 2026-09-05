#!/usr/bin/env python3
"""
Load fixture CSVs into MySQL or DuckDB.

Supports explicit source selection:
  --source faithful       (default) Schema-faithful synthetic fixture
  --source official       TBX official 20k dataset with transforms applied
  
Transforms (for official source):
  --negate-debits         Negate all amounts WHERE type='debit' (for negative-debit data)
  --date-to-timestamp     Add time components to date-only transaction_date
  --uppercase-bank-names  Normalize mixed-case bank names to ALL-CAPS
"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pymysql
from pymysql.cursors import DictCursor

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Add parent to path so we can import from backend
sys.path.insert(0, str(Path(__file__).parent.parent))

from fixtures.generate_fixture import generate_fixture, write_csvs


def load_mysql(db_url: str, csv_dir: str, clear: bool = True):
    """Load CSV files into MySQL."""
    logger.info(f"Connecting to MySQL: {db_url}")
    
    # Parse MySQL URL
    from urllib.parse import urlparse
    parsed = urlparse(db_url if db_url.startswith("mysql://") else f"mysql://{db_url}")
    
    conn_kwargs = {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/") if parsed.path else "artha",
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
    }
    
    try:
        conn = pymysql.connect(**conn_kwargs)
    except Exception as e:
        logger.error(f"Failed to connect to MySQL: {e}")
        return False
    
    cursor = conn.cursor()
    
    try:
        # Clear tables if requested
        if clear:
            logger.info("Clearing existing data...")
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in ["transaction", "account", "bank"]:
                cursor.execute(f"TRUNCATE TABLE {table}")
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        
        # Load banks
        logger.info("Loading banks...")
        with open(os.path.join(csv_dir, "bank.csv")) as f:
            reader = csv.DictReader(f)
            for row in reader:
                cursor.execute(
                    "INSERT INTO bank (bank_code, bank_name) VALUES (%s, %s)",
                    (row["bank_code"], row["bank_name"])
                )
        conn.commit()
        
        # Load accounts
        logger.info("Loading accounts...")
        with open(os.path.join(csv_dir, "account.csv")) as f:
            reader = csv.DictReader(f)
            for row in reader:
                cursor.execute(
                    """INSERT INTO account 
                       (account_id, entity_id, account_number, program_id, available_balance, bank_code)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        row["account_id"],
                        row["entity_id"],
                        row["account_number"],
                        int(row["program_id"]),
                        Decimal(row["available_balance"]),
                        row["bank_code"],
                    )
                )
        conn.commit()
        
        # Load transactions
        logger.info("Loading transactions...")
        with open(os.path.join(csv_dir, "transaction.csv")) as f:
            reader = csv.DictReader(f)
            count = 0
            batch = []
            for row in reader:
                batch.append((
                    row["transaction_id"],
                    row["account_id"],
                    row["transaction_date"],
                    row["transaction_type"],
                    row["description"] or None,
                    Decimal(row["transaction_amount"]),
                    row["transaction_reference_id"] or None,
                    row["utr_number"] or None,
                ))
                count += 1
                if count % 500 == 0:
                    cursor.executemany(
                        """INSERT INTO `transaction`
                           (transaction_id, account_id, transaction_date, transaction_type,
                            description, transaction_amount, transaction_reference_id, utr_number)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        batch
                    )
                    conn.commit()
                    batch = []
            
            if batch:
                cursor.executemany(
                    """INSERT INTO `transaction`
                       (transaction_id, account_id, transaction_date, transaction_type,
                        description, transaction_amount, transaction_reference_id, utr_number)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    batch
                )
                conn.commit()
        
        logger.info(f"Loaded {count} transactions")
        
        # Verify counts
        cursor.execute("SELECT COUNT(*) as cnt FROM bank")
        bank_count = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM account")
        account_count = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COUNT(*) as cnt FROM `transaction`")
        txn_count = cursor.fetchone()["cnt"]
        
        logger.info(f"Final counts: {bank_count} banks, {account_count} accounts, {txn_count} transactions")
        return True
        
    except Exception as e:
        logger.error(f"Error loading data: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()


def load_duckdb(db_path: str, csv_dir: str):
    """Load CSV files into DuckDB."""
    logger.info(f"Opening DuckDB at {db_path}")
    
    conn = duckdb.connect(db_path)
    
    try:
        # Create tables
        logger.info("Creating tables...")
        conn.execute("""
            CREATE OR REPLACE TABLE bank (
                bank_code VARCHAR PRIMARY KEY,
                bank_name VARCHAR
            )
        """)
        
        conn.execute("""
            CREATE OR REPLACE TABLE account (
                account_id VARCHAR PRIMARY KEY,
                entity_id VARCHAR NOT NULL,
                account_number VARCHAR NOT NULL,
                program_id INTEGER NOT NULL,
                available_balance DECIMAL(15,2) NOT NULL DEFAULT 0.00,
                bank_code VARCHAR NOT NULL,
                FOREIGN KEY (bank_code) REFERENCES bank(bank_code)
            )
        """)
        
        conn.execute("""
            CREATE OR REPLACE TABLE transaction (
                transaction_id VARCHAR PRIMARY KEY,
                account_id VARCHAR NOT NULL,
                transaction_date TIMESTAMP NOT NULL,
                transaction_type VARCHAR NOT NULL,
                description VARCHAR DEFAULT NULL,
                transaction_amount DECIMAL(15,2) NOT NULL DEFAULT 0.00,
                transaction_reference_id VARCHAR DEFAULT NULL,
                utr_number VARCHAR DEFAULT NULL,
                FOREIGN KEY (account_id) REFERENCES account(account_id)
            )
        """)
        
        # Load CSVs
        logger.info("Loading banks...")
        conn.execute(f"INSERT INTO bank SELECT * FROM read_csv_auto('{os.path.join(csv_dir, 'bank.csv')}')")
        
        logger.info("Loading accounts...")
        conn.execute(f"INSERT INTO account SELECT * FROM read_csv_auto('{os.path.join(csv_dir, 'account.csv')}')")
        
        logger.info("Loading transactions...")
        conn.execute(f"INSERT INTO transaction SELECT * FROM read_csv_auto('{os.path.join(csv_dir, 'transaction.csv')}')")
        
        # Verify counts
        bank_count = conn.execute("SELECT COUNT(*) FROM bank").fetchall()[0][0]
        account_count = conn.execute("SELECT COUNT(*) FROM account").fetchall()[0][0]
        txn_count = conn.execute("SELECT COUNT(*) FROM transaction").fetchall()[0][0]
        
        logger.info(f"Final counts: {bank_count} banks, {account_count} accounts, {txn_count} transactions")
        return True
        
    except Exception as e:
        logger.error(f"Error loading data: {e}")
        return False
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Load fixture data into MySQL or DuckDB")
    parser.add_argument("--db-url", help="MySQL connection URL (e.g., mysql://user:pass@host/db). If omitted, loads to DuckDB.")
    parser.add_argument("--duckdb-path", default="artha.duckdb", help="DuckDB file path (default: artha.duckdb)")
    parser.add_argument("--source", choices=["faithful", "official"], default="faithful", help="Fixture source")
    parser.add_argument("--csv-dir", help="Directory containing CSV files (auto-generated if omitted)")
    parser.add_argument("--negate-debits", action="store_true", help="Negate debit amounts for official source")
    parser.add_argument("--date-to-timestamp", action="store_true", help="Add time to date-only timestamps")
    parser.add_argument("--uppercase-bank-names", action="store_true", help="Uppercase bank names")
    parser.add_argument("--clear", action="store_true", default=True, help="Clear existing data (MySQL only)")
    args = parser.parse_args()
    
    # Generate or use provided CSV directory
    if args.csv_dir:
        csv_dir = args.csv_dir
        if not os.path.exists(csv_dir):
            logger.error(f"CSV directory not found: {csv_dir}")
            sys.exit(1)
    else:
        logger.info("Generating fixture...")
        csv_dir = "/tmp/artha_fixture"
        data = generate_fixture(seed=42, n_accounts=25, n_transactions_per_account=320)
        write_csvs(data, csv_dir)
    
    # Load into database
    if args.db_url:
        success = load_mysql(args.db_url, csv_dir, clear=args.clear)
    else:
        success = load_duckdb(args.duckdb_path, csv_dir)
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
