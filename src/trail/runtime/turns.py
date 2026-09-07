"""One turn, as a sequence of frames.

This is the single implementation of "run the agent and narrate what happened",
and both endpoints drain it: the streaming one renders each frame as it
arrives, the JSON one keeps the last. Two endpoints over one generator is what
stops the streamed answer and the buffered answer from being able to disagree.

The frame vocabulary is ``events.py``'s. What this module adds is the order:

1. ``stage`` frames from the graph's ``custom`` channel, as they happen — in
   pipeline order, including the skips for gates the dial left out, which the
   trace middleware emits from the hook where each would have run. Arrival
   order is the true order, so a client renders the rail without sorting it.
2. one ``turn`` frame with the finished answer.
3. one ``error`` frame, if something failed.
4. one ``trace`` frame, always last, even after an error — a failed turn is the
   one you most want the trace link for.

Two telemetry invariants, both of which fail silently when broken:
``current_trace_id()`` must be read *inside* the span, and ``flush_telemetry()``
must be awaited *after* it closes. An unended span is not exportable, and a
trace id read outside one is ``None``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from trail.config import Settings
from trail.runtime.events import StageEvent, ns_since, stage_from_chunk
from trail.telemetry import current_trace_id, flush_telemetry, span, trace_url

#: Frame names that cross the wire. ``turn`` carries the answer; ``error``
#: carries the exception object rather than a rendered payload, so the JSON
#: endpoint can re-raise it and answer 400/409/502 while the streaming endpoint
#: renders it — a response whose body has already begun cannot become a 500.
STAGE = "stage"
TURN = "turn"
ERROR = "error"
TRACE = "trace"

Frame = tuple[str, Any]


#: How much of the question becomes the trace's title. Long enough to tell two
#: turns apart in a list, short enough to stay on one row.
_TITLE_CHARS = 70


def _title(message: str) -> str:
    """A trace title, from the question that was asked.

    The alternative is what Langfuse falls back to — the HTTP route — which is
    identical for every turn this service has ever served and therefore
    identifies none of them.
    """
    single_line = " ".join(message.split())
    if len(single_line) <= _TITLE_CHARS:
        return single_line
    return single_line[:_TITLE_CHARS] + "…"


def _final_text(messages: list[Any]) -> str:
    """The last assistant message's text, or empty if there is none."""
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        content = message.content
        if isinstance(content, str):
            return content
        return " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def run_turn(
    agent: Any,
    *,
    thread_id: str,
    message: str,
    settings: Settings,
    customer_id: str | None = None,
) -> AsyncIterator[Frame]:
    """Drive one turn and yield its frames.

    ``customer_id`` is who the turn acts for, and it travels in ``configurable``
    alongside the thread — the one dict LangGraph hands to every tool. That is
    the point: the tools read identity from the same place they read the
    session, so nothing in the agent, the prompt or the model's output has a
    say in whose account is touched. ``app.py`` resolves it from the channel's
    signed header and is the only caller that should ever pass one; ``None``
    is for in-process callers (tests, direct drives) that have no channel, and
    it leaves the key out rather than inventing a customer here.

    ``stream_mode`` asks for three channels and uses two of them today.
    ``custom`` is the rail. ``values`` is how the finished message is recovered
    without a second read of the checkpointer. ``messages`` is requested even
    though nothing consumes it yet: it is the token channel, and asking for it
    now means adding token streaming later is a change to the client and not to
    this contract.
    """
    started = time.perf_counter_ns()
    configurable: dict[str, Any] = {"thread_id": thread_id}
    if customer_id is not None:
        configurable["customer_id"] = customer_id
    config = {"configurable": configurable}
    failure: BaseException | None = None
    trace_id: str | None = None
    final: list[Any] = []

    # Five of these attributes exist purely so a trace is readable, and they
    # are the difference between a wall of spans and something a person can
    # scan.
    #
    # `as_root` does not reparent anything — the ASGI request span is still the
    # tree's root, which is honest, because it is. What it does is elect *this*
    # span as the one whose trace-level attributes win. Without it the trace
    # takes its name from the HTTP route and every trace reads
    # `POST /threads/{thread_id}/turns/stream`, which is true of all of them
    # and therefore identifies none of them.
    #
    # `session_id` is the thread, which collects a whole conversation into one
    # view instead of leaving its turns as unrelated traces. `trace_name` and
    # `trace_input` put the question on the row itself, findable from a list.
    with span(
        "trail.turn",
        **{
            "trail.thread_id": thread_id,
            "trail.as_root": True,
            "trail.observation_type": "agent",
            "trail.session_id": thread_id,
            "trail.trace_name": _title(message),
            "trail.trace_input": message,
            "trail.input": message,
        },
    ) as active:
        trace_id = current_trace_id()
        try:
            async for mode, chunk in agent.astream(
                {"messages": [{"role": "user", "content": message}]},
                config,
                stream_mode=["custom", "values", "messages"],
            ):
                if mode == "custom":
                    event = stage_from_chunk(chunk)
                    if event is not None:
                        yield STAGE, event.model_dump(mode="json")
                elif mode == "values" and isinstance(chunk, dict):
                    final = chunk.get("messages") or final
        except Exception as exc:
            # Caught, not swallowed: it leaves as an `error` frame so the
            # streaming endpoint can report it in a body that has already begun.
            failure = exc
            active.set_attribute("trail.level", "ERROR")
            active.set_attribute("trail.status_message", str(exc)[:200])
        # Set inside the span: an attribute added after the context manager
        # exits lands on a span that has already been handed to the exporter.
        answer = _final_text(final)
        active.set_attribute("trail.output", answer)
        active.set_attribute("trail.trace_output", answer)

    # After the span closes, never inside it: an unended span cannot be
    # exported, so flushing early ships an incomplete trace or none at all.
    await flush_telemetry()

    if failure is None:
        yield (
            TURN,
            {"thread_id": thread_id, "text": answer, "ns": ns_since(started)},
        )
    else:
        yield ERROR, failure

    yield TRACE, {"trace_id": trace_id, "trace_url": trace_url(trace_id)}


def greeting_frames(thread_id: str, text: str) -> list[Frame]:
    """The opening line, as frames, with no model call behind it.

    A greeting is not an answer and charging a turn's tokens for one would put
    a cost on the scoreboard that bought nothing. It is emitted as a ``turn``
    frame with ``ns`` absent rather than zero, for the same reason an unpriced
    model costs ``None``: a measurement that was never taken is not a
    measurement of zero.
    """
    if not text:
        return []
    return [
        (
            STAGE,
            StageEvent(
                name="greeting", kind="io", label="abertura", status="done"
            ).model_dump(mode="json"),
        ),
        (TURN, {"thread_id": thread_id, "text": text, "ns": None}),
    ]
