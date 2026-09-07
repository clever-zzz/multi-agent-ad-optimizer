"""Prometheus instrumentation.

All metrics are declared once here and imported elsewhere so cardinality stays
bounded and label names remain consistent.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests by method, route and status class.",
    labelnames=("method", "route", "status_class"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds.",
    labelnames=("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

AGENT_RUNS_TOTAL = Counter(
    "agent_runs_total",
    "Optimization runs by terminal status.",
    labelnames=("status",),
    registry=REGISTRY,
)

AGENT_RUN_DURATION_SECONDS = Histogram(
    "agent_run_duration_seconds",
    "End-to-end optimization run duration.",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)

AGENT_STEP_TOTAL = Counter(
    "agent_step_total",
    "Individual agent invocations by agent name and outcome.",
    labelnames=("agent", "outcome"),
    registry=REGISTRY,
)

AGENT_STEP_DURATION_SECONDS = Histogram(
    "agent_step_duration_seconds",
    "Per-agent execution latency.",
    labelnames=("agent",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 45.0),
    registry=REGISTRY,
)

LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "LLM invocations by provider, model and outcome.",
    labelnames=("provider", "model", "outcome"),
    registry=REGISTRY,
)

LLM_LATENCY_SECONDS = Histogram(
    "llm_latency_seconds",
    "LLM round-trip latency.",
    labelnames=("provider",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 45.0),
    registry=REGISTRY,
)

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Tokens consumed by kind (prompt/completion).",
    labelnames=("provider", "model", "kind"),
    registry=REGISTRY,
)

LLM_SPEND_USD_TOTAL = Counter(
    "llm_spend_usd_total",
    "Estimated cumulative model spend in USD.",
    labelnames=("provider", "model"),
    registry=REGISTRY,
)

ALERTS_TOTAL = Counter(
    "alerts_raised_total",
    "Monitoring alerts raised by rule.",
    labelnames=("rule", "severity"),
    registry=REGISTRY,
)

ACTIONS_TOTAL = Counter(
    "optimization_actions_total",
    "Optimization actions produced by type and execution outcome.",
    labelnames=("action_type", "outcome"),
    registry=REGISTRY,
)

ACTIVE_RUNS = Gauge(
    "active_optimization_runs",
    "Optimization runs currently executing in this process.",
    registry=REGISTRY,
)

QUEUE_DEPTH = Gauge(
    "job_queue_depth",
    "Pending background jobs.",
    registry=REGISTRY,
)

CACHE_OPS_TOTAL = Counter(
    "cache_operations_total",
    "Cache accesses by result.",
    labelnames=("result",),
    registry=REGISTRY,
)

DB_QUERY_DURATION_SECONDS = Histogram(
    "db_query_duration_seconds",
    "Repository query latency.",
    labelnames=("repository",),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    """Serialise the registry for the /metrics endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
