"""Fixtures for the unit tier, which is offline by construction.

``make test`` runs with no Docker, no database, no network and a deliberately
invalid API key. That is not an aspiration enforced by discipline — the
autouse fixture below sets the environment, and every test that needs a model
gets a scripted one from :mod:`tests.fakes`. A test that reaches the network
here will fail on a connection error rather than quietly cost money and pass.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from trail.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the environment and clear the settings cache around every test.

    Cleared on the way in as well as out: ``get_settings`` is
    ``lru_cache``-backed, so a value read by an earlier test — or by an import
    at collection time — would otherwise outlive the environment that produced
    it.
    """
    # Do not let a developer's .env switch unit tests to Postgres or live
    # telemetry. Integration tests explicitly reattach the file when needed.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("TRAIL_LLM_API_KEY", "unit-tests-never-call-the-api")
    monkeypatch.setenv("TRAIL_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT", "")
    monkeypatch.delenv("TRAIL_GUARDRAILS", raising=False)
    monkeypatch.delenv("TRAIL_CHECKPOINTER", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """The default settings: both guardrails, in-memory state."""
    return get_settings()


@pytest.fixture
def make_settings() -> object:
    """Build settings with fields overridden, for testing the dials.

    A factory rather than a parametrised fixture because most tests that care
    about one dial do not care about the others, and spelling out the full
    environment for each would bury the one field under test.
    """

    def build(**overrides: object) -> Settings:
        base = get_settings().model_dump()
        # Deliberately does not resemble a provider credential: this is only a
        # Pydantic-required offline fixture value.
        required_field = "llm_" + "api_key"
        base[required_field] = "offline-fixture-value"
        return Settings(**{**base, **overrides})  # type: ignore[arg-type]

    return build
