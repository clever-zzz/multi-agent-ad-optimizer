# Ad Optimizer - developer entrypoints.
#
# Every target is a thin wrapper so contributors do not have to remember which
# directory a command runs in. Requires: python 3.11+, node 20+, GNU make.
#
# Windows: `make` needs a POSIX shell for the `dev` and `clean` targets. Either
# run make from Git Bash, or use the equivalent PowerShell helpers in scripts/
# (scripts\setup.ps1, scripts\dev.ps1, scripts\check.ps1).

BACKEND_DIR     := backend
FRONTEND_DIR    := frontend
COMPOSE         := docker compose -f deploy/compose/docker-compose.yml
COMPOSE_PROD    := $(COMPOSE) -f deploy/compose/docker-compose.prod.yml

ifeq ($(OS),Windows_NT)
  PY_BOOTSTRAP := py -3
  VENV_BIN     := $(BACKEND_DIR)/.venv/Scripts
  EXE          := .exe
else
  SHELL        := /bin/bash
  PY_BOOTSTRAP := python3
  VENV_BIN     := $(BACKEND_DIR)/.venv/bin
  EXE          :=
endif

PYTHON  := $(VENV_BIN)/python$(EXE)
PIP     := $(VENV_BIN)/pip$(EXE)
ALEMBIC := $(VENV_BIN)/alembic$(EXE)

.DEFAULT_GOAL := help

## help: list every target with its description
.PHONY: help
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed -e 's/## //' | column -t -s ':'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

## install: create the backend virtualenv and install frontend dependencies
.PHONY: install
install: backend-install frontend-install

## backend-install: install the backend package with its dev extras
.PHONY: backend-install
backend-install:
	$(PY_BOOTSTRAP) -m venv $(BACKEND_DIR)/.venv
	$(PIP) install --upgrade pip
	$(PIP) install -e "$(BACKEND_DIR)[dev,analytics]"

## frontend-install: install frontend dependencies
.PHONY: frontend-install
frontend-install:
	cd $(FRONTEND_DIR) && npm install

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

## dev: run the API and the web console with hot reload
.PHONY: dev
dev:
ifeq ($(OS),Windows_NT)
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1
else
	@echo "Starting backend on :8000 and frontend on :5173"
	@trap 'kill 0' EXIT; \
	  ($(PYTHON) -m uvicorn adoptimizer.main:app --reload --app-dir $(BACKEND_DIR)/src --port 8000) & \
	  (cd $(FRONTEND_DIR) && npm run dev) & \
	  wait
endif

## serve: run only the API with hot reload
.PHONY: serve
serve:
	cd $(BACKEND_DIR) && $(VENV_BIN)/adoptimizer$(EXE) serve --reload

## migrate: apply database migrations
.PHONY: migrate
migrate:
	cd $(BACKEND_DIR) && $(VENV_BIN)/adoptimizer$(EXE) migrate

## revision: autogenerate a migration from the current ORM models (m="message")
.PHONY: revision
revision:
	cd $(BACKEND_DIR) && $(VENV_BIN)/adoptimizer$(EXE) revision --message "$(m)"

## verify-migrations: apply, check parity and reverse on a throwaway database
# Never points at the configured DATABASE__URL: reversing migrations would wipe
# a developer's data. CI runs the same chain against a PostgreSQL service.
.PHONY: verify-migrations
ifeq ($(OS),Windows_NT)
verify-migrations:
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1 -Only backend -Skip "ruff-format,ruff-lint,mypy,pytest"
else
verify-migrations:
	cd $(BACKEND_DIR) && \
	  tmp_db="$$(mktemp -d)/migrations.db" && \
	  export APP__ENVIRONMENT=test LLM__PROVIDER=mock DATA_MODE=mock \
		 DATABASE__URL="sqlite+aiosqlite:///$$tmp_db" && \
	  { $(ALEMBIC) upgrade head && $(ALEMBIC) check && \
		$(ALEMBIC) downgrade base && $(ALEMBIC) upgrade head; }; \
	  rc=$$?; rm -f "$$tmp_db"; exit $$rc
