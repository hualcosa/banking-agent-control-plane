"""Risk signals and the policy that reads them. Deterministic, ordered, small.

Two questions, kept apart because they have different owners:

* :func:`assess_risk` — *how dangerous does this look?* Signals in, a score
  and a level out. Mocked in V0: three signals, fixed weights. A fraud engine
  replaces this function and nothing else.
* :func:`evaluate` — *should it be allowed under these conditions?* Rules in
  order, first match wins, and the fall-through is what the capability
  registry says the action needs. This is where a bank's policy lives, in
  code that a reviewer can read top to bottom — never in a prompt.

Every verdict names its rule. "Denied" is not an answer; "denied by
``pix_hard_limit``" is one, and it is what the ledger stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from control_plane.actions import (
    ASSURANCE_RANK,
    Context,
    CreatePix,
    RiskLevel,
    capability,
)

Decision = Literal[
    "ALLOW",
    "DENY",
    "REQUIRE_MORE_INFO",
    "REQUIRE_CONFIRMATION",
    "REQUIRE_STEP_UP_AUTH",
    "ESCALATE",
]

#: Above this a PIX needs strong assurance — a step-up — before confirmation.
STEP_UP_ABOVE = Decimal("1000")
#: Above this a PIX from an assistant is refused outright. A ceiling on what
#: the autonomous path may ever move; raising it is a policy change, not a
#: config change, and lives here for that reason.
HARD_LIMIT = Decimal("5000")
#: An amount the risk engine calls unusual for this customer. Mocked: a real
#: engine derives it from the customer's history.
UNUSUAL_ABOVE = Decimal("500")


@dataclass(frozen=True)
class Risk:
    score: float
    level: RiskLevel
    signals: tuple[str, ...]


def assess_risk(action: CreatePix, ctx: Context, *, known_recipients: set[str]) -> Risk:
    """Mocked risk: three signals with fixed weights.

    # ponytail: weighted sum of three booleans. A real engine takes velocity,
    # location and behavioural history; the interface — action + context in,
    # ``Risk`` out — is the part meant to survive that.
    """
    signals: list[str] = []
    score = 0.0
    if action.recipient_id not in known_recipients:
        signals.append("new_recipient")
        score += 0.3
    if action.amount > UNUSUAL_ABOVE:
        signals.append("unusual_amount")
        score += 0.4
    if not ctx.device_trusted:
        signals.append("untrusted_device")
        score += 0.3
    level: RiskLevel = "high" if score >= 0.6 else "medium" if score >= 0.3 else "low"
    return Risk(score=round(score, 2), level=level, signals=tuple(signals))


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule: str
    reason: str


def evaluate(action: CreatePix, ctx: Context, risk: Risk) -> Verdict:
    """The rule set. Ordered; the first rule that applies decides."""
    cap = capability(action)

    if action.amount > HARD_LIMIT:
        return Verdict(
            "DENY",
            "pix_hard_limit",
            f"PIX acima do limite do assistente (R$ {HARD_LIMIT:.2f})",
        )

    needs_strong = action.amount > STEP_UP_ABOVE or risk.level == "high"
    required = "strong" if needs_strong else cap.required_assurance
    if ASSURANCE_RANK[ctx.assurance] < ASSURANCE_RANK[required]:
        why = "valor acima de" if action.amount > STEP_UP_ABOVE else "risco alto:"
        detail = (
            f"R$ {STEP_UP_ABOVE:.2f}"
            if action.amount > STEP_UP_ABOVE
            else ", ".join(risk.signals)
        )
        return Verdict(
            "REQUIRE_STEP_UP_AUTH",
            "pix_step_up",
            f"{why} {detail}; exige autenticação forte no aplicativo",
        )

    if cap.requires_confirmation:
        return Verdict(
            "REQUIRE_CONFIRMATION",
            "capability_requires_confirmation",
            "ação sensível: exige confirmação explícita do cliente",
        )

    return Verdict("ALLOW", "default_allow", "")
