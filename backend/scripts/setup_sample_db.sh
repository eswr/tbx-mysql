#!/bin/bash
# Setup reproducible artha_sample database for realistic manual integration testing
# This script:
#   1. Drops and recreates artha_sample with proper UTF8MB4 collation
#   2. Applies schema.sql
#   3. Loads sample_data.sql
#   4. Asserts row counts (10 banks, 10 accounts, 10 transactions)
#   5. Runs smoke test assertions on computed totals
#   6. Fails the script if any assertion fails

set -e  # Exit on first error

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

MYSQL_USER="root"
MYSQL_PASSWORD="root"
MYSQL_HOST="127.0.0.1"
MYSQL_PORT="3306"
MYSQL_CONTAINER="artha-mysql"

DB_NAME="artha_sample"
ARTHA_USER="artha"
ARTHA_PASSWORD="artha"

echo "=========================================="
echo "Setting up artha_sample database"
echo "=========================================="

# Step 1: Drop and recreate database with proper collation
echo ""
echo "[1/5] Dropping and recreating ${DB_NAME} database..."
docker exec -i "$MYSQL_CONTAINER" mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -e "
    DROP DATABASE IF EXISTS ${DB_NAME};
    CREATE DATABASE ${DB_NAME}
        CHARACTER SET utf8mb4
        COLLATE utf8mb4_unicode_ci;
" || {
    echo "ERROR: Failed to create database"
    exit 1
}

# Step 2: Grant privileges to artha user
echo "[2/5] Granting privileges to '${ARTHA_USER}' user..."
docker exec -i "$MYSQL_CONTAINER" mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -e "
    GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${ARTHA_USER}'@'%';
    FLUSH PRIVILEGES;
" || {
    echo "ERROR: Failed to grant privileges"
    exit 1
}

# Step 3: Apply schema
echo "[3/5] Applying schema to ${DB_NAME}..."
docker exec -i "$MYSQL_CONTAINER" mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$DB_NAME" \
    < "$PROJECT_ROOT/sql/schema.sql" || {
    echo "ERROR: Failed to apply schema"
    exit 1
}

# Step 4: Load sample data
echo "[4/5] Loading sample data into ${DB_NAME}..."
docker exec -i "$MYSQL_CONTAINER" mysql -u"$ARTHA_USER" -p"$ARTHA_PASSWORD" "$DB_NAME" \
    < "$PROJECT_ROOT/sql/sample_data.sql" || {
    echo "ERROR: Failed to load sample data"
    exit 1
}

# Step 5: Validate row counts
echo "[5/5] Validating row counts..."

# Function to run a query and return the result
run_query() {
    docker exec -i "$MYSQL_CONTAINER" mysql -N -u"$ARTHA_USER" -p"$ARTHA_PASSWORD" "$DB_NAME" -e "$1"
}

# Helper function for assertions
assert_equals() {
    local actual=$1
    local expected=$2
    local description=$3
    
    if [ "$actual" -eq "$expected" ]; then
        echo "  ✓ ${description}: ${actual}"
        return 0
    else
        echo "  ✗ ${description}: expected ${expected}, got ${actual}"
        return 1
    fi
}

# Helper function for decimal assertions (with tolerance for floating point)
assert_decimal_equals() {
    local actual=$1
    local expected=$2
    local description=$3
    
    # Simplified: just check if values are equal as strings (MySQL returns formatted decimals)
    if [ "$actual" = "$expected" ]; then
        echo "  ✓ ${description}: ${actual}"
        return 0
    else
        # For near-matches with minor floating point variance, compare with awk
        local is_close=$(awk -v a="$actual" -v e="$expected" 'BEGIN { diff = (a - e < 0) ? (e - a) : (a - e); print (diff < 0.01) ? "yes" : "no" }')
        if [ "$is_close" = "yes" ]; then
            echo "  ✓ ${description}: ${actual}"
            return 0
        else
            echo "  ✗ ${description}: expected ${expected}, got ${actual}"
            return 1
        fi
    fi
}

ASSERTIONS_PASSED=0
ASSERTIONS_FAILED=0

echo ""
echo "Row count assertions:"

