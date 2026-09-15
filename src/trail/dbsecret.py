"""Secrets from Secrets Manager into the places the process reads them.

`TRAIL_DATABASE_URL` carries no password on purpose (see `config.py`): libpq
takes it from `PGPASSWORD`. In compose that variable comes from `.env`. In
AWS it comes from the secret RDS generated, and this is the one function that
moves it — at boot, once, into the environment, never into a log or a DSN.
The confirmation key travels the same way: `TRAIL_CONFIRMATION_SECRET` is
what `ControlPlane` seals a confirmed action with, and a deployment that
shipped the dev default would have a confirmation anyone can forge.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from trail.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _fetch_with_boto3(arn: str) -> str:
    import boto3  # extra `aws`; imported here so the default install stays SDK-free

    return boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]


def apply_secrets(
    settings: Settings, *, fetch: Callable[[str], str] | None = None
) -> bool:
    """Populate `PGPASSWORD` and `TRAIL_CONFIRMATION_SECRET`. `False` if neither ARN is set."""
    get = fetch or _fetch_with_boto3
    applied = False
    db_arn = settings.database_secret_arn.strip()
    if db_arn:
        secret = json.loads(get(db_arn))
        os.environ["PGPASSWORD"] = str(secret["password"])
        logger.info("database password loaded from secret …%s", db_arn[-6:])
        applied = True
    conf_arn = settings.confirmation_secret_arn.strip()
    if conf_arn:
        os.environ["TRAIL_CONFIRMATION_SECRET"] = get(conf_arn).strip()
        get_settings.cache_clear()  # the plane reads it through Settings, later
        logger.info("confirmation secret loaded from secret …%s", conf_arn[-6:])
        applied = True
    return applied
