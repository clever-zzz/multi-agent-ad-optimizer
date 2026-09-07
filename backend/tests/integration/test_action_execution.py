"""Execution of approved actions: the only code path that moves real money.

These tests drive ``ActionService`` directly against an in-memory database and
the recording mock adapter. Every branch of ``_apply`` is asserted here rather
than reached incidentally by an end-to-end run, because a wrong budget or bid
write is the most expensive bug this product can have.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from adoptimizer.core.config import DatabaseSettings, SecuritySettings
from adoptimizer.core.errors import (
    ActionRequiresApprovalError,
    ConflictError,
    ExternalServiceError,
    NotFoundError,
    PermissionDeniedError,
)
from adoptimizer.core.security import Role, TokenClaims
from adoptimizer.domain.enums import (
    ActionStatus,
    ActionType,
    CampaignStatus,
    CreativeStatus,
    Platform,
)
from adoptimizer.domain.statistics import required_sample_size
from adoptimizer.infra.ads.base import AdsPlatformClient
from adoptimizer.infra.ads.mock import MockAdsClient
from adoptimizer.infra.db.models import ABTest, AuditLog, Campaign, OptimizationAction, User
from adoptimizer.infra.db.session import Database
from adoptimizer.repositories.campaigns import CampaignRepository, CreativeRepository
from adoptimizer.services.actions import ActionService, _parse_float

OPERATOR_ID = "usr_operator"


class StubRegistry:
    """Stands in for PlatformRegistry: hands out one client or fails loudly."""

    def __init__(self, client: AdsPlatformClient | None = None) -> None:
        self._client = client

    def for_platform(self, platform: Platform | str) -> AdsPlatformClient:
        if self._client is None:
            raise ExternalServiceError("No adapter registered for platform " + str(platform))
        return self._client


def make_claims(role: Role = Role.ADMIN, subject: str = OPERATOR_ID) -> TokenClaims:
    """Claims for a role, without paying for a real token round trip."""
    now = datetime.now(UTC)
    return TokenClaims(
        subject=subject,
        email=role.value + ".tester@adoptimizer.dev",
        role=role,
        session_id="ses_service_test",
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
    )


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """A private in-memory SQLite schema, far cheaper than booting the app."""
    instance = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await instance.create_all()
    yield instance
    await instance.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[Any]:
    async with database.unit_of_work() as unit:
        yield unit


@pytest.fixture
async def operator(session: Any) -> User:
    """A user row so the approved_by foreign key resolves."""
    account = User(
        id=OPERATOR_ID,
        email="operator@adoptimizer.dev",
        full_name="Operator",
        hashed_password="not-a-real-hash",
        role=Role.ADMIN.value,
    )
    session.add(account)
    await session.flush()
    return account


@pytest.fixture
def adapter() -> MockAdsClient:
    return MockAdsClient(Platform.MOCK)


@pytest.fixture
def security() -> SecuritySettings:
    return SecuritySettings(argon2_time_cost=1, argon2_memory_cost_kib=8192)


def build_service(
    session: Any, *, adapter: AdsPlatformClient | None, security: SecuritySettings
) -> ActionService:
    return ActionService(session, platforms=StubRegistry(adapter), security=security)


@pytest.fixture
async def campaign(session: Any, operator: User) -> Campaign:
    """A live campaign that also exists on the ad network."""
    return await CampaignRepository(session).create(
        name="Service test campaign",
        platform=Platform.MOCK,
        daily_budget=1000.0,
        total_budget=30000.0,
        target_cpa=80.0,
        target_roas=2.5,
        start_date=date(2026, 1, 1),
        external_id="ext_camp_1",
        status=CampaignStatus.ACTIVE,
        created_by=operator.id,
    )


async def add_creative(
    session: Any,
    campaign_id: str,
    *,
    ab_group: str = "control",
    status: CreativeStatus = CreativeStatus.ACTIVE,
    headline: str = "Headline",
) -> Any:
    return await CreativeRepository(session).create(
        campaign_id=campaign_id,
        headline=headline,
        description="body",
        ab_group=ab_group,
        status=status,
        origin="agent",
    )


async def add_action(
    session: Any,
    campaign_id: str,
    action_type: ActionType,
    *,
    after_value: str = "",
    status: ActionStatus = ActionStatus.PROPOSED,
    creative_id: str | None = None,
    confidence: float = 0.8,
    reason: str = "proposed by the optimizer",
    action_id: str | None = None,
) -> OptimizationAction:
    action = OptimizationAction(
        id=action_id or "act_" + action_type.value + "_" + str(confidence).replace(".", ""),
        campaign_id=campaign_id,
        creative_id=creative_id,
        action_type=action_type.value,
        status=status.value,
        before_value="",
        after_value=after_value,
        reason=reason,
        confidence=confidence,
        proposed_by="optimize",
    )
    session.add(action)
    await session.flush()
    return action


async def audit_actions(session: Any, resource_id: str) -> list[str]:
    """Every audit action name recorded against one resource, oldest first.

    AuditRepository adds rows without flushing so a failed request cannot write
    a half entry, which means the session runs with autoflush off and the rows
    only reach the database at commit. Flushing here mirrors that.
    """
    await session.flush()
    statement = (
        select(AuditLog)
        .where(AuditLog.resource_id == resource_id)
        .order_by(AuditLog.created_at.asc())
    )
    rows = (await session.execute(statement)).scalars().all()
    return [row.action for row in rows]


class TestValueParsing:
    """Agent output is prose; the executor must not guess at a number."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1200", 1200.0),
            ("1200.50", 1200.5),
            ("¥1,200.50", 1200.5),
            ("$2,000", 2000.0),
            ("  850  ", 850.0),
        ],
    )
    def test_currency_and_separators_are_stripped(self, raw: str, expected: float) -> None:
        assert _parse_float(raw) == expected

    @pytest.mark.parametrize("raw", ["", "n/a", "increase", "twelve hundred", "-"])
    def test_non_numeric_values_are_rejected_not_defaulted(self, raw: str) -> None:
        assert _parse_float(raw) is None


