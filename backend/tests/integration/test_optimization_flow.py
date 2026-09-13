"""End-to-end optimisation loop: run the agents, then approve and execute.

This is the behavioural heart of the product, so it covers the human-in-the-loop
gate as well as the happy path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from adoptimizer.core.config import Settings
from adoptimizer.core.errors import ValidationFailure
from adoptimizer.infra.db.models import LLMSpendRecord, OptimizationRun
from adoptimizer.infra.db.session import Database
from adoptimizer.repositories.runs import RunRepository
from adoptimizer.services.optimization import OptimizationService

from ..conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API, access_token, bearer

# Every step of the loop must emit at least one event, so a silently skipped
# agent fails here rather than showing up as a gap on the run timeline.
LOOP_AGENTS = {"monitor", "audience", "creative", "bidding", "optimize", "critic"}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return bearer(await access_token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


async def run_sync(
    client: httpx.AsyncClient, headers: dict[str, str], **payload: Any
) -> dict[str, Any]:
    """Trigger a run and block until it finishes."""
    body: dict[str, Any] = {"background": False, "max_iterations": 1, "window_days": 7}
    body.update(payload)
    response = await client.post(API + "/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "succeeded", run.get("error_message")
    return run


@pytest.fixture
async def completed_run(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    return await run_sync(client, admin)


class TestRunLifecycle:
    async def test_run_succeeds_with_the_mock_stack(self, completed_run: dict[str, Any]) -> None:
        assert completed_run["status"] == "succeeded"
        assert completed_run["id"].startswith("run_")
        assert completed_run["trigger_type"] == "api"
        assert completed_run["started_at"] and completed_run["finished_at"]
        assert completed_run["error_message"] is None

    async def test_run_covers_the_seeded_portfolio(self, completed_run: dict[str, Any]) -> None:
        assert len(completed_run["campaign_ids"]) == 8
        assert completed_run["max_iterations"] == 1
        assert completed_run["iteration"] >= 1

    async def test_summary_reports_what_the_agents_did(self, completed_run: dict[str, Any]) -> None:
        summary = completed_run["summary"]
        assert summary["status"] == "succeeded"
        assert summary["campaigns"] == 8
        assert summary["creatives_generated"] > 0
        assert summary["bidding_decisions"] > 0
        assert summary["budget_adjustments"] > 0
        assert summary["actions"] > 0
        assert summary["alerts_raised"] > 0

    async def test_the_seeded_data_is_designed_to_produce_work(
        self, completed_run: dict[str, Any]
    ) -> None:
        """A run that finds nothing cannot prove the loop works."""
        counts = completed_run["summary"]["action_counts"]
        assert sum(counts.values()) > 0
        assert counts

    async def test_no_campaign_holds_two_contradictory_proposals(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        """The optimizer fires one proposal per rule; the critic reconciles them.

        Without the critic every seeded campaign ended a run holding both
        `pause_campaign` and a spend change, which asked an operator to approve
        two mutually exclusive outcomes for the same campaign. The run detail now
        also carries the withheld proposals, so the check is on what survived -
        the actionable queue - rather than on every row ever proposed.
        """
        detail = (await client.get(API + "/runs/" + completed_run["id"], headers=admin)).json()
        spend = {"adjust_budget", "adjust_bid"}
        actions = [a for a in detail["actions"] if a["status"] != "suppressed"]
        paused = {a["campaign_id"] for a in actions if a["action_type"] == "pause_campaign"}
        tuned = {a["campaign_id"] for a in actions if a["action_type"] in spend}
        assert paused.isdisjoint(tuned), "contradictions survived: " + str(sorted(paused & tuned))

    async def test_the_critic_shrinks_the_approval_queue(
        self, completed_run: dict[str, Any]
    ) -> None:
        """The seeded portfolio is deliberately conflicted, so this must bite."""
        summary = completed_run["summary"]
        assert summary["actions_proposed"] >= summary["actions"]
        assert summary["actions_suppressed"] == summary["actions_proposed"] - summary["actions"]
        assert summary["critic_findings"] > 0
        assert summary["actions_suppressed"] > 0

    async def test_the_critic_reviews_the_proposals_the_optimizer_just_made(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        detail = (await client.get(API + "/runs/" + completed_run["id"], headers=admin)).json()
        started = {
            event["agent"]: event["seq"]
            for event in detail["events"]
            if event["event_type"] == "agent.started"
        }
        assert started["critic"] > started["optimize"]

        verdicts = [event for event in detail["events"] if event["agent"] == "critic"]
        completed = [event for event in verdicts if event["event_type"] == "agent.completed"]
        assert completed, "the critic never reported a verdict"
        assert completed[-1]["payload"]["summary"]["suppressed"] >= 0

    async def test_run_detail_exposes_the_agent_timeline(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        response = await client.get(API + "/runs/" + completed_run["id"], headers=admin)
        assert response.status_code == 200
        detail = response.json()
        assert detail["run"]["id"] == completed_run["id"]
        assert len(detail["events"]) >= len(LOOP_AGENTS)
        agents = {event["agent"] for event in detail["events"]}
        assert agents >= LOOP_AGENTS
        # The run detail is the full record, so it carries every proposal the
        # optimizer made - including the ones the critic withheld. The summary's
        # `actions` is the post-review count, and `actions_proposed` is the raw
        # one; the detail matches the raw count because suppression is a mark.
        assert len(detail["actions"]) == completed_run["summary"]["actions_proposed"]
        assert (
            len([a for a in detail["actions"] if a["status"] == "proposed"])
            == completed_run["summary"]["actions"]
        )
        assert len(detail["allocations"]) == completed_run["summary"]["budget_adjustments"]

    async def test_events_are_ordered_by_sequence(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        detail = (await client.get(API + "/runs/" + completed_run["id"], headers=admin)).json()
        sequences = [event["seq"] for event in detail["events"]]
        assert sequences == sorted(sequences)
        assert sequences[0] >= 0

    async def test_run_list_is_paginated_and_filterable(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (await client.get(API + "/runs", headers=admin)).json()
        assert page["total"] >= 1
        assert any(run["id"] == completed_run["id"] for run in page["items"])

        succeeded = (
            await client.get(API + "/runs", params={"status": "succeeded"}, headers=admin)
        ).json()
        assert all(run["status"] == "succeeded" for run in succeeded["items"])

        failed = (
            await client.get(API + "/runs", params={"status": "failed"}, headers=admin)
        ).json()
        assert failed["total"] == 0

    async def test_unknown_run_is_a_problem_document(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(API + "/runs/run_does_not_exist", headers=admin)
        assert response.status_code == 404
        body = response.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404

    async def test_iteration_cap_is_enforced(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.post(
            API + "/runs", json={"background": True, "max_iterations": 999}, headers=admin
        )
        assert response.status_code == 422


class TestIdempotency:
    async def test_the_same_key_returns_the_same_run(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        headers = {**admin, "Idempotency-Key": "probe-key-0001"}
        first = await client.post(
            API + "/runs",
            json={"background": True, "campaign_ids": ["campaign_missing"]},
            headers=headers,
        )
        # The probe campaign does not exist, so the request is rejected before a
        # run is created; repeat it to prove the key is at least accepted twice.
        assert first.status_code in {202, 409}
        second = await client.post(
            API + "/runs",
            json={"background": True, "campaign_ids": ["campaign_missing"]},
            headers=headers,
        )
        assert second.status_code == first.status_code

    async def test_a_replayed_key_does_not_create_a_second_run(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        headers = {**admin, "Idempotency-Key": "probe-key-0002"}
        first = await run_sync(client, headers, max_iterations=1)
        second = await client.post(
            API + "/runs", json={"background": False, "max_iterations": 1}, headers=headers
        )
        assert second.status_code == 202
        assert second.json()["id"] == first["id"]
        page = (await client.get(API + "/runs", headers=admin)).json()
        assert page["total"] == 1

    async def test_concurrent_replays_create_exactly_one_run(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        """Two callers racing one key must not each walk away with a run.

        `start_run` looks the key up before it inserts, so both callers can miss.
        The unique index is what actually arbitrates; without it this produces two
        runs and the platform gets asked to do the same work twice.
        """
        headers = {**admin, "Idempotency-Key": "probe-key-race"}
        payload = {"background": False, "max_iterations": 1}
        first, second = await asyncio.gather(
            client.post(API + "/runs", json=payload, headers=headers),
            client.post(API + "/runs", json=payload, headers=headers),
        )
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert first.json()["id"] == second.json()["id"]
        page = (await client.get(API + "/runs", headers=admin)).json()
        assert page["total"] == 1

    async def test_the_index_arbitrates_when_the_lookup_misses(
        self, client: httpx.AsyncClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drive the IntegrityError branch on purpose.

        A genuine race is timing-dependent, so blind the first lookup instead. The
        replay then reaches the insert, loses, and must still come back holding the
        original run rather than a 500 or a second row.
        """
        headers = {**admin, "Idempotency-Key": "probe-key-blind"}
        first = await run_sync(client, headers, max_iterations=1)

        original = RunRepository.find_by_idempotency_key
        calls = {"count": 0}

        async def blind_once(
            self: RunRepository, key: str, actor_id: str
        ) -> OptimizationRun | None:
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return await original(self, key, actor_id)

        monkeypatch.setattr(RunRepository, "find_by_idempotency_key", blind_once)
        second = await client.post(
            API + "/runs", json={"background": False, "max_iterations": 1}, headers=headers
        )

        assert calls["count"] >= 2, "the loser must look the key up again after rolling back"
        assert second.status_code == 202, second.text
        assert second.json()["id"] == first["id"]
        page = (await client.get(API + "/runs", headers=admin)).json()
        assert page["total"] == 1

    async def test_a_key_with_no_actor_is_refused(self, app: FastAPI) -> None:
        """An unscoped key would make the guarantee silently inoperative.

        The index is (key, actor) and SQL treats NULLs as distinct, so a key sent
        by nobody is never deduplicated. Refusing beats pretending otherwise - a
        scheduler firing on a tick is exactly the caller that would trip here.
        """
        container = app.state.container
        async with container.database.unit_of_work() as session:
            with pytest.raises(ValidationFailure):
                await OptimizationService(container, session).start_run(
                    session, idempotency_key="orphan-key", actor_id=None
                )


