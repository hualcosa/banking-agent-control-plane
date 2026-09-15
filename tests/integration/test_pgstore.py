"""`PgStore` against a real database — the half of the store nothing else covers.

Every assertion here is one that `MemoryStore` cannot fail: the object the
plane mutates *is* the object the store holds, so in memory a lost write is
invisible. The question this file asks is the one that only has a wrong answer
in production — does the state survive the process?

"A restart" is modelled the way the crash harness models it: discard the
`ControlPlane` and build a new one over a store pointed at the same database.
That is what a container coming back up looks like, and it is why these tests
need Postgres and cannot be faked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, MockBank, ProposedPix
from control_plane.pgstore import PgStore

pytestmark = pytest.mark.integration

MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
CTX = Context(customer_id="cust_123", session_id="thread-1")
OTHER = Context(customer_id="cust_999", session_id="thread-9")


def pix(recipient: str, amount: str) -> ProposedPix:
    return ProposedPix(recipient=recipient, amount=Decimal(amount))


@pytest.fixture
def dsn() -> str:
    url = os.environ.get("TRAIL_DATABASE_URL")
    if not url:
        pytest.skip("TRAIL_DATABASE_URL unset. Run `make up && make test-integration`.")
    return url


@pytest.fixture
def store(dsn: str) -> Iterator[PgStore]:
    """A store on a clean pair of tables.

    Truncating rather than dropping keeps the schema the startup path applied,
    which is itself under test: if `ensure_schema` did not run, TRUNCATE fails
    with "relation does not exist" and names the real problem.
    """
    store = PgStore(dsn)
    with store.pool.connection() as conn:
        conn.execute("TRUNCATE intents, ledger_events")
    yield store
    store.close()


@pytest.fixture
def bank() -> MockBank:
    return MockBank()


@pytest.fixture
def plane(bank: MockBank, store: PgStore) -> ControlPlane:
    return ControlPlane(bank, clock=lambda: MIDDAY, store=store)


def test_the_schema_is_applied_on_startup_not_by_initdb(dsn: str) -> None:
    """initdb only runs on an empty volume, so a developer whose volume
    predates these tables would otherwise get "relation does not exist"
    forever. Constructing a second store must be harmless."""
    first, second = PgStore(dsn), PgStore(dsn)
    with second.pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM intents").fetchone()
    assert row["n"] >= 0
    first.close()
    second.close()


def test_an_intent_survives_the_process_that_created_it(
    plane: ControlPlane, bank: MockBank, dsn: str
) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    assert plane.confirm(CTX, proposed.confirmation_id).status == "COMPLETED"

    reborn = ControlPlane(bank, clock=lambda: MIDDAY, store=PgStore(dsn))
    assert reborn.status(CTX, proposed.intent_id).status == "COMPLETED"
    assert reborn.explain(CTX, proposed.intent_id) != []


def test_confirming_again_after_a_restart_does_not_pay_twice(
    plane: ControlPlane, bank: MockBank, dsn: str
) -> None:
    """Invariant I1, across the boundary that makes it hard. The idempotency
    key is the intent id, and the intent id came back out of the database."""
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, proposed.confirmation_id)
    assert len(bank.payments) == 1

    reborn = ControlPlane(bank, clock=lambda: MIDDAY, store=PgStore(dsn))
    again = reborn.confirm(CTX, proposed.confirmation_id)
    assert again.status == "COMPLETED"
    assert len(bank.payments) == 1


def test_assurance_survives_the_round_trip(
    plane: ControlPlane, bank: MockBank, dsn: str
) -> None:
    """The column that would be easiest to forget. If `context` were stored as
    customer/session/channel only, a stepped-up intent would come back
    medium-assurance and policy would demand an authentication the customer
    already completed."""
    proposed = plane.propose(CTX, pix("João", "1500"))
    assert proposed.status == "REQUIRE_STEP_UP_AUTH"
    plane.step_up(CTX, proposed.intent_id)

    reborn = ControlPlane(bank, clock=lambda: MIDDAY, store=PgStore(dsn))
    revived = reborn.store.get(proposed.intent_id)
    assert revived.context.assurance == "strong"
    assert revived.state == "AWAITING_CONFIRMATION"


def test_ownership_holds_across_a_restart(
    plane: ControlPlane, bank: MockBank, dsn: str
) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    reborn = ControlPlane(bank, clock=lambda: MIDDAY, store=PgStore(dsn))
    assert reborn.confirm(OTHER, proposed.confirmation_id).status == "DENY"
    assert reborn.explain(OTHER, proposed.intent_id) == []
    assert bank.payments == {}


def test_the_trail_comes_back_in_the_order_it_happened(plane: ControlPlane) -> None:
    """Ordered by `seq`, not by clock: events written in one transaction can
    share a timestamp and still have an order."""
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, proposed.confirmation_id)
    kinds = [e.kind for e in plane.explain(CTX, proposed.intent_id)]
    assert kinds.index("policy") < kinds.index("confirmation")
    assert kinds.index("confirmation") < kinds.index("execution_request")
    assert kinds.index("execution_request") < kinds.index("backend_response")


def test_unsettled_is_what_a_restart_sweep_would_ask(plane: ControlPlane) -> None:
    """The restart sweep's query, against the index built for it."""
    waiting = plane.propose(CTX, pix("Renata", "300"))
    assert {i.id for i in plane.store.unsettled(["AWAITING_CONFIRMATION"])} == {
        waiting.intent_id
    }
    assert plane.store.unsettled(["SUBMITTED"]) == []
    plane.confirm(CTX, waiting.confirmation_id)
    assert plane.store.unsettled(["AWAITING_CONFIRMATION"]) == []


def test_a_confirmation_id_cannot_name_two_intents(plane: ControlPlane) -> None:
    """Enforced by a unique index rather than by hoping. Two intents sharing a
    token is not a duplicate row, it is a payment authorised twice."""
    a = plane.propose(CTX, pix("Renata", "300"))
    b = plane.propose(CTX, pix("Renata", "400"))
    assert a.confirmation_id != b.confirmation_id

    stolen = plane.store.get(b.intent_id)
    stolen.confirmation_id = a.confirmation_id
    with pytest.raises(Exception, match=r"intents_confirmation_idx|unique"):
        plane.store.put(stolen)
