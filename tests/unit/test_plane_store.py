"""The storage seam: the plane asks a `Store`, not a dict.

These tests exist to keep V1 honest. `MemoryStore` and `PgStore` have to be
interchangeable, so everything asserted here is asserted through the
interface — if a test reaches past `Store` into a dict, it is testing the
implementation this seam exists to make replaceable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, MemoryStore, MockBank, ProposedPix
from control_plane.store import Store

pytestmark = pytest.mark.unit

CTX = Context(customer_id="cust_123", session_id="thread-1")
MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def pix(recipient: str, amount: str) -> ProposedPix:
    return ProposedPix(recipient=recipient, amount=Decimal(amount))


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def plane(store: MemoryStore) -> ControlPlane:
    return ControlPlane(MockBank(), clock=lambda: MIDDAY, store=store)


def test_memory_store_satisfies_the_protocol(store: MemoryStore) -> None:
    assert isinstance(store, Store)


def test_the_plane_uses_the_store_it_was_given(
    plane: ControlPlane, store: MemoryStore
) -> None:
    """Not the default one. A plane that quietly built its own would pass every
    other test here and lose everything on the first restart."""
    out = plane.propose(CTX, pix("Renata", "300"))
    assert store.get(out.intent_id) is not None
    assert store.events_for(out.intent_id) != []


def test_every_state_hop_is_saved(plane: ControlPlane, store: MemoryStore) -> None:
    """`_move` writes both halves. Read the state back through the interface,
    never off the object the plane still holds."""
    proposed = plane.propose(CTX, pix("Renata", "300"))
    assert store.get(proposed.intent_id).state == "AWAITING_CONFIRMATION"
    plane.confirm(CTX, proposed.confirmation_id)
    assert store.get(proposed.intent_id).state == "COMPLETED"
    assert store.get(proposed.intent_id).receipt is not None


def test_a_confirmation_resolves_to_exactly_one_intent(
    plane: ControlPlane, store: MemoryStore
) -> None:
    a = plane.propose(CTX, pix("Renata", "300"))
    b = plane.propose(CTX, pix("Renata", "400"))
    assert a.confirmation_id != b.confirmation_id
    assert store.by_confirmation(a.confirmation_id).id == a.intent_id
    assert store.by_confirmation("conf_nope") is None


def test_by_confirmation_says_nothing_about_ownership(
    plane: ControlPlane, store: MemoryStore
) -> None:
    """The store answers about ids; the plane decides who may see them. Two
    places deciding that is the bug this repository exists to prevent."""
    proposed = plane.propose(CTX, pix("Renata", "300"))
    assert store.by_confirmation(proposed.confirmation_id) is not None
    other = Context(customer_id="cust_999", session_id="thread-9")
    assert plane.confirm(other, proposed.confirmation_id).status == "DENY"
    assert plane.bank.payments == {}


def test_unsettled_finds_what_a_restart_would_have_to_resolve(
    plane: ControlPlane, store: MemoryStore
) -> None:
    """T7's only question, asked of the interface before T7 exists."""
    waiting = plane.propose(CTX, pix("Renata", "300"))
    stepping = plane.propose(CTX, pix("João", "1500"))
    assert {i.id for i in store.unsettled(["AWAITING_CONFIRMATION"])} == {
        waiting.intent_id
    }
    assert {i.id for i in store.unsettled(["AWAITING_STEP_UP"])} == {stepping.intent_id}
    assert store.unsettled(["SUBMITTED"]) == []
    plane.confirm(CTX, waiting.confirmation_id)
    assert store.unsettled(["AWAITING_CONFIRMATION"]) == []


def test_the_ledger_only_ever_grows(plane: ControlPlane, store: MemoryStore) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    before = len(store)
    trail = list(store.events_for(proposed.intent_id))
    plane.confirm(CTX, proposed.confirmation_id)
    assert len(store) > before
    assert store.events_for(proposed.intent_id)[: len(trail)] == trail
