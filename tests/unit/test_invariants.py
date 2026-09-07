"""The matrix: five invariants against every way this system can be attacked.

`make matrix` is the spine of the project's claim. Everything else says the
control plane *should* hold; this counts how often it does, across N seeded
trials per cell, with no model anywhere near it.

Three rules make the number mean something.

**Every trial is seeded and reported.** A failure prints the seed that
produced it, so "it fails sometimes" is never the end of an investigation.

**Debits are counted, not inferred.** `MockBank` is idempotent by key, so
`len(bank.payments)` would assert the *bank's* guarantee and pass even if the
plane called it five times. `CountingBank` counts the moments money actually
moved, which is the only number invariant I1 is about.

**A cell that does not apply says so.** Forcing every invariant onto every
scenario would fill the table with green that means nothing; `None` is a real
answer and the renderer prints it as `·`.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from control_plane import Context, ControlPlane, MemoryStore, MockBank, ProposedPix
from control_plane.state import CONFIRMATION_TTL
from tests.fakes import Crash, FaultyBank

pytestmark = pytest.mark.matrix

#: Trials per cell. The roadmap's gate is N >= 100.
N = 100

MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
SECRET = "matrix-secret"

CUSTOMER = "cust_123"
#: Recipients the demo bank knows, with the risk each one carries: `contact_renata`
#: has been paid before, `contact_joao` has not (the `new_recipient` signal).
KNOWN = ("Renata", "João", "Maria")


def _counted(bank: Any, call: Callable[..., Any], kw: dict[str, Any]) -> Any:
    """Count a debit by its evidence, not by the call returning.

    `MockBank` files the receipt and *then* raises on the `.13` tripwire, and
    `FaultyBank` raises after a real debit — so "did the call return" is the
    wrong question. The right one is whether a receipt landed under a key that
    had none before, which is true in both cases and in `finally`.
    """
    already = kw["idempotency_key"] in bank.payments
    try:
        return call(**kw)
    finally:
        if not already and kw["idempotency_key"] in bank.payments:
            bank.debits += 1


class CountingBank(MockBank):
    """Counts the moments money moved, not the receipts filed.

    The distinction is the whole of I1: the bank returns the same receipt for a
    repeated key without debiting again, so a plane that called it twice would
    still leave one entry in `payments`. This counts calls that got past that.
    """

    def __post_init__(self) -> None:  # pragma: no cover - dataclass hook
        pass

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.debits = 0

    def create_pix(self, **kw: Any) -> dict[str, Any]:
        return _counted(self, super().create_pix, kw)


class CountingFaultyBank(FaultyBank):
    """`FaultyBank`, counting the same way."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.debits = 0

    def create_pix(self, **kw: Any) -> dict[str, Any]:
        return _counted(self, super().create_pix, kw)


class Clock:
    def __init__(self, at: datetime = MIDDAY) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class Trial:
    """What one seeded run of one scenario left behind."""

    seed: int
    bank: Any
    store: MemoryStore
    ctx: Context
    intent_id: str | None = None
    #: The action the customer actually consented to, as JSON.
    authorized: dict[str, Any] | None = None
    #: Every outcome the scenario collected, in order.
    outcomes: list[Any] = field(default_factory=list)
    #: Set when the scenario's whole point is that no intent exists.
    refused_early: bool = False
    #: Set when the scenario rewrote the action after the token was issued.
    tampered: bool = False
    #: Set when the scenario left the bank's answer in doubt, so the intent
    #: MUST end up somewhere a human or `reconcile` can act on. Declared by the
    #: scenario rather than inferred from the trail: inferring it let a
    #: disabled restart sweep read as "does not apply" instead of "stuck".
    expects_resolution: bool = False

    @property
    def events(self) -> list[Any]:
        return [] if self.intent_id is None else self.store.events_for(self.intent_id)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    @property
    def intent(self) -> Any:
        return None if self.intent_id is None else self.store.get(self.intent_id)


