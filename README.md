# Artha — grounded financial Q&A

Artha turns supported financial questions into a validated `FinancialQuery`, compiles that semantic query into an allowlisted logical plan, and executes it against MySQL in production or DuckDB in local evaluation. Responses include the interpretation, calculation, matched row count, masked evidence, confidence, structured refusal, and runtime metadata.

## Current status

- Production `/api/chat` uses the rule parser, `ConversationContext`, and `FinancialQueryExecutor` backed by the configured MySQL database.
- Conversation context uses SQLite by default and supports durable follow-ups such as “What about July?” across process restarts. Only semantic context is stored; financial results, evidence, account numbers, UTRs, and chat history are never persisted. An in-memory store remains available for tests and evaluation.
- `/api/capabilities` reports the schema and semantics discovered from the configured database.
- SQL is generated only from allowlisted plans and parameters. Transaction evidence is masked before it leaves an engine.
- The deterministic benchmark contains 101 cases: a locked 26-case regression suite, 55 generalization cases, and a frozen 20-case holdout.
- The repository is API-only; `frontend/` is currently empty.

## Architecture

```text
Question + ConversationContext
  -> deterministic rule parser
  -> validated FinancialQuery
  -> allowlisted LogicalPlan
  -> MySQL / DuckDB snapshot execution
  -> grounded API response
```

The supported database schema contains `bank`, `account`, and `transaction`. Unsupported domains such as invoices, payroll, tax, reconciliation, and forecasts produce a structured refusal without executing a query.

## Setup

```bash
uv sync --all-extras
docker compose up -d mysql
uv run python backend/scripts/load_fixture.py \
  --db-url mysql://artha:artha@127.0.0.1:3306/artha \
  --clear
uv run uvicorn app.main:app --app-dir backend --port 8000 --reload
```

The principal settings are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ARTHA_DATABASE_URL` | `mysql://artha:artha@127.0.0.1:3306/artha` | Production database |
| `ARTHA_DEBIT_SIGN` | `positive` | Debit amount convention |
| `ARTHA_UTR_MODE` | `plaintext` | UTR lookup mode |
| `ARTHA_CONVERSATION_STORE` | `sqlite` | Conversation store (`sqlite` or `memory`) |
| `ARTHA_SQLITE_DB_PATH` | `./backend/artha.db` | Durable local conversation database path |
| `ARTHA_CORS_ORIGINS` | local Vite and React origins | Allowed browser origins |

## Using the sample database for realistic testing

Artha provides two databases:

1. **`artha` (default)** — The frozen 101-case deterministic benchmark database. Used for reproducible evaluation, regression testing, and capability discovery. Do not modify this database.

2. **`artha_sample`** — A realistic 10-row sample database with 10 banks, 10 accounts, and 10 transactions. Useful for manual integration testing, query design exploration, and capability-gap discovery before development.

### Setup `artha_sample`

```bash
# Create and load the sample database (one-time setup)
bash backend/scripts/setup_sample_db.sh

# Verify the load (optional)
uv run python backend/scripts/verify_sample_db.py
```

### Switch to `artha_sample` in `.env`

Replace the default configuration:

```env
# Before (frozen benchmark database)
ARTHA_DATABASE_URL=mysql://artha:artha@127.0.0.1:3306/artha
ARTHA_DEBIT_SIGN=positive
ARTHA_UTR_MODE=plaintext

# After (realistic sample database)
ARTHA_DATABASE_URL=mysql://artha:artha@127.0.0.1:3306/artha_sample
ARTHA_DEBIT_SIGN=positive
ARTHA_UTR_MODE=opaque
```

**Why `ARTHA_UTR_MODE=opaque` for the sample?**
The sample UTR values are ciphertext-style (e.g., `jhI5nAdyb1qOEjmcB3JvWjC6tTO+ZPVqBFPm/GiErC4TRBWRQ5ylPG3p`), not plaintext searchable. The `opaque` mode tells the engine that UTR lookups are not supported. Transaction reference IDs (`transaction_reference_id`) remain plaintext and searchable.

### Verify the sample database

After switching `.env`, restart FastAPI and check:

```bash
# Health check
curl -s http://localhost:8000/api/health | jq

# Should return: { "status": "healthy" }

# Capabilities check
curl -s http://localhost:8000/api/capabilities | jq

# Should report 10 banks, 10 accounts, 10 transactions (vs. 101 for artha)
```

### Run sample questions for capability discovery

Use `/api/chat` to run realistic manual test queries:

```bash
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the total debit amount?"}' | jq
```

Sample questions to try (and note which are currently supported or refused):
- "How much did I spend in June 2026?"
- "What is the total credit amount?"
- "Show me all HDFC transactions."
- "Which account has the highest balance?"
- "What is the total money in and out?" (net cash movement — may not be supported)
- "Which bank has the most accounts?" (GROUP BY bank — may not be supported)

