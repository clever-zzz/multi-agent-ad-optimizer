"""Operator command line interface.

Every routine operation has a command so deployments and incident response do
not depend on someone remembering a curl invocation.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import typer

from .core.clock import utc_today
from .core.config import IngestSettings, Settings, get_settings, load_environment
from .core.logging import configure_logging, get_logger

# Upper bound for every day-window option. Ten years is past any real telemetry
# window and far short of what ``timedelta`` accepts, so a typo such as
# ``--days 1000000000`` is refused by the parser with a usage message instead of
# surfacing much later as a bare ``OverflowError`` from inside a repository.
MAX_WINDOW_DAYS = 3650


app = typer.Typer(
    name="adoptimizer",
    help="Operate the multi-agent advertising optimization platform.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _configure_logging() -> None:
    """Reconfigure logging from settings before any command runs.

    ``get_logger`` configures structlog on first use, which happens at import
    time - before any settings exist - so it falls back to hard-coded defaults
    (INFO, JSON). Only ``serve`` used to re-read ``OBSERVABILITY__*`` afterwards,
    which left every other command emitting INFO JSON logs however the operator
    had configured them. A stray line on stdout is not cosmetic: it breaks
    anything that parses a command's output, including this project's own CLI
    tests. ``--help`` never reaches here, so a broken configuration still lets an
    operator read the usage text.
    """
    settings = get_settings()
    configure_logging(
        level=settings.observability.log_level,
        json_logs=settings.observability.json_logs,
    )


logger = get_logger(__name__)


def _print(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address"),  # noqa: S104
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, help="Enable autoreload for development"),
    workers: int = typer.Option(1, help="Worker processes (ignored when reload is on)"),
) -> None:
    """Run the API server."""
    import uvicorn

    settings = get_settings()
    configure_logging(
        level=settings.observability.log_level, json_logs=settings.observability.json_logs
    )
    uvicorn.run(
        "adoptimizer.main:app",
        host=host,
        port=port,
        reload=reload,
        workers=1 if reload else workers,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


@app.command()
def migrate(
    revision: str = typer.Option("head", help="Target revision"),
    offline: bool = typer.Option(False, help="Emit SQL instead of applying it"),
) -> None:
    """Apply database migrations."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database.url.replace("+aiosqlite", ""))
    if offline:
        command.upgrade(config, revision, sql=True)
    else:
        command.upgrade(config, revision)
    typer.echo("Migrations applied up to " + revision)


