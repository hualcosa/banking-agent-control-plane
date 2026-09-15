"""`MockBank` whose book of payments lives in Postgres.

Still a mock — the same contacts, the same opening balance, the same `,13`
timeout tripwire. What moves is the one piece of state that another process
has to be able to read: `payments`. `trail reconcile` builds its own bank in
its own process; with `MockBank` that book is empty and every answer is
"never paid" (docs/runbook.md §6). With this class the operator asks the same
table the agent wrote, which is what makes an `UNKNOWN` resolvable at 3am.

Balances and `paid_before` stay in memory, per conversation, on purpose: they
are policy inputs and sharing them across conversations is the measurement
bug that per-conversation planes removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

from psycopg.types.json import Jsonb

from control_plane.bank import (
    TIMEOUT_CENTS,
    BankError,
    BankTimeout,
    InsufficientFunds,
    MockBank,
)
from control_plane.pg_pool import sync_pool
from control_plane.pgstore import SCHEMA
from control_plane.state import new_id


@dataclass
class PgBank(MockBank):
    """One customer's worth of bank; the payments table is shared."""

    dsn: str = ""
    pool: ConnectionPool | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.pool is None:
            self.pool = sync_pool(self.dsn)
            if SCHEMA.exists():
                with self.pool.connection() as conn:
                    conn.execute(SCHEMA.read_text())

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()

    def fresh(self) -> PgBank:
        """Same book, opening balance — the per-conversation scope `plane_for` needs."""
        return replace(
            self,
            accounts=dict(self._opening_accounts),
            paid_before=set(self._opening_paid_before),
            payments={},
            pool=self.pool,
        )

    # --- the book, in Postgres -------------------------------------------

    def payment(self, idempotency_key: str) -> dict[str, Any] | None:
        assert self.pool is not None
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT receipt FROM bank_payments WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
        return None if row is None else row["receipt"]

    def known_payments(self) -> int:
        assert self.pool is not None
        with self.pool.connection() as conn:
            row = conn.execute("SELECT count(*) AS n FROM bank_payments").fetchone()
        return int(row["n"]) if row else 0

    def create_pix(
        self,
        *,
        idempotency_key: str,
        source_account: str,
        recipient_id: str,
        amount: Decimal,
    ) -> dict[str, Any]:
        if (existing := self.payment(idempotency_key)) is not None:
            return existing
        if self.contact(recipient_id) is None:
            raise BankError(f"unknown recipient: {recipient_id}")
        if self.balance(source_account) < amount:
            raise InsufficientFunds("insufficient funds")

        self.accounts[source_account] -= amount
        self.paid_before.add(recipient_id)
        receipt = {
            "payment_id": new_id("pay"),
            "end_to_end_id": f"E{new_id('')[1:].upper()}",
            "status": "COMPLETED",
            "amount": str(amount),
            "recipient_id": recipient_id,
        }
        assert self.pool is not None
        with self.pool.connection() as conn:
            # ON CONFLICT DO NOTHING + re-read: two processes racing on the same
            # key both end up returning the row that won, which is what an
            # idempotency key promises.
            conn.execute(
                "INSERT INTO bank_payments (idempotency_key, receipt) VALUES (%s, %s) "
                "ON CONFLICT (idempotency_key) DO NOTHING",
                (idempotency_key, Jsonb(receipt)),
            )
        stored = self.payment(idempotency_key) or receipt
        if amount % 1 == TIMEOUT_CENTS:
            raise BankTimeout("the bank did not answer")
        return stored
