"""Where conversation state lives between turns, as a swappable slot.

Two stores, and the distinction is the one LangGraph draws:

**Checkpointer** — thread-scoped, short-term. Snapshots of graph state, keyed by
``thread_id``. This is the conversation: what was said, which tools ran, where
the graph paused. Resuming a thread is reading its last checkpoint.

**Store** — cross-thread, long-term. Application-defined JSON under a
namespace. This is what the agent knows about a *person* rather than about a
*conversation*, and it is what makes a brand-new thread able to greet someone
by the preference they stated last week.

Both are constructor arguments to ``create_agent``, which is the whole reason
this file is a registry and not an implementation: the unit suite runs against
``memory``, a laptop runs against ``memory``, production runs against
``postgres``, and the agent code is identical in all three. Persistence,
resumption, time travel and fault tolerance are exactly the undifferentiated
heavy lifting this scaffold exists to not write.

The one operational trap: ``from_conn_string`` returns an async context
manager. Entering it inside a request handler closes the pool when the request
ends. It has to be entered in the application lifespan and held for the life of
the process — see :func:`open_persistence`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

logger = logging.getLogger(__name__)

#: The valid values of ``TRAIL_CHECKPOINTER``, named here so an unknown one
#: fails at startup with the set in the message rather than at the first turn.
KINDS = ("memory", "postgres")


@dataclass(frozen=True)
class Persistence:
    """The pair a compiled agent needs. Either may be ``None``."""

    checkpointer: Any
    store: Any
    kind: str

    @property
    def durable(self) -> bool:
        """Whether a thread survives the process that created it."""
        return self.kind != "memory"


@asynccontextmanager
async def open_persistence(kind: str, database_url: str) -> AsyncIterator[Persistence]:
    """Yield a checkpointer and a store for ``kind``, held open for the block.

    Use this in a FastAPI lifespan, never per request::

        @asynccontextmanager
        async def lifespan(app):
            async with open_persistence(settings.checkpointer, settings.database_url) as p:
                app.state.agent = build_agent(..., persistence=p)
                yield

    ``memory`` needs no teardown and loses every thread on restart, which is
    the correct default for tests and a first run and the wrong one for
    anything a user will come back to.
    """
    if kind not in KINDS:
        valid = ", ".join(KINDS)
        raise ValueError(
            f"unknown checkpointer {kind!r}; TRAIL_CHECKPOINTER must be one of: {valid}"
        )

    if kind == "memory":
        yield Persistence(
            checkpointer=InMemorySaver(), store=InMemoryStore(), kind=kind
        )
        return

    # Imported here rather than at module scope so the unit suite — which runs
    # with no database and no `psycopg` connection — can import this module.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore
    from langgraph.store.postgres.base import PoolConfig
    from psycopg_pool import AsyncConnectionPool

    from control_plane.pg_pool import async_pool_options

    async with (
        AsyncConnectionPool(database_url, **async_pool_options()) as checkpointer_pool,
        AsyncPostgresStore.from_conn_string(
            database_url,
            # PoolConfig forwards extra keys to AsyncConnectionPool, `check` included.
            pool_config=PoolConfig(**async_pool_options()),  # type: ignore[typeddict-item]
        ) as store,
    ):
        checkpointer = AsyncPostgresSaver(conn=checkpointer_pool)
        # Creates the checkpoint and store tables if they are absent. Skipping
        # it produces a "relation does not exist" error on the first turn that
        # reads like a connection problem and is not one.
        await checkpointer.setup()
        await store.setup()
        logger.info("persistence ready: postgres")
        yield Persistence(checkpointer=checkpointer, store=store, kind=kind)
