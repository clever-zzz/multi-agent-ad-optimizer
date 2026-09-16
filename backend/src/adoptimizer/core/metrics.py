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

INGEST_RECORDS_TOTAL = Counter(
    "ingest_records_total",
    "Metric records offered to ingestion, by feed and disposition. "
    "created + updated is what landed; rejected and unresolved are what the "
    "producer has to fix, and a rising unresolved count means campaigns are "
    "being created faster than they are being given external ids.",
    labelnames=("source", "outcome"),
    registry=REGISTRY,
)

INGEST_BATCHES_TOTAL = Counter(
    "ingest_batches_total",
    "Ingestion attempts by feed, including dry runs.",
    labelnames=("source", "mode"),
    registry=REGISTRY,
)

INGEST_TICKS_TOTAL = Counter(
    "ingest_ticks_total",
    "Scheduled ingestion attempts by feed and outcome. 'ran' pulled, 'skipped' "
    "found the window already covered and 'lost_lease' found another holder "
    "pulling it - all three are healthy. Only 'failed' should page.",
    labelnames=("source", "outcome"),
    registry=REGISTRY,
)

INGEST_LAG_DAYS = Gauge(
    "ingest_lag_days",
    "Days between the newest day a feed has covered and today, as of its last "
    "scheduled attempt. The staleness alert lives here rather than on the tick "
    "counter: ticks can keep succeeding while the window stays capped and the "
    "data falls further behind. No series at all means the feed has never been "
    "pulled, which absent() reports.",
    labelnames=("source",),
    registry=REGISTRY,
)

WAREHOUSE_READS_TOTAL = Counter(
    "warehouse_reads_total",
    "Analytical reads by outcome. 'ok' served rows, 'empty' found none and "
    "'degraded' could not reach the warehouse and fell back to the primary "
    "datastore. Degrading is the right behaviour - a warehouse outage must not "
    "take the optimization loop down with it - but it is invisible by design, "
    "which is exactly why it needs a counter: a steady 'degraded' rate means "
    "every report is silently reading a different source than the operator "
    "thinks it is.",
    labelnames=("backend", "outcome"),
    registry=REGISTRY,
)

WAREHOUSE_WRITES_TOTAL = Counter(
    "warehouse_writes_total",
    "Analytical rows offered to the warehouse write path, by table and "
    "disposition. 'written' landed, 'skipped' was refused by column-type "
    "normalisation and 'failed' means the insert raised. A backfill that "
    "reports written=0 is the failure this counter exists to make visible.",
    labelnames=("table", "outcome"),
    registry=REGISTRY,
)

ACTIVE_RUNS = Gauge(
    "active_optimization_runs",
    "Optimization runs currently executing in this process.",
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
