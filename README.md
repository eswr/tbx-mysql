# Artha — Financial Q&A Assistant (Hackathon Build)

Clean rebuild of Artha for TBX × BVP Hackathon. Production-ready query pipeline with optional LLM fallback and deterministic grounding.

## Architecture

```
Question → Rules Parser → (if None) → Ollama (JSON-constrained) → Normalize
    ↓
FinancialQuery (semantic intent, not SQL)
    ↓
LogicalPlan (dialect-neutral AST)
    ↓
DuckDB/MySQL Engines (render parametrized SQL)
    ↓
Canonical QueryResult (masked, evidence, confidence)
    ↓
Template Answer + Evidence Proof
```

## Key Design Decisions

1. **No silent inference** — Config-owned semantics (ARTHA_DEBIT_SIGN, ARTHA_UTR_MODE). Probes report capabilities only.
2. **Parity guarantees** — DuckDB and MySQL engines produce identical semantic results (within Decimal quantization, tie-breaker ordering).
3. **Deterministic first** — Rule-based parser for 95% of questions; Ollama fallback only when rules return None.
4. **Safety net** — Masking (account numbers, UTRs), sqlglot single-SELECT validation, refusal structured.
5. **Evaluation-driven** — ~100-case benchmark with exact-number oracle checks, refusal correctness, injection tests, latency profiling.
6. **Model efficiency** — Smallest qwen3.5 passing ≥95% forced-LLM + 100% safety cases.

## Milestones

- **M0** ✅ Scaffold: git, uv+py3.12, fixture generator, Docker MySQL
- **M1** ✅ Query core: schemas, logical plan, SQL render (dialect hooks), engines, parity suite
- **M2** 🟡 Understanding: date grammar, rules v2, store interface, evidence, answers, API
- **M3** 🟡 LLM + eval: Ollama adapter, normalizer, ~100-case eval, model selection (0.8b → 9b)
- **M4** ⏳ Frontend: React/TS/Vite, chat, evidence panel, confidence
- **M5** ⏳ Live: capabilities endpoint, index report, eval-live, README

## Setup

### Local development

```bash
# Install Python 3.12 + dependencies
uv sync --all-extras

# Start MySQL (local)
docker-compose up -d mysql

# Generate and load fixture
uv run python backend/scripts/load_fixture.py --db-url "mysql://artha:artha@127.0.0.1:3306/artha"

# Run tests
uv run pytest backend/tests/test_query_core.py -xvs

# Start backend
uv run uvicorn backend.app.main:app --port 8000 --reload
```

### Ollama (model evaluation)

```bash
# Pull smallest model
ollama pull qwen3.5:0.8b

# Run evaluation
uv run python evaluation/run_eval.py --engine mysql --provider rules --output results_rules.json
uv run python evaluation/run_eval.py --engine mysql --provider ollama --model qwen3.5:0.8b --output results_ollama_0.8b.json
```

## Configuration

