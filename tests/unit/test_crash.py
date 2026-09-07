"""The crash window (adversary **A15**), and what it costs today.

`plane._execute` writes `execution_request`, calls the bank, then writes the
receipt. A process that dies between those two lines leaves an intent in
`SUBMITTED` with the money possibly gone — and `SUBMITTED` has no way out
that anything currently takes: `reconcile` only accepts `UNKNOWN`, and the
`("SUBMITTED", "timeout")` edge exists in `state.py` with nobody to fire it.

`FaultyBank` puts that window under a test's control, and a "restart" here is
literal: throw the `ControlPlane` away and build a new one over the *same*
`MemoryStore`. That is exactly what durable storage buys, which is why this
is provable before Postgres exists.

These tests assert what is true today, gaps included. Every assertion marked
"the gap" is a fact this repository intends to stop being true — **T7 closes
it** with the restart sweep. What is asserted as a guarantee, and must never
regress, is the money: the bank is paid exactly once, across the crash and
across the restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, MemoryStore, ProposedPix
from tests.fakes import Crash, FaultyBank

pytestmark = pytest.mark.unit

CTX = Context(customer_id="cust_123", session_id="thread-1")
MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
OPENING_BALANCE = Decimal("2543.10")
AMOUNT = Decimal("300")


def pix(recipient: str, amount: Decimal) -> ProposedPix:
    return ProposedPix(recipient=recipient, amount=amount)


def plane_over(bank: FaultyBank, store: MemoryStore) -> ControlPlane:
    """A fresh process over storage that outlived the last one."""
    return ControlPlane(bank, clock=lambda: MIDDAY, store=store)


def crash_mid_payment(bank: FaultyBank, store: MemoryStore) -> str:
    """Drive one PIX until the process dies inside the bank call.

    Returns the intent id. The plane built here is deliberately not returned:
    it is the object the crash destroyed.
    """
    plane = plane_over(bank, store)
    proposed = plane.propose(CTX, pix("Renata", AMOUNT))
    assert proposed.status == "REQUIRE_CONFIRMATION"
    with pytest.raises(Crash):
        plane.confirm(CTX, proposed.confirmation_id)
    return proposed.intent_id


def kinds(store: MemoryStore, intent_id: str) -> list[str]:
    return [e.kind for e in store.events_for(intent_id)]


def test_the_crash_lands_inside_the_window_it_claims_to_test() -> None:
    """The instrument itself: the bank paid, and the ledger stops one line
    short of saying so. If this ever fails, every other test in this file is
    testing some other window."""
    bank, store = FaultyBank(crash_at="after_pay"), MemoryStore()
    intent_id = crash_mid_payment(bank, store)

    trail = kinds(store, intent_id)
    assert "execution_request" in trail
    assert "backend_response" not in trail
    assert bank.payment(intent_id) is not None  # idempotency_key == intent id
    assert store.get(intent_id).receipt is None


def test_after_a_crash_the_intent_is_stranded_in_submitted() -> None:
    """The gap. A restart inherits an intent that is neither settled nor
    moving, and no code path takes it anywhere. **T7 closes this** by sweeping
    `SUBMITTED → UNKNOWN` on start-up; until then this is the honest state of
    the system and the test says so out loud."""
    bank, store = FaultyBank(crash_at="after_pay"), MemoryStore()
    intent_id = crash_mid_payment(bank, store)

    restarted = plane_over(bank, store)
    assert restarted.store.get(intent_id).state == "SUBMITTED"
    assert restarted.status(CTX, intent_id).data["state"] == "SUBMITTED"
    # The query T7 will ask on start-up already answers. Nobody asks it yet.
    assert [i.id for i in store.unsettled(["SUBMITTED"])] == [intent_id]


def test_reconcile_refuses_a_stranded_intent_because_it_only_takes_unknown() -> None:
    """The gap, from the customer's side: the one operation that could find
    the money declines to look, because the intent never reached `UNKNOWN`.
    **T7 closes this** — the sweep is what puts it there. Note the refusal is
    still safe: refusing to look never pays twice."""
    bank, store = FaultyBank(crash_at="after_pay"), MemoryStore()
    intent_id = crash_mid_payment(bank, store)

    restarted = plane_over(bank, store)
    out = restarted.reconcile(CTX, intent_id)
    assert out.status == "DENY"
    assert restarted.store.get(intent_id).state == "SUBMITTED"
    assert "reconciliation" not in kinds(store, intent_id)
    # The receipt the sweep will find is sitting in the bank the whole time.
    assert bank.payment(intent_id)["status"] == "COMPLETED"


def test_the_bank_is_paid_exactly_once_across_the_crash_and_the_restart() -> None:
    """The guarantee, not a gap. Whatever T7 changes about the state, this is
    the invariant (**I1**) that must survive it: one intent, one debit, no
    blind retry — including when the agent repeats its confirmation to a
    freshly started process."""
    bank, store = FaultyBank(crash_at="after_pay"), MemoryStore()
    intent_id = crash_mid_payment(bank, store)
    confirmation_id = store.get(intent_id).confirmation_id

    restarted = plane_over(bank, store)
    restarted.confirm(CTX, confirmation_id)  # the agent, saying "yes" again
    restarted.status(CTX, intent_id)

    assert list(bank.payments) == [intent_id]
    assert bank.balance("checking_001") == OPENING_BALANCE - AMOUNT
    assert kinds(store, intent_id).count("execution_request") == 1
    assert "duplicate_confirmation" in kinds(store, intent_id)


def test_a_crash_before_the_call_looks_identical_from_storage() -> None:
    """Why the sweep cannot simply assume the money moved. Same stranded
    `SUBMITTED`, same ledger, and nothing debited — only the bank knows the
    difference, which is precisely what `reconcile` is for once T7 hands it an
    `UNKNOWN` to work on."""
    bank, store = FaultyBank(crash_at="before_pay"), MemoryStore()
    intent_id = crash_mid_payment(bank, store)

    assert store.get(intent_id).state == "SUBMITTED"
    assert kinds(store, intent_id)[-1] == "execution_request"
    assert bank.payments == {}
    assert bank.balance("checking_001") == OPENING_BALANCE
