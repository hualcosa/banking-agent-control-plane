"""Typed actions, the session context they run in, and the capability registry.

This is the boundary the whole repository is about. Natural language stops at
the agent; what crosses into the control plane is one of the models below,
validated by pydantic before any code here can see it. There is no
``action: str`` anywhere — an action the registry does not know is a type
error, not a runtime branch.

Two shapes for a PIX, and the split is the design:

* :class:`ProposedPix` is what the *agent* emits. It names the recipient the
  way the user did ("Renata") and carries the amount as the user said it.
* :class:`CreatePix` is the *canonical* action: a resolved ``recipient_id``, a
  validated source account, a two-decimal amount. Only the control plane can
  build one, because only the control plane resolves contacts.

An agent that could emit a ``CreatePix`` directly could also invent a
``recipient_id`` — and a fabricated identifier that passes validation is the
most expensive kind of wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Assurance = Literal["low", "medium", "strong"]
RiskLevel = Literal["low", "medium", "high"]

#: The order the assurance ladder climbs. ``strong`` is what a step-up grants.
ASSURANCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "strong": 2}


class Context(BaseModel):
    """Who is acting, from where, and how sure we are it is them.

    Mocked in V0 — the runtime stamps one customer on every session — but
    every decision below already takes it, so a real identity provider is a
    change to the caller and not to the policy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_id: str
    session_id: str
    channel: Literal["whatsapp", "web", "mobile", "voice"] = "whatsapp"
    device_trusted: bool = True
    assurance: Assurance = "medium"


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

#: Positive, at most two decimals. Money is a ``Decimal`` end to end; a float
#: that reaches this layer is a bug in the caller, not a rounding question.
Money = Annotated[Decimal, Field(gt=0, decimal_places=2, max_digits=12)]


class GetBalance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["GET_BALANCE"] = "GET_BALANCE"
    account_id: str = "checking_001"


class GetCardTransactions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["GET_CARD_TRANSACTIONS"] = "GET_CARD_TRANSACTIONS"
    days: int = Field(default=7, ge=1, le=90)


class ProposedPix(BaseModel):
    """What the agent may say: a name and an amount. Not executable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["PROPOSE_PIX"] = "PROPOSE_PIX"
    recipient: str = Field(min_length=1)
    amount: Money
    currency: Literal["BRL"] = "BRL"


class CreatePix(BaseModel):
    """The canonical action. Built only by the control plane, after resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["CREATE_PIX"] = "CREATE_PIX"
    source_account: str
    recipient_id: str
    amount: Money
    currency: Literal["BRL"] = "BRL"


ReadAction = GetBalance | GetCardTransactions
Action = Annotated[
    GetBalance | GetCardTransactions | CreatePix, Field(discriminator="action")
]


# --------------------------------------------------------------------------
# Capability registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """What the control plane must do before an action of this kind runs."""

    action: str
    risk_level: RiskLevel
    requires_confirmation: bool
    required_assurance: Assurance
    auditable: bool = True


CAPABILITIES: dict[str, Capability] = {
    c.action: c
    for c in (
        Capability("GET_BALANCE", "low", False, "low"),
        Capability("GET_CARD_TRANSACTIONS", "low", False, "low"),
        Capability("CREATE_PIX", "high", True, "medium"),
    )
}


def capability(action: GetBalance | GetCardTransactions | CreatePix) -> Capability:
    return CAPABILITIES[action.action]
