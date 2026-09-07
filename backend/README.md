# Ad Optimizer Backend

FastAPI + SQLAlchemy 2 (async) + LangGraph. Runs a five-agent optimization loop
over campaign telemetry and proposes budget, bid, creative and experiment
changes. Nothing reaches an ad platform without an explicit approval.

Full documentation lives in [`../docs/production/`](../docs/production/). This
file is the short version for someone who just opened the directory.

## Quick start

```powershell
# Windows, from the repository root - creates the venv, installs both tiers,
# copies the .env examples, then migrates + seeds + serves.
..\scripts\setup.ps1
..\scripts\dev.ps1 -BackendOnly
```

```bash
# Or by hand
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,analytics]"      # Windows
# .venv/bin/pip install -e ".[dev,analytics]"        # macOS / Linux

cp .env.example .env
.venv/Scripts/adoptimizer migrate
.venv/Scripts/adoptimizer seed
.venv/Scripts/adoptimizer serve
```

With the default `.env` (SQLite + `LLM__PROVIDER=mock` + `DATA_MODE=mock`) the
service needs no network, no API key and no containers. The first boot on
SQLite creates the schema, stamps `alembic_version` and seeds the bootstrap
administrator plus a deterministic demo dataset.

- OpenAPI: <http://localhost:8000/docs>
- Liveness: <http://localhost:8000/healthz>
- Readiness (aggregated dependency health): <http://localhost:8000/readyz>
- Metrics: <http://localhost:8000/metrics>
- Login: `admin@adoptimizer.dev` / `Adm1n!ChangeMe` — change it immediately.

## Layout

```
src/adoptimizer/
  core/          config, logging, security (Argon2id/JWT/RBAC), errors,
                 middleware, Prometheus metrics, composition root
  domain/        pure business rules - KPIs, pricing, budget allocation,
                 anomaly detection, creative scoring, statistics. No I/O.
  agents/        the five agents plus the BaseAgent template method
  orchestrator/  LangGraph supervisor graph, state reducers, event bus
  llm/           provider gateway: retry, timeout, structured output,
                 per-call spend accounting, monthly budget guardrail
  infra/         db (async engine + ORM models), cache, ClickHouse warehouse,
                 ad platform adapters (mock / google / meta / tiktok)
  repositories/  persistence access
  services/      application use cases and transaction boundaries
  api/           routers, health probes, DTO schemas
  cli.py         operator commands (serve/migrate/revision/seed/run/healthcheck/token)
migrations/      Alembic. env.py runs on the async engine; SQLite uses batch mode.
tests/           unit/ (pure rules) + integration/ (full HTTP stack, temp SQLite)
```

Dependencies point one way: `api → services → {repositories, domain,
orchestrator, llm} → infra`. `domain/` imports no framework and no I/O, which
is why it is fully unit-testable.

## Optional extras

| Extra | Adds | Needed when |
|---|---|---|
| `analytics` | cvxpy | You want the convex budget solver instead of the greedy fallback |
| `postgres` | asyncpg | `DATABASE__URL` points at PostgreSQL |
| `clickhouse` | clickhouse-connect | `DATA_MODE=warehouse` |
| `openai` | langchain-openai | `LLM__PROVIDER` is a hosted model |
| `worker` | arq | Reserved for the queue-based dispatcher (see ADR-0002) |
| `dev` | pytest, ruff, mypy, respx | Development and CI |

The container image installs `[postgres,analytics,worker]`.

## Tests and quality gates

```bash
.venv/Scripts/pytest --cov          # 591 tests, branch coverage floor 78%
.venv/Scripts/ruff format --check src tests
.venv/Scripts/ruff check src tests
.venv/Scripts/mypy src              # strict
```

The suite is hermetic: a per-test SQLite file, the mock LLM provider, the mock
platform adapter and an in-memory cache. No Docker, no network.

From the repository root, `scripts\check.ps1` (Windows) or `make check` runs
every gate CI runs, in the same order.

## Operating notes

- **`--workers 1` is mandatory.** Runs are dispatched as in-process asyncio
  tasks and the SSE timeline is served by the same process. Scale with replicas,
  not workers. See [`../docs/adr/0002-in-process-run-dispatch.md`](../docs/adr/0002-in-process-run-dispatch.md).
- **Migrations own the PostgreSQL schema.** SQLite is a development convenience
  only, and production boot is refused when `DATABASE__URL` points at it.
- **`APP__ENVIRONMENT=production`** enables a startup validator that rejects
  weak JWT secrets, weak bootstrap passwords, wildcard CORS and SQLite. It also
  turns off `/docs` and `/redoc`.
- Every configuration key is documented inline in [`.env.example`](.env.example).