class TestApprovalGate:
    """Nothing reaches an ad platform without an explicit human decision."""

    async def test_actions_are_proposed_not_applied(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions", params={"run_id": completed_run["id"]}, headers=admin
            )
        ).json()
        assert page["total"] > 0
        assert all(action["status"] == "proposed" for action in page["items"])
        assert all(action["executed_at"] is None for action in page["items"])

    async def test_execute_before_approval_is_refused(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])
        response = await client.post(API + "/actions/" + action_id + "/execute", headers=admin)
        # The caller holds action:execute, so an unapproved action is a state
        # conflict, not an authorisation failure. Documented in 03-api-reference.
        assert response.status_code == 409
        refused = response.json()
        assert refused["code"] == "approval_required"
        assert "must be approved before execution" in refused["detail"]

    async def test_approve_then_execute(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])

        approved = await client.post(API + "/actions/" + action_id + "/approve", headers=admin)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["approved_by"]

        executed = await client.post(API + "/actions/" + action_id + "/execute", headers=admin)
        assert executed.status_code == 200, executed.text
        assert executed.json()["status"] == "executed"
        assert executed.json()["executed_at"]

    async def test_executing_twice_conflicts(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])
        await client.post(API + "/actions/" + action_id + "/approve", headers=admin)
        assert (
            await client.post(API + "/actions/" + action_id + "/execute", headers=admin)
        ).status_code == 200
        again = await client.post(API + "/actions/" + action_id + "/execute", headers=admin)
        assert again.status_code == 409

    async def test_approving_twice_conflicts(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])
        assert (
            await client.post(API + "/actions/" + action_id + "/approve", headers=admin)
        ).status_code == 200
        again = await client.post(API + "/actions/" + action_id + "/approve", headers=admin)
        assert again.status_code == 409

    async def test_reject_records_the_reason(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])
        response = await client.post(
            API + "/actions/" + action_id + "/reject",
            json={"reason": "Budget freeze this quarter"},
            headers=admin,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"

        executed = await client.post(API + "/actions/" + action_id + "/execute", headers=admin)
        assert executed.status_code == 409
        assert executed.json()["code"] == "conflict"

    async def test_bulk_approve(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions", params={"run_id": completed_run["id"]}, headers=admin
            )
        ).json()
        ids = [action["id"] for action in page["items"]][:3]
        response = await client.post(API + "/actions/bulk", json={"action_ids": ids}, headers=admin)
        assert response.status_code == 200
        body = response.json()
        assert sorted(body["approved"]) == sorted(ids)
        assert body["executed"] == []

    async def test_bulk_approve_and_execute(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions", params={"run_id": completed_run["id"]}, headers=admin
            )
        ).json()
        ids = [action["id"] for action in page["items"]][:2]
        response = await client.post(
            API + "/actions/bulk", json={"action_ids": ids, "execute": True}, headers=admin
        )
        assert response.status_code == 200
        body = response.json()
        assert sorted(body["executed"]) == sorted(ids)

    async def test_bulk_filters_on_confidence(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions", params={"run_id": completed_run["id"]}, headers=admin
            )
        ).json()
        ids = [action["id"] for action in page["items"]]
        response = await client.post(
            API + "/actions/bulk", json={"action_ids": ids, "min_confidence": 0.99}, headers=admin
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body["approved"]) <= set(ids)
        # ``skipped`` carries {id, reason} objects, not bare ids, and every member
        # of the batch lands in exactly one of the two lists.
        skipped = body["skipped"]
        assert {entry["id"] for entry in skipped} == set(ids) - set(body["approved"])
        assert all(entry["reason"] for entry in skipped)

    async def test_action_filters(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(API + "/actions", params={"status": "proposed"}, headers=admin)
        ).json()
        assert all(action["status"] == "proposed" for action in page["items"])

        by_type = (
            await client.get(API + "/actions", params={"type": "adjust_budget"}, headers=admin)
        ).json()
        assert all(action["action_type"] == "adjust_budget" for action in by_type["items"])

    async def test_unknown_action_is_404(
        self, client: httpx.AsyncClient, admin: dict[str, str]
    ) -> None:
        response = await client.get(API + "/actions/action_missing", headers=admin)
        assert response.status_code == 404