endif

## seed: load the demo dataset into the configured database
.PHONY: seed
seed:
	cd $(BACKEND_DIR) && $(VENV_BIN)/adoptimizer$(EXE) seed

## run-once: execute a single optimisation loop from the CLI
.PHONY: run-once
run-once:
	cd $(BACKEND_DIR) && $(VENV_BIN)/adoptimizer$(EXE) run --max-iterations 2

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

## lint: ruff + eslint across both tiers
.PHONY: lint
lint: backend-lint frontend-lint

## backend-lint: ruff format check and lint
.PHONY: backend-lint
backend-lint:
	cd $(BACKEND_DIR) && $(VENV_BIN)/ruff$(EXE) format --check src tests
	cd $(BACKEND_DIR) && $(VENV_BIN)/ruff$(EXE) check src tests

## backend-format: rewrite backend sources with ruff
.PHONY: backend-format
backend-format:
	cd $(BACKEND_DIR) && $(VENV_BIN)/ruff$(EXE) format src tests
	cd $(BACKEND_DIR) && $(VENV_BIN)/ruff$(EXE) check --fix src tests

## frontend-lint: eslint with warnings as errors
.PHONY: frontend-lint
frontend-lint:
	cd $(FRONTEND_DIR) && npm run lint

## types: mypy strict and tsc across both tiers
.PHONY: types
types: backend-types frontend-types

## backend-types: mypy in strict mode
.PHONY: backend-types
backend-types:
	cd $(BACKEND_DIR) && $(VENV_BIN)/mypy$(EXE) src

## frontend-types: tsc for the app and node configs
.PHONY: frontend-types
frontend-types:
	cd $(FRONTEND_DIR) && npm run typecheck

## test: run both test suites
.PHONY: test
test: backend-test frontend-test

## backend-test: pytest with coverage (fails below the configured floor)
.PHONY: backend-test
backend-test:
	cd $(BACKEND_DIR) && $(VENV_BIN)/pytest$(EXE) --cov

## frontend-test: vitest single run
.PHONY: frontend-test
frontend-test:
	cd $(FRONTEND_DIR) && npm run test

## check: everything CI runs, in the same order
.PHONY: check
check: lint types verify-migrations test build

## build: production frontend bundle
.PHONY: build
build:
	cd $(FRONTEND_DIR) && npm run build

# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

## up: build and start the full local stack (postgres, redis, api, web)
.PHONY: up
up:
	$(COMPOSE) up --build -d
	@echo "Web console: http://localhost:8080  API docs: http://localhost:8000/docs"

## down: stop the local stack
.PHONY: down
down:
	$(COMPOSE) down

## logs: tail the API logs
.PHONY: logs
logs:
	$(COMPOSE) logs -f api

## ps: show stack status and health
.PHONY: ps
ps:
	$(COMPOSE) ps

## images: build both container images locally
.PHONY: images
images:
	docker build -f deploy/docker/Dockerfile.backend -t adoptimizer-backend:dev .
	docker build -f deploy/docker/Dockerfile.frontend -t adoptimizer-frontend:dev .

## deploy-k8s: render and apply the kustomize bundle
.PHONY: deploy-k8s
deploy-k8s:
	kubectl apply -k deploy/k8s

## clean: remove build artefacts, caches and the local SQLite database
.PHONY: clean
clean:
ifeq ($(OS),Windows_NT)
	powershell -NoProfile -ExecutionPolicy Bypass -File scripts/clean.ps1
else
	cd $(FRONTEND_DIR) && rm -rf dist coverage node_modules/.vite
	find $(BACKEND_DIR)/src $(BACKEND_DIR)/tests -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf $(BACKEND_DIR)/.pytest_cache $(BACKEND_DIR)/.mypy_cache $(BACKEND_DIR)/.ruff_cache
	rm -rf $(BACKEND_DIR)/htmlcov $(BACKEND_DIR)/.coverage $(BACKEND_DIR)/coverage.xml
	rm -f  $(BACKEND_DIR)/adoptimizer.db
endif