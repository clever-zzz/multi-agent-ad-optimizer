"""Metrics are declared in one file and emitted everywhere else.

A metric that is declared but never incremented is worse than a missing one: a
counter with labels exports no series until it is first used, so a dead metric
does not read as zero -- it does not appear at all. ``alerts_raised_total`` and
``optimization_actions_total`` sat dead behind a dashboard that was never built,
and ``job_queue_depth`` described a background queue this project does not have
(ADR-0002 dispatches runs in-process). Nothing failed; the numbers were simply
absent, which is indistinguishable from "nothing happened".
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import adoptimizer

PACKAGE_ROOT = Path(adoptimizer.__file__).parent
METRICS_MODULE = PACKAGE_ROOT / "core" / "metrics.py"

_EMISSION = re.compile(r"\b([A-Z][A-Z0-9_]+)\.(?:labels|inc|dec|set|observe|time)\b")

# Pinned so that adding or removing a metric is a deliberate act: several docs
# quote the count, and a silent change would make them wrong in a way nobody
# notices until an interviewer counts.
EXPECTED = {
    "ACTIONS_TOTAL",
    "ACTIVE_RUNS",
    "AGENT_RUNS_TOTAL",
    "AGENT_RUN_DURATION_SECONDS",
    "AGENT_STEP_DURATION_SECONDS",
    "AGENT_STEP_TOTAL",
    "ALERTS_TOTAL",
    "CACHE_OPS_TOTAL",
    "DB_QUERY_DURATION_SECONDS",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
    "INGEST_BATCHES_TOTAL",
    "INGEST_LAG_DAYS",
    "INGEST_RECORDS_TOTAL",
    "INGEST_TICKS_TOTAL",
    "LLM_CALLS_TOTAL",
    "LLM_LATENCY_SECONDS",
    "LLM_SPEND_USD_TOTAL",
    "LLM_TOKENS_TOTAL",
    "WAREHOUSE_READS_TOTAL",
    "WAREHOUSE_WRITES_TOTAL",
}


def _declared() -> dict[str, str]:
    """Metric constant -> Prometheus series name, as declared in metrics.py."""
    declared: dict[str, str] = {}
    for node in ast.parse(METRICS_MODULE.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            declared[target.id] = first.value
    return declared


def _emitted_elsewhere() -> set[str]:
    """Every metric constant touched by an emission call outside metrics.py."""
    emitted: set[str] = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == METRICS_MODULE:
            continue
        emitted.update(_EMISSION.findall(path.read_text(encoding="utf-8")))
    return emitted


class TestMetricWiring:
    def test_every_declared_metric_is_emitted_somewhere(self) -> None:
        dead = set(_declared()) - _emitted_elsewhere()
        assert not dead, (
            "declared but never incremented, so these export no series at all: "
            + ", ".join(sorted(dead))
        )

    def test_the_declared_set_is_pinned(self) -> None:
        assert set(_declared()) == EXPECTED

    def test_no_series_name_is_reused(self) -> None:
        names = list(_declared().values())
        assert len(names) == len(set(names))
