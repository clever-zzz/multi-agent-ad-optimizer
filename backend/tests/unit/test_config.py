"""Unit tests for configuration loading and the production hardening gate.

Nested groups are only split on "__" when they come from the environment, so
every test here drives Settings through real environment variables. That keeps
the suite honest about the contract the deployment manifests rely on.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

import adoptimizer
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
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
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


def parse_dotenv(text: str) -> list[tuple[str, str]]:
    """Pull ``KEY=value`` pairs out of a dotenv file, skipping comments and blanks.

    Deliberately naive about quoting and continuations: ``.env.example`` is a
    flat list of single-line assignments, and a parser that handled more than
    that would silently disagree with what python-dotenv actually feeds Settings.
    """
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and _ENV_KEY_RE.fullmatch(key):
            pairs.append((key, value))
    return pairs


class TestDefaults:
    def test_the_shipped_env_example_boots(self, clean_env: pytest.MonkeyPatch) -> None:
        """`cp .env.example .env` is step one of the quickstart, so it must load.

        A dotenv file spells "no override" as an empty value. For a complex field
        pydantic-settings hands that empty string to the JSON decoder, so
        `LLM__PRICING=` used to abort during import with a `dict_type` error -
        before one line of the application had run, and only for somebody
        following the documented setup. Driving every key in the shipped file
        through the real environment is what stops the example from rotting away
        from what Settings accepts.
        """
        example = pathlib.Path(__file__).resolve().parents[2] / ".env.example"
        pairs = parse_dotenv(example.read_text(encoding="utf-8"))
        assert pairs, f"parsed no keys out of {example}"
        for key, value in pairs:
            clean_env.setenv(key, value)

        settings = load()

        assert settings.app.environment is Environment.DEVELOPMENT
        assert settings.llm.provider is LLMProvider.MOCK
        assert settings.data_mode is DataMode.MOCK
        assert settings.ingest.sources == ["synthetic"]

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

    def test_statement_timeout_reaches_the_driver(self) -> None:
        """asyncpg wants it nested under ``server_settings``.

        A bare ``statement_timeout`` connect arg is accepted and then ignored, so
        the setting used to be configurable and inert at the same time.
        """
        from adoptimizer.infra.db.session import Database

        cfg = DatabaseSettings(url=PROD_DATABASE, statement_timeout_ms=15_000)
        assert Database(cfg)._engine_kwargs()["connect_args"] == {
            "server_settings": {"statement_timeout": "15000"}
        }

    def test_no_statement_timeout_leaves_connect_args_alone(self) -> None:
        from adoptimizer.infra.db.session import Database

        cfg = DatabaseSettings(url=PROD_DATABASE)
        assert "connect_args" not in Database(cfg)._engine_kwargs()

    def test_sqlite_never_receives_server_settings(self) -> None:
        from adoptimizer.infra.db.session import Database

        cfg = DatabaseSettings(url="sqlite+aiosqlite:///:memory:", statement_timeout_ms=15_000)
        kwargs = Database(cfg)._engine_kwargs()
        assert kwargs["connect_args"] == {"check_same_thread": False}


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

    def test_a_blank_pricing_env_var_means_no_override(self, clean_env: pytest.MonkeyPatch) -> None:
        """Blank is how `.env.example` says "use the built-in table", not a typo.

        An empty string survives pydantic-settings and reaches the validator as
        `''`. A whitespace-only one does not: the JSON decoder rejects it first
        and pydantic-settings raises `SettingsError` before validation starts.
        So the environment path only has to handle exactly-empty, and the
        whitespace case below is about direct construction.
        """
        clean_env.setenv("LLM__PRICING", "")

        assert load().llm.pricing == {}

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_pricing_value_means_no_override_when_set_directly(
        self, blank: str | None
    ) -> None:
        assert LLMSettings(pricing=blank).pricing == {}  # type: ignore[arg-type]

    def test_a_real_pricing_override_still_wins_over_the_blank_handling(
        self, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("LLM__PRICING", '{"qwen-plus": [0.113, 0.282]}')

        assert load().llm.pricing == {"qwen-plus": (0.113, 0.282)}


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


def _settings_env_names() -> set[str]:
    """Every environment variable the settings model actually reads."""
    names: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for field_name, field in model.model_fields.items():
            env = f"{prefix}__{field_name}".upper() if prefix else field_name.upper()
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation, env)
            else:
                names.add(env)

    walk(Settings, "")
    return names


def _documented_env_names() -> set[str]:
    """Keys named in .env.example, counting the commented-out examples.

    A commented key is still documentation - the operator copies it in - so the
    check has to look past the ``#``.
    """
    text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example").read_text(
        encoding="utf-8"
    )
    pattern = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)
    return set(pattern.findall(text))


class TestEnvExampleCoversTheSettingsModel:
    """The settings model and .env.example are two copies of one list.

    They drift in both directions and neither failure is loud: a setting with no
    example is a knob nobody knows exists, and an example with no setting is a
    line an operator can add that changes nothing at all.
    """

    def test_every_setting_is_documented(self) -> None:
        undocumented = sorted(_settings_env_names() - _documented_env_names())
        assert not undocumented, "no example in .env.example: " + ", ".join(undocumented)

    def test_every_documented_key_is_a_setting_or_a_platform_credential(self) -> None:
        from adoptimizer.cli import CREDENTIAL_KEYS, OPTIONAL_CREDENTIAL_KEYS

        # Platform credentials are read with ``os.getenv`` by the adapters rather
        # than through the settings model, because they are forwarded into the
        # process environment by ``load_environment()``.
        credentials = {
            key
            for group in (*CREDENTIAL_KEYS.values(), *OPTIONAL_CREDENTIAL_KEYS.values())
            for key in group
        }
        unknown = sorted(_documented_env_names() - _settings_env_names() - credentials)
        assert not unknown, "documented but read by nothing: " + ", ".join(unknown)


# Settings that nothing reads, with the reason each one is inert. Listing them is
# what turns "declared and then forgotten" into a recorded decision -- the same
# treatment ``RateLimitSettings.burst`` already carries in its own docstring.
#
# An entry here is a debt, not an endorsement: the honest reason for most of
# them is "the subsystem was never built".
SETTINGS_WITHOUT_A_READER: dict[str, str] = {
    "app.max_request_body_bytes": "no body-size guard is installed; do not trust this cap",
    "clickhouse.native_port": "the client speaks the HTTP interface, not the native protocol",
    "observability.log_request_body": "request-body logging is not implemented",
    "observability.otlp_endpoint": "OTLP export is not implemented",
    "observability.service_name": "only meaningful once OTLP export exists",
    "observability.tracing_enabled": "OTLP export is not implemented",
    "rate_limit.burst": "fixed-window limiter has no burst allowance; see RateLimitSettings",
}


def _settings_leaf_paths() -> set[str]:
    """Dotted path of every scalar setting, e.g. ``observability.otlp_endpoint``."""
    paths: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for field_name, field in model.model_fields.items():
            path = f"{prefix}.{field_name}" if prefix else field_name
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation, path)
            else:
                paths.add(path)

    walk(Settings, "")
    return paths


def _attribute_names_read_outside_config() -> set[str]:
    """Every attribute name read anywhere in the package except config.py."""
    package = pathlib.Path(adoptimizer.__file__).parent
    blob = "\n".join(
        path.read_text(encoding="utf-8")
        for path in package.rglob("*.py")
        if path.name != "config.py"
    )
    return set(re.findall(r"\.([a-z_][a-z0-9_]*)", blob))


class TestEverySettingIsRead:
    """A setting nobody reads is worse than a missing one: it validates at boot,
    appears in .env.example, and changes nothing.

    This matches on the leaf attribute name, so it is a **lower bound** on dead
    settings: a field called ``enabled`` or ``name`` looks read because some
    other object has that attribute. It still catches the whole class of
    uniquely-named knobs, which is where the drift actually accumulates.
    """

    def test_every_setting_is_read_or_declared_inert(self) -> None:
        read = _attribute_names_read_outside_config()
        dead = sorted(
            path
            for path in _settings_leaf_paths()
            if path.rsplit(".", 1)[-1] not in read and path not in SETTINGS_WITHOUT_A_READER
        )
        assert not dead, "declared, validated, and then ignored: " + ", ".join(dead)

    def test_the_inert_list_has_no_stale_entries(self) -> None:
        stale = sorted(set(SETTINGS_WITHOUT_A_READER) - _settings_leaf_paths())
        assert not stale, "no longer a setting: " + ", ".join(stale)

    def test_every_inert_entry_carries_a_reason(self) -> None:
        for path, reason in SETTINGS_WITHOUT_A_READER.items():
            assert reason.strip(), path + " needs a reason"
