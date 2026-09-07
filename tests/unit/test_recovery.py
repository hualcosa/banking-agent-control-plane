"""What survives a restart, and what a "yes" is worth an hour later.

Two tasks share this file because they answer the same question from
different ends: T7 asks what the process owes an intent it abandoned, T8 asks
what a token still authorises. Both were unexpressable in V0 — not scenarios
it got wrong, but scenarios it could not be wrong about, because it kept
nothing to check against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, MemoryStore, MockBank, ProposedPix
from control_plane.state import CONFIRMATION_TTL, action_digest
from tests.fakes import Crash, FaultyBank

pytestmark = pytest.mark.unit

MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
CTX = Context(customer_id="cust_123", session_id="thread-1")


def pix(recipient: str, amount: str) -> ProposedPix:
    return ProposedPix(recipient=recipient, amount=Decimal(amount))


class Clock:
    """A clock a test can push forward. `now` is read, never guessed."""

    def __init__(self, at: datetime = MIDDAY) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def plane_over(store: MemoryStore, bank: MockBank, clock: Clock) -> ControlPlane:
    return ControlPlane(bank, clock=clock, store=store, secret="test-secret")


# --------------------------------------------------------------------------
# T7 — the restart sweep
# --------------------------------------------------------------------------


def test_a_crash_mid_payment_is_resolved_by_the_sweep_and_pays_once() -> None:
    """The whole point of T7, end to end.

    Kill the process between the bank call and the receipt, restart over the
    same storage, sweep, reconcile — and assert the bank was paid exactly
    once. Before the sweep existed this intent had no way out of SUBMITTED.
    """
    store, clock = MemoryStore(), Clock()
    bank = FaultyBank(crash_at="after_pay")

    dying = plane_over(store, bank, clock)
    proposed = dying.propose(CTX, pix("Renata", "300"))
    with pytest.raises(Crash):
        dying.confirm(CTX, proposed.confirmation_id)
    assert store.get(proposed.intent_id).state == "SUBMITTED"
    assert len(bank.payments) == 1, "the bank really was paid before the crash"

    del dying  # the process is gone; only the store survives
    reborn = plane_over(store, bank, clock)
    assert reborn.sweep() == [proposed.intent_id]
    assert store.get(proposed.intent_id).state == "UNKNOWN"

    settled = reborn.reconcile(CTX, proposed.intent_id)
    assert settled.status == "COMPLETED"
    assert len(bank.payments) == 1, "reconciliation asks; it never re-pays"


def test_a_crash_before_the_call_reconciles_to_failed_without_paying() -> None:
    """Same evidence in storage, opposite truth at the bank — which is why
    the sweep resolves to UNKNOWN and lets `reconcile` ask, instead of
    guessing either way."""
    store, clock = MemoryStore(), Clock()
    bank = FaultyBank(crash_at="before_pay")

    dying = plane_over(store, bank, clock)
    proposed = dying.propose(CTX, pix("Renata", "300"))
    with pytest.raises(Crash):
        dying.confirm(CTX, proposed.confirmation_id)

    reborn = plane_over(store, bank, clock)
    assert reborn.sweep() == [proposed.intent_id]
    settled = reborn.reconcile(CTX, proposed.intent_id)
    assert settled.status == "FAILED"
    assert bank.payments == {}


def test_the_sweep_touches_nothing_that_was_not_in_flight() -> None:
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    done = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, done.confirmation_id)
    waiting = plane.propose(CTX, pix("Renata", "400"))

    assert plane.sweep() == []
    assert store.get(done.intent_id).state == "COMPLETED"
    assert store.get(waiting.intent_id).state == "AWAITING_CONFIRMATION"


def test_the_sweep_is_idempotent() -> None:
    """Boot twice, resolve once. A second sweep must not find the intent it
    already moved, or a restart loop would rewrite history on every pass."""
    store, clock = MemoryStore(), Clock()
    bank = FaultyBank(crash_at="after_pay")
    dying = plane_over(store, bank, clock)
    proposed = dying.propose(CTX, pix("Renata", "300"))
    with pytest.raises(Crash):
        dying.confirm(CTX, proposed.confirmation_id)

    reborn = plane_over(store, bank, clock)
    assert reborn.sweep() == [proposed.intent_id]
    assert reborn.sweep() == []


def test_the_sweep_is_recorded_in_the_trail() -> None:
    """An intent that changed state without a request still has to explain
    itself: "who moved this and why" is the audit question."""
    store, clock = MemoryStore(), Clock()
    bank = FaultyBank(crash_at="after_pay")
    dying = plane_over(store, bank, clock)
    proposed = dying.propose(CTX, pix("Renata", "300"))
    with pytest.raises(Crash):
        dying.confirm(CTX, proposed.confirmation_id)

    plane_over(store, bank, clock).sweep()
    kinds = [e.kind for e in store.events_for(proposed.intent_id)]
    assert "restart_sweep" in kinds
    swept = next(
        e for e in store.events_for(proposed.intent_id) if e.kind == "restart_sweep"
    )
    assert swept.detail == {"found_in": "SUBMITTED", "resolved_to": "UNKNOWN"}


# --------------------------------------------------------------------------
# T8 — a confirmation expires
# --------------------------------------------------------------------------


def test_a_confirmation_older_than_the_ttl_is_cancelled_not_honoured() -> None:
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    proposed = plane.propose(CTX, pix("Renata", "300"))
    clock.advance(CONFIRMATION_TTL + timedelta(seconds=1))

    out = plane.confirm(CTX, proposed.confirmation_id)
    assert out.status == "CANCELLED"
    assert bank.payments == {}
    assert store.get(proposed.intent_id).state == "CANCELLED"
    assert "expirada" in out.message


def test_a_confirmation_inside_the_ttl_still_works() -> None:
    """The boundary in the direction that must not break: an expiry that
    fires early is an outage, not a control."""
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    proposed = plane.propose(CTX, pix("Renata", "300"))
    clock.advance(CONFIRMATION_TTL - timedelta(seconds=1))
    assert plane.confirm(CTX, proposed.confirmation_id).status == "COMPLETED"
    assert len(bank.payments) == 1


def test_an_expired_confirmation_cannot_be_revived_by_asking_again() -> None:
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    proposed = plane.propose(CTX, pix("Renata", "300"))
    clock.advance(CONFIRMATION_TTL * 2)
    plane.confirm(CTX, proposed.confirmation_id)
    assert plane.confirm(CTX, proposed.confirmation_id).status == "DENY"
    assert bank.payments == {}


def test_the_expiry_is_recorded_with_what_it_measured() -> None:
    store, clock = MemoryStore(), Clock()
    plane = plane_over(store, MockBank(), clock)
    proposed = plane.propose(CTX, pix("Renata", "300"))
    clock.advance(CONFIRMATION_TTL * 3)
    plane.confirm(CTX, proposed.confirmation_id)

    event = next(
        e
        for e in store.events_for(proposed.intent_id)
        if e.kind == "confirmation_expired"
    )
    assert event.detail["ttl_seconds"] == int(CONFIRMATION_TTL.total_seconds())


# --------------------------------------------------------------------------
# T8 — the action cannot change under the token
# --------------------------------------------------------------------------


def test_an_action_mutated_after_confirmation_is_refused() -> None:
    """The scenario that had no test because it had no field. Rewrite the
    amount on a confirmed-pending intent and the digest no longer matches."""
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    proposed = plane.propose(CTX, pix("Renata", "300"))
    tampered = store.get(proposed.intent_id)
    tampered.action = tampered.action.model_copy(update={"amount": Decimal("3000")})
    store.put(tampered)

    out = plane.confirm(CTX, proposed.confirmation_id)
    assert out.status == "DENY"
    assert bank.payments == {}
    assert "mudou" in out.message


def test_a_mutated_recipient_is_refused_too() -> None:
    """Amount is the obvious one; the recipient is the one that empties an
    account into a stranger's."""
    store, clock = MemoryStore(), Clock()
    bank = MockBank()
    plane = plane_over(store, bank, clock)

    proposed = plane.propose(CTX, pix("Renata", "300"))
    tampered = store.get(proposed.intent_id)
    tampered.action = tampered.action.model_copy(
        update={"recipient_id": "contact_joao"}
    )
    store.put(tampered)

    assert plane.confirm(CTX, proposed.confirmation_id).status == "DENY"
    assert bank.payments == {}


