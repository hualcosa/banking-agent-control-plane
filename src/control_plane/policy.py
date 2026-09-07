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
from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from control_plane.actions import (
    ASSURANCE_RANK,
    Context,
    CreatePix,
    RiskLevel,
    capability,
)
from control_plane.state import now as system_now

Decision = Literal[
    "ALLOW",
    "DENY",
    "REQUIRE_MORE_INFO",
    "REQUIRE_CONFIRMATION",
    "REQUIRE_STEP_UP_AUTH",
    "ESCALATE",
]

#: Above this a PIX needs strong assurance — a step-up — before confirmation.
#: Institution's choice.
STEP_UP_ABOVE = Decimal("1000")
#: Above this a PIX from an assistant is refused outright. **This is the
#: per-transaction ceiling for this channel, and it is the institution's
#: number, not the regulator's.** BACEN fixes no per-PIX ceiling: Resolução
#: BCB nº 142/2021 lets each institution set its own limits per channel, and
#: only the nighttime cap below is written into the norm. A ceiling on what
#: the autonomous path may ever move; raising it is a policy change, not a
#: config change, and lives here for that reason.
HARD_LIMIT = Decimal("5000")
#: **Regulator-fixed.** Resolução BCB nº 142/2021 caps PIX to a natural
#: person at R$ 1.000 during the nighttime window. The institution may set a
#: *lower* cap, never a higher one without the customer asking for it.
NIGHT_LIMIT = Decimal("1000")
#: **Regulator-fixed.** The window runs 20h–06h *local Brazilian time* —
#: [NIGHT_START_HOUR, 24) ∪ [0, NIGHT_END_HOUR) — so a UTC instant is
#: converted to :data:`BR_TZ` before its hour is read. Comparing a UTC hour
#: to a Brazilian clock silently moves the window by three hours.
NIGHT_START_HOUR = 20
NIGHT_END_HOUR = 6
#: The timezone the norm is written in. Brazil has had no DST since 2019, but
#: naming the zone (rather than a fixed -03:00) keeps that a fact about the
#: tz database and not an assumption baked into this module.
BR_TZ = ZoneInfo("America/Sao_Paulo")
#: An amount the risk engine calls unusual for this customer. Mocked: a real
#: engine derives it from the customer's history.
UNUSUAL_ABOVE = Decimal("500")
#: Below this, the transcriber is not confident enough for the words to be
#: taken at face value. Institution's choice, and the one number in this
#: module a real deployment should tune against its own recogniser's
#: calibration rather than inherit.
HEARD_CLEARLY = 0.85


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
    if ctx.stt_confidence is not None and ctx.stt_confidence < HEARD_CLEARLY:
        # A transcript the recogniser is unsure of is not a weaker instruction,
        # it is a different one: "trezentos" and "treze" differ by a factor of
        # twenty, and "Renata" and "Renato" are different people. The signal is
        # deliberately here, in risk, rather than as a rule of its own —
        # uncertainty about what was said raises how carefully the action is
        # treated; it does not decide the action.
        signals.append("low_stt_confidence")
        score += 0.4
    level: RiskLevel = "high" if score >= 0.6 else "medium" if score >= 0.3 else "low"
    return Risk(score=round(score, 2), level=level, signals=tuple(signals))


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule: str
    reason: str


def is_nighttime(moment: datetime) -> bool:
    """Is ``moment`` inside the BACEN nighttime window, on a Brazilian clock?"""
    hour = moment.astimezone(BR_TZ).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def evaluate(
    action: CreatePix, ctx: Context, risk: Risk, *, now: datetime | None = None
) -> Verdict:
    """The rule set. Ordered; the first rule that applies decides.

    ``now`` is the clock, injected. ``None`` means "ask the system" — that
    default is what lets every caller stay unchanged while a test pins the
    hour to either side of a boundary.
    """
    cap = capability(action)
    moment = system_now() if now is None else now

    if action.amount > HARD_LIMIT:
        return Verdict(
            "DENY",
            "pix_hard_limit",
            f"PIX acima do limite do assistente (R$ {HARD_LIMIT:.2f})",
        )

    # Before the step-up rule on purpose: a denial that is only reachable
    # after the customer has already authenticated is not a denial, it is a
    # trap. V0 treats every recipient as a natural person — the pessimistic
    # reading, and the only one the mocked contact book supports.
    if action.amount > NIGHT_LIMIT and is_nighttime(moment):
        return Verdict(
            "DENY",
            "pix_nighttime_limit",
            f"PIX acima de R$ {NIGHT_LIMIT:.2f} entre "
            f"{NIGHT_START_HOUR}h e {NIGHT_END_HOUR:02d}h "
            "não é permitido (Resolução BCB nº 142/2021)",
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
