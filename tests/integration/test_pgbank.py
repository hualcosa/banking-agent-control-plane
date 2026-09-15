"""`PgBank` against a real database: the book survives the process that paid.

The scenario is the runbook's: one process pays an amount that trips the
timeout, the intent lands in UNKNOWN, and a *different* plane — new bank
object, new store object, same DSN — reconciles it to COMPLETED. With
`MockBank` the second plane answers FAILED, which docs/runbook.md §6 warned
about; this file is the test that the warning can now be deleted.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, ProposedPix
from control_plane.pgbank import PgBank
from control_plane.pgstore import PgStore

pytestmark = pytest.mark.integration

MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
CTX = Context(customer_id="cust_pgbank", session_id="thread-pgbank")


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("TRAIL_DATABASE_URL")
    if not url:
        pytest.skip("TRAIL_DATABASE_URL unset. Run `make up && make test-integration`.")
    return url


@pytest.fixture
def clean(dsn: str) -> Iterator[str]:
    store = PgStore(dsn)
    with store.pool.connection() as conn:
        conn.execute("TRUNCATE intents, ledger_events, bank_payments")
    store.close()
    yield dsn


def plane_over(dsn: str) -> ControlPlane:
    return ControlPlane(
        PgBank(dsn=dsn), clock=lambda: MIDDAY, store=PgStore(dsn), secret="s"
    )


def test_a_payment_made_by_one_process_is_found_by_another(clean: str) -> None:
    first = plane_over(clean)
    proposed = first.propose(
        CTX, ProposedPix(recipient="Renata", amount=Decimal("10.13"))
    )
    assert proposed.status == "REQUIRE_CONFIRMATION"
    outcome = first.confirm(CTX, proposed.confirmation_id)
    assert outcome.status == "UNKNOWN"
    assert first.bank.known_payments() == 1

    second = plane_over(clean)  # "the operator's process"
    assert second.bank.known_payments() == 1
    reconciled = second.reconcile(CTX, outcome.intent_id)
    assert reconciled.status == "COMPLETED"
    assert reconciled.data["receipt"]["amount"] == "10.13"


def test_the_same_key_twice_debits_once_across_processes(clean: str) -> None:
    a = PgBank(dsn=clean)
    b = PgBank(dsn=clean)
    kw = {
        "idempotency_key": "k-shared",
        "source_account": "checking_001",
        "recipient_id": "contact_renata",
        "amount": Decimal("5"),
    }
    r1 = a.create_pix(**kw)
    r2 = b.create_pix(**kw)
    assert r1["payment_id"] == r2["payment_id"]
    assert a.known_payments() == 1