class TestApprovalGate:
    async def test_execute_before_approval_is_a_state_conflict(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        """The caller holds action:execute, so this is 409 and never 403."""
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(session, adapter=adapter, security=security)

        with pytest.raises(ActionRequiresApprovalError):
            await service.execute(action.id, claims=make_claims())

        assert action.status == ActionStatus.PROPOSED.value
        assert campaign.daily_budget == 1000.0
        assert adapter.calls == []

    async def test_autonomous_mode_auto_approves_a_proposal(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient
    ) -> None:
        """With the gate off, a proposal is approved and executed in one call."""
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(
            session, adapter=adapter, security=SecuritySettings(require_action_approval=False)
        )

        executed, result = await service.execute(action.id, claims=make_claims())

        assert executed.status == ActionStatus.EXECUTED.value
        assert executed.approved_by == OPERATOR_ID
        assert executed.approved_at is not None
        assert result is not None and result.success
        assert campaign.daily_budget == 1500.0

    async def test_replaying_an_executed_action_is_rejected(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())
        await service.execute(action.id, claims=make_claims())
        calls_after_first = len(adapter.calls)

        with pytest.raises(ConflictError):
            await service.execute(action.id, claims=make_claims())

        assert len(adapter.calls) == calls_after_first

    async def test_a_rejected_action_can_never_be_executed(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.reject(action.id, claims=make_claims(), reason="budget freeze")

        with pytest.raises(ConflictError):
            await service.execute(action.id, claims=make_claims())

        assert adapter.calls == []
        assert campaign.daily_budget == 1000.0

    async def test_a_role_without_execute_rights_is_refused(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        with pytest.raises(PermissionDeniedError):
            await service.execute(action.id, claims=make_claims(Role.ANALYST))

        assert action.status == ActionStatus.APPROVED.value
        assert adapter.calls == []

    async def test_approve_twice_is_a_conflict(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.ADJUST_BID, after_value="12")
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        with pytest.raises(ConflictError):
            await service.approve(action.id, claims=make_claims())

    async def test_reject_appends_the_operator_reason(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.ADJUST_BID, after_value="12")
        service = build_service(session, adapter=None, security=security)

        rejected = await service.reject(action.id, claims=make_claims(), reason="CPA is fine")

        assert rejected.status == ActionStatus.REJECTED.value
        assert "Rejected: CPA is fine" in rejected.reason
        assert await audit_actions(session, action.id) == ["action.rejected"]

    async def test_rejecting_an_executed_action_is_a_conflict(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1500"
        )
        service = build_service(session, adapter=MockAdsClient(), security=security)
        await service.approve(action.id, claims=make_claims())
        await service.execute(action.id, claims=make_claims())

        with pytest.raises(ConflictError):
            await service.reject(action.id, claims=make_claims())

    async def test_a_rejection_reason_is_truncated(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.ADJUST_BID, after_value="12")
        service = build_service(session, adapter=None, security=security)

        rejected = await service.reject(action.id, claims=make_claims(), reason="z" * 900)

        assert len(rejected.reason) < 1000
        assert rejected.reason.endswith("z")


class TestBudgetExecution:
    async def test_a_budget_change_is_written_locally_and_pushed(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="¥1,875.25"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert campaign.daily_budget == 1875.25
        assert executed.status == ActionStatus.EXECUTED.value
        assert executed.executed_at is not None
        assert executed.error_message is None
        assert result is not None
        assert executed.external_reference == result.external_reference
        assert adapter.calls == [
            {
                "operation": "update_budget",
                "sequence": 1,
                "external_id": "ext_camp_1",
                "daily_budget": 1875.25,
            }
        ]
        assert await audit_actions(session, action.id) == ["action.approved", "action.executed"]

    async def test_the_local_ledger_and_the_network_receive_the_same_rounded_budget(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        """A float artefact must never make the two systems of record disagree."""
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1234.5678"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())
        await service.execute(action.id, claims=make_claims())

        assert campaign.daily_budget == 1234.57
        assert adapter.calls[0]["daily_budget"] == 1234.57

    @pytest.mark.parametrize("value", ["n/a", "", "0", "-50", "increase by 10%"])
    async def test_an_unusable_budget_value_fails_the_action(
        self,
        session: Any,
        campaign: Campaign,
        adapter: MockAdsClient,
        security: SecuritySettings,
        value: str,
    ) -> None:
        """Ambiguous prose is refused, never coerced into a spend change."""
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value=value, action_id="act_bad"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        with pytest.raises(ConflictError):
            await service.execute(action.id, claims=make_claims())

        assert action.status == ActionStatus.FAILED.value
        assert action.error_message is not None
        assert "no usable budget value" in action.error_message
        assert campaign.daily_budget == 1000.0
        assert adapter.calls == []
        assert "action.failed" in await audit_actions(session, action.id)

    async def test_a_campaign_unknown_to_the_network_is_still_updated_locally(
        self, session: Any, operator: User, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        """Local state is the system of record; the push is best effort."""
        local_only = await CampaignRepository(session).create(
            name="Local only",
            platform=Platform.MOCK,
            daily_budget=500.0,
            total_budget=5000.0,
            target_cpa=80.0,
            target_roas=2.0,
            start_date=date(2026, 1, 1),
            external_id=None,
            created_by=operator.id,
        )
        action = await add_action(
            session, local_only.id, ActionType.ADJUST_BUDGET, after_value="750"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert local_only.daily_budget == 750.0
        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value
        assert adapter.calls == []

    async def test_a_missing_adapter_degrades_to_a_local_only_write(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        """An unconfigured network must not strand approved work."""
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1400"
        )
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert campaign.daily_budget == 1400.0
        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value
        assert executed.external_reference is None

    async def test_a_campaign_that_vanished_does_not_block_execution(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BUDGET, after_value="1400"
        )
        action.campaign_id = "camp_deleted"
        await session.flush()
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert executed.status == ActionStatus.EXECUTED.value
        assert result is None
        assert adapter.calls == []


class TestBidExecution:
    async def test_a_bid_change_is_recorded_locally_only(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        """Bidding strategy APIs differ per network, so nothing is pushed."""
        action = await add_action(session, campaign.id, ActionType.ADJUST_BID, after_value="¥12.5")
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert campaign.current_bid_cpm == 12.5
        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value
        assert adapter.calls == []

    @pytest.mark.parametrize("value", ["0", "-3", "lower", ""])
    async def test_an_unusable_bid_is_refused(
        self,
        session: Any,
        campaign: Campaign,
        adapter: MockAdsClient,
        security: SecuritySettings,
        value: str,
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.ADJUST_BID, after_value=value, action_id="act_bid"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        with pytest.raises(ConflictError):
            await service.execute(action.id, claims=make_claims())

        assert action.status == ActionStatus.FAILED.value
        assert campaign.current_bid_cpm == 0.0

    async def test_a_bid_survives_a_missing_campaign_row(
        self, session: Any, security: SecuritySettings
    ) -> None:
        action = await add_action(session, "camp_absent", ActionType.ADJUST_BID, after_value="9.25")
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert executed.status == ActionStatus.EXECUTED.value
        assert result is None


class TestCampaignStatusExecution:
    async def test_pause_sets_local_status_and_calls_the_network(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(
            session, campaign.id, ActionType.PAUSE_CAMPAIGN, reason="ROAS collapsed"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert campaign.status == CampaignStatus.PAUSED.value
        assert executed.status == ActionStatus.EXECUTED.value
        assert result is not None and result.success
        assert adapter.calls[0]["operation"] == "pause_campaign"
        assert adapter.calls[0]["reason"] == "ROAS collapsed"

    async def test_resume_reactivates_a_paused_campaign(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        campaign.status = CampaignStatus.PAUSED.value
        action = await add_action(session, campaign.id, ActionType.RESUME_CAMPAIGN)
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        assert campaign.status == CampaignStatus.ACTIVE.value
        assert adapter.calls[0]["operation"] == "resume_campaign"

    async def test_the_reason_is_truncated_to_what_the_network_accepts(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.PAUSE_CAMPAIGN, reason="x" * 500)
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        assert len(adapter.calls[0]["reason"]) == 200

    async def test_pause_without_an_adapter_still_pauses_locally(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.PAUSE_CAMPAIGN)
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert campaign.status == CampaignStatus.PAUSED.value
        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value


class TestCreativeStatusExecution:
    async def test_pause_targets_the_named_creative(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        doomed = await add_creative(session, campaign.id, headline="Fatigued")
        keeper = await add_creative(session, campaign.id, headline="Still fresh")
        action = await add_action(
            session, campaign.id, ActionType.PAUSE_CREATIVE, creative_id=doomed.id
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        assert doomed.status == CreativeStatus.PAUSED.value
        assert keeper.status == CreativeStatus.ACTIVE.value
        assert adapter.calls[0]["operation"] == "pause_creative"
        assert adapter.calls[0]["external_id"] == doomed.id

    async def test_pause_without_a_creative_id_falls_back_to_the_campaign(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        creative = await add_creative(session, campaign.id)
        action = await add_action(session, campaign.id, ActionType.PAUSE_CREATIVE)
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert creative.status == CreativeStatus.PAUSED.value
        assert executed.status == ActionStatus.EXECUTED.value
        assert result is not None

    async def test_a_stale_creative_id_falls_back_rather_than_failing(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        creative = await add_creative(session, campaign.id)
        action = await add_action(
            session, campaign.id, ActionType.PAUSE_CREATIVE, creative_id="cre_deleted"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        assert creative.status == CreativeStatus.PAUSED.value

    async def test_resume_reactivates_a_paused_creative(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        creative = await add_creative(session, campaign.id, status=CreativeStatus.PAUSED)
        action = await add_action(
            session, campaign.id, ActionType.RESUME_CREATIVE, creative_id=creative.id
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        assert creative.status == CreativeStatus.ACTIVE.value
        # Approving a resume must never instruct the network to pause the ad.
        assert adapter.calls[0]["operation"] == "resume_creative"

    async def test_a_campaign_with_no_creatives_raises_not_found(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.PAUSE_CREATIVE)
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        with pytest.raises(NotFoundError):
            await service.execute(action.id, claims=make_claims())

        assert action.status == ActionStatus.FAILED.value
        assert adapter.calls == []


class TestExperimentExecution:
    async def test_start_ab_test_creates_an_experiment_sized_by_power_analysis(
        self, session: Any, campaign: Campaign, adapter: MockAdsClient, security: SecuritySettings
    ) -> None:
        control = await add_creative(session, campaign.id, ab_group="control")
        variant = await add_creative(session, campaign.id, ab_group="variant_a")
        rejected = await add_creative(
            session, campaign.id, ab_group="variant_b", status=CreativeStatus.REJECTED
        )
        action = await add_action(
            session, campaign.id, ActionType.START_AB_TEST, reason="Creative fatigue detected"
        )
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        experiment = (await session.execute(select(ABTest))).scalars().one()
        assert experiment.campaign_id == campaign.id
        assert experiment.control_creative_id == control.id
        assert experiment.variant_creative_id == variant.id
        assert experiment.required_sample_size == required_sample_size(0.025, 0.10)
        assert experiment.required_sample_size > 0
        assert experiment.minimum_detectable_effect == 0.10
        assert experiment.metric == "ctr"
        assert experiment.traffic_split == 0.5
        assert experiment.hypothesis == "Creative fatigue detected"
        assert experiment.name == "Agent experiment for " + campaign.id
        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value
        assert adapter.calls == []
        assert rejected.id not in (experiment.control_creative_id, experiment.variant_creative_id)

    async def test_start_ab_test_without_creatives_still_records_the_intent(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.START_AB_TEST)
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        experiment = (await session.execute(select(ABTest))).scalars().one()
        assert experiment.control_creative_id is None
        assert experiment.variant_creative_id is None

    async def test_a_long_hypothesis_is_truncated_to_the_column_width(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.START_AB_TEST, reason="r" * 900)
        service = build_service(session, adapter=None, security=security)
        await service.approve(action.id, claims=make_claims())

        await service.execute(action.id, claims=make_claims())

        experiment = (await session.execute(select(ABTest))).scalars().one()
        assert len(experiment.hypothesis) == 500


class TestAdvisoryActions:
    @pytest.mark.parametrize(
        "action_type",
        [ActionType.REFRESH_CREATIVE, ActionType.EXPAND_AUDIENCE, ActionType.STOP_AB_TEST],
    )
    async def test_actions_with_no_executor_are_recorded_and_push_nothing(
        self,
        session: Any,
        campaign: Campaign,
        adapter: MockAdsClient,
        security: SecuritySettings,
        action_type: ActionType,
    ) -> None:
        """Advisory proposals stay auditable without touching the network."""
        action = await add_action(session, campaign.id, action_type, action_id="act_advisory")
        service = build_service(session, adapter=adapter, security=security)
        await service.approve(action.id, claims=make_claims())

        executed, result = await service.execute(action.id, claims=make_claims())

        assert result is None
        assert executed.status == ActionStatus.EXECUTED.value
        assert adapter.calls == []
        assert campaign.daily_budget == 1000.0
        assert campaign.status == CampaignStatus.ACTIVE.value


class TestQueriesAndBulkApproval:
    async def test_list_actions_dispatches_on_run_status_and_pending(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        proposed = await add_action(session, campaign.id, ActionType.EXPAND_AUDIENCE)
        executed = await add_action(
            session,
            campaign.id,
            ActionType.ADJUST_BID,
            after_value="10",
            status=ActionStatus.EXECUTED,
        )
        service = build_service(session, adapter=None, security=security)

        assert [item.id for item in await service.list_actions()] == [proposed.id]
        assert [item.id for item in await service.list_actions(status=ActionStatus.EXECUTED)] == [
            executed.id
        ]
        assert await service.list_actions(run_id="run_missing") == []
        assert await service.get(proposed.id) is proposed

    async def test_get_raises_for_an_unknown_action(
        self, session: Any, security: SecuritySettings
    ) -> None:
        service = build_service(session, adapter=None, security=security)
        with pytest.raises(NotFoundError):
            await service.get("act_missing")

    async def test_bulk_approve_reports_why_each_id_was_skipped(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        good = await add_action(session, campaign.id, ActionType.EXPAND_AUDIENCE, confidence=0.9)
        weak = await add_action(session, campaign.id, ActionType.REFRESH_CREATIVE, confidence=0.2)
        settled = await add_action(
            session,
            campaign.id,
            ActionType.ADJUST_BID,
            after_value="8",
            status=ActionStatus.EXECUTED,
            confidence=0.95,
        )
        service = build_service(session, adapter=None, security=security)

        outcome = await service.bulk_approve(
            [good.id, weak.id, settled.id, "act_missing"],
            claims=make_claims(),
            min_confidence=0.5,
        )

        assert outcome["approved"] == [good.id]
        assert {entry["id"]: entry["reason"] for entry in outcome["skipped"]} == {
            weak.id: "low_confidence",
            settled.id: "status_executed",
            "act_missing": "not_found",
        }
        assert good.status == ActionStatus.APPROVED.value
        assert weak.status == ActionStatus.PROPOSED.value

    async def test_bulk_approve_without_a_floor_approves_everything_proposed(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        first = await add_action(session, campaign.id, ActionType.EXPAND_AUDIENCE, confidence=0.1)
        second = await add_action(session, campaign.id, ActionType.REFRESH_CREATIVE, confidence=0.2)
        service = build_service(session, adapter=None, security=security)

        outcome = await service.bulk_approve([first.id, second.id], claims=make_claims())

        assert sorted(outcome["approved"]) == sorted([first.id, second.id])
        assert outcome["skipped"] == []

    async def test_bulk_approve_requires_the_approve_permission(
        self, session: Any, campaign: Campaign, security: SecuritySettings
    ) -> None:
        action = await add_action(session, campaign.id, ActionType.EXPAND_AUDIENCE)
        service = build_service(session, adapter=None, security=security)

        with pytest.raises(PermissionDeniedError):
            await service.bulk_approve([action.id], claims=make_claims(Role.VIEWER))

        assert action.status == ActionStatus.PROPOSED.value
