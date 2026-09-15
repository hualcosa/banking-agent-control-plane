"""The wire vocabulary, and the only thing the browser and the CLI agree on.

A frame is ``(name, payload)``. Four names cross the boundary — ``stage``,
``turn``, ``error``, ``trace`` — and they are the Server-Sent Event names the
streaming endpoint writes, so this vocabulary is the wire contract rather than
an internal convenience.

The interesting one is ``stage``. Its payload is a :class:`StageEvent`, and
every field of it exists to keep the client ignorant of the agent's domain:

* ``name`` and ``kind`` say *what ran*, in terms any agent has — a guard, the
  model, a tool.
* ``label`` is the human string. It travels **on the wire** rather than living
  in a lookup table in the frontend, because a lookup table in the frontend is
  how the last version of this file ended up knowing the words *extrair*,
  *julgar* and *gate*. A rail that renders whatever arrives can render an agent
  nobody has written yet.
* ``status`` has four values, and ``blocked`` is the load-bearing one. Without
  it a guardrail that fired and a guardrail that passed look identical, and the
  one thing this scaffold claims to show is which of those happened.
* ``ns`` is nanoseconds rather than milliseconds, because the steps this
  scaffold exists to make visible are four orders of magnitude apart: a gate
  runs in microseconds and a model call in seconds. Milliseconds cannot hold
  both — see :func:`ns_since`.

``skip`` is equally deliberate. When a guardrail is switched off it still emits
its frame, marked skipped, and the rail renders it struck through rather than
hiding it — the same argument ``ui/DESIGN.md`` makes about a skipped stage:
a hidden cell lets an absence pass for a success.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: Sent with the event stream, and the third one is not optional in front of
#: nginx: with proxy buffering on, nginx holds the whole response until the
#: generator finishes and the client sees every stage arrive at once, at the
#: end — a stream that is indistinguishable from a slow request.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

StageKind = Literal["guard_in", "model", "tool", "guard_out", "io"]
StageStatus = Literal["start", "done", "skip", "blocked"]


class StageEvent(BaseModel):
    """One step of the pipeline, as the client reads it.

    ``detail`` is deliberately an open dict rather than a union of typed
    payloads: a guard puts its violations there, the model call puts tokens and
    cost, a tool puts its arguments. Closing that shape would mean every new
    kind of middleware needs a schema change in two languages before it can say
    anything about itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: StageKind
    label: str
    status: StageStatus
    #: Nanoseconds. See :func:`ns_since` for why this is not milliseconds.
    ns: int | None = None
    detail: dict[str, Any] | None = None


def ns_since(started: int) -> int:
    """Nanoseconds since a :func:`time.perf_counter_ns` reading.

    Nanoseconds, and integer, and both halves of that are a fix for the same
    bug. The previous version returned ``int(seconds * 1000)``, which truncates
    toward zero — so every guardrail in this system reported **0 ms**, because
    a regex over a short string takes single-digit *micro*seconds and a
    millisecond is a thousand of those.

    That zero was not wrong so much as unreadable: this repository's own rule is
    that a measurement never taken is not a measurement of zero, and ``0 ms``
    read exactly like "did not run" for the one kind of step whose cheapness is
    the argument.

    So the wire carries the finest integer unit available and no unit
    conversion happens until something has to be shown to a person. A client
    picks the scale — ``1.6 µs`` for a gate, ``1.47 s`` for a model call, from
    the same field.
    """
    return time.perf_counter_ns() - started


def sse(event: str, data: Any) -> str:
    """Render one Server-Sent Event.

    Compact JSON with no separator padding, and ``ensure_ascii`` off because an
    SSE stream is UTF-8 by specification and escaping "pendência" into
    ``\\u00ea`` would cost bytes to make the wire harder to read.
    ``json.dumps`` escapes any newline inside a string, so the payload is always
    the single ``data:`` line the format requires.
    """
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def iter_sse(lines: Any) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``(event, data)`` from an SSE line stream, as they arrive.

    The inverse of :func:`sse`, and it lives beside it for the reason this
    module exists: the wire contract is one vocabulary, not one per client.
    The CLI and the eval harness both read this stream, and a harness with its
    own parser would be a second reading of a format only one of them was
    tested against.

    Async and incremental on purpose: buffering the whole response before
    parsing it would make the rail appear all at once, at the end, which is a
    stream indistinguishable from a slow request — the exact failure the
    ``X-Accel-Buffering: no`` header above exists to prevent.

    Deliberately minimal otherwise: this endpoint sends only ``event:`` and
    ``data:``, one line of each per frame, and handling ids, retries and
    multi-line data would be handling cases the server never produces.
    """
    event: str | None = None
    async for line in lines:
        if not line:
            event = None
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            yield event, json.loads(line[5:].strip())


def duration(ns: int | None) -> str:
    """A nanosecond count, at the scale a person reads it.

    The inverse of :attr:`StageEvent.ns`, and the second half of the argument
    that field's docstring makes: the wire carries the finest integer unit
    available, and no unit conversion happens until something has to be shown
    to a person. This is that moment, and it is shared — the chat rail and the
    eval scorecard render the same number the same way.

    The steps on one rail span four orders of magnitude — a regex gate runs in
    microseconds, a model call in seconds — so a single unit cannot show both.
    Milliseconds was the unit before this, and it rendered every guardrail in
    the system as ``0 ms``: true, useless, and easily read as "did not run".

    Three significant figures at each scale, which is the most anyone acts on
    and the least that still distinguishes a 1.6 µs check from a 9.2 µs one.
    """
    if ns is None:
        return ""
    if ns < 1_000:
        return f"{ns} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.1f} ms"
    return f"{ns / 1_000_000_000:.2f} s"


def error_json(exc: BaseException) -> dict[str, Any]:
    """The ``error`` event's payload for whatever went wrong.

    An HTTP error keeps its status and its detail: a 502 on an upstream model
    failure and a 409 on a finished thread are answers the client can act on.
    Anything else is a bug, is logged here with its traceback — the JSON path
    gets that from the ASGI server, the streaming path would otherwise get it
    from nowhere — and is reported as a bare 500 rather than leaking an
    exception message into a browser.

    Imported lazily so this module stays importable without FastAPI, which is
    what lets the guards and the event vocabulary be unit-tested on their own.
    """
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        return {"status": exc.status_code, "detail": str(exc.detail)}
    logger.error("streamed turn failed", exc_info=exc)
    return {"status": 500, "detail": "internal error"}


def emit(event: StageEvent) -> None:
    """Push one stage frame onto the graph's custom stream.

    Called from inside middleware hooks and tools, which are graph nodes, so
    ``get_stream_writer`` resolves. Outside a graph execution — a unit test
    calling a guard directly — there is no writer and this is a no-op rather
    than an error: a guard's verdict is the thing under test, and refusing to
    run without a stream would make the guards testable only end to end.
    """
    from langgraph.config import get_stream_writer

    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    if writer is None:
        return
    writer({"trail_stage": event.model_dump(mode="json")})


def stage_from_chunk(chunk: Any) -> StageEvent | None:
    """Recover a :class:`StageEvent` from a ``custom`` stream chunk, or ``None``.

    The custom channel carries whatever any node wrote to it, including data
    from tools that know nothing about this module, so the envelope key is what
    separates our frames from someone else's.
    """
    if not isinstance(chunk, dict):
        return None
    payload = chunk.get("trail_stage")
    if payload is None:
        return None
    return StageEvent.model_validate(payload)
