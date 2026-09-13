"""Domain enumerations shared by every layer."""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """Advertising platform the campaign runs on."""

    GOOGLE = "google"
    META = "meta"
    TIKTOK = "tiktok"
    MOCK = "mock"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class CreativeType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"


class CreativeStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    REJECTED = "rejected"


class EventType(StrEnum):
    IMPRESSION = "impression"
    CLICK = "click"
    CONVERSION = "conversion"


class DeviceType(StrEnum):
    MOBILE = "mobile"
    DESKTOP = "desktop"
    TABLET = "tablet"


class Gender(StrEnum):
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class AgentName(StrEnum):
    """The agents of the optimization loop."""

    MONITOR = "monitor"
    AUDIENCE = "audience"
    CREATIVE = "creative"
    BIDDING = "bidding"
    OPTIMIZE = "optimize"
    CRITIC = "critic"


class RunStatus(StrEnum):
    """Lifecycle of an optimization run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


class ActionType(StrEnum):
    """Operations the optimizer can propose."""

    PAUSE_CREATIVE = "pause_creative"
    RESUME_CREATIVE = "resume_creative"
    ADJUST_BUDGET = "adjust_budget"
    ADJUST_BID = "adjust_bid"
    PAUSE_CAMPAIGN = "pause_campaign"
    RESUME_CAMPAIGN = "resume_campaign"
    REFRESH_CREATIVE = "refresh_creative"
    START_AB_TEST = "start_ab_test"
    STOP_AB_TEST = "stop_ab_test"
    EXPAND_AUDIENCE = "expand_audience"


class ActionStatus(StrEnum):
    """Human-in-the-loop approval state for a proposed action."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AlertRule(StrEnum):
    LOW_CTR = "low_ctr"
    HIGH_CPA = "high_cpa"
    LOW_ROAS = "low_roas"
    BURN_RATE = "burn_rate"
    IMPRESSION_COLLAPSE = "impression_collapse"
    FREQUENCY_FATIGUE = "frequency_fatigue"


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class ABTestStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    CONCLUDED = "concluded"
    CANCELLED = "cancelled"


class Role(StrEnum):
    """Principal roles, ordered from most to least privileged.

    ``INGESTOR`` deliberately sits outside that ordering. It is a machine
    identity for the metrics pipeline: it holds fewer permissions than
    ``VIEWER``, but one of them is a write, so "least privileged" would be a
    misleading label for it. What it is, is *narrow* - a stolen ingestion
    credential can fabricate numbers and nothing else, where a stolen
    ``OPTIMIZER`` credential could also move real budget on a real ad platform.
    """

    ADMIN = "admin"
    OPTIMIZER = "optimizer"
    ANALYST = "analyst"
    VIEWER = "viewer"
    INGESTOR = "ingestor"


class Permission(StrEnum):
    """Capabilities checked by the API layer.

    Routers assert permissions rather than roles so the mapping between the two
    can change without touching endpoint code.
    """

    CAMPAIGN_READ = "campaign:read"
    CAMPAIGN_WRITE = "campaign:write"
    RUN_READ = "run:read"
    RUN_TRIGGER = "run:trigger"
    ACTION_APPROVE = "action:approve"
    ACTION_EXECUTE = "action:execute"
    ALERT_READ = "alert:read"
    ALERT_ACK = "alert:ack"
    CREATIVE_WRITE = "creative:write"
    METRICS_READ = "metrics:read"
    METRICS_WRITE = "metrics:write"
    USER_MANAGE = "user:manage"
    AUDIT_READ = "audit:read"
    SYSTEM_READ = "system:read"
