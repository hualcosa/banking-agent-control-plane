"""The pipeline rail, and the thing that makes a model call a Langfuse generation.

This is the ``I`` of the acronym. It does two jobs that look separate and are
not:

1. **Emit a stage frame per hook**, so the client can render what ran, in what
   order, with a real measurement behind each cell. Nothing here knows what the
   agent is *for*; it reports ``model`` and ``tool:<name>``, and the labels
   travel on the wire.

2. **Stamp the OTel span attributes that Langfuse promotes into a generation.**
   This job is easy to lose, because losing it breaks nothing.

On (2), the trap, written down because it cost this repository a rewrite:
``telemetry.py`` is *passive*. Its exporter wrapper aliases ``trail.model`` onto
``gen_ai.request.model`` on the way out, and it has a hard guard —
``if "trail.model" not in attributes: return span_``. The runtime TRAIL was
extracted from produced those attributes inside its own LLM client. That client
is gone. If
this middleware does not produce them, every trace still arrives, still looks
fine, and carries no model, no tokens and no cost. The failure is silent by
construction, which is why ``awrap_model_call`` opens the span itself rather
than relying on any auto-instrumentation to have done it.

``wrap_model_call`` and not ``after_model``: only the wrapping hook is on both
sides of the call, so only it can time the call and attribute the span it
opened. ``after_model`` sees the result and not the request.
"""

from __future__ import annotations

import json
import time
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langgraph.runtime import Runtime

from trail import costs
from trail.runtime.events import StageEvent, emit, ns_since
from trail.runtime.middleware.guards import omitted_by
from trail.telemetry import span

#: How much of a payload reaches a span. Generous, because the point of
#: recording input and output is being able to read them, and stingy enough
#: that one runaway tool result does not become the trace.
_PAYLOAD_CHARS = 8_000


def as_payload(value: Any) -> str:
    """``value`` as a string a person can read in a trace viewer.

    JSON when it is structured, so the viewer can pretty-print and fold it;
    plain text when it already is text, because wrapping a paragraph in quotes
    and escapes makes it harder to read, not easier.
    """
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    if len(value) > _PAYLOAD_CHARS:
        return value[:_PAYLOAD_CHARS] + f"… (+{len(value) - _PAYLOAD_CHARS} chars)"
    return value


def _readable_messages(messages: Any) -> list[dict[str, Any]]:
    """The conversation as role/content pairs, dropping the framework's wrapping.

    A trace viewer showing repr'd LangChain objects is showing the library, not
    the conversation. Tool calls are kept because "the model asked for
    search_docs with this query" is usually the thing you opened the trace to
    find out.
    """
    readable: list[dict[str, Any]] = []
    for message in messages or []:
        entry: dict[str, Any] = {
            "role": getattr(message, "type", "?"),
            "content": getattr(message, "content", ""),
        }
        if tool_calls := getattr(message, "tool_calls", None):
            entry["tool_calls"] = [
                {"name": c.get("name"), "args": c.get("args")} for c in tool_calls
            ]
        readable.append(entry)
    return readable


def _tool_output(result: Any) -> Any:
    """What the tool returned, unwrapped from whatever the framework wrapped it in.

    A tool result arrives as a message, or a list of them, or the bare value —
    and a trace that showed the wrapper would be showing the framework rather
    than the answer the model was given.
    """
    if isinstance(result, list):
        return [_tool_output(item) for item in result]
    content = getattr(result, "content", None)
    return result if content is None else content


def _model_role(messages: Any) -> tuple[str, str]:
    """What this model call turned out to be for: ``(name, label)``.

    An agent loop calls the model more than once per turn and the calls do
    different jobs. The first decides which tools to reach for; the last writes
    the answer. Labelling both of them ``modelo`` puts two identical cells on
    the rail with different durations and no way to tell which is which —
    exactly the ambiguity the rail exists to remove.

    Derived from what came back rather than from a counter, so it stays right
    for an agent that takes four tool rounds, or none.
    """
    last = messages[-1] if messages else None
    calls = getattr(last, "tool_calls", None) if last is not None else None
    if calls:
        # Named when there is one, counted when there are several: "which tool
        # did it pick" is the question, and with three picks the answer is the
        # three cells that follow.
        if len(calls) == 1:
            return "model.tools", f"model→{calls[0].get('name', 'tool')}"
        return "model.tools", f"model→{len(calls)} tools"
    return "model.answer", "model→answer"


