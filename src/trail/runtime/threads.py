"""The conversation index: which threads exist, and what to call them.

LangGraph's checkpointer knows a thread's *contents* but offers no way to
enumerate threads, so a sidebar needs an index. This is it, and it is
deliberately built on the ``store`` — the cross-thread slot that has been open
since the runtime was written and unused until now — rather than on a table of
our own or a query against ``checkpoints``.

Not a table of our own, because the argument that emptied ``db/schema.sql``
still holds: a second store means a second thing to migrate, back up and
disagree with itself.

Not a query against ``checkpoints``, which does hold every ``thread_id``,
because that schema is LangGraph's. The library creates and migrates it, which
is exactly why this repository declares none of it — and a query written
against someone else's versioned schema breaks on their next release, silently,
in production.

The index is also where a thread's **owner** lives. A conversation belongs to
the customer whose turn created it, every endpoint answers only about that
customer's threads, and a thread belonging to someone else is indistinguishable
from one that never existed — the rule ``ControlPlane.explain`` already follows
for a ledger trail. It is recorded in the record this module writes rather than
beside LangGraph's ``checkpoints`` for the reason above: that row is the
library's, this one is ours.

The index follows the same dial as everything else: with
``TRAIL_CHECKPOINTER=memory`` it lives in memory and dies with the process,
alongside the conversations it indexes. That is not a defect to hide — it is
the same trade, and :func:`list_threads` returns the flag that lets a client say
so instead of rendering an empty list that looks like a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

#: Where thread records live in the store. A tuple, because store namespaces
#: are hierarchical and a future index (per agent) nests under this rather than
#: colliding with it.
NAMESPACE = ("threads",)

#: The field naming the customer a thread belongs to, inside the record.
#:
#: Ownership is recorded *here*, in TRAIL's own index row, and not in a
#: namespace of its own (``("threads", customer_id)``) and not in a table beside
#: LangGraph's ``checkpoints``. Both alternatives were considered and both are
#: worse:
#:
#: * A per-customer **namespace** scopes the listing for free, but it makes the
#:   one question every other endpoint asks — *who owns this thread id?* — a
#:   scan across namespaces instead of a single ``aget``. Ownership has to be
#:   answerable from the id alone, because that is all a request carries.
#: * A **table** would mean declaring conversation state in ``db/schema.sql``,
#:   which is the duplication that file's header refuses; the checkpointer owns
#:   the row keyed by ``thread_id`` and this index is the part TRAIL owns.
#:
#: So it is one more key in a record this module already writes, reads and
#: deletes as a unit — no second store, no second lifetime, nothing to migrate.
OWNER = "customer_id"

#: How much of the first question becomes the title. Long enough to tell two
#: conversations apart in a narrow sidebar, short enough not to wrap twice.
TITLE_CHARS = 60

#: How many records one listing reads before filtering. See :func:`list_threads`
#: for why this is a scan rather than a page.
SCAN_LIMIT = 1_000


def title_from(message: str) -> str:
    """A thread title, from the first thing the person asked.

    No model call. Generating a title costs a turn's tokens to produce a string
    the question already is, and a generated title can be wrong about a
    conversation the user remembers perfectly well by its opening line.
    """
    single_line = " ".join(message.split())
    if len(single_line) <= TITLE_CHARS:
        return single_line
    return single_line[:TITLE_CHARS].rstrip() + "…"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ThreadSummary:
    """One row of the sidebar."""

    thread_id: str
    title: str
    turns: int
    created_at: str
    updated_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "turns": self.turns,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


async def open_thread(
    store: Any, thread_id: str, customer_id: str | None = None
) -> None:
    """Record a thread that has been created but not yet spoken to.

    Written on creation so the record carries a real ``created_at``, and so an
    abandoned thread exists in the store to be counted. It does **not** reach
    the sidebar — see :func:`list_threads`.

    ``customer_id`` is who it belongs to. ``None`` is the in-process caller
    with no channel behind it (tests, direct drives), the same convention
    :func:`~trail.runtime.turns.run_turn` uses: it records no owner rather than
    inventing one.
    """
    if store is None:
        return
    now = _now()
    await store.aput(
        NAMESPACE,
        thread_id,
        {
            "title": "",
            "turns": 0,
            "created_at": now,
            "updated_at": now,
            OWNER: customer_id,
        },
    )


async def record_turn(
    store: Any, thread_id: str, message: str, customer_id: str | None = None
) -> None:
    """Bump a thread's turn count, and title it from its first message.

    The title is set once and never rewritten: a conversation is remembered by
    how it opened, and a title that changes under the reader is a title they
    cannot use to find anything.

    Ownership is set once too, and by the same rule: a thread belongs to the
    customer whose turn created it. A turn from anyone else on a thread that
    already has an owner writes **nothing** — the endpoints refuse that request
    before it gets here, and a store function that would happily relabel
    someone else's conversation is a second place for that rule to be wrong.
    """
    if store is None:
        return
    existing = await store.aget(NAMESPACE, thread_id)
    value: dict[str, Any] = dict(existing.value) if existing else {}
    owner = value.get(OWNER)
    if owner is not None and customer_id is not None and owner != customer_id:
        return
    turns = int(value.get("turns", 0)) + 1
    await store.aput(
        NAMESPACE,
        thread_id,
        {
            "title": value.get("title") or title_from(message),
            "turns": turns,
            "created_at": value.get("created_at") or _now(),
            "updated_at": _now(),
            OWNER: owner if owner is not None else customer_id,
        },
    )


async def list_threads(
    store: Any, *, customer_id: str | None = None, limit: int = 50, offset: int = 0
) -> list[ThreadSummary]:
    """One customer's conversations, most recently used first. Threads with no
    turns are omitted.

    ``customer_id`` is the scope, and it is not a convenience: a list is a
    disclosure. Another customer's threads must not appear, and must not be
    countable either — the filter runs before the page is cut, so an offset
    into someone else's conversations is an offset into nothing rather than a
    gap that reveals how many there were.

    ``None`` is the in-process caller with no channel, which sees the whole
    index. That path has no identity to scope by and no HTTP surface to reach
    it through; ``app.py`` always passes a resolved customer.

    Sorted here rather than by the store, because ``asearch`` orders by
    relevance for a semantic query and by nothing in particular without one.
    Recency is the order a sidebar means.

    **The zero-turn filter is a correction, and the reasoning is worth keeping.**
    Indexing on creation was chosen so an abandoned thread would be visible as
    one — a real diagnostic, if abandonment were rare. It is not: the browser
    opens a thread on every page load and on every "new conversation" click, so
    within an afternoon a third of the list was threads nobody had spoken to.
    A sidebar full of empty rows is not a diagnostic, it is a sidebar nobody can
    use.

    The records still exist and still carry their timestamps, so "how many
    threads were opened and never used" remains answerable. It is a metric, and
    a metric does not belong in a navigation list.
    """
    if store is None:
        return []
    # Read a bounded window and filter it here, rather than asking the store for
    # `limit` rows and hoping enough survive. The store cannot filter on a value
    # it does not index, so a page of records can be a page of nothing — twenty
    # abandoned threads ahead of one real conversation would return an empty
    # first page, which reads as "you have no conversations".
    #
    # Over-fetching by a multiple only moves that cliff; it does not remove it.
    # So: one bounded read, filtered, then paged.
    #
    # This means the index is not truly paged, and for a local scaffold with a
    # sidebar that is the right trade — `SCAN_LIMIT` rows is more conversations
    # than one demo produces, and the alternative is a loop that reads until it
    # has enough, which is real paging complexity bought for a case nobody has.
    # When someone does: index `turns` in the store and filter there.
    #
    # The scope goes to the store as a `filter` so the bounded window is spent
    # on this customer's records rather than on everyone's — with one window
    # for the whole index, a busy neighbour could push a quiet customer's
    # threads past `SCAN_LIMIT` and out of their own sidebar. It is applied
    # again below because this module cannot prove what every store backend
    # does with a filter it does not index, and a scoping rule that a backend
    # can silently decline to enforce is not a scoping rule.
    items = await store.asearch(
        NAMESPACE,
        limit=SCAN_LIMIT,
        **({} if customer_id is None else {"filter": {OWNER: customer_id}}),
    )
    summaries = [
        ThreadSummary(
            thread_id=item.key,
            title=str(item.value.get("title") or "").strip(),
            turns=int(item.value.get("turns", 0)),
            created_at=str(item.value.get("created_at") or ""),
            updated_at=str(item.value.get("updated_at") or ""),
        )
        for item in items
        if int(item.value.get("turns", 0)) > 0
        and (customer_id is None or item.value.get(OWNER) == customer_id)
    ]
    summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
    return summaries[offset : offset + limit]


async def owner_of(store: Any, thread_id: str) -> str | None:
    """The customer a thread belongs to, or ``None`` if the index names none.

    ``None`` covers two cases on purpose — the index has no record of this id
    at all, and it has one that predates ownership or was written by an
    in-process caller. Neither names a customer, so neither can be used to
    refuse one: this answers *who owns it*, and :func:`visible_to` is what
    answers *may this caller see it*.
    """
    if store is None:
        return None
    record = await store.aget(NAMESPACE, thread_id)
    if record is None:
        return None
    owner = record.value.get(OWNER)
    return owner if isinstance(owner, str) else None


async def visible_to(store: Any, thread_id: str, customer_id: str | None) -> bool:
    """Whether ``customer_id`` may read this thread.

    A thread that belongs to someone else and a thread that never existed are
    both ``False``, which is the whole point: the caller turns both into the
    same 404, so the endpoint cannot be used to test whether a thread id is
    real. This is the rule ``ControlPlane.explain`` follows for a ledger trail,
    said once more for a transcript.

    Two cases answer ``True`` without an owner check, and both are the absence
    of a question rather than a permissive answer to one: no store (a runtime
    built with no persistence has no index to ask) and no ``customer_id`` (an
    in-process caller with no channel, which is not reachable over HTTP —
    ``app.py`` answers 401 long before it gets here).
    """
    if store is None or customer_id is None:
        return True
    record = await store.aget(NAMESPACE, thread_id)
    return record is not None and record.value.get(OWNER) == customer_id


async def forget(store: Any, thread_id: str) -> None:
    """Drop a thread from the index.

    Unscoped on purpose: ownership is checked by the caller, which has to
    distinguish "not yours" from "not there" *before* deciding what to answer,
    and a delete that silently did nothing would leave that decision made in
    two places. The endpoint calls :func:`visible_to` first.

    The conversation itself stays in the checkpointer. This removes it from the
    list, which is what "delete" means to someone tidying a sidebar — and it
    keeps this module from reaching into storage it does not own to do
    something irreversible.
    """
    if store is None:
        return
    await store.adelete(NAMESPACE, thread_id)