async def first_action_id(client: httpx.AsyncClient, headers: dict[str, str], run_id: str) -> str:
    page = (await client.get(API + "/actions", params={"run_id": run_id}, headers=headers)).json()
    assert page["items"], "the seeded dataset should always produce at least one proposal"
    return str(page["items"][0]["id"])


class TestCriticFindingsAreDurable:
    """The critic's verdicts must outlive the process that produced them.

    Withholding used to exist only in the run's in-memory graph state, so the
    docstring's promise - that an operator can disagree with the critic and
    approve a suppressed proposal anyway - was not actually reachable: the
    proposal had no row to approve, and the reason had nowhere to live.
    """

    async def test_withheld_proposals_are_persisted_with_their_own_status(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        detail = (await client.get(API + "/runs/" + completed_run["id"], headers=admin)).json()
        suppressed = [action for action in detail["actions"] if action["status"] == "suppressed"]
        assert suppressed, "the seeded portfolio is deliberately conflicted"
        assert completed_run["summary"]["actions_suppressed"] == len(suppressed)

    async def test_the_approval_queue_hides_what_the_critic_withheld(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions", params={"run_id": completed_run["id"]}, headers=admin
            )
        ).json()
        assert page["total"] > 0
        assert all(action["status"] == "proposed" for action in page["items"])

    async def test_suppressed_proposals_are_listable_on_request(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions",
                params={"status": "suppressed", "run_id": completed_run["id"]},
                headers=admin,
            )
        ).json()
        assert page["total"] > 0
        assert all(action["status"] == "suppressed" for action in page["items"])

    async def test_findings_endpoint_explains_each_withholding(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        findings = (
            await client.get(API + "/runs/" + completed_run["id"] + "/findings", headers=admin)
        ).json()
        assert len(findings) == completed_run["summary"]["critic_findings"]
        assert all(finding["kind"] for finding in findings)
        assert all(finding["reason"] for finding in findings)
        assert [action_id for f in findings for action_id in f["suppressed_action_ids"]]

    async def test_the_summary_names_the_rules_that_withheld(
        self, completed_run: dict[str, Any]
    ) -> None:
        summary = completed_run["summary"]
        by_kind = summary["critic_findings_by_kind"]
        assert by_kind
        assert sum(by_kind.values()) == summary["critic_findings"]

    async def test_an_operator_can_overrule_the_critic(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        """The escape hatch the "mark rather than delete" design exists for."""
        page = (
            await client.get(
                API + "/actions",
                params={"status": "suppressed", "run_id": completed_run["id"]},
                headers=admin,
            )
        ).json()
        assert page["items"], "nothing was suppressed, so the override cannot be exercised"
        action_id = page["items"][0]["id"]

        approved = await client.post(API + "/actions/" + action_id + "/approve", headers=admin)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"

    async def test_the_override_is_flagged_in_the_audit_trail(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/actions",
                params={"status": "suppressed", "run_id": completed_run["id"]},
                headers=admin,
            )
        ).json()
        action_id = page["items"][0]["id"]
        await client.post(API + "/actions/" + action_id + "/approve", headers=admin)

        audit = (
            await client.get(
                API + "/admin/audit", params={"action": "action.approved"}, headers=admin
            )
        ).json()
        entry = next(item for item in audit["items"] if item["resource_id"] == action_id)
        assert entry["after"]["overruled_critic"] is True


class TestAlertsFromARun:
    async def test_detective_alerts_are_persisted(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        assert completed_run["summary"]["alerts_raised"] > 0
        page = (await client.get(API + "/alerts", headers=admin)).json()
        assert page["total"] > 0
        assert all(
            alert["status"] in {"open", "acknowledged", "resolved"} for alert in page["items"]
        )

    async def test_alert_summary_counts_by_severity(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        summary = (await client.get(API + "/alerts/summary", headers=admin)).json()
        assert isinstance(summary, dict)
        assert summary

    async def test_acknowledge_then_resolve(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (await client.get(API + "/alerts", params={"status": "open"}, headers=admin)).json()
        assert page["items"]
        alert_id = page["items"][0]["id"]

        acked = await client.post(
            API + "/alerts/" + alert_id + "/acknowledge", json={}, headers=admin
        )
        assert acked.status_code == 200
        assert acked.json()["status"] == "acknowledged"

        resolved = await client.post(
            API + "/alerts/" + alert_id + "/resolve", json={}, headers=admin
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"


class TestAuditTrail:
    async def test_every_state_change_is_recorded(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        action_id = await first_action_id(client, admin, completed_run["id"])
        await client.post(API + "/actions/" + action_id + "/approve", headers=admin)

        page = (await client.get(API + "/admin/audit", headers=admin)).json()
        actions = {entry["action"] for entry in page["items"]}
        assert "run.started" in actions
        assert "action.approved" in actions

    async def test_audit_entries_carry_the_actor(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(API + "/admin/audit", params={"action": "run.started"}, headers=admin)
        ).json()
        assert page["items"]
        entry = page["items"][0]
        assert entry["actor_email"] == ADMIN_EMAIL
        assert entry["resource_id"] == completed_run["id"]

    async def test_audit_can_be_filtered_by_resource_type(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        page = (
            await client.get(
                API + "/admin/audit", params={"resource_type": "optimization_run"}, headers=admin
            )
        ).json()
        assert all(entry["resource_type"] == "optimization_run" for entry in page["items"])


class TestAnalyticsAfterARun:
    async def test_overview_reports_portfolio_kpis(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        body = (await client.get(API + "/analytics/overview", headers=admin)).json()
        assert body
        assert isinstance(body, dict)

    async def test_snapshots_are_ordered_by_spend(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        rows = (await client.get(API + "/analytics/snapshots", headers=admin)).json()
        assert len(rows) >= 2, "the seeded dataset covers eight campaigns over 21 days"
        spends = [row["total_cost"] for row in rows]
        assert spends == sorted(spends, reverse=True)
        # Derived metrics survive serialization (a demo regression).
        assert all("ctr" in row and "roas" in row and "cpa" in row for row in rows)

    async def test_timeseries_is_daily(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        rows = (
            await client.get(API + "/analytics/timeseries", params={"days": 7}, headers=admin)
        ).json()
        assert isinstance(rows, list)

    async def test_llm_spend_is_tracked(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        body = (await client.get(API + "/analytics/llm-spend", headers=admin)).json()
        assert isinstance(body, dict)

    async def test_the_mock_gateway_reports_zero_cost(
        self, client: httpx.AsyncClient, admin: dict[str, str], completed_run: dict[str, Any]
    ) -> None:
        assert completed_run["llm_cost_usd"] == 0.0

    async def test_the_run_usage_matches_the_spend_ledger(
        self,
        client: httpx.AsyncClient,
        admin: dict[str, str],
        settings: Settings,
        completed_run: dict[str, Any],
    ) -> None:
        """The run reports the tokens the ledger billed, never zeros.

        The console used to show 0 tokens for a run that had genuinely called
        the model, because nothing folded gateway usage into the run summary.
        Reading `llm_spend` back is the only assertion that catches that class
        of bug: it compares what was persisted against what was billed.
        """
        run_id = completed_run["id"]
        database = Database(settings.database)
        try:
            async with database.session() as session:
                rows = (
                    await session.execute(
                        select(
                            LLMSpendRecord.prompt_tokens,
                            LLMSpendRecord.completion_tokens,
                            LLMSpendRecord.cost_usd,
                        ).where(LLMSpendRecord.run_id == run_id)
                    )
                ).all()
        finally:
            await database.engine.dispose()

        assert rows, "a completed loop bills at least one model call"
        detail = (await client.get(API + "/runs/" + run_id, headers=admin)).json()["run"]
        assert detail["prompt_tokens"] == sum(row.prompt_tokens for row in rows) > 0
        assert detail["completion_tokens"] == sum(row.completion_tokens for row in rows) > 0
        assert detail["llm_cost_usd"] == pytest.approx(sum(row.cost_usd for row in rows))
        # The summary the UI renders has to carry the same numbers as the columns.
        usage = detail["summary"]["usage"]
        assert usage["prompt_tokens"] == detail["prompt_tokens"]
        assert usage["completion_tokens"] == detail["completion_tokens"]