def world(
    rng: random.Random, *, bank: Any | None = None
) -> tuple[Any, ControlPlane, MemoryStore, Clock, Context]:
    bank = bank if bank is not None else CountingBank()
    store, clock = MemoryStore(), Clock()
    ctx = Context(
        customer_id=CUSTOMER,
        session_id=f"thread-{rng.randrange(10_000)}",
        assurance="medium",
    )
    plane = ControlPlane(bank, clock=clock, store=store, secret=SECRET)
    return bank, plane, store, clock, ctx


def safe_amount(rng: random.Random) -> Decimal:
    """Under every limit, so the scenario tests what it means to test rather
    than tripping the nighttime cap or the hard limit by accident."""
    cents = rng.choice(["00", "25", "50", "99"])
    return Decimal(f"{rng.randrange(10, 400)}.{cents}")


# --------------------------------------------------------------------------
# scenarios — each returns a Trial
# --------------------------------------------------------------------------


def happy_path(rng: random.Random) -> Trial:
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.outcomes.append(out)
    t.intent_id = out.intent_id
    if out.status == "REQUIRE_CONFIRMATION":
        t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
        t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    return t


def repeated_confirmation(rng: random.Random) -> Trial:
    """The agent repeats itself — the single most likely model failure."""
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    for _ in range(rng.randrange(2, 6)):
        t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    return t


def crash_after_pay(rng: random.Random) -> Trial:
    """The window that costs a customer money: paid, then the process died."""
    bank, plane, store, clock, ctx = world(
        rng, bank=CountingFaultyBank(crash_at="after_pay")
    )
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    t.expects_resolution = True
    with pytest.raises(Crash):
        plane.confirm(ctx, out.confirmation_id)

    reborn = ControlPlane(bank, clock=clock, store=store, secret=SECRET)
    reborn.sweep()
    if rng.random() < 0.5:  # an agent that retries before anyone reconciles
        t.outcomes.append(reborn.confirm(ctx, out.confirmation_id))
    t.outcomes.append(reborn.reconcile(ctx, out.intent_id))
    return t


def crash_before_pay(rng: random.Random) -> Trial:
    """Identical evidence in storage, opposite truth at the bank."""
    bank, plane, store, clock, ctx = world(
        rng, bank=CountingFaultyBank(crash_at="before_pay")
    )
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    t.expects_resolution = True
    with pytest.raises(Crash):
        plane.confirm(ctx, out.confirmation_id)

    reborn = ControlPlane(bank, clock=clock, store=store, secret=SECRET)
    reborn.sweep()
    t.outcomes.append(reborn.reconcile(ctx, out.intent_id))
    return t


def bank_timeout(rng: random.Random) -> Trial:
    """The `.13` tripwire: the bank paid and then stopped answering."""
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    amount = Decimal(f"{rng.randrange(10, 400)}.13")
    out = plane.propose(ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=amount))
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    t.expects_resolution = True
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    for _ in range(rng.randrange(1, 4)):  # a nervous agent checking again
        t.outcomes.append(plane.reconcile(ctx, out.intent_id))
    return t


def borrowed_confirmation(rng: random.Random) -> Trial:
    """Another session, or another customer, holding a real token."""
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")

    thief = Context(
        customer_id=rng.choice([CUSTOMER, "cust_999"]),
        session_id=f"thread-{rng.randrange(10_000)}-other",
        assurance=rng.choice(["medium", "strong"]),
    )
    t.outcomes.append(plane.confirm(thief, out.confirmation_id))
    t.outcomes.append(plane.cancel(thief, out.confirmation_id))
    t.outcomes.append(plane.reconcile(thief, out.intent_id))
    return t


def expired_confirmation(rng: random.Random) -> Trial:
    """A yes from long enough ago that it is no longer a yes."""
    bank, plane, store, clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    clock.advance(CONFIRMATION_TTL + timedelta(seconds=rng.randrange(1, 100_000)))
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    return t


