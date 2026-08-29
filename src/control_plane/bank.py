"""The bank, mocked. Three capabilities and the two failure modes that matter.

Everything the control plane will one day say to a real core banking API it
says to this class, so the adapter surface is visible now: ``balance``,
``card_transactions``, ``create_pix``, ``payment`` (the lookup reconciliation
needs), ``contacts`` (recipient resolution). Nothing else.

The mock is deliberately not perfectly reliable. ``create_pix`` honours an
idempotency key — the same key twice returns the same receipt and debits
once — and it simulates the failure that idempotency exists for: an amount
whose cents are ``.13`` is paid *and then* raises :class:`BankTimeout`, so
the caller sees a timeout for a payment that went through. A control plane
that retries on that timeout without the key would pay twice; one that
reconciles finds the receipt. Both behaviours are tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from control_plane.state import new_id


class BankError(Exception):
    """A definitive refusal from the bank. The payment did not happen."""


class InsufficientFunds(BankError):
    pass


class BankTimeout(Exception):
    """No answer. The payment may or may not have happened."""


#: Cents that trigger a simulated timeout-after-success. A tripwire for the
#: demo and the tests; documented in the README.
TIMEOUT_CENTS = Decimal("0.13")


@dataclass(frozen=True)
class Contact:
    id: str
    name: str
    pix_key: str


@dataclass(frozen=True)
class Transaction:
    at: date
    merchant: str
    amount: Decimal
    category: str


@dataclass
class MockBank:
    """One customer's worth of bank, in memory."""

    accounts: dict[str, Decimal] = field(
        default_factory=lambda: {"checking_001": Decimal("2543.10")}
    )
    contacts: tuple[Contact, ...] = (
        Contact("contact_renata", "Renata Silva", "renata.silva@pix.example"),
        Contact("contact_joao", "João Pereira", "+5511999990000"),
        Contact("contact_maria", "Maria Souza", "123.456.789-00"),
        # Two Anas, on purpose: "send 50 to Ana" must come back as a question.
        Contact("contact_ana_lima", "Ana Lima", "ana.lima@pix.example"),
        Contact("contact_ana_costa", "Ana Costa", "ana.costa@pix.example"),
    )
    #: Anyone this customer has paid before. Feeds the ``new_recipient`` signal.
    paid_before: set[str] = field(default_factory=lambda: {"contact_renata"})
    #: ``idempotency_key → receipt``. The ledger of what actually moved.
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- reads --------------------------------------------------------------

    def balance(self, account_id: str) -> Decimal:
        if account_id not in self.accounts:
            raise BankError(f"conta desconhecida: {account_id}")
        return self.accounts[account_id]

    def card_transactions(self, days: int) -> list[Transaction]:
        today = date.today()
        seed = [
            (1, "iFood", "129.00", "alimentação"),
            (1, "Uber", "23.90", "transporte"),
            (2, "Netflix", "55.90", "assinatura"),
            (3, "Drogasil", "87.45", "farmácia"),
            (5, "Amazon", "249.99", "compras"),
            (9, "Posto Shell", "180.00", "combustível"),
        ]
        return [
            Transaction(today - timedelta(days=d), m, Decimal(a), c)
            for d, m, a, c in seed
            if d <= days
        ]

    def find_contacts(self, name: str) -> list[Contact]:
        needle = name.strip().casefold()
        return [c for c in self.contacts if needle in c.name.casefold()]

    def contact(self, contact_id: str) -> Contact | None:
        return next((c for c in self.contacts if c.id == contact_id), None)

    def payment(self, idempotency_key: str) -> dict[str, Any] | None:
        """What the bank knows about a key. The reconciliation query."""
        return self.payments.get(idempotency_key)

    # --- the write ------------------------------------------------------------

    def create_pix(
        self,
        *,
        idempotency_key: str,
        source_account: str,
        recipient_id: str,
        amount: Decimal,
    ) -> dict[str, Any]:
        if (existing := self.payments.get(idempotency_key)) is not None:
            return existing
        if self.contact(recipient_id) is None:
            raise BankError(f"destinatário desconhecido: {recipient_id}")
        if self.balance(source_account) < amount:
            raise InsufficientFunds("saldo insuficiente")

        self.accounts[source_account] -= amount
        self.paid_before.add(recipient_id)
        receipt = {
            "payment_id": new_id("pay"),
            "end_to_end_id": f"E{new_id('')[1:].upper()}",
            "status": "COMPLETED",
            "amount": str(amount),
            "recipient_id": recipient_id,
        }
        self.payments[idempotency_key] = receipt
        if amount % 1 == TIMEOUT_CENTS:
            raise BankTimeout("sem resposta do banco")
        return receipt
