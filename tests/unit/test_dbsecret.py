from __future__ import annotations

import json
import os

import pytest

from trail.config import get_settings
from trail.dbsecret import apply_secrets

pytestmark = pytest.mark.unit


def test_without_an_arn_nothing_happens(monkeypatch) -> None:
    monkeypatch.delenv("PGPASSWORD", raising=False)
    assert apply_secrets(get_settings(), fetch=lambda arn: "never") is False
    assert "PGPASSWORD" not in os.environ


def test_the_secret_password_becomes_pgpassword(monkeypatch) -> None:
    monkeypatch.setenv(
        "TRAIL_DATABASE_SECRET_ARN", "arn:aws:secretsmanager:sa-east-1:1:secret:x"
    )
    get_settings.cache_clear()
    seen: list[str] = []

    def fetch(arn: str) -> str:
        seen.append(arn)
        return json.dumps({"username": "trail", "password": "s3cr3t", "host": "h"})

    assert apply_secrets(get_settings(), fetch=fetch) is True
    assert os.environ["PGPASSWORD"] == "s3cr3t"
    assert seen == ["arn:aws:secretsmanager:sa-east-1:1:secret:x"]


def test_the_confirmation_secret_reaches_settings(monkeypatch) -> None:
    monkeypatch.setenv(
        "TRAIL_CONFIRMATION_SECRET_ARN", "arn:aws:secretsmanager:sa-east-1:1:secret:c"
    )
    get_settings.cache_clear()
    assert apply_secrets(get_settings(), fetch=lambda arn: "plain-text-key") is True
    assert get_settings().confirmation_secret.get_secret_value() == "plain-text-key"