def mutated_action(rng: random.Random) -> Trial:
    """The action is rewritten between the yes and the execution."""
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")

    victim = store.get(out.intent_id)
    if rng.random() < 0.5:
        update = {"amount": victim.action.amount + Decimal("1000")}
    else:
        # Anything but the recipient it already had — a "mutation" that
        # rewrites a field to its own value proves nothing, and 16% of the
        # first run of this matrix did exactly that.
        others = [
            c
            for c in ("contact_renata", "contact_joao", "contact_maria")
            if c != victim.action.recipient_id
        ]
        update = {"recipient_id": rng.choice(others)}
    victim.action = victim.action.model_copy(update=update)
    store.put(victim)
    t.tampered = True
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    return t


def ambiguous_recipient(rng: random.Random) -> Trial:
    """Two Anas, or a name the bank has never heard of."""
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    name = rng.choice(["Ana", "Ana", "Ricardo", "Zzz"])
    out = plane.propose(ctx, ProposedPix(recipient=name, amount=safe_amount(rng)))
    t.outcomes.append(out)
    t.intent_id = out.intent_id
    t.refused_early = True
    if out.status == "REQUIRE_CONFIRMATION":  # never expected; the check catches it
        t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
        t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    return t


def bank_refuses(rng: random.Random) -> Trial:
    """The money leaves between the proposal and the confirmation.

    Draining the account *before* proposing would mostly be caught by the
    precondition check, which is the plane refusing early rather than the bank
    refusing late — a different path, and not the one this cell is for. Moving
    the balance after the token is issued is also the realistic version: a card
    payment cleared while the customer was reading the confirmation.
    """
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    bank.accounts["checking_001"] = Decimal(rng.randrange(0, 9))
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))
    t.outcomes.append(plane.reconcile(ctx, out.intent_id))
    return t


def concurrent_execute(rng: random.Random) -> Trial:
    """Two workers that both read AUTHORIZED before either wrote SUBMITTED.

    A lost update — the race a single-row lock prevents and an in-memory dict
    does not. It exists because without it the idempotency key is never
    exercised: the state machine refuses a repeated confirm long before
    `_execute` is reached, so every other cell in this column proves the
    machine works rather than that the key does. A mutation run made that
    concrete — breaking the key left the whole matrix green.
    """
    bank, plane, store, _clock, ctx = world(rng)
    t = Trial(seed=rng.seed_value, bank=bank, store=store, ctx=ctx)  # type: ignore[attr-defined]
    out = plane.propose(
        ctx, ProposedPix(recipient=rng.choice(KNOWN), amount=safe_amount(rng))
    )
    t.intent_id = out.intent_id
    if out.status != "REQUIRE_CONFIRMATION":
        return t
    t.authorized = store.get(out.intent_id).action.model_dump(mode="json")
    t.outcomes.append(plane.confirm(ctx, out.confirmation_id))

    for _ in range(rng.randrange(1, 4)):
        stale = store.get(out.intent_id)
        stale.state = "AUTHORIZED"  # what the second worker still believes
        store.put(stale)
        t.outcomes.append(plane._execute(stale))
    return t


SCENARIOS: dict[str, Callable[[random.Random], Trial]] = {
    "concurrent_execute": concurrent_execute,
    "happy": happy_path,
    "repeat_confirm": repeated_confirmation,
    "crash_after_pay": crash_after_pay,
    "crash_before_pay": crash_before_pay,
    "bank_timeout": bank_timeout,
    "borrowed_token": borrowed_confirmation,
    "expired_yes": expired_confirmation,
    "mutated_action": mutated_action,
    "ambiguous": ambiguous_recipient,
    "bank_refuses": bank_refuses,
}


# --------------------------------------------------------------------------
# the invariants — each returns True, False, or None for "does not apply"
# --------------------------------------------------------------------------


def i1_never_pays_twice(t: Trial) -> bool | None:
    """Nunca mover dinheiro duas vezes pelo mesmo pedido."""
    return t.bank.debits <= 1


