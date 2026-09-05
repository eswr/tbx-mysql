"""Shared pytest fixtures."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def fixture_dir():
    """Generate and return fixture CSV directory."""
    fixture_path = Path("/tmp/artha_parity_fixture")

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from fixtures.generate_fixture import generate_fixture, write_csvs

    data = generate_fixture(seed=42, n_accounts=25, n_transactions_per_account=320)
    write_csvs(data, str(fixture_path))

    return str(fixture_path)


@pytest.fixture(scope="session")
def duckdb_path():
    """Create a DuckDB database with fixture data."""
    from scripts.load_fixture import load_duckdb

    db_path = "/tmp/artha_parity.duckdb"

    # Load fixture
    fixture_dir_val = Path("/tmp/artha_parity_fixture")
    from fixtures.generate_fixture import generate_fixture, write_csvs

    data = generate_fixture(seed=42, n_accounts=25, n_transactions_per_account=320)
    write_csvs(data, str(fixture_dir_val))

    assert load_duckdb(db_path, str(fixture_dir_val))

    return db_path


@pytest.fixture(scope="session")
def mysql_available():
    """Check if MySQL is available via docker."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=artha-mysql"],
            capture_output=True,
            timeout=5,
        )
        return "artha-mysql" in result.stdout.decode()
    except:
        return False


@pytest.fixture(scope="session")
def mysql_url():
    """MySQL connection URL (if available)."""
    return "mysql://artha:artha@127.0.0.1:3306/artha"
