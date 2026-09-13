"""Application configuration.

Single source of truth for runtime settings. Everything is read from the
environment (optionally seeded from a .env file) and validated eagerly so a
misconfigured deployment fails fast instead of degrading silently at runtime.

Nested groups use the "__" delimiter, e.g. DATABASE__URL, SECURITY__JWT_SECRET.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A feed name ends up in three places that all punish free text: a Prometheus
# label, the ``ingest_batches.source`` column and the ``daily_metrics.source``
# provenance column. Bounding the shape here bounds the cardinality of the
# series, and ``schemas.ingest`` re-exports this same constant so the wire
# contract and the configuration cannot drift apart.
SOURCE_NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,29}$"

_SOURCE_NAME_RE = re.compile(SOURCE_NAME_PATTERN)


class Environment(StrEnum):
    """Deployment stage. Drives insecure-default rejection and log verbosity."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class LLMProvider(StrEnum):
    """Supported model backends. "mock" needs no network or credentials."""

    MOCK = "mock"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    OPENAI_COMPATIBLE = "openai_compatible"


class DataMode(StrEnum):
    """Where campaign telemetry comes from."""

    MOCK = "mock"
    WAREHOUSE = "warehouse"


INSECURE_SECRET_SENTINELS = frozenset(
    {"", "changeme", "change-me", "secret", "dev-secret", "insecure", "password"}
)


class AppSettings(BaseModel):
    """Service identity and HTTP behaviour."""

    name: str = "Ad Optimizer"
    version: str = "1.0.0"
    environment: Environment = Environment.DEVELOPMENT
    api_v1_prefix: str = "/api/v1"
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    cors_allow_credentials: bool = True
    trusted_hosts: list[str] = Field(default_factory=lambda: ["*"])
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_request_body_bytes: int = Field(default=2 * 1024 * 1024, gt=0)


class DatabaseSettings(BaseModel):
    """Primary OLTP store: campaigns, runs, users, audit trail."""

    url: str = "sqlite+aiosqlite:///./adoptimizer.db"
    echo: bool = False
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    pool_recycle_seconds: int = Field(default=1800, ge=30)
    pool_pre_ping: bool = True
    statement_timeout_ms: int | None = Field(default=None, ge=0)

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def dialect(self) -> str:
        """Database engine family, without the driver.

        `postgresql+asyncpg://` and `sqlite+aiosqlite://` both collapse to
        their engine name so readiness probes and hardening checks compare
        against a stable value instead of leaking the installed driver.
        """
        scheme = self.url.split(":", 1)[0]
        return scheme.split("+", 1)[0]


class RedisSettings(BaseModel):
    """Cache, rate limiting and the background job broker."""

    url: str = "redis://localhost:6379/0"
    enabled: bool = True
    max_connections: int = Field(default=50, ge=1)
    socket_timeout_seconds: float = Field(default=5.0, gt=0)
    socket_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    key_prefix: str = "adoptimizer"
    cache_ttl_seconds: int = Field(default=300, ge=0)


class ClickHouseSettings(BaseModel):
    """Analytical warehouse for high-cardinality ad events."""

    enabled: bool = False
    host: str = "localhost"
    http_port: int = Field(default=8123, ge=1, le=65535)
    native_port: int = Field(default=9000, ge=1, le=65535)
    user: str = "default"
    password: SecretStr = SecretStr("")
    database: str = "ad_optimizer"
    secure: bool = False
    verify_tls: bool = True
    connect_timeout_seconds: float = Field(default=10.0, gt=0)
    query_timeout_seconds: float = Field(default=30.0, gt=0)