def i2_never_executes_unauthorized(t: Trial) -> bool | None:
    """Nunca executar sem autorização válida.

    Executing means an `execution_request` in the trail. It is legitimate only
    if this principal confirmed *this* intent first, and the confirmation was
    neither expired nor against a different action.
    """
    kinds = t.kinds()
    if "execution_request" not in kinds:
        return None if t.refused_early else True
    if "confirmation" not in kinds:
        return False
    if kinds.index("confirmation") > kinds.index("execution_request"):
        return False
    if "confirmation_expired" in kinds or "digest_mismatch" in kinds:
        return False

    intent = t.intent
    # Derived here, not read off the plane's own bookkeeping. Checking for a
    # `confirmation_expired` event would mean that deleting the TTL check also
    # deletes the evidence of it — a mutation run proved exactly that, and this
    # matrix would have stayed green while a stale yes moved money.
    if (
        intent is not None
        and intent.confirmed_at
        and intent.confirmation_issued_at
        and intent.confirmed_at - intent.confirmation_issued_at > CONFIRMATION_TTL
    ):
        return False

    confirmed = next(e for e in t.events if e.kind == "confirmation")
    return (
        confirmed.detail["by"] == t.ctx.customer_id
        and confirmed.detail["session"] == t.ctx.session_id
    )


def i3_executes_only_what_was_confirmed(t: Trial) -> bool | None:
    """Nunca executar uma ação diferente da que foi confirmada.

    When the action was rewritten under the token, the invariant is not
    "it executed the right one" — it is that it executed *nothing*. Reporting
    that as "does not apply" would quietly turn the scenario this matrix
    cares most about into a blank cell.
    """
    requests = [e for e in t.events if e.kind == "execution_request"]
    if t.tampered:
        return not requests
    if not requests:
        return None
    if t.authorized is None:
        return False
    confirmed = next((e for e in t.events if e.kind == "confirmation"), None)
    if confirmed is None:
        return False
    return confirmed.detail["action"] == t.authorized


def i4_ambiguity_stops(t: Trial) -> bool | None:
    """Ambiguidade → para, não chuta."""
    if not t.refused_early:
        return None
    statuses = {o.status for o in t.outcomes}
    if statuses & {"COMPLETED", "SUBMITTED", "PENDING"}:
        return False
    return t.bank.debits == 0 and t.intent is None


def i5_unknown_asks_the_bank(t: Trial) -> bool | None:
    """Estado desconhecido → pergunta pro banco, nunca tenta de novo às cegas."""
    if not t.expects_resolution:
        return None
    intent = t.intent
    if intent is None:
        return False
    if intent.state == "SUBMITTED":
        # Stuck. The bank was called, the answer never came, and nothing can
        # move it: `reconcile` only accepts UNKNOWN. That is the failure the
        # sweep exists to prevent, and it must read as a violation rather than
        # as a cell that quietly stopped applying.
        return False
    if intent.state == "UNKNOWN":
        return True  # unresolved is honest; a wrong resolution is not
    if intent.state not in ("COMPLETED", "FAILED"):
        return False
    # It resolved. It must have asked, and the answer must match what the bank
    # actually did — measured as debits, not by looking the receipt up under
    # the same idempotency key the plane used. A mutation run showed why:
    # corrupt the key and both sides of that comparison go wrong together, so
    # the check agreed with the bug instead of catching it.
    if "reconciliation" not in t.kinds():
        return False
    return (intent.state == "COMPLETED") == (t.bank.debits > 0)


INVARIANTS: dict[str, Callable[[Trial], bool | None]] = {
    "I1 no double spend": i1_never_pays_twice,
    "I2 no unauthorized": i2_never_executes_unauthorized,
    "I3 exact action": i3_executes_only_what_was_confirmed,
    "I4 stops on doubt": i4_ambiguity_stops,
    "I5 asks the bank": i5_unknown_asks_the_bank,
}


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

