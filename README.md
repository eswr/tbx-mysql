# Artha — grounded financial Q&A

Artha turns supported financial questions into a validated `FinancialQuery`, compiles that semantic query into an allowlisted logical plan, and executes it against MySQL in production or DuckDB in local evaluation. Responses include the interpretation, calculation, matched row count, masked evidence, confidence, structured refusal, and runtime metadata.

## Current status

- Production `/api/chat` uses the rule parser, `ConversationContext`, and `FinancialQueryExecutor` backed by the configured MySQL database. Ollama is an optional rules-miss fallback and is disabled by default. **No model selected for the current Ollama JSON-schema fallback adapter.**
- Conversation context uses SQLite by default and supports durable follow-ups such as “What about July?” across process restarts. Only semantic context is stored; financial results, evidence, account numbers, UTRs, and chat history are never persisted. An in-memory store remains available for tests and evaluation.
- `/api/capabilities` reports the schema and semantics discovered from the configured database.
- SQL is generated only from allowlisted plans and parameters. Transaction evidence is masked before it leaves an engine.
- The deterministic benchmark contains 101 cases: a locked 26-case regression suite, 55 generalization cases, and a frozen 20-case holdout.
- The repository is API-only; `frontend/` is currently empty.

## Architecture

```text
Question + ConversationContext
  -> deterministic rule parser
     -> rules miss + ARTHA_OLLAMA_ENABLED=true
        -> Ollama semantic fallback
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
| `ARTHA_OLLAMA_ENABLED` | `false` | Enable the optional Ollama fallback only after an explicit rules miss |
| `ARTHA_OLLAMA_MODEL` | `qwen3.5:0.8b` | Ollama model name. No model selected for the current Ollama JSON-schema fallback adapter |
| `ARTHA_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama API endpoint |
| `ARTHA_OLLAMA_TIMEOUT` | `30.0` | Ollama request timeout in seconds |
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

# Corrected, frozen forced-Ollama model-selection benchmark
uv run python evaluation/benchmark_ollama.py \
  --model qwen3.5:4b \
  --output evaluation/results_ollama_qwen3_5_4b_corrected_20260905.json

# Offline re-scoring of a saved run; reuses stored generations and never calls Ollama
uv run python evaluation/benchmark_ollama.py \
  --rescore evaluation/results_ollama_qwen3_5_4b_corrected_20260905.json \
  --output evaluation/results_ollama_qwen3_5_4b_rescored_20260905.json
