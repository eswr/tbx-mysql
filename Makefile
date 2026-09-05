.DEFAULT_GOAL := help
SHELL := /bin/bash

# Run backend from the repo root with `backend` on the import path so that
# `app.*` resolves the same way it does under pytest.
VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
RUFF        := $(VENV)/bin/ruff
UVICORN     := $(VENV)/bin/uvicorn
FRONTEND    := frontend
API_PORT    ?= 8000
WEB_PORT    ?= 5173
DB_URL      ?= mysql://artha:artha@127.0.0.1:3306/artha

export PYTHONPATH := backend

.PHONY: help
help: ## Show this help
	@echo "Artha — AI Finance Assistant"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Quick start:  make setup && make db-up && make seed && make dev"

# ---------------------------------------------------------------- setup

.PHONY: setup
setup: install-backend install-frontend env ## Install everything and create .env

.PHONY: install-backend
install-backend: ## Create the venv and install Python deps
	@command -v uv >/dev/null 2>&1 \
		&& uv venv $(VENV) && uv pip install --python $(PY) -e ".[dev]" ruff \
		|| { python3 -m venv $(VENV) && $(PIP) install -e ".[dev]" ruff; }

.PHONY: install-frontend
install-frontend: ## Install npm deps
	cd $(FRONTEND) && npm install

.PHONY: env
env: ## Create .env from .env.example if missing
	@test -f .env || { cp .env.example .env; echo "Created .env from .env.example"; }
	@test -f $(FRONTEND)/.env || { cp $(FRONTEND)/.env.example $(FRONTEND)/.env; echo "Created $(FRONTEND)/.env"; }

# ---------------------------------------------------------------- database

.PHONY: db-up
db-up: ## Start MySQL and wait for it to accept connections
	docker compose up -d mysql
	@echo "Waiting for MySQL to become healthy..."
	@for i in $$(seq 1 60); do \
		if docker compose exec -T mysql mysqladmin ping -h localhost -u artha -partha --silent >/dev/null 2>&1; then \
			echo "MySQL is ready."; exit 0; \
		fi; sleep 2; \
	done; echo "MySQL did not become ready in time." >&2; exit 1

.PHONY: db-down
db-down: ## Stop MySQL (keeps the data volume)
	docker compose down

.PHONY: db-reset
db-reset: ## Destroy the MySQL volume and start clean
	docker compose down -v
	$(MAKE) db-up

.PHONY: seed
seed: ## Load the synthetic fixture into MySQL
	$(PY) backend/scripts/load_fixture.py --db-url "$(DB_URL)" --clear

.PHONY: db-shell
db-shell: ## Open a MySQL shell against the app database
	docker compose exec mysql mysql -u artha -partha artha

# ---------------------------------------------------------------- run

.PHONY: dev
dev: ## Run backend and frontend together (Ctrl-C stops both)
	@echo "API  → http://127.0.0.1:$(API_PORT)"
	@echo "Web  → http://localhost:$(WEB_PORT)"
	@trap 'kill 0' EXIT INT TERM; \
	$(MAKE) --no-print-directory backend & \
	$(MAKE) --no-print-directory frontend & \
	wait

.PHONY: backend
backend: ## Run the FastAPI backend with reload
	$(UVICORN) app.main:app --app-dir backend --reload --port $(API_PORT)

.PHONY: frontend
frontend: ## Run the Vite dev server
	cd $(FRONTEND) && npm run dev

# ---------------------------------------------------------------- verify

.PHONY: check
check: lint typecheck test build ## Everything CI would run

.PHONY: all
all: lint pytype test-all frontend-test typecheck build eval eval-holdout diff-check ## Complete local release verification

.PHONY: test
test: ## Run the Python test suite (skips tests needing live MySQL)
	$(PYTEST) backend/tests -q -m "not requires_mysql"

.PHONY: test-all
test-all: ## Run every test, including those requiring live MySQL
	$(PYTEST) backend/tests -q

.PHONY: test-cov
test-cov: ## Run tests with a coverage report
	$(PYTEST) backend/tests -q --cov=backend/app --cov-report=term-missing

.PHONY: lint
lint: ## Lint Python and type-check the frontend
	$(RUFF) check backend evaluation
	$(RUFF) format --check backend evaluation

.PHONY: pytype
pytype: ## Type-check backend and evaluation Python
	uvx ty check backend evaluation --error-on-warning

.PHONY: fmt
fmt: ## Auto-format and auto-fix Python
	$(RUFF) check --fix backend evaluation
	$(RUFF) format backend evaluation

.PHONY: typecheck
typecheck: ## Type-check the frontend
	cd $(FRONTEND) && npm run typecheck

.PHONY: frontend-test
frontend-test: ## Run the frontend unit and component tests
	cd $(FRONTEND) && npm test

.PHONY: build
build: ## Production build of the frontend
	cd $(FRONTEND) && npm run build

.PHONY: eval
eval: ## Run the evaluation suite against MySQL
	$(PY) evaluation/run_eval.py --engine mysql --mysql-url "$(DB_URL)"

.PHONY: eval-holdout
eval-holdout: ## Run the final MySQL evaluation including the frozen holdout
	$(PY) evaluation/run_eval.py --engine mysql --mysql-url "$(DB_URL)" --include-holdout

.PHONY: diff-check
diff-check: ## Check patches for whitespace errors
	git diff --check

.PHONY: smoke
smoke: ## Hit the running API with a few grounded questions
	@for q in "What is my total available balance?" "How many accounts per bank?" "How much did I spend last month?"; do \
		echo "── $$q"; \
		curl -s -X POST http://127.0.0.1:$(API_PORT)/api/chat \
			-H 'Content-Type: application/json' \
			-d "{\"question\": \"$$q\"}" \
			| $(PY) -c 'import json,sys; b=json.load(sys.stdin); print(" ", b["answer"]); print("  confidence:", b["confidence"]["level"], "| matched:", b["matched_count"])'; \
	done

# ---------------------------------------------------------------- cleanup

.PHONY: clean
clean: ## Remove build artefacts and caches
	rm -rf $(FRONTEND)/dist $(FRONTEND)/node_modules/.vite
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache

.PHONY: clean-all
clean-all: clean ## Also remove the venv and node_modules
	rm -rf $(VENV) $(FRONTEND)/node_modules
