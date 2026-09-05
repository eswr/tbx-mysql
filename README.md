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
