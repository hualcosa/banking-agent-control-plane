"""Postgres pool limits are pinned without a live database."""

from __future__ import annotations

import pytest

from control_plane.pg_pool import (
    POOL_MAX_SIZE,
    POOL_MIN_SIZE,
    async_connect_kwargs,
    sync_connect_kwargs,
)

pytestmark = pytest.mark.unit


def test_every_pool_is_capped() -> None:
    assert POOL_MIN_SIZE == 0
    assert POOL_MAX_SIZE == 2


def test_keepalives_are_on_every_connection() -> None:
    for kwargs in (sync_connect_kwargs(), async_connect_kwargs()):
        assert kwargs["keepalives"] == 1
        assert kwargs["keepalives_idle"] == 30
        assert kwargs["keepalives_interval"] == 10


def test_every_pool_checks_a_connection_before_handing_it_out() -> None:
    # AgentCore gives idle sessions no CPU, so keepalives never fire and the
    # pool's own maintenance never runs; the first turn after an idle gap got a
    # dead connection and returned 500. Checkout has to test the connection.
    from psycopg_pool import AsyncConnectionPool, ConnectionPool

    from control_plane.pg_pool import async_pool_options, sync_pool

    pool = sync_pool("postgresql://unused@127.0.0.1:1/none", open=False)
    assert pool._check is ConnectionPool.check_connection
    assert async_pool_options()["check"] is AsyncConnectionPool.check_connection
