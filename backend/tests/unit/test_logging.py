"""Unit tests for structured logging: the live-stdout handler and context binding.

Everything in here is small enough to mistake for ceremony, and it is not.
``logging.StreamHandler`` captures its stream at construction while
``configure_logging`` runs again on every CLI command and every application boot,
so the root logger ends up holding the stream of whoever configured it last. Once
that stream is closed the record is lost *and* logging writes its own diagnosis
into whatever stderr happens to be current - which is how a scheduling tick's
JSON result picked up unrelated noise from a previous test's buffer.
"""

from __future__ import annotations

import contextlib
import io
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from adoptimizer.core import logging as logging_config
from adoptimizer.core.logging import (
    LiveStdoutHandler,
    _current_stdout,
    _inject_context,
    _is_tty,
    bind_request_context,
    clear_request_context,
    configure_logging,
    ensure_configured,
)


class _Tty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _record(message: str) -> logging.LogRecord:
    """A bare record. No formatter is attached, so ``emit`` writes the message."""
    return logging.LogRecord("adoptimizer.test", logging.INFO, __file__, 1, message, None, None)


def _event() -> dict[str, Any]:
    return {"event": "e"}


def _closed() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.fixture(autouse=True)
def restored_logging() -> Iterator[None]:
    """Give back the process-global logging state and the ambient context vars."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    configured = logging_config._configured
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging_config._configured = configured
    clear_request_context()


class TestCurrentStdout:
    def test_returns_the_stream_the_process_has_now(self) -> None:
        assert _current_stdout() is sys.stdout

    def test_skips_a_stdout_that_is_already_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _closed())
        monkeypatch.setattr(sys, "__stdout__", real)
        assert _current_stdout() is real

    def test_falls_back_to_stderr_when_both_stdouts_are_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = io.StringIO()
        monkeypatch.setattr(sys, "stdout", _closed())
        monkeypatch.setattr(sys, "__stdout__", None)
        monkeypatch.setattr(sys, "stderr", err)
        assert _current_stdout() is err

    def test_returns_none_when_there_is_nowhere_to_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "__stdout__", None)
        monkeypatch.setattr(sys, "stderr", _closed())
        assert _current_stdout() is None


class TestIsTty:
    def test_asks_the_stream_when_it_is_usable(self) -> None:
        assert _is_tty(_Tty()) is True
        assert _is_tty(io.StringIO()) is False

    def test_a_missing_stream_is_not_a_terminal(self) -> None:
        assert _is_tty(None) is False

    def test_a_closed_stream_answers_instead_of_raising(self) -> None:
        # isatty() on a closed StringIO raises, so the guard has to come first or
        # "should I colourise?" turns into a boot failure on a dead stream.
        with pytest.raises(ValueError, match="closed"):
            _closed().isatty()
        assert _is_tty(_closed()) is False


class TestLiveStdoutHandler:
    def test_emit_follows_the_stdout_the_process_has_now(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = io.StringIO()
        monkeypatch.setattr(sys, "stdout", first)
        handler = LiveStdoutHandler()

        second = io.StringIO()
        monkeypatch.setattr(sys, "stdout", second)
        handler.emit(_record("second stream"))

        assert first.getvalue() == ""
        assert "second stream" in second.getvalue()

    def test_a_closed_stream_captured_at_construction_does_not_break_emit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdout", _closed())
        handler = LiveStdoutHandler()

        live = io.StringIO()
        monkeypatch.setattr(sys, "stdout", live)
        handler.emit(_record("still arrives"))

        assert "still arrives" in live.getvalue()

    def test_a_plain_stream_handler_loses_the_same_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure being guarded against, pinned so it cannot quietly return."""
        monkeypatch.setattr(logging, "raiseExceptions", True)
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            logging.StreamHandler(_closed()).emit(_record("lost"))
        assert diagnostics.getvalue().startswith("--- Logging error ---")

    def test_emit_drops_the_record_when_no_stream_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A console-less process (pythonw, a Windows service) has none of the
        # three. Raising inside emit would make logging diagnose every single
        # record, which is louder than the outage it is reporting.
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "__stdout__", None)
        monkeypatch.setattr(sys, "stderr", None)
        LiveStdoutHandler().emit(_record("nowhere to go"))


class TestConfigureLogging:
    def test_reconfiguring_leaves_exactly_one_live_handler(self) -> None:
        configure_logging(level="INFO", json_logs=False)
        configure_logging(level="DEBUG", json_logs=False)

        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], LiveStdoutHandler)
        assert root.level == logging.DEBUG

    def test_ensure_configured_installs_a_handler_when_nothing_has(self) -> None:
        logging.getLogger().handlers.clear()
        logging_config._configured = False

        ensure_configured()

        assert isinstance(logging.getLogger().handlers[0], LiveStdoutHandler)

    def test_ensure_configured_is_a_no_op_once_configured(self) -> None:
        configure_logging(level="INFO", json_logs=False)
        handler = logging.getLogger().handlers[0]

        ensure_configured()

        assert logging.getLogger().handlers[0] is handler

    def test_the_console_renderer_survives_a_console_less_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # sys.stderr is None there too, and asking it whether it is a tty used to
        # be an AttributeError during configure rather than a colour preference.
        monkeypatch.setattr(sys, "stderr", None)
        configure_logging(level="INFO", json_logs=False)
        assert len(logging.getLogger().handlers) == 1


class TestContextInjection:
    def test_bound_context_rides_along_on_every_record(self) -> None:
        bind_request_context(request_id="req_1", principal="ops@adoptimizer.dev", run_id="run_1")

        event = _inject_context(logging.getLogger("t"), "info", _event())

        assert event["request_id"] == "req_1"
        assert event["principal"] == "ops@adoptimizer.dev"
        assert event["run_id"] == "run_1"

    def test_injection_never_overwrites_a_key_the_caller_set(self) -> None:
        bind_request_context(run_id="ambient")

        event = _inject_context(
            logging.getLogger("t"), "info", {"event": "e", "run_id": "explicit"}
        )

        assert event["run_id"] == "explicit"

    def test_clearing_removes_what_binding_set(self) -> None:
        bind_request_context(request_id="req_2")
        clear_request_context()

        assert _inject_context(logging.getLogger("t"), "info", _event()) == {"event": "e"}
