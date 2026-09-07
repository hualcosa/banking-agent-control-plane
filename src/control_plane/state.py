"""The payment intent, its state machine, and the ledger that records both.

A sensitive action is not a function call; it is an object with a lifecycle.
``CREATED → VALIDATED → AWAITING_CONFIRMATION → AUTHORIZED → SUBMITTED →
COMPLETED`` is the happy path, and every hop is a row in :data:`TRANSITIONS`.
A hop that is not in the table raises :class:`IllegalTransition` — an agent
that repeats itself, a retry that arrives late, a confirmation for an intent
already executed: all of them hit this one guard and none of them move money.

``UNKNOWN`` is a real state, not an error. A timeout after ``SUBMITTED`` means
the bank may or may not have paid; the only correct thing to do is record
that and ask the bank later (see ``plane.reconcile``). Treating a timeout as
``FAILED`` and retrying is how a customer pays twice.

The :class:`Ledger` is append-only and holds two things: state transitions
and pipeline events (what was interpreted, what policy said, who confirmed).
``plane.explain(ctx, intent_id)`` reads it back in order, for the
principal that wrote it. That reading — not the model's
chain of thought — is the answer to "why did this action happen".

# ponytail: `Ledger` is still the in-process implementation. The two Postgres
# tables exist (intents, ledger_events) and `PgStore` writes them; this class is
# what `MemoryStore` uses and what the unit tier runs on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from secrets import token_hex
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.actions import Context, CreatePix

State = Literal[
    "CREATED",
    "VALIDATED",
    "AWAITING_STEP_UP",
    "AWAITING_CONFIRMATION",
    "AUTHORIZED",
    "SUBMITTED",
    "PENDING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "REVERSED",
    "UNKNOWN",
]

TERMINAL: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED", "REVERSED"})

#: ``(from, event) → to``. The whole machine, readable in one screen.
TRANSITIONS: dict[tuple[str, str], State] = {
    ("CREATED", "validate"): "VALIDATED",
    ("CREATED", "reject"): "CANCELLED",
    ("VALIDATED", "reject"): "CANCELLED",
    ("VALIDATED", "require_step_up"): "AWAITING_STEP_UP",
    ("VALIDATED", "await_confirmation"): "AWAITING_CONFIRMATION",
    ("AWAITING_STEP_UP", "step_up"): "VALIDATED",
    ("AWAITING_STEP_UP", "cancel"): "CANCELLED",
    ("AWAITING_CONFIRMATION", "confirm"): "AUTHORIZED",
    ("AWAITING_CONFIRMATION", "cancel"): "CANCELLED",
    ("AUTHORIZED", "submit"): "SUBMITTED",
    ("SUBMITTED", "complete"): "COMPLETED",
    ("SUBMITTED", "pend"): "PENDING",
    ("SUBMITTED", "fail"): "FAILED",
    ("SUBMITTED", "timeout"): "UNKNOWN",
    ("PENDING", "complete"): "COMPLETED",
    ("PENDING", "fail"): "FAILED",
    ("UNKNOWN", "complete"): "COMPLETED",
    ("UNKNOWN", "fail"): "FAILED",
    ("COMPLETED", "reverse"): "REVERSED",
}


class IllegalTransition(Exception):
    def __init__(self, intent_id: str, state: str, event: str) -> None:
        super().__init__(f"{intent_id}: no transition from {state} on {event!r}")
        self.intent_id, self.state, self.event = intent_id, state, event


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{token_hex(6)}"


class Intent(BaseModel):
    """One sensitive action, from proposal to a terminal state.

    Mutable on purpose — this is the one place state is allowed to change —
    and only :func:`transition` changes ``state``.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str = Field(default_factory=lambda: new_id("pix"))
    action: CreatePix
    context: Context
    state: State = "CREATED"
    created_at: datetime = Field(default_factory=now)
    #: The token the customer confirms. Issued on entering
    #: ``AWAITING_CONFIRMATION``; binds a "yes" to this exact object.
    confirmation_id: str | None = None
    #: When the token was issued. A confirmation is consent to act *now*, so
    #: it expires: without this field "the customer said yes yesterday" is not
    #: a scenario the system can be wrong about, because it cannot tell.
    confirmation_issued_at: datetime | None = None
    #: A digest of the action as it was when the token was issued. Consent is
    #: to one exact object, and this is what makes that checkable rather than
    #: merely intended — see :func:`action_digest`.
    action_digest: str | None = None
    confirmed_at: datetime | None = None
    confirmed_by: str | None = None
    #: What the bank answered. ``None`` until ``SUBMITTED`` resolves.
    receipt: dict[str, Any] | None = None
    #: Why a terminal state was reached, when it was not success.
    reason: str = ""

    @property
    def idempotency_key(self) -> str:
        """One intent, one key, forever. The agent can repeat itself; this cannot."""
        return self.id


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime = Field(default_factory=now)
    intent_id: str
    kind: str
    detail: dict[str, Any] = Field(default_factory=dict)


class Ledger:
    """Append-only. Nothing here is ever updated or deleted."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, intent_id: str, kind: str, **detail: Any) -> Event:
        event = Event(intent_id=intent_id, kind=kind, detail=detail)
        self._events.append(event)
        return event

    def for_intent(self, intent_id: str) -> list[Event]:
        return [e for e in self._events if e.intent_id == intent_id]

    def __len__(self) -> int:
        return len(self._events)


#: How long a "yes" is good for. Short on purpose: a confirmation is consent
#: to move money now, and the customer who said it is still in the
#: conversation. Long enough to survive a slow reply, not long enough for the
#: phone to change hands.
CONFIRMATION_TTL = timedelta(minutes=5)


def action_digest(action: CreatePix, secret: str) -> str:
    """A tamper-evident fingerprint of the action a token was issued against.

    Keyed rather than a bare hash: an unkeyed digest tells you the action
    changed, but anyone who can write an intent can also recompute the digest
    to match. The key is what makes the two writes require two capabilities.

    The payload is the action's canonical JSON with sorted keys, so the digest
    depends on the *values* and not on field order or dict iteration.
    """
    payload = json.dumps(action.model_dump(mode="json"), sort_keys=True)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def transition(intent: Intent, event: str, ledger: Ledger, **detail: Any) -> Intent:
    """Move ``intent`` along one edge of the machine, or refuse.

    The ledger row is written *before* the state changes so a crash between
    the two leaves a record of an attempted hop rather than a silent one.
    """
    target = TRANSITIONS.get((intent.state, event))
    if target is None:
        ledger.append(
            intent.id, "transition_refused", from_state=intent.state, event=event
        )
        raise IllegalTransition(intent.id, intent.state, event)
    ledger.append(
        intent.id,
        "transition",
        from_state=intent.state,
        event=event,
        to=target,
        **detail,
    )
    intent.state = target
    return intent
