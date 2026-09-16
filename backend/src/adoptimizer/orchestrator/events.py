"""Event bus for run progress.

Agents publish typed events; the API layer subscribes and streams them to the
browser over SSE. Subscribers are isolated: a slow or disconnected consumer can
never block or fail a run, which is why each gets a bounded queue with a
drop-oldest policy.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from ..core.ids import ulid
from ..core.logging import get_logger
from ..domain.enums import AgentName

logger = get_logger(__name__)

DEFAULT_QUEUE_SIZE = 512


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One observable step of an optimization run."""

    run_id: str
    seq: int
    event_type: str
    agent: str = AgentName.MONITOR.value
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=ulid)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_sse(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "type": self.event_type,
            "agent": self.agent,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


class EventSink(Protocol):
    """Durable destination for run events."""

    async def persist(self, event: AgentEvent) -> None: ...

    async def forget(self, run_id: str) -> None:
        """Release whatever the sink caches for a run once that run is over."""
        ...


class NullEventSink:
    """Sink used when persistence is disabled, for example in unit tests."""

    async def persist(self, event: AgentEvent) -> None:
        return None

    async def forget(self, run_id: str) -> None:
        return None


class EventBus:
    """Fan-out hub: publishers write once, many subscribers stream."""

    def __init__(
        self, *, queue_size: int = DEFAULT_QUEUE_SIZE, sink: EventSink | None = None
    ) -> None:
        self._queue_size = queue_size
        self._sink = sink or NullEventSink()
        self._subscribers: dict[str, asyncio.Queue[AgentEvent | None]] = {}
        self._counters: dict[str, int] = {}
        self._history: dict[str, list[AgentEvent]] = {}
        self._history_limit = 2000
        self._lock = asyncio.Lock()

    async def next_seq(self, run_id: str) -> int:
        """Allocate a monotonically increasing sequence number per run."""
        async with self._lock:
            current = self._counters.get(run_id, 0) + 1
            self._counters[run_id] = current
            return current

    async def publish(
        self,
        run_id: str,
        event_type: str,
        *,
        agent: str = AgentName.MONITOR.value,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        """Persist an event then fan it out to every subscriber."""
        seq = await self.next_seq(run_id)
        event = AgentEvent(
            run_id=run_id, seq=seq, event_type=event_type, agent=agent, payload=payload or {}
        )

        history = self._history.setdefault(run_id, [])
        history.append(event)
        if len(history) > self._history_limit:
            del history[: len(history) - self._history_limit]

        try:
            await self._sink.persist(event)
        except Exception as exc:  # pragma: no cover - persistence must not break runs
            logger.error("event_persist_failed", run_id=run_id, error=str(exc))

        for subscriber_id, queue in list(self._subscribers.items()):
            if not subscriber_id.startswith(run_id + ":"):
                continue
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _drop_oldest(queue)
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:  # pragma: no cover
                    logger.warning("event_dropped", run_id=run_id, subscriber=subscriber_id)
        return event

    def subscribe(self, run_id: str) -> tuple[str, asyncio.Queue[AgentEvent | None]]:
        """Register a subscriber and return its id and queue."""
        subscriber_id = run_id + ":" + ulid()
        self._subscribers[subscriber_id] = asyncio.Queue(maxsize=self._queue_size)
        return subscriber_id, self._subscribers[subscriber_id]

    def set_sink(self, sink: EventSink) -> None:
        """Attach a durable destination after construction."""
        self._sink = sink

    def unsubscribe(self, subscriber_id: str) -> None:
        """Remove a subscriber when its stream ends."""
        self._subscribers.pop(subscriber_id, None)

    def replay(self, run_id: str, *, after_seq: int = 0) -> list[AgentEvent]:
        """Return buffered events so a reconnecting client can resume."""
        return [e for e in self._history.get(run_id, []) if e.seq > after_seq]

    async def stream(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        idle_timeout: float = 30.0,
        heartbeat_limit: int | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Yield buffered then live events until the run terminates.

        Subscribing before replaying closes the window where an event published
        between the two would be lost; anything the replay already emitted is
        dropped by the ``seen`` cursor rather than delivered twice.

        ``heartbeat_limit`` bounds how long one call may idle. Callers that also
        read a durable event log use it to hand control back periodically and
        re-check whether the run finished elsewhere. Without it, a subscriber in
        a process that is not executing the run would emit keep-alives forever.
        """
        subscriber_id, queue = self.subscribe(run_id)
        seen = after_seq
        heartbeats = 0
        try:
            for event in self.replay(run_id, after_seq=after_seq):
                seen = max(seen, event.seq)
                yield event
                if event.event_type in TERMINAL_EVENTS:
                    return

            while True:
                # A distinct name: `event` is already narrowed to AgentEvent by the
                # replay loop above, and reusing it here would make the sentinel
                # branch look unreachable to the type checker.
                queued: AgentEvent | None
                try:
                    queued = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except TimeoutError:
                    heartbeats += 1
                    yield _heartbeat(run_id)
                    if heartbeat_limit is not None and heartbeats >= heartbeat_limit:
                        return
                    continue
                if queued is None:
                    return
                if queued.seq <= seen:
                    continue
                seen = queued.seq
                yield queued
                if queued.event_type in TERMINAL_EVENTS:
                    return
        finally:
            self.unsubscribe(subscriber_id)

    async def close_run(self, run_id: str) -> None:
        """Signal every subscriber of a run that no more events will arrive.

        Also tells the sink to drop the run. A sink keeps per-run state the bus
        cannot see, and the only state it can clear by itself is the one a
        terminal event carries - so a task that died before publishing anything
        terminal keeps its entry for the life of the process. That is exactly the
        run the reaper marks failed, and the reaper already calls this method,
        which is why the forwarding lives here rather than at each call site.

        A sink that fails to forget is logged, not raised: the run is already
        over and a leaked cache entry must not turn teardown into an error.
        """
        for subscriber_id, queue in list(self._subscribers.items()):
            if subscriber_id.startswith(run_id + ":"):
                queue.put_nowait(None)
        self._history.pop(run_id, None)
        self._counters.pop(run_id, None)
        try:
            await self._sink.forget(run_id)
        except Exception as exc:
            logger.error("event_sink_forget_failed", run_id=run_id, error=str(exc))


TERMINAL_EVENTS = frozenset({"run.succeeded", "run.failed", "run.cancelled"})


class InProcessEventBus(EventBus):
    """Alias kept for readability at construction sites."""


def _drop_oldest(queue: asyncio.Queue[AgentEvent | None]) -> None:
    with contextlib.suppress(asyncio.QueueEmpty):
        queue.get_nowait()


def _heartbeat(run_id: str) -> AgentEvent:
    return AgentEvent(run_id=run_id, seq=-1, event_type="heartbeat", agent="supervisor")