def _model_name(request: ModelRequest) -> str:
    """The model's own name, however the provider spells the attribute.

    ``model_name`` is the LangChain convention and ``model`` is what several
    integrations actually set. Falling back to the class name keeps the span
    attribute present — and therefore the Langfuse promotion working — even for
    a model object that reports neither.
    """
    model = request.model
    # `model_id` is where ChatBedrockConverse keeps it.
    for attribute in ("model_name", "model", "model_id", "name"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


class TraceMiddleware(AgentMiddleware):
    """Measures the model and the tools, and narrates both onto the stream."""

    name = "trace"

    def __init__(
        self,
        *,
        mode: str = "both",
        thread_id: str | None = None,
        prompt_version: str | None = None,
        prices: dict[str, costs.ModelPrice] | None = None,
    ):
        super().__init__()
        self.mode = mode
        self.thread_id = thread_id
        self.prompt_version = prompt_version
        self.prices = prices

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Open the rail, and report the input gate if the dial left it out.

        Emitted from here rather than from the turn runner so it lands in
        pipeline order. A skip yielded before the stream starts arrives before
        everything, which forces the client to re-sort — and any sort that can
        place a skip can also scramble the real interleaving of model and tool
        calls, which is the one ordering a reader is actually reading for.
        """
        for name, kind, label in omitted_by(self.mode):
            if kind == "guard_in":
                emit(StageEvent(name=name, kind=kind, label=label, status="skip"))
        return None

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Any,
    ) -> ModelResponse:
        model = _model_name(request)
        attributes: dict[str, Any] = {
            "trail.model": model,
            "trail.observation_type": "generation",
            # The prompt, as a conversation rather than as repr'd objects. This
            # is the single most useful thing in the trace and the easiest to
            # leave out, because nothing looks broken without it — the span has
            # a latency, a cost and a token count, and no way to tell what any
            # of them were spent on.
            "trail.input": as_payload(_readable_messages(request.messages)),
        }
        if self.thread_id:
            attributes["trail.thread_id"] = self.thread_id
        if self.prompt_version:
            attributes["trail.prompt_version"] = self.prompt_version

        # The `start` frame cannot know yet which job this call is doing — that
        # is decided by what comes back. It exists to drive a spinner, and the
        # `done` frame that replaces it carries the real name.
        emit(StageEvent(name="model", kind="model", label="model", status="start"))
        started = time.perf_counter_ns()

        # `trail.model` is set on entry, not on exit: an exception leaves the
        # span with the attribute the exporter's alias rule looks for, so a
        # failed call still arrives typed instead of anonymous.
        with span("trail.llm.model", **attributes) as active:
            try:
                response = await handler(request)
            except Exception as exc:
                # A model call that raised still ran, and how long it ran before
                # failing is the number someone debugging wants. Without this the
                # rail shows a `start` with no settled frame after it — a cell
                # that renders as nothing, which is the one outcome the whole
                # design exists to prevent. The exception propagates untouched.
                emit(
                    StageEvent(
                        name="model",
                        kind="model",
                        label="model",
                        status="blocked",
                        ns=ns_since(started),
                        detail={"error": type(exc).__name__, "message": str(exc)[:200]},
                    )
                )
                raise
            elapsed = ns_since(started)

            messages = getattr(response, "result", None) or []
            usage = (
                costs.usage_from_message(messages[-1], model, self.prices)
                if messages
                else costs.Usage()
            )

            if messages:
                active.set_attribute(
                    "trail.output", as_payload(_readable_messages(messages[-1:]))
                )
            active.set_attribute("trail.input_tokens", usage.input_tokens)
            active.set_attribute("trail.output_tokens", usage.output_tokens)
            active.set_attribute(
                "trail.cache_read_input_tokens", usage.cache_read_tokens
            )
            active.set_attribute(
                "trail.cache_creation_input_tokens", usage.cache_write_tokens
            )
            if usage.cost_usd is not None:
                active.set_attribute("trail.cost_usd", usage.cost_usd)

        name, label = _model_role(messages)
        emit(
            StageEvent(
                name=name,
                kind="model",
                label=label,
                status="done",
                ns=elapsed,
                detail=usage.as_detail(),
            )
        )
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Any,
    ) -> Any:
        """Measure one tool call, and record what it was asked and what it said.

        A tool call is its own observation kind in Langfuse, so it renders as a
        tool rather than as an unlabelled span between two model calls. The
        arguments are what tell a reader *why* the model reached for it, which
        is usually the question that opened the trace.
        """
        tool = request.tool_call.get("name", "tool")
        emit(StageEvent(name=f"tool:{tool}", kind="tool", label=tool, status="start"))
        started = time.perf_counter_ns()

        with span(
            f"trail.tool.{tool}",
            **{
                "trail.observation_type": "tool",
                "trail.input": as_payload(request.tool_call.get("args", {})),
            },
        ) as active:
            try:
                result = await handler(request)
            except Exception as exc:
                # A tool that raised still ran, and how long it ran before
                # failing is the number someone debugging actually wants.
                # Reporting it rather than swallowing it keeps the rail honest;
                # the exception continues to propagate untouched.
                active.set_attribute("trail.level", "ERROR")
                active.set_attribute("trail.status_message", str(exc)[:200])
                emit(
                    StageEvent(
                        name=f"tool:{tool}",
                        kind="tool",
                        label=tool,
                        status="blocked",
                        ns=ns_since(started),
                        detail={"error": type(exc).__name__},
                    )
                )
                raise
            active.set_attribute("trail.output", as_payload(_tool_output(result)))

        emit(
            StageEvent(
                name=f"tool:{tool}",
                kind="tool",
                label=tool,
                status="done",
                ns=ns_since(started),
            )
        )
        return result

    async def aafter_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Close the rail.

        Emitted after every other middleware's ``after_agent`` has run, because
        this middleware is first in the list and the hooks unwind in reverse —
        so a blocked output guard has already reported itself by the time this
        fires, and an output gate the dial left out is reported here, last,
        where it would have run.
        """
        for name, kind, label in omitted_by(self.mode):
            if kind == "guard_out":
                emit(StageEvent(name=name, kind=kind, label=label, status="skip"))
        emit(StageEvent(name="finish", kind="io", label="finish", status="done"))
        return None