def test_the_mismatch_is_recorded_without_being_silently_swallowed() -> None:
    store, clock = MemoryStore(), Clock()
    plane = plane_over(store, MockBank(), clock)
    proposed = plane.propose(CTX, pix("Renata", "300"))
    tampered = store.get(proposed.intent_id)
    tampered.action = tampered.action.model_copy(update={"amount": Decimal("1")})
    store.put(tampered)
    plane.confirm(CTX, proposed.confirmation_id)

    kinds = [e.kind for e in store.events_for(proposed.intent_id)]
    assert "digest_mismatch" in kinds
    assert "execution_request" not in kinds


def test_the_digest_is_keyed_not_merely_hashed() -> None:
    """An unkeyed digest tells you the action changed; anyone who can rewrite
    the action can also recompute a bare hash to match. Two secrets over the
    same action must disagree."""
    action = ProposedPix(recipient="Renata", amount=Decimal("300"))
    store, clock = MemoryStore(), Clock()
    plane = plane_over(store, MockBank(), clock)
    proposed = plane.propose(CTX, action)
    intent = store.get(proposed.intent_id)

    assert action_digest(intent.action, "test-secret") == intent.action_digest
    assert action_digest(intent.action, "another-secret") != intent.action_digest


def test_the_digest_does_not_depend_on_field_order() -> None:
    """Canonical JSON, so a round trip through a database that reorders keys
    does not read as tampering."""
    store, clock = MemoryStore(), Clock()
    plane = plane_over(store, MockBank(), clock)
    proposed = plane.propose(CTX, pix("Renata", "300"))
    intent = store.get(proposed.intent_id)

    reordered = type(intent.action).model_validate(
        dict(reversed(list(intent.action.model_dump(mode="json").items())))
    )
    assert action_digest(reordered, "test-secret") == intent.action_digest


# --------------------------------------------------------------------------
# T7 — the sweep actually runs at boot
# --------------------------------------------------------------------------


def test_the_banking_spec_sweeps_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep nobody calls is a method, not a recovery. The runtime knows
    only `spec.on_startup`; what it does belongs to the example."""
    from examples.banking import agent as banking, tools

    store, clock = MemoryStore(), Clock()
    bank = FaultyBank(crash_at="after_pay")
    plane = plane_over(store, bank, clock)
    monkeypatch.setattr(tools, "PLANE", plane)
    monkeypatch.setattr(banking, "PLANE", plane)

    assert banking.sweep_on_boot() == []

    proposed = plane.propose(CTX, pix("Renata", "300"))
    with pytest.raises(Crash):
        plane.confirm(CTX, proposed.confirmation_id)

    (line,) = banking.sweep_on_boot()
    assert proposed.intent_id in line
    assert "UNKNOWN" in line
    assert store.get(proposed.intent_id).state == "UNKNOWN"
    assert banking.build().on_startup is banking.sweep_on_boot
