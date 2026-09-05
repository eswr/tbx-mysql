# Artha hackathon build summary

## Merge-ready scope

The backend now has a complete deterministic production path:

```text
POST /api/chat
  -> rules understanding (+ prior ConversationContext)
  -> validated FinancialQuery
  -> allowlisted compiler
  -> FinancialQueryExecutor
  -> consistent MySQL snapshot
  -> grounded response contract
```

The response contract contains `interpretation`, `calculation`, `matched_count`, `evidence`, `confidence`, `refusal`, and `meta`, alongside the human-readable answer and conversation ID. Supported follow-ups inherit the previous semantic filters and replace the requested period. Pre-execution refusals do not touch the database; valid empty results return a grounded `no_data` refusal.

`GET /api/capabilities` is implemented because it is part of the documented API. It reports live database discovery rather than a hard-coded capability list. `GET /api/health` continues to report database reachability.

## Evaluation result

The benchmark has 26 deterministic cases across exact numbers, filters, dates, multi-turn behavior, unsupported requests, ambiguity, injection, and empty data.

| Rules revision | MySQL result | Accuracy |
| --- | ---: | ---: |
| Before current rule changes | 11/26 | 42.3% |
| Current rule changes | 26/26 | 100% |

Both saved runs use reference date 2026-09-05, fixture seed 42, and record the rules/cases SHA-256 hashes:

- `evaluation/results_mysql_prerules_20260905.json`
- `evaluation/results_mysql_postrules_20260905.json`

The evaluation runner checks only fields applicable to each case, executes numeric oracle SQL in the same snapshot as generated plans, distinguishes pre-execution refusals from grounded empty results, and evaluates both turns of multi-turn cases.

## Implemented files

- `backend/app/main.py`: production chat, health, and capability endpoints.
- `backend/app/conversation/context.py`: semantic in-memory conversation store; no financial result values are retained.
- `backend/app/query/compiler.py`: allowlisted semantic compiler.
- `backend/app/query/execution.py`: grounded result and consistent-snapshot executor.
- `backend/app/query/mysql_engine.py`: production snapshot execution.
- `backend/app/query/duckdb_engine.py`: local/evaluation snapshot execution.
- `backend/app/understanding/rules.py`: deterministic parsing, guardrails, filter operators, and follow-up resolution.
- `backend/tests/test_api.py`: response-contract, refusal, capabilities, and multi-turn API tests.
- `backend/tests/test_evaluation.py`: oracle scoring, boundary, parity, and MySQL snapshot tests.
- `evaluation/cases.json`: 26 cases.
- `evaluation/run_eval.py`: reproducible DuckDB/MySQL evaluation CLI.

The project is currently API-only; the `frontend/` directory is empty. Ollama code remains available for experiments but is not part of the production chat path or current benchmark.

## Safety and correctness

- User text cannot supply SQL identifiers; the compiler accepts only known tables, columns, joins, predicates, and sort fields.
- Both dialects use parameters and SELECT-only validation.
- Result and count queries execute in one database snapshot.
- Account numbers and UTR values are masked at the engine boundary.
- Strict and inclusive amount operators preserve the language distinction between “above” and “at least,” and between “below” and “at most.”
- Static type checking passes for `backend` and `evaluation`.
- `artha.duckdb` is removed from tracking, and both it and root `results.json` are ignored.

## Reproduction

```bash
uv sync --all-extras
docker compose up -d mysql
uv run python backend/scripts/load_fixture.py \
  --db-url mysql://artha:artha@127.0.0.1:3306/artha \
  --clear

uv run pytest backend/tests
uvx ty check backend evaluation --error-on-warning

uv run python backend/scripts/load_fixture.py --duckdb-path /tmp/artha-eval.duckdb
uv run python evaluation/run_eval.py \
  --engine duckdb \
  --duckdb-path /tmp/artha-eval.duckdb

uv run pytest backend/tests/test_evaluation.py -m requires_mysql
uv run python evaluation/run_eval.py \
  --engine mysql \
  --mysql-url mysql://artha:artha@127.0.0.1:3306/artha
```

Merge is conditional on the full unit suite, DuckDB 26/26, MySQL parity/snapshot tests, and MySQL 26/26 all passing in the final run.
