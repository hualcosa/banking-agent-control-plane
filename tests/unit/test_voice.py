"""Voice through the same control plane, and what it cost to get there.

Milestone 5 asks whether the plane is really channel-independent. These tests
are the behavioural half of the answer; the numeric half is

    git diff --stat before-voice -- src/control_plane/

Every assertion is about money and state, as in `test_control_plane.py` —
what moved, what did not, and what the ledger says. The voice-specific
machinery is imported, never re-implemented here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane import Context, ControlPlane, MemoryStore, MockBank, ProposedPix
from control_plane.policy import HEARD_CLEARLY, assess_risk
from examples.banking.voice import (
    TRANSCRIPTS,
    WORTH_ACTING_ON,
    spoken_amount,
    transcribe,
    voice_context,
    worth_acting_on,
)

pytestmark = pytest.mark.unit

MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
CUSTOMER = "cust_123"


@pytest.fixture
def plane() -> ControlPlane:
    return ControlPlane(MockBank(), clock=lambda: MIDDAY, store=MemoryStore())


def speak(plane: ControlPlane, said: str, *, session: str = "call-1"):
    """Say something down the phone and see what the plane makes of it."""
    heard = transcribe(said)
    if not worth_acting_on(heard):
        return heard, None
    ctx = voice_context(CUSTOMER, session, heard)
    amount = spoken_amount(heard.text)
    recipient = heard.text.split("pra ")[-1].split("pro ")[-1].strip()
    if amount is None:
        return heard, None
    return heard, plane.propose(ctx, ProposedPix(recipient=recipient, amount=amount))


# --------------------------------------------------------------------------
# the signal
# --------------------------------------------------------------------------


def test_a_shaky_transcript_raises_risk_rather_than_deciding() -> None:
    """Uncertainty about what was said changes how carefully the action is
    treated. It does not choose the action, and it is not a rule of its own."""
    action = ProposedPix(recipient="Renata", amount=Decimal("100"))
    from control_plane.actions import CreatePix

    canonical = CreatePix(
        source_account="checking_001",
        recipient_id="contact_renata",
        amount=Decimal("100"),
    )
    heard_well = Context(
        customer_id=CUSTOMER, session_id="c", channel="voice", stt_confidence=0.97
    )
    heard_badly = Context(
        customer_id=CUSTOMER, session_id="c", channel="voice", stt_confidence=0.6
    )

    clean = assess_risk(canonical, heard_well, known_recipients={"contact_renata"})
    shaky = assess_risk(canonical, heard_badly, known_recipients={"contact_renata"})
    assert "low_stt_confidence" not in clean.signals
    assert "low_stt_confidence" in shaky.signals
    assert shaky.score > clean.score
    assert action.recipient == "Renata"  # the proposal itself is unchanged


def test_a_text_channel_leaves_the_confidence_unset() -> None:
    """`None`, not 1.0: "certainly typed" and "perfectly heard" are different
    facts, and a policy that cannot tell them apart cannot reason about
    either."""
    typed = Context(customer_id=CUSTOMER, session_id="t")
    assert typed.stt_confidence is None
    from control_plane.actions import CreatePix

    action = CreatePix(
        source_account="checking_001",
        recipient_id="contact_renata",
        amount=Decimal("100"),
    )
    risk = assess_risk(action, typed, known_recipients={"contact_renata"})
    assert "low_stt_confidence" not in risk.signals


def test_the_confidence_threshold_is_a_boundary_not_a_vibe() -> None:
    from control_plane.actions import CreatePix

    action = CreatePix(
        source_account="checking_001",
        recipient_id="contact_renata",
        amount=Decimal("100"),
    )

    def signals(confidence: float) -> tuple[str, ...]:
        ctx = Context(
            customer_id=CUSTOMER,
            session_id="c",
            channel="voice",
            stt_confidence=confidence,
        )
        return assess_risk(action, ctx, known_recipients={"contact_renata"}).signals

    assert "low_stt_confidence" in signals(HEARD_CLEARLY - 0.01)
    assert "low_stt_confidence" not in signals(HEARD_CLEARLY)


# --------------------------------------------------------------------------
# the dangerous class
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "said", [s for s, (_h, _c, kind) in TRANSCRIPTS.items() if kind == "magnitude"]
)
def test_a_magnitude_collapse_never_executes(plane: ControlPlane, said: str) -> None:
    """ "Trezentos" heard as "treze" is a twenty-fold error the customer would
    not catch in a spoken read-back. Whatever else happens, money must not
    move on it without a confirmation the customer can actually check."""
    heard, out = speak(plane, said)
    assert heard.text != heard.truth, "this row is supposed to be misheard"
    assert plane.bank.payments == {}
    if out is not None:
        assert out.status in (
            "REQUIRE_CONFIRMATION",
            "REQUIRE_STEP_UP_AUTH",
            "REQUIRE_MORE_INFO",
            "DENY",
        )


@pytest.mark.parametrize(
    "said", [s for s, (_h, _c, kind) in TRANSCRIPTS.items() if kind == "recipient"]
)
def test_a_misheard_recipient_never_executes(plane: ControlPlane, said: str) -> None:
    """ "Renata" and "Renato" are one phoneme apart and two different people."""
    _heard, out = speak(plane, said)
    assert plane.bank.payments == {}
    if out is not None:
        assert out.status != "COMPLETED"


def test_a_transcript_nobody_can_read_is_not_an_instruction() -> None:
    """Below the floor the channel says "I did not catch that" — it does not
    hand a guess to the control plane and let policy carry it."""
    from examples.banking.voice import Heard

    assert not worth_acting_on(Heard(text="...", confidence=WORTH_ACTING_ON - 0.01))
    assert worth_acting_on(Heard(text="ok", confidence=WORTH_ACTING_ON))


def test_a_clean_transcript_still_goes_through(plane: ControlPlane) -> None:
    """Without this the table only proves that low confidence blocks things."""
    heard, out = speak(plane, "manda cem reais pra Renata Silva")
    assert heard.text == heard.truth
    assert out is not None and out.status == "REQUIRE_CONFIRMATION"
    assert out.data["display"] == "R$ 100,00"


# --------------------------------------------------------------------------
# cross-channel: voice proposes, text confirms
# --------------------------------------------------------------------------


def test_voice_proposes_text_confirms_and_the_bank_is_paid_once(
    plane: ControlPlane,
) -> None:
    """The milestone's headline path. Reading a value back over a phone line
    and accepting "sim" is the weakest confirmation available — a misheard yes
    on a misheard amount compounds — so the channel that cannot confirm safely
    hands off to one that can."""
    _heard, proposed = speak(plane, "manda cem reais pra Renata Silva")
    assert proposed.status == "REQUIRE_CONFIRMATION"

    typed = Context(customer_id=CUSTOMER, session_id="whatsapp-9", channel="whatsapp")
    done = plane.confirm(typed, proposed.confirmation_id)
    assert done.status == "COMPLETED"
    assert len(plane.bank.payments) == 1

    kinds = [e.kind for e in plane.explain(typed, proposed.intent_id)]
    assert "channel_handoff" in kinds
    handoff = next(
        e
        for e in plane.explain(typed, proposed.intent_id)
        if e.kind == "channel_handoff"
    )
    assert handoff.detail["proposed_on"] == "voice"
    assert handoff.detail["confirmed_on"] == "whatsapp"


def test_the_handoff_is_not_a_general_loosening(plane: ControlPlane) -> None:
    """A text-proposed intent still cannot be confirmed from another text
    session. Only voice hands off, because only voice cannot confirm safely."""
    typed = Context(customer_id=CUSTOMER, session_id="whatsapp-1", channel="whatsapp")
    proposed = plane.propose(
        typed, ProposedPix(recipient="Renata", amount=Decimal("100"))
    )
    other = Context(customer_id=CUSTOMER, session_id="whatsapp-2", channel="whatsapp")
    assert plane.confirm(other, proposed.confirmation_id).status == "DENY"
    assert plane.bank.payments == {}


def test_another_customer_cannot_pick_up_a_voice_intent(plane: ControlPlane) -> None:
    """The customer boundary does not move, in any channel."""
    _heard, proposed = speak(plane, "manda cem reais pra Renata Silva")
    thief = Context(customer_id="cust_999", session_id="whatsapp-9", channel="whatsapp")
    assert plane.confirm(thief, proposed.confirmation_id).status == "DENY"
    assert plane.bank.payments == {}


def test_the_confirmed_action_is_the_one_that_was_heard(plane: ControlPlane) -> None:
    """Cross-channel does not mean cross-action: the digest still binds the
    yes to the exact object, whichever channel says it."""
    _heard, proposed = speak(plane, "manda cem reais pra Renata Silva")
    tampered = plane.store.get(proposed.intent_id)
    tampered.action = tampered.action.model_copy(update={"amount": Decimal("3000")})
    plane.store.put(tampered)

    typed = Context(customer_id=CUSTOMER, session_id="whatsapp-9", channel="whatsapp")
    assert plane.confirm(typed, proposed.confirmation_id).status == "DENY"
    assert plane.bank.payments == {}
