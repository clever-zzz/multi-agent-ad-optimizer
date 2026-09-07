"""Run cancellation and the SSE progress stream.

The stream is what the operator watches while a run executes, so it has to
replay the durable log, resume from a cursor, and terminate on its own. A stream
that never closes leaks a connection per open dashboard tab.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from adoptimizer.core.container import Container
from adoptimizer.domain.enums import RunStatus
from adoptimizer.repositories.runs import RunRepository

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, access_token, bearer


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


@pytest.fixture
def container(app: FastAPI) -> Container:
    return app.state.container  # type: ignore[no-any-return]


async def run_sync(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        API + "/runs",
        json={"background": False, "max_iterations": 1, "window_days": 7},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "succeeded", run.get("error_message")
    return run


async def make_queued_run(container: Container, *, status: RunStatus = RunStatus.PENDING) -> str:
    """A run row nothing is executing, so cancel and stream are deterministic."""
    async with container.database.unit_of_work() as session:
        run = await RunRepository(session).create(
            campaign_ids=["camp_placeholder"],
            parameters={"window_days": 7},
            max_iterations=1,
            trigger_type="api",
        )
        if status is not RunStatus.PENDING:
            run.status = status.value
        await session.commit()
        return run.id


def parse_sse(body: str) -> list[dict[str, Any]]:
    """Split an SSE body into frames of {field: value}."""
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                fields.setdefault("comments", []).append(line)
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if fields:
            frames.append(fields)
    return frames


def data_payloads(body: str) -> list[dict[str, Any]]:
    """Every data: frame that carries JSON, in stream order."""
    payloads = []
    for frame in parse_sse(body):
        raw = frame.get("data")
        if not raw or raw == "{}":
            continue
        payloads.append(json.loads(raw))
    return payloads


class TestRunStream:
    async def test_a_finished_run_replays_its_whole_timeline_and_closes(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)

        response = await client.get(API + "/runs/" + run["id"] + "/stream", headers=admin)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"

        payloads = data_payloads(response.text)
        assert payloads, "the durable log held events but the stream sent none"
        assert payloads[0]["run_id"] == run["id"]
        sequences = [item["seq"] for item in payloads]
        assert sequences == sorted(sequences)
        assert payloads[-1]["type"] == "run.succeeded"
        # The closing frame is what tells a browser to stop reconnecting.
        assert response.text.rstrip().endswith("event: stream.closed\ndata: {}")

    async def test_every_frame_carries_an_id_and_an_event_name(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        """EventSource needs both to resume and to dispatch by type."""
        run = await run_sync(client, admin)

        response = await client.get(API + "/runs/" + run["id"] + "/stream", headers=admin)

        frames = [frame for frame in parse_sse(response.text) if "data" in frame]
        assert frames
        for frame in frames[:-1]:
            assert frame["id"].isdigit()
            assert frame["event"]
        assert frames[-1]["event"] == "stream.closed"

    async def test_the_stream_honours_the_agent_timeline(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)
        detail = (await client.get(API + "/runs/" + run["id"], headers=admin)).json()

        response = await client.get(API + "/runs/" + run["id"] + "/stream", headers=admin)

        streamed = data_payloads(response.text)
        assert len(streamed) == len(detail["events"])
        assert [item["agent"] for item in streamed] == [
            event["agent"] for event in detail["events"]
        ]

    async def test_last_seq_resumes_without_replaying_the_past(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        """A reconnecting dashboard must not re-render every step."""
        run = await run_sync(client, admin)
        full = data_payloads(
            (await client.get(API + "/runs/" + run["id"] + "/stream", headers=admin)).text
        )
        assert len(full) > 3
        cursor = full[2]["seq"]

        response = await client.get(
            API + "/runs/" + run["id"] + "/stream",
            params={"lastSeq": str(cursor)},
            headers=admin,
        )

        resumed = data_payloads(response.text)
        assert resumed, "resuming past the cursor still owes the remaining events"
        assert all(item["seq"] > cursor for item in resumed)
        assert [item["seq"] for item in resumed] == [
            item["seq"] for item in full if item["seq"] > cursor
        ]
        assert resumed[-1]["type"] == "run.succeeded"

    async def test_a_cursor_past_the_end_closes_immediately(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)

        response = await client.get(
            API + "/runs/" + run["id"] + "/stream", params={"lastSeq": "999999"}, headers=admin
        )

        assert data_payloads(response.text) == []
        assert "stream.closed" in response.text

    async def test_a_non_numeric_cursor_is_ignored_rather_than_rejected(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)

        response = await client.get(
            API + "/runs/" + run["id"] + "/stream", params={"lastSeq": "abc"}, headers=admin
        )

        assert response.status_code == 200
        assert data_payloads(response.text)

    async def test_a_terminal_run_with_no_events_closes_without_hanging(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        """A run cancelled before it started has no timeline to tail."""
        run_id = await make_queued_run(container, status=RunStatus.CANCELLED)

        response = await client.get(API + "/runs/" + run_id + "/stream", headers=admin)

        assert response.status_code == 200
        assert data_payloads(response.text) == []
        assert "stream.closed" in response.text

    async def test_streaming_an_unknown_run_is_a_404(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(API + "/runs/run_missing/stream", headers=admin)

        assert response.status_code == 404
        assert response.headers["content-type"] == "application/problem+json"

    async def test_the_stream_requires_authentication(self, client: httpx.AsyncClient) -> None:
        response = await client.get(API + "/runs/run_any/stream")

        assert response.status_code == 401

    async def test_a_read_only_role_may_watch_a_run(
        self, client: httpx.AsyncClient, make_user: Any, admin: dict[str, str]
    ) -> None:
        """Watching is read-only, so an analyst dashboard must not be blocked."""
        analyst = await make_user("analyst")
        run = await run_sync(client, admin)

        response = await client.get(
            API + "/runs/" + run["id"] + "/stream", headers=analyst["headers"]
        )

        assert response.status_code == 200
        assert data_payloads(response.text)


class TestRunCancellation:
    async def test_a_queued_run_can_be_cancelled(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        run_id = await make_queued_run(container)

        response = await client.post(API + "/runs/" + run_id + "/cancel", headers=admin)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == run_id
        assert body["status"] == "cancelled"
        assert body["finished_at"]
        assert "Cancelled by" in str(body["error_message"])

    async def test_cancelling_writes_an_audit_entry(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        run_id = await make_queued_run(container)

        await client.post(API + "/runs/" + run_id + "/cancel", headers=admin)
        entries = (await client.get(API + "/admin/audit", headers=admin)).json()

        recorded = [
            item
            for item in entries["items"]
            if item["action"] == "run.cancelled" and item["resource_id"] == run_id
        ]
        assert recorded, "a cancellation without an audit trail is not defensible"

    async def test_cancelling_twice_is_a_conflict(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        run_id = await make_queued_run(container)
        first = await client.post(API + "/runs/" + run_id + "/cancel", headers=admin)
        assert first.status_code == 200

        second = await client.post(API + "/runs/" + run_id + "/cancel", headers=admin)

        assert second.status_code == 409
        assert "already finished" in second.json()["detail"]

    async def test_a_finished_run_cannot_be_cancelled(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        run = await run_sync(client, admin)

        response = await client.post(API + "/runs/" + run["id"] + "/cancel", headers=admin)

        assert response.status_code == 409
        assert (await client.get(API + "/runs/" + run["id"], headers=admin)).json()["run"][
            "status"
        ] == "succeeded"

    async def test_cancelling_an_unknown_run_is_a_404(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.post(API + "/runs/run_missing/cancel", headers=admin)

        assert response.status_code == 404

    async def test_a_read_only_role_cannot_cancel(
        self, client: httpx.AsyncClient, make_user: Any, container: Container
    ) -> None:
        viewer = await make_user("viewer")
        run_id = await make_queued_run(container)

        response = await client.post(API + "/runs/" + run_id + "/cancel", headers=viewer["headers"])

        assert response.status_code == 403

    async def test_cancellation_reaches_the_live_stream(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        """The dashboard must see the run stop, not spin until it times out."""
        run_id = await make_queued_run(container)
        await client.post(API + "/runs/" + run_id + "/cancel", headers=admin)

        response = await client.get(API + "/runs/" + run_id + "/stream", headers=admin)

        assert response.status_code == 200
        assert "stream.closed" in response.text


class TestRunListing:
    async def test_the_status_filter_restricts_the_page(
        self, client: httpx.AsyncClient, admin: dict[str, str], container: Container
    ) -> None:
        completed = await run_sync(client, admin)
        queued = await make_queued_run(container)

        succeeded = (
            await client.get(API + "/runs", params={"status": "succeeded"}, headers=admin)
        ).json()
        pending = (
            await client.get(API + "/runs", params={"status": "pending"}, headers=admin)
        ).json()

        assert [item["id"] for item in succeeded["items"]] == [completed["id"]]
        assert succeeded["total"] == 1
        assert [item["id"] for item in pending["items"]] == [queued]

    async def test_an_unknown_status_is_rejected(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(API + "/runs", params={"status": "nonsense"}, headers=admin)

        assert response.status_code == 422

    async def test_an_empty_history_pages_cleanly(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        body = (await client.get(API + "/runs", headers=admin)).json()

        assert body["items"] == []
        assert body["total"] == 0
        assert body["page"] == 1
