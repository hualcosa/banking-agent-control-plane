"""Postgres pool limits shared by every store this process opens.

AgentCore keeps idle runtime sessions alive; each session historically opened
four psycopg pools at default size and exhausted RDS. Every pool uses the same
tiny cap and TCP keepalives so stale connections fail fast instead of aborting
mid-query.

Keepalives are not enough on their own: an idle AgentCore session gets no CPU,
so neither the kernel's keepalive probes nor the pool's maintenance tasks run,
and a connection can die unnoticed between turns. Every pool therefore checks
a connection on checkout and replaces it if the check fails — one round trip
per checkout, paid instead of a 500 on the first turn after an idle gap.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

POOL_MIN_SIZE = 0
POOL_MAX_SIZE = 2

_KEEPALIVE: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
}


def sync_connect_kwargs() -> dict[str, Any]:
    return {"row_factory": dict_row, **_KEEPALIVE}


def async_connect_kwargs() -> dict[str, Any]:
    return {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        **_KEEPALIVE,
    }


def sync_pool(dsn: str, *, open: bool = True) -> ConnectionPool:
    return ConnectionPool(
        dsn,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        kwargs=sync_connect_kwargs(),
        check=ConnectionPool.check_connection,
        open=open,
    )


def async_pool_options() -> dict[str, Any]:
    """Keyword arguments for an ``AsyncConnectionPool`` or LangGraph ``PoolConfig``."""
    return {
        "min_size": POOL_MIN_SIZE,
        "max_size": POOL_MAX_SIZE,
        "kwargs": async_connect_kwargs(),
        "check": AsyncConnectionPool.check_connection,
    }