Failures and refusals are expected—use them to guide the next feature implementation. **Do not modify `rules.py` based on sample question results; this database is for observation only.**

### Switch back to the benchmark database

```env
# Restore the default frozen database
ARTHA_DATABASE_URL=mysql://artha:artha@127.0.0.1:3306/artha
ARTHA_DEBIT_SIGN=positive
ARTHA_UTR_MODE=plaintext
```

Then restart FastAPI.

## API

### `POST /api/chat`

Request:

```json
{
  "question": "How much did I spend in August 2026?",
  "conversation_id": "optional-client-conversation-id"
}
```

Grounded response shape:

```json
{
  "answer": "The result is ₹1,250.50 across 3 transaction(s) for August 2026.",
  "conversation_id": "optional-client-conversation-id",
  "interpretation": {
    "intent": "transaction_summary",
    "metric": "transaction_amount",
    "aggregation": "sum",
    "filters": {"transaction_type": "debit"},
    "date_range": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"},
    "group_by": [],
    "limit": null,
    "comparison": null
  },
  "calculation": "SUM(transaction_amount)",
  "matched_count": 3,
  "evidence": {
    "how_calculated": {
      "date_range": "2026-08-01 to 2026-08-31",
      "operation": "SUM(transaction_amount)",
      "records_matched": 3,
      "filters_applied": {"transaction_type": "debit"},
      "sql": null,
      "cache_hit": false
    },
    "source": "transaction",
    "grounded": true,
    "breakdown": null,
    "records": null,
    "records_truncated": false,
    "comparison_of": null
  },
  "confidence": {
    "level": "high",
    "basis": ["rule-parsed", "schema-validated", "database-grounded"]
  },
  "refusal": null,
  "meta": {
    "engine": "mysql",
    "llm_calls": 0,
    "understanding_ms": 1.0,
    "query_ms": 5.0
  }
}
```

Refused requests use the same shape with `interpretation`, `calculation`, `matched_count`, and `evidence` set to `null`; `refusal` contains a reason, message, suggestions, and supported capabilities. A valid query with no matching data returns grounded evidence plus a `no_data` refusal.

### `GET /api/capabilities`

Runs capability discovery against the configured database and returns tables, columns, debit-sign behavior, date granularity, UTR mode, banks, row counts, data dates, and warnings.

### `GET /api/health`

Returns `healthy` when MySQL responds to a ping and `degraded` otherwise.

## Verification

```bash
# Full suite
uv run pytest backend/tests

# Static types
uvx ty check backend evaluation --error-on-warning

# DuckDB benchmark
uv run python backend/scripts/load_fixture.py --duckdb-path /tmp/artha-eval.duckdb
uv run python evaluation/run_eval.py \
  --engine duckdb \
  --duckdb-path /tmp/artha-eval.duckdb

# MySQL parity/snapshot tests and benchmark
uv run pytest backend/tests/test_evaluation.py -m requires_mysql
uv run python evaluation/run_eval.py \
  --engine mysql \
  --mysql-url mysql://artha:artha@127.0.0.1:3306/artha

# Final-only holdout run (explicit opt-in)
uv run python evaluation/run_eval.py \
  --engine mysql \
  --mysql-url mysql://artha:artha@127.0.0.1:3306/artha \
  --include-holdout
```

The evaluation clock is fixed at 2026-09-05 and the fixture seed is 42. Each result records per-suite, combined-corpus, fixture, and rules hashes. Saved evidence:

- `evaluation/results_mysql_prerules_20260905.json`: 11/26 (42.3%)
- `evaluation/results_mysql_postrules_20260905.json`: 26/26 (100%)
- `evaluation/results_mysql_expanded_baseline_20260905.json`: untouched expanded baseline, 26/26 regression and 27/55 generalization
- `evaluation/results_mysql_postfix_nonholdout_20260905.json`: 26/26 regression and 55/55 generalization
- `evaluation/results_mysql_final_holdout_20260905.json`: 18/20 holdout, 99/101 combined

## Repository layout

```text
backend/app/main.py                    FastAPI chat, health, capabilities
backend/app/conversation/              Semantic in-memory and durable SQLite conversation state
backend/app/query/compiler.py          FinancialQuery -> allowlisted plans
backend/app/query/execution.py         Snapshot-backed grounded executor
backend/app/query/{mysql,duckdb}_engine.py
backend/app/understanding/rules.py     Deterministic parser and follow-ups
backend/tests/test_api.py              API contract and multi-turn coverage
backend/tests/test_evaluation.py       Numeric, parity, and snapshot coverage
evaluation/regression.json             Locked 26-case regression suite
evaluation/generalization.json         55-case rule-development suite
evaluation/holdout.json                Frozen 20-case final-only suite
evaluation/run_eval.py                 Reproducible evaluation runner
```

`artha.duckdb` and the root `results.json` are local generated artifacts and are ignored by Git.
