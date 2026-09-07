"""Application configuration.

Single source of truth for runtime settings. Everything is read from the
environment (optionally seeded from a .env file) and validated eagerly so a
misconfigured deployment fails fast instead of degrading silently at runtime.

Nested groups use the "__" delimiter, e.g. DATABASE__URL, SECURITY__JWT_SECRET.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    timeout_seconds: float = Field(default=45.0, gt=0)
    max_retries: int = Field(default=3, ge=0)
    retry_base_delay_seconds: float = Field(default=0.5, gt=0)
    concurrency: int = Field(default=4, ge=1)
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    monthly_budget_usd: float = Field(default=200.0, ge=0)
    fail_open_to_mock: bool = True

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
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide cached settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read configuration. Intended for tests."""
    get_settings.cache_clear()
    return get_settings()