#: What each scenario must actually reach. Without this a cell can go from
#: "100/100" to "does not apply" and the suite stays green — which is exactly
#: what happened when a mutation run disabled the restart sweep.
MUST_APPLY: dict[str, set[str]] = {
    "happy": {"I1 no double spend", "I2 no unauthorized", "I3 exact action"},
    "repeat_confirm": {"I1 no double spend", "I2 no unauthorized"},
    "concurrent_execute": {"I1 no double spend"},
    "crash_after_pay": {"I1 no double spend", "I5 asks the bank"},
    "crash_before_pay": {"I1 no double spend", "I5 asks the bank"},
    "bank_timeout": {"I1 no double spend", "I5 asks the bank"},
    "borrowed_token": {"I1 no double spend", "I2 no unauthorized"},
    "expired_yes": {"I1 no double spend", "I2 no unauthorized"},
    "mutated_action": {"I1 no double spend", "I3 exact action"},
    "ambiguous": {"I1 no double spend", "I4 stops on doubt"},
    "bank_refuses": {"I1 no double spend", "I2 no unauthorized"},
}

#: (invariant, scenario) -> {"held": int, "n/a": int, "failed": [seeds]}
CELLS: dict[tuple[str, str], dict[str, Any]] = defaultdict(
    lambda: {"held": 0, "na": 0, "failed": []}
)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_invariants_hold(scenario: str) -> None:
    run = SCENARIOS[scenario]
    for seed in range(N):
        rng = random.Random(f"{scenario}:{seed}")
        rng.seed_value = seed  # type: ignore[attr-defined]
        trial = run(rng)
        for name, check in INVARIANTS.items():
            cell = CELLS[(name, scenario)]
            verdict = check(trial)
            if verdict is None:
                cell["na"] += 1
            elif verdict:
                cell["held"] += 1
            else:
                cell["failed"].append(seed)

    broken = {
        name: CELLS[(name, scenario)]["failed"][:5]
        for name in INVARIANTS
        if CELLS[(name, scenario)]["failed"]
    }
    assert not broken, f"scenario {scenario!r} broke invariants (seeds): {broken}"

    # A cell that used to carry a number and now reads `·` has not passed — it
    # has stopped asking. Pinning which invariants each scenario must actually
    # reach is what stops a regression from hiding as "does not apply".
    applied = {
        name
        for name in INVARIANTS
        if CELLS[(name, scenario)]["held"] or CELLS[(name, scenario)]["failed"]
    }
    assert MUST_APPLY[scenario] <= applied, (
        f"scenario {scenario!r} stopped exercising "
        f"{sorted(MUST_APPLY[scenario] - applied)} — a blank cell is not a pass"
    )


@pytest.fixture(scope="module", autouse=True)
def matrix_table() -> Any:
    """Print the matrix after the cells are filled.

    The table is the deliverable — a suite that only says "passed" does not
    tell a reader that `crash_after_pay` was exercised a hundred times.
    """
    yield
    if not CELLS:  # pragma: no cover - only when the module is deselected
        return
    scenarios = sorted(SCENARIOS)
    width = max(len(n) for n in INVARIANTS) + 1
    head = " " * width + " ".join(f"{s[:16]:>16}" for s in scenarios)
    lines = ["", f"invariant matrix — N={N} seeded trials per cell", "", head]
    for name in INVARIANTS:
        row = [f"{name:<{width}}"]
        for scenario in scenarios:
            cell = CELLS[(name, scenario)]
            applicable = cell["held"] + len(cell["failed"])
            if cell["failed"]:
                row.append(
                    f"{'FAIL ' + str(len(cell['failed'])) + '/' + str(applicable):>16}"
                )
            elif applicable == 0:
                row.append(f"{'·':>16}")
            else:
                row.append(f"{str(cell['held']) + '/' + str(applicable):>16}")
        lines.append(" ".join(row))
    lines += [
        "",
        f"  n/m = held in all m trials where it applied, out of N={N} run",
        "  ·   = never applied in this scenario",
        "",
        "  A small m is not a weaker result, it is a narrower one: the scenario",
        "  reached that invariant m times. A cell that reads · is a question this",
        "  scenario cannot ask.",
        "",
    ]
    print("\n".join(lines))