class SecuritySettings(BaseModel):
    """Authentication, authorisation and credential policy."""

    jwt_secret: SecretStr = SecretStr("dev-only-insecure-secret")
    jwt_algorithm: Literal["HS256", "HS512", "RS256"] = "HS256"
    access_token_ttl_minutes: int = Field(default=30, ge=1)
    refresh_token_ttl_days: int = Field(default=7, ge=1)
    issuer: str = "adoptimizer"
    audience: str = "adoptimizer-api"
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_cost_kib: int = Field(default=65536, ge=8192)
    argon2_parallelism: int = Field(default=2, ge=1)
    # Validated eagerly: a reserved domain such as ``.local`` is rejected by the
    # login schema, which would otherwise leave the seeded admin unable to sign in.
    bootstrap_admin_email: EmailStr = "admin@adoptimizer.dev"
    bootstrap_admin_password: SecretStr = SecretStr("Adm1n!ChangeMe")
    password_min_length: int = Field(default=10, ge=8)
    max_failed_logins: int = Field(default=5, ge=1)
    lockout_seconds: int = Field(default=900, ge=0)
    require_action_approval: bool = True
    # Verifying the session store on every request makes logout, account
    # deactivation and role changes effective immediately instead of at access
    # token expiry. Turn it off only for read-heavy, latency-critical traffic.
    verify_session_on_request: bool = True


class RateLimitSettings(BaseModel):
    """Token-bucket limits applied per authenticated principal."""

    enabled: bool = True
    default_requests_per_minute: int = Field(default=300, ge=1)
    burst: int = Field(default=60, ge=1)
    write_requests_per_minute: int = Field(default=60, ge=1)
    optimize_runs_per_hour: int = Field(default=20, ge=1)


