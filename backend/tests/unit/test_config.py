"""Unit tests for configuration loading and the production hardening gate.

Nested groups are only split on "__" when they come from the environment, so
every test here drives Settings through real environment variables. That keeps
the suite honest about the contract the deployment manifests rely on.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from adoptimizer.core.config import (
    INSECURE_SECRET_SENTINELS,
    DatabaseSettings,
    DataMode,
    Environment,
    IngestSettings,
    LLMProvider,
    LLMSettings,
    ObservabilitySettings,
    Settings,
    get_settings,
    reload_settings,
)

STRONG_SECRET = "a-genuinely-unique-production-signing-key-0123456789"
PROD_DATABASE = "postgresql+asyncpg://adoptimizer:secret@db:5432/adoptimizer"

# Variables a developer's shell or .env could leak into the suite.
LEAKY_KEYS = (
    "APP__NAME",
    "APP__ENVIRONMENT",
    "APP__CORS_ALLOW_ORIGINS",
    "DATABASE__URL",
    "SECURITY__JWT_SECRET",
    "SECURITY__BOOTSTRAP_ADMIN_PASSWORD",
    "LLM__PROVIDER",
    "LLM__API_KEY",
    "LLM__PRICING",
    "DATA_MODE",
    "REDIS__ENABLED",
    "CLICKHOUSE__ENABLED",
    "OPTIMIZATION__MAX_ITERATIONS",
    "OBSERVABILITY__LOG_LEVEL",
    # A developer who has switched the resident loop on locally would otherwise
    # hand that cadence to every test in the suite.
    "INGEST__SCHEDULER_ENABLED",
    "INGEST__SOURCES",
    "INGEST__INTERVAL_MINUTES",
    "INGEST__LOOKBACK_DAYS",
    "INGEST__MAX_CATCHUP_DAYS",
    "INGEST__LEASE_TTL_SECONDS",
    "INGEST__DRY_RUN",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Start every test from a known-empty environment and reset the cache after."""
    for key in LEAKY_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    # monkeypatch undoes its own edits only *after* this fixture finishes, so a
    # test that deliberately exported an invalid value would still see it here and
    # poison the cached settings for every later test. Clear the keys by hand,
    # refresh the cache, then put them back so monkeypatch's bookkeeping is intact.
    stashed = {key: os.environ.pop(key, None) for key in LEAKY_KEYS}
    try:
        reload_settings()
    finally:
        for key, value in stashed.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def prod_env(clean_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A configuration that satisfies every production hardening rule."""
    clean_env.setenv("APP__ENVIRONMENT", "production")
    clean_env.setenv("SECURITY__JWT_SECRET", STRONG_SECRET)
    clean_env.setenv("SECURITY__BOOTSTRAP_ADMIN_PASSWORD", "Un1que!BootstrapPwd")
    clean_env.setenv("APP__CORS_ALLOW_ORIGINS", '["https://ads.example.com"]')
    clean_env.setenv("DATABASE__URL", PROD_DATABASE)
    return clean_env


def load() -> Settings:
    """Read settings from the environment only, ignoring any local .env file."""
    return Settings(_env_file=None)  # type: ignore[call-arg]


class TestDefaults:
    def test_boots_offline_with_no_environment(self) -> None:
        settings = load()
        assert settings.app.environment is Environment.DEVELOPMENT
        assert settings.llm.provider is LLMProvider.MOCK
        assert settings.data_mode is DataMode.MOCK
        assert settings.database.is_sqlite is True
        assert settings.redis.enabled is True

    def test_action_approval_is_on_by_default(self) -> None:
        assert load().security.require_action_approval is True


class TestEnvironment:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            (Environment.DEVELOPMENT, False),
            (Environment.TEST, False),
            (Environment.STAGING, True),
            (Environment.PRODUCTION, True),
        ],
    )
    def test_production_like(self, env: Environment, expected: bool) -> None:
        assert env.is_production_like is expected


class TestDatabaseSettings:
    @pytest.mark.parametrize(
        ("url", "dialect", "is_sqlite"),
        [
            ("sqlite+aiosqlite:///./x.db", "sqlite", True),
            ("postgresql+asyncpg://u:p@h:5432/db", "postgresql", False),
        ],
    )
    def test_dialect_detection(self, url: str, dialect: str, is_sqlite: bool) -> None:
        cfg = DatabaseSettings(url=url)
        assert cfg.dialect == dialect
        assert cfg.is_sqlite is is_sqlite


class TestProductionHardening:
    def test_a_valid_production_config_is_accepted(self, prod_env: pytest.MonkeyPatch) -> None:
        settings = load()
        assert settings.app.environment is Environment.PRODUCTION
        assert settings.database.dialect == "postgresql"

    def test_short_jwt_secret_is_refused(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("SECURITY__JWT_SECRET", "only-31-characters-long-abcdefg")
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            load()

    def test_every_insecure_sentinel_is_refused(self, prod_env: pytest.MonkeyPatch) -> None:
        for sentinel in sorted(INSECURE_SECRET_SENTINELS):
            prod_env.setenv("SECURITY__JWT_SECRET", sentinel)
            with pytest.raises(ValidationError, match="JWT_SECRET"):
                load()

    def test_weak_bootstrap_password_is_refused(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("SECURITY__BOOTSTRAP_ADMIN_PASSWORD", "changeme")
        with pytest.raises(ValidationError, match="BOOTSTRAP_ADMIN_PASSWORD"):
            load()

    def test_wildcard_cors_is_refused(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("APP__CORS_ALLOW_ORIGINS", '["*"]')
        with pytest.raises(ValidationError, match="CORS_ALLOW_ORIGINS"):
            load()

    def test_sqlite_is_refused_in_production(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("DATABASE__URL", "sqlite+aiosqlite:///./prod.db")
        with pytest.raises(ValidationError, match="PostgreSQL"):
            load()

    def test_every_problem_is_reported_together(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("SECURITY__JWT_SECRET", "changeme")
        prod_env.setenv("DATABASE__URL", "sqlite+aiosqlite:///./prod.db")
        prod_env.setenv("APP__CORS_ALLOW_ORIGINS", '["*"]')
        with pytest.raises(ValidationError) as info:
            load()
        message = str(info.value)
        assert "JWT_SECRET" in message
        assert "PostgreSQL" in message
        assert "CORS_ALLOW_ORIGINS" in message

    def test_staging_is_held_to_the_same_standard(self, prod_env: pytest.MonkeyPatch) -> None:
        prod_env.setenv("APP__ENVIRONMENT", "staging")
        prod_env.setenv("SECURITY__JWT_SECRET", "changeme")
        with pytest.raises(ValidationError, match="JWT_SECRET"):
            load()

    def test_development_is_not_hardened(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("APP__ENVIRONMENT", "development")
        clean_env.setenv("SECURITY__JWT_SECRET", "changeme")
        clean_env.setenv("DATABASE__URL", "sqlite+aiosqlite:///./dev.db")
        assert load().app.environment is Environment.DEVELOPMENT


class TestNestedEnvironmentParsing:
    def test_double_underscore_delimiter(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("APP__NAME", "Renamed")
        clean_env.setenv("OPTIMIZATION__MAX_ITERATIONS", "7")
        clean_env.setenv("DATA_MODE", "warehouse")
        settings = load()
        assert settings.app.name == "Renamed"
        assert settings.optimization.max_iterations == 7
        assert settings.data_mode is DataMode.WAREHOUSE

    def test_list_values_are_json(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("APP__CORS_ALLOW_ORIGINS", '["https://a.io","https://b.io"]')
        assert load().app.cors_allow_origins == ["https://a.io", "https://b.io"]

    def test_out_of_range_values_are_refused(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("OPTIMIZATION__MAX_ITERATIONS", "99")
        with pytest.raises(ValidationError):  # bounded by le=10
            load()


class TestLLMSettings:
    def test_mock_provider_needs_no_key(self) -> None:
        assert LLMSettings(provider=LLMProvider.MOCK).api_key.get_secret_value() == ""

    @pytest.mark.parametrize("provider", ["openai", "azure_openai", "openai_compatible"])
    def test_hosted_provider_requires_a_key(self, provider: str) -> None:
        with pytest.raises(ValidationError, match="API_KEY"):
            LLMSettings(provider=provider)  # type: ignore[arg-type]

    def test_hosted_provider_with_a_key_is_accepted(self) -> None:
        cfg = LLMSettings(provider="openai", api_key="sk-test")  # type: ignore[arg-type]
        assert cfg.api_key.get_secret_value() == "sk-test"

    def test_fail_open_to_mock_is_the_default(self) -> None:
        assert LLMSettings().fail_open_to_mock is True

    def test_pricing_overrides_default_to_empty(self) -> None:
        """No override means the built-in table in llm/base.py is used as-is."""
        assert LLMSettings().pricing == {}

    def test_pricing_is_parsed_from_a_json_environment_variable(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        """Regional bills differ from list prices, so this has to be tunable."""
        clean_env.setenv("LLM__PRICING", '{"qwen-plus": [0.113, 0.282], "default": [0.5, 1.5]}')

        pricing = load().llm.pricing

        assert pricing["qwen-plus"] == (0.113, 0.282)
        assert pricing["default"] == (0.5, 1.5)

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must not be negative"):
            LLMSettings(pricing={"qwen-plus": (-0.1, 0.282)})

    def test_a_negative_price_is_refused_when_it_arrives_from_the_environment(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("LLM__PRICING", '{"qwen-plus": [0.113, -1]}')

        with pytest.raises(ValidationError, match="LLM__PRICING"):
            load()

    def test_a_zero_price_is_accepted(self) -> None:
        """Free tiers and promotional credit are legitimate, not a typo."""
        assert LLMSettings(pricing={"qwen-plus": (0.0, 0.0)}).pricing["qwen-plus"] == (0.0, 0.0)


class TestObservability:
    def test_log_level_is_normalised(self) -> None:
        assert ObservabilitySettings(log_level="debug").log_level == "DEBUG"

    def test_unknown_log_level_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="log_level must be one of"):
            ObservabilitySettings(log_level="verbose")


class TestPublicDict:
    def test_exposes_no_secrets(self) -> None:
        payload = load().public_dict()
        blob = repr(payload).lower()
        assert "jwt_secret" not in payload
        assert STRONG_SECRET.lower() not in blob
        assert payload["environment"] == "development"
        assert payload["llm_provider"] == "mock"
        assert payload["database_dialect"] == "sqlite"
        assert payload["require_action_approval"] is True


class TestSettingsCache:
    def test_get_settings_is_cached(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("APP__NAME", "First")
        first = reload_settings()
        clean_env.setenv("APP__NAME", "Second")
        assert get_settings() is first
        assert get_settings().app.name == "First"

    def test_reload_settings_picks_up_changes(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("APP__NAME", "First")
        reload_settings()
        clean_env.setenv("APP__NAME", "Second")
        assert reload_settings().app.name == "Second"


class TestIngestSettings:
    """The scheduled pull's policy knobs.

    These are the values the deployment manifests publish, so a validator that
    quietly rewrites one is a deployment that behaves differently from the file
    describing it.
    """

    def test_the_defaults_pull_the_demo_feed_on_a_six_hour_cadence(self) -> None:
        settings = IngestSettings()

        assert settings.sources == ["synthetic"]
        assert settings.interval_minutes == 360
        assert settings.lookback_days == 3
        assert settings.max_catchup_days == 14
        assert settings.lease_ttl_seconds == 1800
        assert settings.scheduler_enabled is True
        assert settings.dry_run is False

    def test_feed_names_are_trimmed_lowered_and_deduplicated(self) -> None:
        """They become metric labels, so two spellings must not make two series."""
        settings = IngestSettings(sources=[" Platform ", "platform", "meta.ads"])

        assert settings.sources == ["platform", "meta.ads"]

    def test_blank_entries_are_dropped(self) -> None:
        assert IngestSettings(sources=["   ", "platform"]).sources == ["platform"]

    def test_an_empty_feed_list_is_refused(self) -> None:
        """A schedule with nothing to pull would report success forever."""
        with pytest.raises(ValidationError, match="at least one feed"):
            IngestSettings(sources=[])

    def test_a_list_of_only_blanks_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least one feed"):
            IngestSettings(sources=["  ", ""])

    @pytest.mark.parametrize(
        "name",
        [
            "has space",
            "-leading-dash",
            ".leading-dot",
            # strip() forgives the ends only: a tab in the middle still reaches
            # the pattern, and the pattern is what bounds the metric label.
            "tab\tinside",
            "a" * 31,
            "emoji\u9ad8",
        ],
    )
    def test_a_shape_that_would_corrupt_a_label_is_refused(self, name: str) -> None:
        with pytest.raises(ValidationError, match="must match"):
            IngestSettings(sources=[name])

    def test_a_recovery_bound_narrower_than_the_lookback_is_refused(self) -> None:
        """Otherwise every ordinary run reports a gap it is not allowed to close."""
        with pytest.raises(ValidationError, match="INGEST__MAX_CATCHUP_DAYS"):
            IngestSettings(lookback_days=7, max_catchup_days=3)

    def test_a_recovery_bound_equal_to_the_lookback_is_allowed(self) -> None:
        assert IngestSettings(lookback_days=7, max_catchup_days=7).max_catchup_days == 7

    def test_a_cadence_that_cannot_fire_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            IngestSettings(interval_minutes=0)


class TestIngestEnvironment:
    """The exact keys the ConfigMap and the compose anchor publish."""

    def test_every_manifest_key_reaches_the_settings(self, clean_env: pytest.MonkeyPatch) -> None:
        clean_env.setenv("INGEST__SCHEDULER_ENABLED", "false")
        clean_env.setenv("INGEST__SOURCES", '["platform"]')
        clean_env.setenv("INGEST__INTERVAL_MINUTES", "60")
        clean_env.setenv("INGEST__LOOKBACK_DAYS", "5")
        clean_env.setenv("INGEST__MAX_CATCHUP_DAYS", "30")
        clean_env.setenv("INGEST__LEASE_TTL_SECONDS", "900")
        clean_env.setenv("INGEST__DRY_RUN", "true")

        settings = load().ingest

        assert settings.scheduler_enabled is False
        assert settings.sources == ["platform"]
        assert settings.interval_minutes == 60
        assert settings.lookback_days == 5
        assert settings.max_catchup_days == 30
        assert settings.lease_ttl_seconds == 900
        assert settings.dry_run is True

    def test_an_absent_block_leaves_the_defaults_alone(self, clean_env: pytest.MonkeyPatch) -> None:
        assert load().ingest == IngestSettings()

    def test_a_feed_list_that_is_not_json_fails_at_boot(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        """Refusing to start beats starting a schedule that pulls nothing.

        A list field is parsed as JSON before the validators ever see it, so a
        bare word raises pydantic-settings' own SettingsError rather than a
        ValidationError. Either way the process does not come up, which is the
        property that matters: the manifests must publish valid JSON.
        """
        clean_env.setenv("INGEST__SOURCES", "platform")

        with pytest.raises(SettingsError):
            load()

    def test_an_unschedulable_combination_fails_at_boot(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("INGEST__LOOKBACK_DAYS", "30")
        clean_env.setenv("INGEST__MAX_CATCHUP_DAYS", "7")

        with pytest.raises(ValidationError, match="INGEST__MAX_CATCHUP_DAYS"):
            load()