# Test 1: Bank count
bank_count=$(run_query "SELECT COUNT(*) FROM bank;")
if assert_equals "$bank_count" 10 "Bank count"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 2: Account count
account_count=$(run_query "SELECT COUNT(*) FROM account;")
if assert_equals "$account_count" 10 "Account count"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 3: Transaction count
transaction_count=$(run_query "SELECT COUNT(*) FROM \`transaction\`;")
if assert_equals "$transaction_count" 10 "Transaction count"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

echo ""
echo "Smoke test assertions (computed totals):"

# Test 4: Total debit amount
total_debit=$(run_query "SELECT SUM(transaction_amount) FROM \`transaction\` WHERE transaction_type='debit';")
if assert_decimal_equals "$total_debit" "249806.00" "Total debit amount"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 5: Total credit amount
total_credit=$(run_query "SELECT SUM(transaction_amount) FROM \`transaction\` WHERE transaction_type='credit';")
if assert_decimal_equals "$total_credit" "296810.00" "Total credit amount"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 6: Credit minus debit (net cash movement)
net_movement=$(run_query "SELECT (SUM(CASE WHEN transaction_type='credit' THEN transaction_amount ELSE 0 END) - SUM(CASE WHEN transaction_type='debit' THEN transaction_amount ELSE 0 END)) FROM \`transaction\`;")
if assert_decimal_equals "$net_movement" "47004.00" "Net cash movement (credit - debit)"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 7: Total available balance
total_balance=$(run_query "SELECT SUM(available_balance) FROM account;")
if assert_decimal_equals "$total_balance" "-81229672.84" "Total available balance"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 8: HDFC available balance
hdfc_balance=$(run_query "SELECT SUM(available_balance) FROM account WHERE bank_code='HDFC';")
if assert_decimal_equals "$hdfc_balance" "-252302939.33" "HDFC available balance"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 9: HDFC transaction count
hdfc_txn_count=$(run_query "SELECT COUNT(*) FROM \`transaction\` t JOIN account a ON t.account_id = a.account_id WHERE a.bank_code='HDFC';")
if assert_equals "$hdfc_txn_count" 7 "HDFC transaction count"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 10: HDFC debit total
hdfc_debit=$(run_query "SELECT SUM(transaction_amount) FROM \`transaction\` t JOIN account a ON t.account_id = a.account_id WHERE a.bank_code='HDFC' AND t.transaction_type='debit';")
if assert_decimal_equals "$hdfc_debit" "240455.00" "HDFC debit amount"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 11: HDFC credit total
hdfc_credit=$(run_query "SELECT SUM(transaction_amount) FROM \`transaction\` t JOIN account a ON t.account_id = a.account_id WHERE a.bank_code='HDFC' AND t.transaction_type='credit';")
if assert_decimal_equals "$hdfc_credit" "260000.00" "HDFC credit amount"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 12: 2026-06-24 debit total
date_debit=$(run_query "SELECT SUM(transaction_amount) FROM \`transaction\` WHERE DATE(transaction_date)='2026-06-24' AND transaction_type='debit';")
if assert_decimal_equals "$date_debit" "169299.00" "2026-06-24 debit total"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

# Test 13: Reference S5314253 matches
ref_count=$(run_query "SELECT COUNT(*) FROM \`transaction\` WHERE transaction_reference_id='S5314253';")
if assert_equals "$ref_count" 1 "Reference S5314253 matches"; then
    ((ASSERTIONS_PASSED++))
else
    ((ASSERTIONS_FAILED++))
fi

echo ""
echo "=========================================="
echo "Assertion Summary: ${ASSERTIONS_PASSED} passed, ${ASSERTIONS_FAILED} failed"
echo "=========================================="

if [ "$ASSERTIONS_FAILED" -gt 0 ]; then
    echo "ERROR: ${ASSERTIONS_FAILED} assertions failed"
    exit 1
fi

echo ""
echo "✓ artha_sample database setup completed successfully!"
echo ""
echo "To verify capabilities:"
echo "  curl -s http://localhost:8000/api/capabilities | jq"
echo ""
echo "To switch to artha_sample in .env:"
echo "  ARTHA_DATABASE_URL=mysql://artha:artha@127.0.0.1:3306/artha_sample"
echo "  ARTHA_UTR_MODE=opaque"
exit 0