```

The evaluation clock is fixed at 2026-09-05 and the fixture seed is 42. Each result records per-suite, combined-corpus, fixture, and rules hashes. Saved evidence:

- `evaluation/results_mysql_prerules_20260905.json`: 11/26 (42.3%)
- `evaluation/results_mysql_postrules_20260905.json`: 26/26 (100%)
- `evaluation/results_mysql_expanded_baseline_20260905.json`: untouched expanded baseline, 26/26 regression and 27/55 generalization
- `evaluation/results_mysql_postfix_nonholdout_20260905.json`: 26/26 regression and 55/55 generalization
- `evaluation/results_mysql_final_holdout_20260905.json`: 18/20 holdout, 99/101 combined
- `evaluation/results_mysql_m3_phase3_20260905.json`: post-tooling deterministic rerun, 99/101 combined and 100% safety/refusal
- `evaluation/results_ollama_qwen3_5_4b_20260905.json`: preserved original 4B run against the original corpus
- `evaluation/results_ollama_qwen3_5_4b_corrected_20260905.json`: one corrected-contract 4B run
- `evaluation/results_ollama_granite3_3_8b_corrected_20260905.json`: one corrected-contract Granite 8B run
- `evaluation/results_mysql_m3_phase3_contractfix_20260905.json`: corrected-contract deterministic rerun, 99/101 combined and 100% safety/refusal
- `evaluation/results_ollama_qwen3_5_4b_rescored_20260905.json`: execution-semantic re-score of the saved 4B run
- `evaluation/results_ollama_granite3_3_8b_rescored_20260905.json`: execution-semantic re-score of the saved Granite 8B run
- `evaluation/results_mysql_m3_merge_20260905.json`: deterministic rerun at merge, 100/101 combined and 100% safety/refusal

### Execution-semantic benchmark scoring

A model answer is scored against what the compiler would actually execute, not against literal field equality:

- `description_contains` compiles to a case-insensitive `ILIKE '%value%'`, so it is compared trimmed and case-folded. `"Selection Electronics"` and `"SELECTION ELECTRONICS"` therefore score identically.
- `min_amount_operator` and `max_amount_operator` are only rendered into SQL when their bound is present, so each is compared only when the corresponding bound is set on both sides.
- Everything else stays an exact comparison: bank, account, reference and UTR identifiers, transaction type, date range, intent, metric, aggregation, group-by, limit, comparison spec, and refusal reason.

All 24 `forced_parity` cases are asserted to be reproducible by the deterministic parser: `understand_question()` is run on each question, checked for semantic equality with the case's `expected_query`, and compiled. Closing that gap required six parser fixes, which also raised the deterministic corpus from 99/101 to 100/101 with no regressions:

- a named bank now scopes an account-balance question (`"How much money do I have in HDFC?"`)
- an inbound money-flow question is a credit summary, not a balance (`"How much money came in last month?"`)
- superlative listings return rows instead of an aggregate (`"Show my largest transactions."`)
- a listing or merchant-scoped question defaults to all time when no period is given, while an unscoped aggregate stays ambiguous
- merchant and `containing` phrases become a `description_contains` filter, ignoring bare bank names
- `"Show all transactions from my SBI accounts."` is a transaction listing, not an account listing

### M3 Phase 3 model-selection result

| Model/run | Overall | Forced parity | Fallback/safety | Safety refusal | Latency mean / p50 / p95 | Tokens prompt / completion / total | Refusals / malformed | Selected |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `qwen3.5:4b` original | 8/33 | 6/24 | 2/9 | 0/6 | 4057.38 / 3820.18 / 5151.49 ms | 7919 / 1934 / 9853 | 13 / 13 | No |
| `qwen3.5:4b` corrected | 8/33 | 7/24 | 1/9 | 0/6 | 3628.43 / 3630.79 / 4724.63 ms | 7929 / 1966 / 9895 | 11 / 11 | No |
| `granite3.3:8b` corrected | 6/33 | 5/24 | 1/9 | 0/6 | 6744.31 / 6686.55 / 9691.76 ms | 8339 / 3045 / 11384 | 5 / 5 | No |
| `qwen3.5:4b` re-scored | 8/33 | 7/24 | 1/9 | 0/6 | 3628.43 / 3630.79 / 4724.63 ms | 7929 / 1966 / 9895 | 11 / 11 | No |
| `granite3.3:8b` re-scored | 7/33 | 6/24 | 1/9 | 0/6 | 6744.31 / 6686.55 / 9691.76 ms | 8339 / 3045 / 11384 | 5 / 5 | No |

**No model selected for the current Ollama JSON-schema fallback adapter.** M3 model selection is **none**.

The two re-scored rows reuse the saved generations byte-for-byte; Ollama was not invoked again, so latency and token counts are identical to their source runs and only the verdicts were recomputed. Re-scoring moved exactly one case: `parity_14` now passes for Granite, which had emitted the correct merchant filter in a different casing than the corpus. Neither re-scored run reaches the fixed gates of at least 32/33 overall and exactly 6/6 safety/refusal, and both remain at 0/6 on safety refusals.

The corrected corpus has 24 compiler-executable parity questions, six exact-reason refusal questions (including capability), and three declared-context questions. The original 4B artifact, the original corpus, and both corrected-contract runs are preserved byte-for-byte and hash-pinned in tests. Prompt, schema, temperature, normalization, and model settings were unchanged; no 9B run or benchmark-driven tuning was performed. The next milestone is M4 frontend.

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
evaluation/forced_ollama.json          Corrected frozen 33-case forced-LLM corpus
evaluation/forced_ollama_original_20260905.json  Archived original forced-LLM corpus
evaluation/benchmark_ollama.py         Forced-Ollama model-selection runner and offline re-scorer
```

`artha.duckdb` and the root `results.json` are local generated artifacts and are ignored by Git.
