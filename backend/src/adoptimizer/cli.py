"""Operator command line interface.

Every routine operation has a command so deployments and incident response do
not depend on someone remembering a curl invocation.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from .core.config import get_settings
from .core.logging import configure_logging, get_logger

app = typer.Typer(
    name="adoptimizer",
    help="Operate the multi-agent advertising optimization platform.",
    no_args_is_help=True,
    add_completion=False,
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


@app.command(name="run")
def run_optimization(
    campaign_ids: list[str] = typer.Option(None, "--campaign", help="Restrict to these campaigns"),
    max_iterations: int = typer.Option(2, help="Iteration cap"),
    window_days: int = typer.Option(7, help="Telemetry window"),
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


def main() -> None:
    """Console-script entrypoint."""
    app()


if __name__ == "__main__":
    main()
