"""The same `Store`, over Postgres. State that survives the process.

Its own module for a reason that is not taste: every line here needs a
database to execute, so it can only be covered by the integration tier, and
the unit tier's ``fail_under = 90`` would fail on the day this file was
added. ``pyproject.toml`` omits it from coverage in the same commit that
creates it — a gate that has to be argued with is a gate nobody trusts.

**Synchronous on purpose.** LangChain runs `def` tools in a threadpool, so a
synchronous ``ConnectionPool`` is reached from a tool without blocking the
event loop, and the plane's 33 tests stay synchronous. Converting the plane
to async would have been the same work spread across every caller.

**The schema is applied at startup, not by initdb.** Postgres only runs
``docker-entrypoint-initdb.d`` on an *empty* volume, so a developer whose
volume predates these tables would get "relation does not exist" forever.
``trail.evals.store`` already solved this; this does the same, from the same
file, which is why every statement in ``db/schema.sql`` is idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from control_plane.actions import Context, CreatePix
from control_plane.state import Event, Intent

#: Written by hand rather than imported from ``trail`` — ``control_plane`` does
#: not depend on the runtime, and that independence is the repository's thesis.
SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

_COLUMNS = (
    "id, customer_id, session_id, context, action, state, created_at, "
    "confirmation_id, confirmation_issued_at, action_digest, "
    "confirmed_at, confirmed_by, receipt, reason"
)


def _to_intent(row: Mapping[str, Any]) -> Intent:
    """One row back into the object the plane mutates."""
    return Intent(
        id=row["id"],
        action=CreatePix.model_validate(row["action"]),
        context=Context.model_validate(row["context"]),
        state=row["state"],
        created_at=row["created_at"],
        confirmation_id=row["confirmation_id"],
        confirmation_issued_at=row["confirmation_issued_at"],
        action_digest=row["action_digest"],
        confirmed_at=row["confirmed_at"],
        confirmed_by=row["confirmed_by"],
        receipt=row["receipt"],
        reason=row["reason"],
    )


class PgStore:
    """`Store` over Postgres. Same five questions, same answers, durable."""

    def __init__(self, dsn: str, *, apply_schema: bool = True) -> None:
        self.pool = ConnectionPool(dsn, kwargs={"row_factory": dict_row}, open=True)
        if apply_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """Apply ``db/schema.sql``. Idempotent, and the reason is in the docstring."""
        if not SCHEMA.exists():  # pragma: no cover - packaging accident
            return
        with self.pool.connection() as conn:
            conn.execute(SCHEMA.read_text())

    def close(self) -> None:
        self.pool.close()

    # ------------------------------------------------------------------
    # the five questions
    # ------------------------------------------------------------------

    def get(self, intent_id: str) -> Intent | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE id = %s", (intent_id,)
            ).fetchone()
        return None if row is None else _to_intent(row)

    def by_confirmation(self, confirmation_id: str) -> Intent | None:
        """At most one — enforced by a unique index, not by hoping."""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE confirmation_id = %s",
                (confirmation_id,),
            ).fetchone()
        return None if row is None else _to_intent(row)

    def unsettled(self, states: Iterable[str]) -> list[Intent]:
        wanted = list(states)
        if not wanted:
            return []
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM intents WHERE state = ANY(%s) "
                "ORDER BY created_at",
                (wanted,),
            ).fetchall()
        return [_to_intent(r) for r in rows]

    def put(self, intent: Intent) -> None:
        """Upsert. The plane calls this after every mutation; here it is the
        only thing standing between a state change and losing it."""
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO intents (
                    id, customer_id, session_id, context, action, state,
                    created_at, confirmation_id, confirmation_issued_at,
                    action_digest, confirmed_at, confirmed_by, receipt, reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    context         = EXCLUDED.context,
                    action          = EXCLUDED.action,
                    state           = EXCLUDED.state,
                    confirmation_id = EXCLUDED.confirmation_id,
                    confirmation_issued_at = EXCLUDED.confirmation_issued_at,
                    action_digest   = EXCLUDED.action_digest,
                    confirmed_at    = EXCLUDED.confirmed_at,
                    confirmed_by    = EXCLUDED.confirmed_by,
                    receipt         = EXCLUDED.receipt,
                    reason          = EXCLUDED.reason
                """,
                (
                    intent.id,
                    intent.context.customer_id,
                    intent.context.session_id,
                    Jsonb(intent.context.model_dump(mode="json")),
                    Jsonb(intent.action.model_dump(mode="json")),
                    intent.state,
                    intent.created_at,
                    intent.confirmation_id,
                    intent.confirmation_issued_at,
                    intent.action_digest,
                    intent.confirmed_at,
                    intent.confirmed_by,
                    None if intent.receipt is None else Jsonb(intent.receipt),
                    intent.reason,
                ),
            )

    def append(self, intent_id: str, kind: str, **detail: Any) -> Event:
        """One row, committed on its own.

        The transaction boundary is the point: T7's ``execution_request`` has
        to be durable *before* the bank is called, so this cannot join a
        caller's larger transaction and be rolled back with it.
        """
        payload = json.loads(json.dumps(detail, default=str))
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO ledger_events (intent_id, kind, detail) "
                "VALUES (%s, %s, %s) RETURNING at",
                (intent_id, kind, Jsonb(payload)),
            ).fetchone()
        return Event(at=row["at"], intent_id=intent_id, kind=kind, detail=payload)

    def events_for(self, intent_id: str) -> list[Event]:
        """In `seq` order, not clock order: two events written in the same
        transaction can share a timestamp and still have happened in order."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT at, intent_id, kind, detail FROM ledger_events "
                "WHERE intent_id = %s ORDER BY seq",
                (intent_id,),
            ).fetchall()
        return [
            Event(
                at=r["at"], intent_id=r["intent_id"], kind=r["kind"], detail=r["detail"]
            )
            for r in rows
        ]

    def __len__(self) -> int:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT count(*) AS n FROM ledger_events").fetchone()
        return int(row["n"])

    def snapshot(self) -> Mapping[str, Intent]:
        """Every intent. A reading affordance — never call it on a request."""
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM intents").fetchall()
        return {r["id"]: _to_intent(r) for r in rows}
