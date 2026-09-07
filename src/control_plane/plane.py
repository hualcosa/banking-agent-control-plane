"""The control plane: propose, confirm, execute, reconcile, explain.

This is the only module an agent imports, and the shape of its methods is the
shape of the boundary:

* **reads** go through :meth:`ControlPlane.query` and come straight back;
* a **PIX** is *proposed* (:meth:`propose`), which resolves the recipient,
  validates preconditions, scores risk and asks policy — and never moves money;
* what policy asks for is then satisfied out of band: :meth:`step_up` for
  assurance, :meth:`confirm` for consent — and only ``confirm`` reaches the
  execution gateway (:meth:`_execute`);
* a timeout leaves the intent ``UNKNOWN`` and :meth:`reconcile` resolves it by
  asking the bank, never by paying again;
* :meth:`explain` reads the ledger back — for the principal that wrote it,
  never for another — which is the audit answer.

Every method takes a :class:`Context` and every intent remembers the one that
created it. A confirmation is honoured only from the same customer **and** the
same session — a "yes" cannot be borrowed across conversations.

# ponytail: one instance per process, dicts for storage. V1 is the same class
# over Postgres and a per-request instance; nothing here touches a framework.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.actions import (
    ASSURANCE_RANK,
    Context,
    CreatePix,
    GetBalance,
    GetCardTransactions,
    ProposedPix,
    ReadAction,
    capability,
)
from control_plane.bank import BankError, BankTimeout, MockBank
from control_plane.policy import Decision, Verdict, assess_risk, evaluate
from control_plane.state import (
    Event,
    IllegalTransition,
    Intent,
    new_id,
    now,
    transition,
)
from control_plane.store import MemoryStore, Store

Status = Decision | Literal["COMPLETED", "FAILED", "CANCELLED", "UNKNOWN", "PENDING"]

#: V0 has one customer with one account. Account selection is a V1 concern.
DEFAULT_ACCOUNT = "checking_001"


def brl(amount: Decimal) -> str:
    """``R$ 1.234,56`` — the format the message to the customer uses."""
    whole, _, cents = f"{amount:.2f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"R$ {grouped},{cents}"


class Outcome(BaseModel):
    """What the control plane says back. The agent relays; it does not decide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Status
    message: str
    intent_id: str | None = None
    confirmation_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ControlPlane:
    """The boundary. One instance owns a bank, a ledger and the live intents.

    ``clock`` is the plane's source of "now", injected so that a rule which
    reads the hour — the BACEN nighttime cap — is testable at a fixed instant
    rather than only between 06h and 20h. The default is the system clock, so
    nothing but a test passes anything here.
    """

    def __init__(
        self,
        bank: MockBank | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        store: Store | None = None,
    ) -> None:
        self.bank = bank if bank is not None else MockBank()
        self.clock = clock if clock is not None else now
        self.store: Store = store if store is not None else MemoryStore()

    @property
    def intents(self) -> Mapping[str, Intent]:
        """Every live intent, read-only. Writes go through the store."""
        return self.store.snapshot()

    @property
    def ledger(self) -> Store:
        """The append-only event sink. Named for what it is, not where it lives."""
        return self.store

    # ----------------------------------------------------------------------
    # reads
    # ----------------------------------------------------------------------

    def query(self, ctx: Context, action: ReadAction) -> Outcome:
        rid = new_id("read")
        self.store.append(
            rid, "request", **self._who(ctx), action=action.model_dump(mode="json")
        )
        cap = capability(action)
        if ASSURANCE_RANK[ctx.assurance] < ASSURANCE_RANK[cap.required_assurance]:
            self.store.append(rid, "result", status="DENY", rule="read_assurance")
            return Outcome(
                status="DENY", message="sessão sem garantia suficiente", intent_id=rid
            )

        data: dict[str, Any]
        if isinstance(action, GetBalance):
            balance = self.bank.balance(action.account_id)
            data = {
                "account_id": action.account_id,
                "balance": str(balance),
                "display": brl(balance),
            }
        else:
            assert isinstance(action, GetCardTransactions)
            data = {
                "days": action.days,
                "transactions": [
                    {
                        "date": t.at.isoformat(),
                        "merchant": t.merchant,
                        "amount": str(t.amount),
                        "display": brl(t.amount),
                        "category": t.category,
                    }
                    for t in self.bank.card_transactions(action.days)
                ],
            }
        self.store.append(rid, "result", status="ALLOW")
        return Outcome(status="ALLOW", message="", intent_id=rid, data=data)

    # ----------------------------------------------------------------------
    # the write path
    # ----------------------------------------------------------------------

    def propose(
        self, ctx: Context, proposed: ProposedPix, *, request_text: str = ""
    ) -> Outcome:
        """Turn what the agent understood into an intent, and say what it needs.

        Never executes. The best outcome here is ``REQUIRE_CONFIRMATION``
        with a ``confirmation_id`` — a token that names one immutable action.
        """
        intent_id = new_id("pix")
        self.store.append(intent_id, "request", **self._who(ctx), text=request_text)
        self.store.append(
            intent_id, "interpreted", proposed=proposed.model_dump(mode="json")
        )

        # 1. entity resolution — fail closed on anything but exactly one match
        matches = self.bank.find_contacts(proposed.recipient)
        self.store.append(
            intent_id,
            "resolution",
            query=proposed.recipient,
            matches=[m.id for m in matches],
        )
        if not matches:
            return Outcome(
                status="REQUIRE_MORE_INFO",
                message=f"nenhum contato chamado {proposed.recipient!r}; peça a chave PIX ou o nome completo",
                intent_id=intent_id,
                data={"candidates": []},
            )
        if len(matches) > 1:
            return Outcome(
                status="REQUIRE_MORE_INFO",
                message=f"{len(matches)} contatos correspondem a {proposed.recipient!r}; pergunte qual",
                intent_id=intent_id,
                data={"candidates": [{"id": m.id, "name": m.name} for m in matches]},
            )
        recipient = matches[0]

        # 2. the canonical action, built here and nowhere else
        action = CreatePix(
            source_account=DEFAULT_ACCOUNT,
            recipient_id=recipient.id,
            amount=proposed.amount,
            currency=proposed.currency,
        )
        intent = Intent(id=intent_id, action=action, context=ctx)
        self.store.put(intent)
        self.store.append(
            intent.id, "canonical_action", action=action.model_dump(mode="json")
        )

        # 3. preconditions
        missing = self._missing_preconditions(intent)
        if missing:
            self._move(intent, "reject", reason="preconditions")
            intent.reason = ", ".join(missing)
            return Outcome(
                status="DENY",
                message="pré-condições não atendidas: " + intent.reason,
                intent_id=intent.id,
                data={"missing_preconditions": missing},
            )
        self._move(intent, "validate")

        return self._decide(intent)

    def step_up(self, ctx: Context, intent_id: str) -> Outcome:
        """Record a strong-assurance approval for one intent and re-run policy.

        Simulated: in V0 the caller *is* the out-of-band channel. What is real
        is the binding — the upgrade applies to this intent only, and the
        policy is evaluated again rather than skipped.
        """
        intent = self._owned(ctx, intent_id)
        if intent is None:
            return self._unknown(intent_id)
        if intent.state != "AWAITING_STEP_UP":
            return self._status(
                intent, "essa ação não está aguardando autenticação", status="DENY"
            )
        intent.context = intent.context.model_copy(update={"assurance": "strong"})
        self.store.put(intent)
        self.store.append(
            intent.id,
            "step_up",
            method="simulated_mobile_biometric",
            at=now().isoformat(),
        )
        self._move(intent, "step_up")
        return self._decide(intent)

    def confirm(self, ctx: Context, confirmation_id: str) -> Outcome:
        """Consent to one exact action, then execute it. Idempotent."""
        intent = self._by_confirmation(ctx, confirmation_id)
        if intent is None:
            return self._unknown(confirmation_id)
        if intent.state in ("COMPLETED", "SUBMITTED", "PENDING", "UNKNOWN"):
            # The agent repeated itself. Money does not.
            self.store.append(intent.id, "duplicate_confirmation", state=intent.state)
            return self._status(intent, "já executado; nenhum novo pagamento foi feito")
        if intent.state != "AWAITING_CONFIRMATION":
            return self._status(
                intent, "essa ação não está aguardando confirmação", status="DENY"
            )

        intent.confirmed_at = self.clock()
        intent.confirmed_by = ctx.customer_id
        self.store.put(intent)
        self.store.append(
            intent.id,
            "confirmation",
            confirmation_id=confirmation_id,
            by=ctx.customer_id,
            session=ctx.session_id,
            channel=ctx.channel,
            at=intent.confirmed_at.isoformat(),
            action=intent.action.model_dump(mode="json"),
        )
        self._move(intent, "confirm")
        self.store.append(
            intent.id, "authorization", assurance=intent.context.assurance
        )
        return self._execute(intent)

    def cancel(self, ctx: Context, confirmation_id: str) -> Outcome:
        intent = self._by_confirmation(ctx, confirmation_id)
        if intent is None:
            return self._unknown(confirmation_id)
        try:
            self._move(intent, "cancel", by=ctx.customer_id)
        except IllegalTransition:
            return self._status(
                intent, "essa ação não pode mais ser cancelada", status="DENY"
            )
        intent.reason = "cancelado pelo cliente"
        self.store.put(intent)
        return self._status(intent, intent.reason)

    def reconcile(self, ctx: Context, intent_id: str) -> Outcome:
        """Resolve ``UNKNOWN`` by asking the bank what it did. Never re-pays."""
        intent = self._owned(ctx, intent_id)
        if intent is None:
            return self._unknown(intent_id)
        if intent.state != "UNKNOWN":
            return self._status(intent)
        receipt = self.bank.payment(intent.idempotency_key)
        self.store.append(intent.id, "reconciliation", found=receipt is not None)
        if receipt is None:
            self._move(intent, "fail", reason="banco não registrou o pagamento")
            intent.reason = "não executado"
            self.store.put(intent)
            return self._status(intent, intent.reason)
        intent.receipt = receipt
        self._move(intent, "complete", reconciled=True)
        return self._status(intent, "confirmado junto ao banco: o pagamento foi feito")

    def status(self, ctx: Context, intent_id: str) -> Outcome:
        intent = self._owned(ctx, intent_id)
        return self._unknown(intent_id) if intent is None else self._status(intent)

    def explain(self, ctx: Context, intent_id: str) -> list[Event]:
        """The audit trail: every persisted event for one intent, in order.

        Scoped like every other method here. The ledger itself names the
        principal that opened the trail, so a read id is protected on the same
        terms as a PIX, and a trail that belongs to someone else is
        indistinguishable from one that never existed: both are ``[]``.
        """
        events = self.store.events_for(intent_id)
        if self._principal_of(events) != (ctx.customer_id, ctx.session_id):
            return []
        return events

    # ----------------------------------------------------------------------
    # internals
    # ----------------------------------------------------------------------

    def _move(self, intent: Intent, event: str, **detail: Any) -> Intent:
        """One hop of the state machine, then saved.

        Never call :func:`transition` directly from here: moving an intent and
        recording where it moved to are one operation, and a store that only
        sees half of it is a store that loses money on the next restart.
        """
        transition(intent, event, self.store, **detail)
        self.store.put(intent)
        return intent

    def _decide(self, intent: Intent) -> Outcome:
        """Risk, then policy, then the transition policy asked for."""
        risk = assess_risk(
            intent.action, intent.context, known_recipients=self.bank.paid_before
        )
        self.store.append(
            intent.id,
            "risk",
            score=risk.score,
            level=risk.level,
            signals=list(risk.signals),
        )
        verdict = evaluate(intent.action, intent.context, risk, now=self.clock())
        self.store.append(
            intent.id,
            "policy",
            decision=verdict.decision,
            rule=verdict.rule,
            reason=verdict.reason,
        )
        return self._apply(intent, verdict)

    def _apply(self, intent: Intent, verdict: Verdict) -> Outcome:
        if verdict.decision == "DENY":
            self._move(intent, "reject", rule=verdict.rule)
            intent.reason = verdict.reason
            self.store.put(intent)
            return self._status(intent, verdict.reason, status="DENY")
        if verdict.decision == "REQUIRE_STEP_UP_AUTH":
            self._move(intent, "require_step_up", rule=verdict.rule)
            return Outcome(
                status="REQUIRE_STEP_UP_AUTH",
                message=verdict.reason,
                intent_id=intent.id,
                data=self._summary(intent),
            )
        if verdict.decision == "REQUIRE_CONFIRMATION":
            intent.confirmation_id = new_id("conf")
            self._move(
                intent, "await_confirmation", confirmation_id=intent.confirmation_id
            )
            return Outcome(
                status="REQUIRE_CONFIRMATION",
                message="apresente exatamente este valor e destinatário e peça confirmação explícita",
                intent_id=intent.id,
                confirmation_id=intent.confirmation_id,
                data=self._summary(intent),
            )
        # ALLOW / ESCALATE for a write are not in V0's rule set. Fail closed:
        # an unexpected verdict on a money path stops rather than guesses.
        self._move(intent, "reject", rule=verdict.rule)
        intent.reason = f"decisão sem caminho de execução: {verdict.decision}"
        return self._status(intent, intent.reason, status="DENY")

    def _execute(self, intent: Intent) -> Outcome:
        """The execution gateway. The one place the bank is asked to move money."""
        self._move(intent, "submit")
        self.store.append(
            intent.id, "execution_request", idempotency_key=intent.idempotency_key
        )
        try:
            receipt = self.bank.create_pix(
                idempotency_key=intent.idempotency_key,
                source_account=intent.action.source_account,
                recipient_id=intent.action.recipient_id,
                amount=intent.action.amount,
            )
        except BankTimeout as exc:
            self.store.append(intent.id, "backend_response", error=str(exc))
            self._move(intent, "timeout")
            return self._status(
                intent,
                "sem resposta do banco; o pagamento pode ter sido feito — verifique antes de repetir",
            )
        except BankError as exc:
            self.store.append(intent.id, "backend_response", error=str(exc))
            self._move(intent, "fail", reason=str(exc))
            intent.reason = str(exc)
            return self._status(intent, str(exc))
        intent.receipt = receipt
        self.store.append(intent.id, "backend_response", receipt=receipt)
        self._move(intent, "complete")
        return self._status(intent, "pagamento concluído")

    def _missing_preconditions(self, intent: Intent) -> list[str]:
        missing: list[str] = []
        try:
            balance = self.bank.balance(intent.action.source_account)
        except BankError:
            missing.append("source_account")
        else:
            if balance < intent.action.amount:
                missing.append("sufficient_funds")
        if self.bank.contact(intent.action.recipient_id) is None:
            missing.append("resolved_recipient")
        return missing

    def _summary(self, intent: Intent) -> dict[str, Any]:
        contact = self.bank.contact(intent.action.recipient_id)
        return {
            "action": intent.action.action,
            "recipient": contact.name if contact else intent.action.recipient_id,
            "amount": str(intent.action.amount),
            "display": brl(intent.action.amount),
            "state": intent.state,
        }

    def _status(
        self, intent: Intent, message: str = "", *, status: Status | None = None
    ) -> Outcome:
        """The intent as an outcome. ``status`` overrides the state-derived one.

        A settled intent reports its state; a *refused request* about an
        intent reports ``DENY`` regardless of that state — "you may not confirm
        this" is a decision about the request, not a fact about the intent.
        """
        data = self._summary(intent)
        if intent.receipt:
            data["receipt"] = intent.receipt
        settled = intent.state in (
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "UNKNOWN",
            "PENDING",
        )
        return Outcome(
            status=status or (intent.state if settled else "DENY"),
            message=message or intent.reason,
            intent_id=intent.id,
            confirmation_id=intent.confirmation_id,
            data=data,
        )

    def _unknown(self, ref: str) -> Outcome:
        return Outcome(
            status="DENY", message=f"referência desconhecida para esta sessão: {ref}"
        )

    def _owned(self, ctx: Context, intent_id: str) -> Intent | None:
        intent = self.store.get(intent_id)
        if intent is None or not self._same_principal(intent, ctx):
            return None
        return intent

    def _by_confirmation(self, ctx: Context, confirmation_id: str) -> Intent | None:
        intent = self.store.by_confirmation(confirmation_id)
        if intent is None or not self._same_principal(intent, ctx):
            return None
        return intent

    @staticmethod
    def _principal_of(events: list[Event]) -> tuple[str, str] | None:
        """Who opened this trail, from the first event that says so.

        Every path into the ledger starts with a ``request`` event carrying
        :meth:`_who`; an id with no such event is nobody's.
        """
        for event in events:
            detail = event.detail
            if "customer" in detail and "session" in detail:
                return str(detail["customer"]), str(detail["session"])
        return None

    @staticmethod
    def _same_principal(intent: Intent, ctx: Context) -> bool:
        return (
            intent.context.customer_id == ctx.customer_id
            and intent.context.session_id == ctx.session_id
        )

    @staticmethod
    def _who(ctx: Context) -> dict[str, Any]:
        return {
            "customer": ctx.customer_id,
            "session": ctx.session_id,
            "channel": ctx.channel,
        }
