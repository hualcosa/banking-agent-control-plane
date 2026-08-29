"""The control plane, with no model anywhere near it.

Every assertion here is about money and state: what moved, what did not, and
what the ledger says about it. If one of these fails, the boundary this
repository exists to build has a hole in it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from control_plane import (
    Context,
    ControlPlane,
    GetBalance,
    GetCardTransactions,
    IllegalTransition,
    Intent,
    Ledger,
    MockBank,
    ProposedPix,
    brl,
)
from control_plane.actions import CreatePix
from control_plane.state import transition

pytestmark = pytest.mark.unit

CTX = Context(customer_id="cust_123", session_id="thread-1")
OTHER_SESSION = Context(customer_id="cust_123", session_id="thread-2")
OTHER_CUSTOMER = Context(customer_id="cust_999", session_id="thread-1")


@pytest.fixture
def plane() -> ControlPlane:
    return ControlPlane(MockBank())


def pix(recipient: str, amount: str) -> ProposedPix:
    return ProposedPix(recipient=recipient, amount=Decimal(amount))


def kinds(plane: ControlPlane, intent_id: str) -> list[str]:
    return [e.kind for e in plane.explain(intent_id)]


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------


def test_an_illegal_transition_is_refused_and_recorded() -> None:
    ledger = Ledger()
    intent = Intent(
        action=CreatePix(
            source_account="checking_001",
            recipient_id="contact_renata",
            amount=Decimal("1"),
        ),
        context=CTX,
    )
    with pytest.raises(IllegalTransition):
        transition(intent, "confirm", ledger)  # CREATED has no `confirm` edge
    assert intent.state == "CREATED"
    assert [e.kind for e in ledger.for_intent(intent.id)] == ["transition_refused"]


def test_the_happy_path_is_a_legal_walk() -> None:
    ledger = Ledger()
    intent = Intent(
        action=CreatePix(
            source_account="checking_001",
            recipient_id="contact_renata",
            amount=Decimal("1"),
        ),
        context=CTX,
    )
    for event in ("validate", "await_confirmation", "confirm", "submit", "complete"):
        transition(intent, event, ledger)
    assert intent.state == "COMPLETED"


# --------------------------------------------------------------------------
# propose: nothing moves
# --------------------------------------------------------------------------


def test_a_proposal_resolves_the_recipient_and_asks_for_confirmation(
    plane: ControlPlane,
) -> None:
    out = plane.propose(CTX, pix("Renata", "300"))

    assert out.status == "REQUIRE_CONFIRMATION"
    assert out.confirmation_id and out.confirmation_id.startswith("conf_")
    assert out.data["recipient"] == "Renata Silva"
    assert out.data["display"] == "R$ 300,00"
    assert plane.intents[out.intent_id].state == "AWAITING_CONFIRMATION"
    assert plane.bank.payments == {}
    assert plane.bank.balance("checking_001") == Decimal("2543.10")


def test_two_matching_contacts_come_back_as_a_question(plane: ControlPlane) -> None:
    out = plane.propose(CTX, pix("Ana", "50"))
    assert out.status == "REQUIRE_MORE_INFO"
    assert {c["name"] for c in out.data["candidates"]} == {"Ana Lima", "Ana Costa"}
    assert out.intent_id not in plane.intents, "no canonical action, no intent"
    assert "resolution" in kinds(plane, out.intent_id)


def test_an_unknown_contact_is_not_guessed(plane: ControlPlane) -> None:
    out = plane.propose(CTX, pix("Zé", "50"))
    assert out.status == "REQUIRE_MORE_INFO"
    assert out.data["candidates"] == []


def test_insufficient_funds_is_a_missing_precondition(plane: ControlPlane) -> None:
    out = plane.propose(CTX, pix("Renata", "3000"))
    assert out.status == "DENY"
    assert out.data["missing_preconditions"] == ["sufficient_funds"]
    assert plane.intents[out.intent_id].state == "CANCELLED"


def test_above_the_hard_limit_is_denied_by_name(plane: ControlPlane) -> None:
    plane.bank.accounts["checking_001"] = Decimal("100000")
    out = plane.propose(CTX, pix("João", "9000"))
    assert out.status == "DENY"
    assert "limite" in out.message
    policy = next(e for e in plane.explain(out.intent_id) if e.kind == "policy")
    assert policy.detail["rule"] == "pix_hard_limit"


# --------------------------------------------------------------------------
# confirm: the only way money moves
# --------------------------------------------------------------------------


def test_confirmation_executes_exactly_once(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    first = plane.confirm(CTX, proposed.confirmation_id)
    second = plane.confirm(CTX, proposed.confirmation_id)

    assert first.status == "COMPLETED"
    assert second.status == "COMPLETED"
    assert "nenhum novo pagamento" in second.message
    assert len(plane.bank.payments) == 1
    assert plane.bank.balance("checking_001") == Decimal("2243.10")
    assert first.data["receipt"] == second.data["receipt"]


def test_a_confirmation_cannot_be_borrowed_from_another_session(
    plane: ControlPlane,
) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    for ctx in (OTHER_SESSION, OTHER_CUSTOMER):
        out = plane.confirm(ctx, proposed.confirmation_id)
        assert out.status == "DENY"
    assert plane.bank.payments == {}
    assert plane.intents[proposed.intent_id].state == "AWAITING_CONFIRMATION"


def test_a_made_up_confirmation_id_moves_nothing(plane: ControlPlane) -> None:
    assert plane.confirm(CTX, "conf_deadbeef").status == "DENY"
    assert plane.bank.payments == {}


def test_a_cancelled_intent_cannot_be_confirmed(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    assert plane.cancel(CTX, proposed.confirmation_id).status == "CANCELLED"
    assert plane.confirm(CTX, proposed.confirmation_id).status == "DENY"
    assert plane.bank.payments == {}


def test_the_ledger_records_who_confirmed_what_and_when(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, proposed.confirmation_id)
    confirmation = next(
        e for e in plane.explain(proposed.intent_id) if e.kind == "confirmation"
    )
    assert confirmation.detail["by"] == "cust_123"
    assert confirmation.detail["session"] == "thread-1"
    assert confirmation.detail["action"]["amount"] == "300"
    assert confirmation.detail["at"]


def test_explain_reconstructs_the_whole_pipeline(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, proposed.confirmation_id)
    trail = kinds(plane, proposed.intent_id)
    for kind in (
        "request",
        "interpreted",
        "resolution",
        "canonical_action",
        "risk",
        "policy",
        "confirmation",
        "authorization",
        "execution_request",
        "backend_response",
    ):
        assert kind in trail, kind
    assert (
        trail.index("policy")
        < trail.index("confirmation")
        < trail.index("execution_request")
    )


# --------------------------------------------------------------------------
# step-up
# --------------------------------------------------------------------------


def test_above_the_step_up_threshold_needs_strong_assurance_first(
    plane: ControlPlane,
) -> None:
    proposed = plane.propose(CTX, pix("Renata", "1500"))
    assert proposed.status == "REQUIRE_STEP_UP_AUTH"
    assert proposed.confirmation_id is None
    assert plane.confirm(CTX, "conf_anything").status == "DENY"

    stepped = plane.step_up(CTX, proposed.intent_id)
    assert stepped.status == "REQUIRE_CONFIRMATION"
    assert plane.intents[proposed.intent_id].context.assurance == "strong"

    done = plane.confirm(CTX, stepped.confirmation_id)
    assert done.status == "COMPLETED"
    assert len(plane.bank.payments) == 1


def test_high_risk_triggers_step_up_below_the_amount_threshold(
    plane: ControlPlane,
) -> None:
    """New recipient + unusual amount: the risk engine, not the amount rule."""
    out = plane.propose(CTX, pix("João", "600"))
    assert out.status == "REQUIRE_STEP_UP_AUTH"
    risk = next(e for e in plane.explain(out.intent_id) if e.kind == "risk")
    assert set(risk.detail["signals"]) == {"new_recipient", "unusual_amount"}
    assert risk.detail["level"] == "high"


def test_step_up_is_bound_to_one_intent(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "1500"))
    assert plane.step_up(OTHER_SESSION, proposed.intent_id).status == "DENY"
    assert plane.step_up(CTX, "pix_nope").status == "DENY"
    assert plane.intents[proposed.intent_id].state == "AWAITING_STEP_UP"


def test_step_up_on_the_wrong_state_is_refused(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    assert plane.step_up(CTX, proposed.intent_id).status == "DENY"


# --------------------------------------------------------------------------
# timeout, UNKNOWN, reconciliation
# --------------------------------------------------------------------------


def test_a_timeout_is_unknown_not_failed_and_never_repays(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300.13"))
    out = plane.confirm(CTX, proposed.confirmation_id)
    assert out.status == "UNKNOWN"
    assert len(plane.bank.payments) == 1, "the bank did pay"

    again = plane.confirm(CTX, proposed.confirmation_id)
    assert again.status == "UNKNOWN"
    assert len(plane.bank.payments) == 1

    resolved = plane.reconcile(CTX, proposed.intent_id)
    assert resolved.status == "COMPLETED"
    assert resolved.data["receipt"]["status"] == "COMPLETED"
    assert plane.bank.balance("checking_001") == Decimal("2242.97")


def test_reconcile_on_a_settled_intent_just_reports(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.confirm(CTX, proposed.confirmation_id)
    assert plane.reconcile(CTX, proposed.intent_id).status == "COMPLETED"
    assert plane.status(CTX, proposed.intent_id).status == "COMPLETED"
    assert plane.status(CTX, "pix_nope").status == "DENY"


def test_reconcile_with_no_bank_record_fails_the_intent(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300.13"))
    plane.confirm(CTX, proposed.confirmation_id)
    del plane.bank.payments[proposed.intent_id]
    assert plane.reconcile(CTX, proposed.intent_id).status == "FAILED"


def test_a_definitive_bank_error_fails_the_intent(plane: ControlPlane) -> None:
    proposed = plane.propose(CTX, pix("Renata", "300"))
    plane.bank.accounts["checking_001"] = Decimal(
        "10"
    )  # funds vanished after validation
    out = plane.confirm(CTX, proposed.confirmation_id)
    assert out.status == "FAILED"
    assert plane.intents[proposed.intent_id].state == "FAILED"
    assert plane.bank.payments == {}


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def test_balance_is_read_and_audited(plane: ControlPlane) -> None:
    out = plane.query(CTX, GetBalance())
    assert out.status == "ALLOW"
    assert out.data["display"] == "R$ 2.543,10"
    assert kinds(plane, out.intent_id) == ["request", "result"]


def test_card_transactions_are_windowed(plane: ControlPlane) -> None:
    week = plane.query(CTX, GetCardTransactions(days=7)).data["transactions"]
    two = plane.query(CTX, GetCardTransactions(days=2)).data["transactions"]
    assert {t["merchant"] for t in two} == {"iFood", "Uber", "Netflix"}
    assert len(week) > len(two)


def test_a_read_below_required_assurance_is_denied(plane: ControlPlane) -> None:
    from control_plane.actions import CAPABILITIES

    strict = CAPABILITIES["GET_BALANCE"].__class__(
        "GET_BALANCE", "low", False, "strong"
    )
    original = CAPABILITIES["GET_BALANCE"]
    CAPABILITIES["GET_BALANCE"] = strict
    try:
        assert plane.query(CTX, GetBalance()).status == "DENY"
    finally:
        CAPABILITIES["GET_BALANCE"] = original


# --------------------------------------------------------------------------
# typing at the boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("amount", ["0", "-5", "1.005", "1e13"])
def test_bad_amounts_never_become_actions(amount: str) -> None:
    with pytest.raises(ValueError):
        ProposedPix(recipient="Renata", amount=Decimal(amount))


def test_brl_formats_like_a_bank_statement() -> None:
    assert brl(Decimal("1234.5")) == "R$ 1.234,50"
    assert brl(Decimal("0.13")) == "R$ 0,13"
    assert brl(Decimal("1000000")) == "R$ 1.000.000,00"
