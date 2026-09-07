"""Where intents and their events live. One interface, two implementations.

The control plane does not know whether its state is a dict or a database,
and that is the entire point of this module: V1 swaps ``MemoryStore`` for
``PgStore`` and ``plane.py`` does not change a line. What the interface has to
express is therefore not "storage" in general but exactly the five questions
the plane asks:

* :meth:`Store.get` — this id, if it exists;
* :meth:`Store.by_confirmation` — the intent this token names, if any;
* :meth:`Store.unsettled` — what did the last process leave mid-flight?
  (nobody calls it yet; the restart sweep is T7, and the query is here
  because designing the interface around it later would mean changing it);
* :meth:`Store.put` — this intent, as it now is;
* :meth:`Store.append` — one more event, forever.

**Ownership is not the store's job.** ``get`` and ``by_confirmation`` answer
about ids, not principals; the plane compares principals afterwards. Pushing
the check down here would mean a second place that decides who may see what,
and two such places eventually disagree — the one bug this repository exists
to make impossible.

**Persistence is explicit.** ``put`` after every mutation, even though a dict
would not need it: with ``MemoryStore`` the plane holds the same object the
store does, so a state change is already visible and ``put`` is a no-op in
effect. Writing it anyway is what keeps ``PgStore`` a drop-in rather than a
rewrite — an in-memory implementation that silently forgives a missing save
is an implementation that teaches the caller a habit the real one punishes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from control_plane.state import Event, Intent, Ledger


@runtime_checkable
class Store(Protocol):
    """The five questions the control plane asks about its own state."""

    def get(self, intent_id: str) -> Intent | None:
        """The intent with this id, or ``None``. Says nothing about ownership."""
        ...

    def by_confirmation(self, confirmation_id: str) -> Intent | None:
        """The intent this token names, or ``None``.

        At most one: a token naming two intents is a payment authorised twice,
        which is why the column is UNIQUE in ``db/schema.sql``.
        """
        ...

    def unsettled(self, states: Iterable[str]) -> list[Intent]:
        """Every intent currently in one of ``states``.

        The restart sweep's only question: what did the last process leave
        mid-flight? Not on any request path.
        """
        ...

    def put(self, intent: Intent) -> None:
        """Save this intent as it now is. Called after every mutation."""
        ...

    def append(self, intent_id: str, kind: str, **detail: Any) -> Event:
        """Record one event. Append-only: nothing here is updated or deleted."""
        ...

    def events_for(self, intent_id: str) -> list[Event]:
        """Every event for one id, in the order it happened."""
        ...

    def __len__(self) -> int:
        """How many events exist. Like :meth:`snapshot`, a reading affordance:
        ``PgStore`` answers it with ``count(*)`` and no request path asks."""
        ...

    def snapshot(self) -> Mapping[str, Intent]:
        """Every live intent, keyed by id.

        A reading affordance for tests and the CLI, never a request path —
        ``PgStore`` answers it with a query and no caller may assume it is
        cheap. It exists because "show me the state" is a real question during
        development, and the alternative was every caller reaching past the
        interface into whichever implementation it happened to have.
        """
        ...


class MemoryStore:
    """A dict and a ledger. The V0 storage, now behind the interface.

    Holds the same ``Intent`` objects the plane mutates, so ``put`` records a
    change that is already visible. That is not a reason to skip calling it —
    see the module docstring.
    """

    def __init__(self) -> None:
        self._intents: dict[str, Intent] = {}
        self.ledger = Ledger()

    def get(self, intent_id: str) -> Intent | None:
        return self._intents.get(intent_id)

    def by_confirmation(self, confirmation_id: str) -> Intent | None:
        for intent in self._intents.values():
            if intent.confirmation_id == confirmation_id:
                return intent
        return None

    def unsettled(self, states: Iterable[str]) -> list[Intent]:
        wanted = set(states)
        return [i for i in self._intents.values() if i.state in wanted]

    def put(self, intent: Intent) -> None:
        self._intents[intent.id] = intent

    def append(self, intent_id: str, kind: str, **detail: Any) -> Event:
        return self.ledger.append(intent_id, kind, **detail)

    def events_for(self, intent_id: str) -> list[Event]:
        return self.ledger.for_intent(intent_id)

    def __len__(self) -> int:
        return len(self.ledger)

    def snapshot(self) -> Mapping[str, Intent]:
        return self._intents