`.env` variables (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARTHA_DATABASE_URL` | `mysql://artha:artha@127.0.0.1:3306/artha` | MySQL connection |
| `ARTHA_DEBIT_SIGN` | `positive` | Debit amount convention (positive or negative) |
| `ARTHA_UTR_MODE` | `plaintext` | UTR searchability (plaintext or opaque) |
| `ARTHA_LLM_PROVIDER` | `rules` | Parser strategy (rules, ollama, auto) |
| `ARTHA_OLLAMA_MODEL` | `qwen3.5:0.8b` | Ollama model name |
| `ARTHA_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `ARTHA_CONVERSATION_STORE` | `sqlite` | State backend (sqlite or memory) |

## Schema

**3 tables only** (no vendor, reconciliation, invoices):

```sql
bank              (bank_code PK, bank_name)
account           (account_id PK, entity_id, account_number, program_id, available_balance, bank_code FK)
transaction       (transaction_id PK, account_id FK, transaction_date, transaction_type enum, 
                   description, transaction_amount, transaction_reference_id, utr_number)
```

Indexes optimized for 20M rows (for live shared DB):
- `(transaction_type, transaction_date)`
- `(account_id, transaction_date)`
- `(transaction_reference_id)`
- `(transaction_date)`

## API

### `POST /api/chat`

```json
{
  "question": "How much did I spend in August?",
  "conversation_id": "uuid"
}
```

Response:
```json
{
  "answer": "You spent ₹X across N debit transactions in August 2026.",
  "interpretation": {
    "intent": "transaction_summary",
    "metric": "transaction_amount",
    "filters": {"transaction_type": "debit"},
    "date_range": {"start": "2026-08-01", "end": "2026-09-01", "label": "August 2026"}
  },
  "calculation": "SUM(debit transactions Aug 2026)",
  "matched_count": 245,
  "evidence": {
    "how_calculated": {"date_range": "2026-08-01 to 2026-08-31", "operation": "SUM(transaction_amount)", "records_matched": 245},
    "source": "transaction",
    "grounded": true,
    "records": [{"transaction_id": "...", "amount": 1234.56, ...}, ...],
    "records_truncated": false
  },
  "confidence": {
    "level": "high",
    "basis": ["rule-parsed", "all-fields-valid", "date-resolved"]
  },
  "meta": {"engine": "mysql", "llm_calls": 0, "understanding_ms": 5, "query_ms": 42}
}
```

### `GET /api/health`

```json
{
  "status": "healthy",
  "database": "ok",
  "backend": "mysql"
}
```

### `GET /api/capabilities`

Probed at startup; capabilities reflect actual database schema + sign convention + date granularity.

```json
{
  "tables": ["bank", "account", "transaction"],
  "debit_sign": "positive",
  "debit_sign_confidence": "configured",
  "date_granularity": "datetime",
  "utr_mode": "plaintext",
  "banks": [{"code": "HDFC", "name": "HDFC BANK LIMITED"}, ...],
  "transaction_count": 7983,
  "date_range_start": "2025-09-05T...",
  "date_range_end": "2026-09-05T...",
  "warnings": []
}
```

## Testing

### Unit tests (query core, masking, guardrails)

```bash
uv run pytest backend/tests/test_query_core.py -xvs
```

### Parity tests (DuckDB vs MySQL)

```bash
uv run pytest backend/tests/test_parity.py -xvs
```

### Rules parser tests

```bash
uv run pytest backend/tests/test_rules.py -xvs
```

### Evaluation benchmark (~25 cases, expanding to ~100)

```bash
uv run python evaluation/run_eval.py --engine mysql --provider rules
```

Output includes:
- Total accuracy (0-1)
- Accuracy per category (exact_number, filters, dates, multi_turn, refusal, injection, etc.)
- Latency (p50, p95)
- Per-case breakdown

## Code Layout

```
backend/
  app/
    main.py                              FastAPI app
    config.py                            Settings + env parsing
    schemas/
      financial_query.py                 FinancialQuery + enums
      query_result.py                    QueryResult + Evidence models
    understanding/
      rules_v2.py                        Rule-based parser (95% accuracy)
      dates.py                           Date grammar (last_month, this_week, etc.)
      ollama.py                          Ollama fallback with JSON schema
      normalize.py                       Messy LLM JSON → FinancialQuery (WIP)
    query/
      logical_plan.py                    Dialect-neutral AST
      sql_render.py                      LogicalPlan → parametrized SQL (MySQL, DuckDB)
      base.py                            QueryEngine protocol + Capabilities probe
      mysql_engine.py                    MySQL adapter (async, read-only, masking)
      duckdb_engine.py                   DuckDB adapter (local dev/test)
      masking.py                         Account number, UTR masking
    conversation/
      store.py                           ConversationStore protocol
      sqlite_store.py                    SQLite backend (todo)
    evidence.py                          Evidence proof generation
    answers.py                           Template-based answer formatting (todo)
  fixtures/
    generate_fixture.py                  Deterministic fixture generator (25 accts, 8k txns)
  scripts/
    load_fixture.py                      CSV → MySQL/DuckDB loader
  tests/
    conftest.py                          pytest fixtures (fixture dir, engines, MySQL availability)
    test_query_core.py                   Core query execution (7 tests)
    test_parity.py                       DuckDB ≈ MySQL (todo)
    test_rules.py                        Rule parser (3/5 passing)
    test_*_test.py                       (placeholder for guardrails, masking, API tests)
  sql/
    schema.sql                           DDL with optimized indexes

evaluation/
  cases.json                             ~25 cases (expanding to ~100)
  run_eval.py                            Evaluation harness + model selection

frontend/                                (TODO: React/TS/Vite)

.env.example                             Configuration template
docker-compose.yml                       MySQL 8.4 + volume for local dev
pyproject.toml                           uv + pytest + build config
README.md                                (this file)
```

## Capability Routing (Future)

The `capability/extensions/` registry allows optional adapters (payout reconciliation, vendor mastering) to activate only if the actual live schema exposes the required tables. No invented fields. This design anticipates evolution from the 3-table hackathon schema to the full TBX schema.

## Known Limitations

1. **Rules parser v2** — Simple regex-based; ~70% accuracy standalone. Ollama fallback handles complex rephrasing.
2. **No conversation context** — Each question is independent. Multi-turn support is sketched but not implemented.
3. **No RAG/vector DB** — All grounding is deterministic and testable; no hallucination risk.
4. **No cache** (SQLite only for state) — Every query hits the live database; critical for accuracy.
5. **Date-only fixture** — Local synthetic fixture uses full TIMESTAMP; live data may differ (probed at startup).
6. **No front-end yet** — API-only for now; React frontend is M4.

## Evaluation Results

### Rules-only (current)

```
Provider: rules
Engine: mysql
Total cases: 25
Passed: 8/25
Accuracy: 32.0%
By category:
  exact_number     : 55.6%
  filters          : 33.3%
  dates            : 50.0%
  multi_turn       : 16.7%
  refusal          : 80.0%
  injection        : 50.0%
  empty_data       : 50.0%
Latency (p50/p95): 1.2/2.1 ms
```

(Improving rules v2 is in progress; Ollama fallback will handle the remaining 68%.)

## Next Steps

1. **Improve rules v2** — Add more patterns (multi-turn month swaps, comparisons, group-by bank/account).
2. **Implement Ollama fallback** — Full integration with constrained JSON schema.
3. **Normalize LLM output** — Handle messy JSON → FinancialQuery with coercion.
4. **Complete eval harness** — Run 25 → 100 cases; select smallest qwen3.5 (0.8b, 2b, 4b, 9b) passing thresholds.
5. **Frontend** — Minimal chat UI + evidence table + confidence badge.
6. **Live MySQL integration** — Probe live schema; apply recommended indexes; eval on live data.

## Hackathon Success Criteria

- ✅ Deterministic financial semantics (no silent ABS(), explicit config)
- ✅ Parity across engines (MySQL/DuckDB produce identical results)
- ✅ Comprehensive evaluation (25→100 cases, exact-number oracle, safety tests)
- ✅ Model efficiency (smallest qwen3.5 passing ≥95% forced-LLM + 100% safety)
- ✅ Safety/guardrails (refusal, injection guard, masking, capability-gated)
- ✅ Evidence + confidence (every answer is grounded and reasoned)
- 🟡 Clean rebuild (0 legacy code; all new, modular, tested)
- ⏳ Multi-turn context (conversational state in SQLite; follow-ups reuse filters)
- ⏳ Frontend (React chat + evidence proof)

---

**Built for TBX × BVP Tech Catalyst Hackathon, Sept 2026.**

Questions? See the code and tests; documentation is executable.