@app.command()
def revision(message: str = typer.Option(..., help="Migration description")) -> None:
    """Autogenerate a migration from the current ORM metadata."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database.url.replace("+aiosqlite", ""))
    command.revision(config, message=message, autogenerate=True)


@app.command()
def seed(force: bool = typer.Option(False, help="Seed even if campaigns already exist")) -> None:
    """Load the deterministic demo dataset."""

    async def _run() -> dict[str, Any]:
        from .core.container import build_container
        from .services.seed import seed_database

        settings = get_settings()
        container = await build_container(settings)
        try:
            async with container.database.unit_of_work() as session:
                result = await seed_database(
                    session,
                    admin_email=settings.security.bootstrap_admin_email,
                    admin_password=settings.security.bootstrap_admin_password.get_secret_value(),
                )
                await session.commit()
            return result
        finally:
            await container.shutdown()

    _print(asyncio.run(_run()))
    _ = force


@app.command()
def ingest(
    source: str = typer.Option("synthetic", "--source", help="Registered feed to pull from"),
    days: int = typer.Option(
        7, "--days", min=1, max=MAX_WINDOW_DAYS, help="Window length in days, ending today"
    ),
    start: str = typer.Option("", "--start", help="Window start YYYY-MM-DD, overrides --days"),
    end: str = typer.Option("", "--end", help="Window end YYYY-MM-DD, defaults to today"),
    dry_run: bool = typer.Option(
        False, "--dry-run/--no-dry-run", help="Report what would land without writing it"
    ),
    payload_file: Path | None = typer.Option(
        None, "--file", help="Push a JSON batch instead of pulling from a feed"
    ),
) -> None:
    """Ingest daily metrics, pulled from a feed or pushed from a JSON file.

    This is the scheduled data path. Run it dry first - the report lists every
    record that would be rejected and why, which is the cheap way to find out
    that a feed has started naming campaigns you have never imported.

    Exits non-zero when a non-empty batch landed nothing at all, so a cron job
    alerting on exit status catches a feed that has quietly stopped matching.
    """
    from .schemas.ingest import IngestBatchIn
    from .services.ingest import IngestService

    # Read on the way in, not inside the coroutine: a blocking file read in an
    # async function stalls the loop, and there is no reason to defer it.
    document: Any = None
    if payload_file is not None:
        document = json.loads(payload_file.read_text(encoding="utf-8"))

    async def _run() -> dict[str, Any]:
        from .core.container import build_container

        settings = get_settings()
        container = await build_container(settings)
        try:
            if document is not None:
                # Accept either a full batch envelope or a bare list of records,
                # because a backfill export is usually just the rows.
                batch = IngestBatchIn.model_validate(
                    document
                    if isinstance(document, dict)
                    else {"source": source, "records": document}
                )
                async with container.database.unit_of_work() as session:
                    report = await IngestService(session).ingest(
                        batch.records,
                        source=batch.source,
                        dry_run=dry_run or batch.dry_run,
                        actor="cli:ingest",
                    )
                    await session.commit()
                return report.model_dump(mode="json")

            window_start = (
                date.fromisoformat(start)
                if start
                else utc_today() - timedelta(days=max(1, days) - 1)
            )
            window_end = date.fromisoformat(end) if end else utc_today()
            async with container.database.unit_of_work() as session:
                report = await IngestService(session).pull(
                    container.ingest_sources,
                    source,
                    start=window_start,
                    end=window_end,
                    dry_run=dry_run,
                    actor="cli:ingest",
                )
                await session.commit()
            return report.model_dump(mode="json")
        finally:
            await container.shutdown()

    report = asyncio.run(_run())
    _print(report)
    if report["received"] and not (report["created"] or report["updated"]):
        raise typer.Exit(code=1)


@app.command()
def scheduler(
    once: bool = typer.Option(
        False, "--once", help="Run a single pass and exit; this is what a CronJob calls"
    ),
    source: list[str] = typer.Option(
        None, "--source", help="Override INGEST__SOURCES with one feed; repeatable"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run/--no-dry-run", help="Also rehearse: report what would land, write nothing"
    ),
    force: bool = typer.Option(
        False, "--force", help="Pull even when the window is already covered"
    ),
    interval: int = typer.Option(
        0, "--interval", help="Resident loop cadence in minutes; 0 keeps the configured value"
    ),
) -> None:
    """Run the metric ingestion schedule, once or forever.

    The window is derived from the calendar, not from a stored cursor, so a
    missed run is closed by the next one rather than lost: the plan reaches back
    to the day after the last covered day, bounded by INGEST__MAX_CATCHUP_DAYS.
    Past that bound the tick reports ``gap_days`` and names the backfill command
    in its ``detail`` instead of quietly narrowing the window.

    Re-pulling is safe, which is what makes the whole thing runnable from more
    than one place: a database lease means one pass pulls a given feed at a time,
    and ingestion upserts one slot per (campaign, creative, date). --dry-run only
    ever adds rehearsal; INGEST__DRY_RUN=true cannot be turned off from here.

    INGEST__SCHEDULER_ENABLED gates the resident loop only. --once pulls whether
    or not it is set, because a CronJob calling --once is scheduled by the
    platform rather than by this process - in production that flag being false is
    exactly how the cadence is handed to Kubernetes. Without --once and with the
    flag off, this command pulls nothing and says so.

    Exits non-zero when a tick failed, or when a tick pulled rows and landed none
    of them - the signature of a feed that has stopped naming campaigns we have.
    """
    from .schemas.scheduling import TickReportOut, TickResult
    from .services.scheduling import IngestScheduler

    overrides: dict[str, Any] = {}
    if source:
        overrides["sources"] = list(source)
    if interval > 0:
        overrides["interval_minutes"] = interval

    async def _run() -> TickReportOut | None:
        from .core.container import build_container

        settings = get_settings()
        # Revalidated rather than model_copy(update=...): a --source value becomes
        # a Prometheus label and a database column, so it has to satisfy the same
        # shape rule the environment does.
        payload = {**settings.ingest.model_dump(), **overrides}
        ingest_settings = IngestSettings.model_validate(payload)
        rehearsal = dry_run or ingest_settings.dry_run

        container = await build_container(settings)
        try:
            instance = IngestScheduler(
                container.database.session_factory,
                container.ingest_sources,
                ingest_settings,
            )
            if once:
                return await instance.tick_all(dry_run=rehearsal, force=force)

            if not ingest_settings.scheduler_enabled:
                _print(
                    {
                        "warning": "INGEST__SCHEDULER_ENABLED=false, so the resident loop is off "
                        "and nothing was pulled. Pass --once to run a single pass now.",
                    }
                )
                return None

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                # Windows has no add_signal_handler. Ctrl+C still arrives there,
                # as a KeyboardInterrupt that unwinds through the finally below.
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(signum, stop.set)
            await instance.run_forever(stop=stop)
            return None
        finally:
            await container.shutdown()

    report = asyncio.run(_run())
    if report is None:
        return
    _print(report.model_dump(mode="json"))
    for tick in report.ticks:
        landed_nothing = bool(tick.received) and not (tick.created or tick.updated)
        if tick.outcome is TickResult.FAILED or landed_nothing:
            raise typer.Exit(code=1)


@app.command(name="run")
def run_optimization(
    campaign_ids: list[str] = typer.Option(None, "--campaign", help="Restrict to these campaigns"),
    max_iterations: int = typer.Option(2, help="Iteration cap"),
    window_days: int = typer.Option(7, min=1, max=MAX_WINDOW_DAYS, help="Telemetry window"),
    wait: bool = typer.Option(True, help="Block until the run finishes"),
) -> None:
    """Execute one optimization loop and print the summary."""

    async def _run() -> dict[str, Any]:
        from .core.container import build_container
        from .services.optimization import OptimizationService

        settings = get_settings()
        container = await build_container(settings)
        try:
            async with container.database.unit_of_work() as session:
                service = OptimizationService(container, session)
                # start_run collects its own inputs and dispatches exactly one
                # execution, so the CLI only has to await it. Executing here as
                # well would run the same loop twice.
                run = await service.start_run(
                    session,
                    campaign_ids=list(campaign_ids or []),
                    max_iterations=max_iterations,
                    window_days=window_days,
                    trigger_type="cli",
                    actor_id=None,
                    background=True,
                )
                run_id = run.id

            if not wait:
                return {"run_id": run_id, "status": "dispatched"}

            completed = await service.wait_for(run_id, timeout=600.0)
            async with container.database.unit_of_work() as session:
                detail = await service.run_detail(session, run_id)
            return {
                "run_id": run_id,
                "completed": completed,
                "status": str(detail["run"].status),
                "summary": detail["run"].summary,
            }
        finally:
            await container.shutdown()

    _print(asyncio.run(_run()))


@app.command()
def healthcheck(url: str = typer.Option("http://localhost:8000", help="Base URL")) -> None:
    """Probe a running instance and exit non-zero when not ready."""
    import httpx

    try:
        live = httpx.get(url + "/healthz", timeout=5.0)
        ready = httpx.get(url + "/readyz", timeout=10.0)
    except httpx.HTTPError as exc:
        typer.echo("Unreachable: " + str(exc), err=True)
        raise typer.Exit(code=2) from exc

    _print(
        {"liveness": live.json(), "readiness_status": ready.status_code, "readiness": ready.json()}
    )
    raise typer.Exit(code=0 if ready.status_code == 200 else 1)


@app.command()
def token(
    email: str = typer.Option(..., help="Account email"),
    password: str = typer.Option(..., prompt=True, hide_input=True, help="Account password"),
    url: str = typer.Option("http://localhost:8000", help="Base URL"),
) -> None:
    """Fetch an access token, handy for curl and scripts."""
    import httpx

    response = httpx.post(
        url + get_settings().app.api_v1_prefix + "/auth/login",
        json={"email": email, "password": password},
        timeout=15.0,
    )
    if response.status_code != 200:
        typer.echo(response.text, err=True)
        raise typer.Exit(code=1)
    _print(response.json())


# ---------------------------------------------------------------------------
# Credential inspection
#
# Handing secrets to a machine is awkward: pasting them into a chat, a ticket or
# a commit is how they leak. The supported path is to write them into
# `backend/.env` (gitignored) and never show them to anyone. This command is the
# other half of that deal - it proves the values were read and are well formed
# while printing only a length and a hash fingerprint, so it is safe to share
# its output.
# ---------------------------------------------------------------------------

CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "google": (
        "GOOGLE_ADS_CLIENT_ID",
        "GOOGLE_ADS_CLIENT_SECRET",
        "GOOGLE_ADS_REFRESH_TOKEN",
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_CUSTOMER_ID",
    ),
    "meta": ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID"),
    "tiktok": ("TIKTOK_ACCESS_TOKEN", "TIKTOK_ADVERTISER_ID"),
}

OPTIONAL_CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {"meta": ("META_APP_SECRET",)}

PLACEHOLDER_MARKERS = ("<", ">", "your-", "your_", "xxxx", "changeme", "placeholder", "todo")


def _mask(value: str) -> dict[str, Any]:
    """Describe a secret without revealing any part of it."""
    if not value:
        return {"present": False}
    warnings: list[str] = []
    if value != value.strip():
        warnings.append("surrounding whitespace - the most common cause of auth failures")
    if value[0] in "'\"" or value[-1] in "'\"":
        warnings.append("looks quoted - .env values must not be wrapped in quotes")
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        warnings.append("looks like an unfilled placeholder")
    return {
        "present": True,
        "length": len(value),
        "sha256_8": hashlib.sha256(value.encode("utf-8")).hexdigest()[:8],
        "warnings": warnings,
    }


async def _first_external_id(settings: Settings) -> str:
    """Borrow a real external id from the local database so --probe needs no args."""
    from sqlalchemy import select

    from .infra.db.models import Campaign
    from .infra.db.session import init_database

    database = await init_database(settings.database)
    try:
        async with database.unit_of_work() as session:
            value = await session.scalar(
                select(Campaign.external_id)
                .where(Campaign.external_id.is_not(None), Campaign.external_id != "")
                .limit(1)
            )
    finally:
        await database.dispose()
    return str(value or "")


async def _probe_platforms(settings: Settings, external_id: str) -> dict[str, Any]:
    """One read-only report call per platform. Nothing here can mutate an account."""
    from .core.config import DataMode
    from .domain.enums import Platform
    from .infra.ads.registry import build_platform_clients

    # Force warehouse mode for the probe: the point is to talk to the real
    # adapter, and the running DATA_MODE may still be mock.
    registry = build_platform_clients(DataMode.WAREHOUSE)
    target = external_id or await _first_external_id(settings)
    report: dict[str, Any] = {"external_id": target, "platforms": {}}
    if not target:
        report["error"] = "no campaign carries an external_id; pass --external-id"
        await registry.close()
        return report

    end = datetime.now(UTC).date()
    start = end - timedelta(days=7)
    try:
        for platform in (Platform.GOOGLE, Platform.META, Platform.TIKTOK):
            entry: dict[str, Any] = {}
            started = datetime.now(UTC)
            try:
                client = registry.for_platform(platform)
                payload = await client.fetch_report(
                    target, start_date=start.isoformat(), end_date=end.isoformat()
                )
                entry["ok"] = True
                entry["rows"] = len(payload.get("rows") or [])
                entry["source"] = payload.get("source")
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = type(exc).__name__ + ": " + str(exc)[:400]
            entry["latency_ms"] = int((datetime.now(UTC) - started).total_seconds() * 1000)
            report["platforms"][platform.value] = entry
    finally:
        await registry.close()
    return report


@app.command()
def creds(
    probe: bool = typer.Option(
        False, "--probe", help="Also attempt one read-only report call per platform"
    ),
    external_id: str = typer.Option(
        "", "--external-id", help="Campaign external id for --probe (default: first in the DB)"
    ),
) -> None:
    """Report which platform credentials are readable, without printing any value."""
    from .core.config import DataMode

    dotenv = load_environment()
    settings = get_settings()

    platforms: dict[str, Any] = {}
    for name, keys in CREDENTIAL_KEYS.items():
        required = {key: _mask(os.getenv(key, "")) for key in keys}
        missing = [key for key, info in required.items() if not info["present"]]
        platforms[name] = {
            "ready": not missing,
            "missing": missing,
            "required": required,
            "optional": {
                key: _mask(os.getenv(key, "")) for key in OPTIONAL_CREDENTIAL_KEYS.get(name, ())
            },
        }

    notes: list[str] = []
    if dotenv is None:
        notes.append("No .env found in " + str(Path.cwd()) + " or next to the backend package.")
    else:
        notes.append("Loaded " + str(dotenv) + " into the process environment.")
    if settings.data_mode == DataMode.MOCK:
        notes.append(
            "DATA_MODE=mock: every platform call is routed to the mock adapter, so real "
            "credentials are read but never used. Set DATA_MODE=warehouse to use them."
        )
    if settings.tools.allow_agent_writes:
        notes.append(
            "TOOLS__ALLOW_AGENT_WRITES=true: agents can mutate a live account without a "
            "human approval. Turn this back off unless you are deliberately testing it."
        )

    payload: dict[str, Any] = {
        "dotenv": str(dotenv) if dotenv else None,
        "data_mode": settings.data_mode.value,
        "platforms": platforms,
        "llm": {
            "provider": settings.llm.provider.value,
            "model": settings.llm.model,
            "api_key": _mask(settings.llm.api_key.get_secret_value()),
            # Not a secret, and the single most common misconfiguration for
            # openai_compatible / azure_openai: without it the provider raises
            # at call time and the gateway degrades to mock.
            "api_base": settings.llm.api_base or None,
        },
        "tool_guardrails": {
            "enabled": settings.tools.enabled,
            "dry_run": settings.tools.dry_run,
            "allow_agent_writes": settings.tools.allow_agent_writes,
            "require_action_approval": settings.security.require_action_approval,
        },
        "notes": notes,
    }

    exit_code = 0
    if probe:
        result = asyncio.run(_probe_platforms(settings, external_id))
        payload["probe"] = result
        probed = result.get("platforms") or {}
        if result.get("error") or any(not entry.get("ok") for entry in probed.values()):
            exit_code = 1

    _print(payload)
    raise typer.Exit(code=exit_code)


warehouse_app = typer.Typer(
    help="Analytical warehouse: mirror metrics into it and check the write path."
)
app.add_typer(warehouse_app, name="warehouse")


@warehouse_app.command("status")
def warehouse_status() -> None:
    """Report whether the configured warehouse can accept writes.

    Prints which sink would be used and its health. A ``null`` sink means writes
    go nowhere at all, which is the single most useful thing to know before
    pointing a backfill at it.

    Exits non-zero when the sink cannot accept writes, so a pre-flight check in
    a deployment script fails rather than silently skipping the backfill.
    """
    from .infra.analytics import build_sink
    from .services.warehouse_sync import WarehouseSyncService

    async def _run() -> dict[str, Any]:
        from .core.container import build_container

        settings = get_settings()
        container = await build_container(settings)
        sink = await build_sink(settings.clickhouse)
        try:
            async with container.database.unit_of_work() as session:
                return await WarehouseSyncService(session, sink).status()
        finally:
            await sink.close()
            await container.shutdown()

    payload = asyncio.run(_run())
    _print(payload)
    if not payload["available"]:
        raise typer.Exit(code=1)


@warehouse_app.command("sync")
def warehouse_sync(
    days: int = typer.Option(
        30, "--days", min=1, max=MAX_WINDOW_DAYS, help="How many days back to mirror"
    ),
    campaign: str = typer.Option("", "--campaign", help="Limit the mirror to one campaign id"),
    dry_run: bool = typer.Option(
        False, "--dry-run/--no-dry-run", help="Report what would move without writing it"
    ),
) -> None:
    """Mirror daily metrics from the primary datastore into the warehouse.

    Idempotent: the target table is a ReplacingMergeTree keyed on
    (campaign, creative, date), so re-running a window corrects rows in place
    instead of duplicating them. That is what makes it safe to schedule and safe
    to re-run after a partial failure.

    Run it dry first. ``--dry-run`` reports how many rows the window holds, which
    answers "how much would this move" before a backfill touches a production
    warehouse.

    Exits non-zero when rows were found but none landed - the shape of a
    misconfigured or unreachable warehouse.
    """
    from .infra.analytics import build_sink
    from .services.warehouse_sync import WarehouseSyncService

    campaign_ids = [campaign] if campaign else None

    async def _run() -> dict[str, Any]:
        from .core.container import build_container

        settings = get_settings()
        container = await build_container(settings)
        sink = await build_sink(settings.clickhouse)
        try:
            async with container.database.unit_of_work() as session:
                report = await WarehouseSyncService(session, sink).sync(
                    days=days, campaign_ids=campaign_ids, dry_run=dry_run
                )
                return report.to_dict()
        finally:
            await sink.close()
            await container.shutdown()

    payload = asyncio.run(_run())
    _print(payload)
    if not payload["ok"]:
        raise typer.Exit(code=1)


def main() -> None:
    """Console-script entrypoint."""
    app()


if __name__ == "__main__":
    main()