class LLMSettings(BaseModel):
    """Model gateway configuration and spend guardrails."""

    provider: LLMProvider = LLMProvider.MOCK
    model: str = "gpt-4o-mini"
    api_key: SecretStr = SecretStr("")
    api_base: str | None = None
    api_version: str | None = None
    organization: str | None = None
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64)
    # Stream token deltas rather than waiting for the whole completion. Some
    # vendors only answer their reasoning models in stream mode (Qwen3 thinking
    # is the usual reason to switch this on), and streaming also turns
    # timeout_seconds into a per-chunk read timeout instead of a cap on the
    # entire generation. Off by default so a deployment keeps its current
    # behaviour until a consumer is ready to render increments.
    stream: bool = False
    timeout_seconds: float = Field(default=45.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    retry_base_delay_seconds: float = Field(default=0.5, gt=0)
    concurrency: int = Field(default=4, ge=1)
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    monthly_budget_usd: float = Field(default=200.0, ge=0)
    # Per-model USD price for one million tokens, as (prompt, completion). The
    # built-in table in llm/base.py is only a starting point: vendor list prices
    # drift, and regional billing differs (a China-region DashScope endpoint
    # bills roughly a third of the international USD list price), so a
    # deployment that wants an honest budget has to state its own numbers.
    # Supplied as JSON, e.g. LLM__PRICING={"qwen-plus": [0.113, 0.282]}
    pricing: dict[str, tuple[float, float]] = Field(default_factory=dict)
    fail_open_to_mock: bool = True

    @field_validator("pricing")
    @classmethod
    def _pricing_must_not_be_negative(
        cls, value: dict[str, tuple[float, float]]
    ) -> dict[str, tuple[float, float]]:
        for model, prices in value.items():
            if prices[0] < 0 or prices[1] < 0:
                msg = f"LLM__PRICING[{model}] must not be negative, got {list(prices)}"
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _require_key_for_hosted_providers(self) -> LLMSettings:
        hosted = {
            LLMProvider.OPENAI,
            LLMProvider.AZURE_OPENAI,
            LLMProvider.OPENAI_COMPATIBLE,
        }
        if self.provider in hosted and not self.api_key.get_secret_value():
            msg = (
                "LLM__API_KEY is required when LLM__PROVIDER="
                + self.provider.value
                + ". Set LLM__PROVIDER=mock to run without credentials."
            )
            raise ValueError(msg)
        return self


class OptimizationSettings(BaseModel):
    """Business tunables for the agent loop."""

    max_iterations: int = Field(default=3, ge=1, le=10)
    default_target_roas: float = Field(default=2.0, gt=0)
    default_target_cpa: float = Field(default=100.0, gt=0)
    alert_ctr_floor: float = Field(default=0.005, ge=0)
    alert_cpa_ceiling: float = Field(default=200.0, gt=0)
    alert_roas_floor: float = Field(default=1.0, ge=0)
    min_impressions_for_alerts: int = Field(default=100, ge=0)
    creative_score_threshold: float = Field(default=40.0, ge=0, le=100)
    max_budget_change_pct: float = Field(default=50.0, ge=0, le=100)
    bid_cap_ratio_of_target_cpa: float = Field(default=0.8, gt=0, le=1)
    use_convex_solver: bool = True
    max_campaigns_per_run: int = Field(default=200, ge=1)


class ToolSettings(BaseModel):
    """Guards on the tool layer - the boundary where code touches real money.

    ``allow_agent_writes`` is the load-bearing one and defaults to False: an
    agent may read anything it is granted, but a mutation it asks for is
    dry-run, recorded and returned to it as a preflight result. Only a
    human-approved execution reaches the platform, and ``dry_run`` turns even
    that into paper trading.
    """

    enabled: bool = True
    dry_run: bool = False
    allow_agent_writes: bool = False
    max_calls_per_run: int = Field(default=500, ge=1)
    audit_persist: bool = True


class IngestSettings(BaseModel):
    """The scheduled pull: cadence, window shape and the missed-run policy.

    The window is derived from the calendar and never from a stored cursor. A
    cursor-derived window turns one corrupted or rolled-back row into a
    permanent gap that nobody notices, because every later run is "correct"
    relative to it. The watermark in ``ingest_watermarks`` is therefore read to
    *prove a run would be redundant*, and written for observability - it is not
    the thing the schedule is computed from.

    Two knobs shape the window:

    - ``lookback_days`` is the overlap that absorbs platform numbers settling
      late. Ad networks revise yesterday for days afterwards, so a pull that
      only ever asks for today slowly goes wrong in a way no error reports.
    - ``max_catchup_days`` is how far back a missed run may reach. Beyond it the
      scheduler says so in the plan and in a metric instead of quietly shrinking
      the window, because the fix is a deliberate backfill
      (``adoptimizer ingest --start ... --end ...``), not a silent guess.

    Re-pulling a day is safe by construction: ingestion upserts one slot per
    (campaign, creative, date) and an absent column never overwrites a stored
    number, so overlap costs a query and cannot corrupt a value.
    """

    scheduler_enabled: bool = True
    sources: list[str] = Field(default_factory=lambda: ["synthetic"])
    interval_minutes: int = Field(default=360, ge=1, le=10_080)
    lookback_days: int = Field(default=3, ge=1, le=90)
    max_catchup_days: int = Field(default=14, ge=1, le=365)
    dry_run: bool = False
    lease_ttl_seconds: int = Field(default=1800, ge=30, le=86_400)

    @field_validator("sources")
    @classmethod
    def _normalise_sources(cls, value: list[str]) -> list[str]:
        """Trim, lowercase, deduplicate, and refuse a shape we cannot label."""
        cleaned: list[str] = []
        for raw in value:
            name = raw.strip().lower()
            if not name:
                continue
            if _SOURCE_NAME_RE.fullmatch(name) is None:
                msg = "each INGEST__SOURCES entry must match " + SOURCE_NAME_PATTERN
                raise ValueError(msg)
            if name not in cleaned:
                cleaned.append(name)
        if not cleaned:
            msg = "INGEST__SOURCES must name at least one feed"
            raise ValueError(msg)
        return cleaned

    @model_validator(mode="after")
    def _catchup_must_cover_the_lookback(self) -> IngestSettings:
        """Otherwise the ordinary window already sits outside the recovery bound.

        Every run would then report a gap it is not allowed to close, which
        turns a safety limit into a permanently red alarm and teaches whoever is
        on call to ignore it.
        """
        if self.max_catchup_days < self.lookback_days:
            msg = (
                "INGEST__MAX_CATCHUP_DAYS ("
                + str(self.max_catchup_days)
                + ") must be at least INGEST__LOOKBACK_DAYS ("
                + str(self.lookback_days)
                + ")"
            )
            raise ValueError(msg)
        return self


class ObservabilitySettings(BaseModel):
    """Logging, metrics and tracing."""

    log_level: str = "INFO"
    json_logs: bool = True
    log_request_body: bool = False
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    otlp_endpoint: str | None = None
    service_name: str = "adoptimizer-backend"

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            msg = "log_level must be one of " + str(sorted(allowed))
            raise ValueError(msg)
        return level


class Settings(BaseSettings):
    """Root configuration object."""

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    optimization: OptimizationSettings = Field(default_factory=OptimizationSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    data_mode: DataMode = DataMode.MOCK

    @model_validator(mode="after")
    def _enforce_production_hardening(self) -> Settings:
        """Refuse to boot in staging/production with demo-grade secrets."""
        if not self.app.environment.is_production_like:
            return self

        problems: list[str] = []
        secret = self.security.jwt_secret.get_secret_value()
        if secret.lower() in INSECURE_SECRET_SENTINELS or len(secret) < 32:
            problems.append("SECURITY__JWT_SECRET must be a unique value of at least 32 characters")

        bootstrap = self.security.bootstrap_admin_password.get_secret_value()
        if bootstrap.lower() in INSECURE_SECRET_SENTINELS or len(bootstrap) < 12:
            problems.append("SECURITY__BOOTSTRAP_ADMIN_PASSWORD must be a strong unique value")

        if "*" in self.app.cors_allow_origins:
            problems.append("APP__CORS_ALLOW_ORIGINS must not be a wildcard in production")

        if self.database.is_sqlite:
            problems.append("DATABASE__URL must point at PostgreSQL in production")

        if problems:
            msg = "Insecure production configuration: " + "; ".join(problems)
            raise ValueError(msg)
        return self

    def public_dict(self) -> dict[str, Any]:
        """Configuration safe to expose on the diagnostics endpoint."""
        return {
            "environment": self.app.environment.value,
            "version": self.app.version,
            "data_mode": self.data_mode.value,
            "llm_provider": self.llm.provider.value,
            "llm_model": self.llm.model,
            "clickhouse_enabled": self.clickhouse.enabled,
            "redis_enabled": self.redis.enabled,
            "database_dialect": self.database.dialect,
            "require_action_approval": self.security.require_action_approval,
            "tools_enabled": self.tools.enabled,
            "tools_dry_run": self.tools.dry_run,
            "tools_allow_agent_writes": self.tools.allow_agent_writes,
        }


def dotenv_path(project_root: Path | None = None) -> Path | None:
    """Locate the `.env` this process should read, or None when there is none.

    The working directory is tried first so an operator can point the app at a
    different file by launching it from elsewhere; the fallback is
    `<project_root>/.env`, which defaults to `backend/` - where the documented
    workflow puts it. ``project_root`` exists as a seam so the search can be
    tested without moving the package.
    """
    root = project_root or Path(__file__).resolve().parents[3]
    for candidate in (Path.cwd() / ".env", root / ".env"):
        if candidate.is_file():
            return candidate
    return None


def load_environment(project_root: Path | None = None) -> Path | None:
    """Export `.env` into `os.environ` and return the file that was loaded.

    Pydantic reads `.env` for the Settings object on its own, but the ad-platform
    adapters call `os.getenv("GOOGLE_ADS_CLIENT_ID")` directly. Without this step
    a credential written to `.env` is invisible to them, every adapter reports
    itself unconfigured, and the failure looks exactly like a wrong key rather
    than like a key that was never read. Real environment variables still win:
    `override=False` is what keeps a container deployment authoritative.
    """
    path = dotenv_path(project_root)
    if path is not None:
        load_dotenv(path, override=False)
    return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    load_environment()
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read configuration. Intended for tests."""
    get_settings.cache_clear()
    return get_settings()
