"""Structured logging.

JSON output in production, human-readable coloured output locally. Every record
is enriched with the request identifier bound by the middleware so a single HTTP
call can be followed across services and background tasks.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, TextIO

import structlog

_configured = False

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
principal_ctx: ContextVar[str | None] = ContextVar("principal", default=None)
run_id_ctx: ContextVar[str | None] = ContextVar("run_id", default=None)


def _inject_context(
    _logger: logging.Logger, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Attach ambient request context to every record."""
    rid = request_id_ctx.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    principal = principal_ctx.get()
    if principal:
        event_dict.setdefault("principal", principal)
    run_id = run_id_ctx.get()
    if run_id:
        event_dict.setdefault("run_id", run_id)
    return event_dict


class LiveStdoutHandler(logging.StreamHandler[TextIO]):
    """A stream handler that resolves ``sys.stdout`` when it emits.

    ``logging.StreamHandler`` captures its stream at construction, and
    ``configure_logging`` is called again by every CLI command and every
    application boot. Each of those calls leaves the root logger holding a stream
    that belonged to whoever configured it last - a short-lived command's stdout,
    a supervisor's redirected pipe, a test runner's per-test buffer. Once that
    stream is closed the handler cannot emit, and the failure is silent in the
    worst way: logging prints its own diagnosis to the *current* stderr instead of
    the record, so the line is lost and something unrelated gets noise appended
    to it.

    Resolving the stream per record costs one attribute read and means a log line
    always goes to the stdout the process has right now.
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    def emit(self, record: logging.LogRecord) -> None:
        stream = _current_stdout()
        if stream is None:
            # Nowhere to put it. Dropping the record is the only safe move:
            # raising inside ``emit`` makes logging print its own diagnosis to
            # stderr for every single record, which is louder than the outage.
            return
        self.stream = stream
        super().emit(record)


def _current_stdout() -> TextIO | None:
    """The stdout this process has right now, or None when it has none at all.

    ``sys.stdout`` is regularly swapped for something that is already closed - a
    CLI command's redirected buffer, a supervisor's pipe, a test runner's
    per-test stream - and on a console-less Windows process (``pythonw``, a
    service) both it and ``sys.__stdout__`` are ``None``. ``sys.stderr`` is
    tried before giving up, because a record that reaches a human on the wrong
    stream still beats one that vanishes.
    """
    for stream in (sys.stdout, sys.__stdout__, sys.stderr):
        if stream is not None and not getattr(stream, "closed", False):
            return stream
    return None


def _is_tty(stream: TextIO | None) -> bool:
    """Whether ``stream`` is an interactive terminal, tolerating a missing one.

    ``sys.stderr`` is ``None`` on the same console-less process that leaves
    ``sys.stdout`` unusable, and ``isatty()`` on a *closed* stream raises instead
    of answering ``False``. "Should I colourise the output?" has to be answerable
    on every process the app can boot on, or the colour preference becomes a
    startup crash.
    """
    if stream is None or getattr(stream, "closed", False):
        return False
    return stream.isatty()


def configure_logging(*, level: str = "INFO", json_logs: bool = True) -> None:
    """Idempotently configure structlog and the stdlib bridge."""
    global _configured

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=_is_tty(sys.stderr))
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = LiveStdoutHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for noisy in (
        "uvicorn.access",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "httpx",
        "httpcore",
        "asyncio",
        "clickhouse_connect",
        "openai",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def ensure_configured() -> None:
    """Configure with defaults when the app factory has not run yet."""
    if not _configured:
        configure_logging()


def get_logger(name: str | None = None) -> Any:
    """Return a bound structured logger."""
    ensure_configured()
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_request_context(
    *,
    request_id: str | None = None,
    principal: str | None = None,
    run_id: str | None = None,
) -> None:
    """Bind ambient context for the current asyncio task."""
    if request_id is not None:
        request_id_ctx.set(request_id)
    if principal is not None:
        principal_ctx.set(principal)
    if run_id is not None:
        run_id_ctx.set(run_id)


def clear_request_context() -> None:
    """Reset ambient context once a request has been served."""
    for ctx in (request_id_ctx, principal_ctx, run_id_ctx):
        ctx.set(None)
