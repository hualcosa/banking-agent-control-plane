"""OpenTelemetry setup: SDK → OTLP/HTTP exporter → Langfuse.

The exporter endpoint is configuration, not code, so the instrumentation
survives the move from Langfuse to Tempo, X-Ray or Datadog untouched — which is
the whole reason OTel is here rather than a vendor SDK. The collector this
exports to today is a container in a compose file and the production target is
AWS; that move has to be a config change or the instrumentation
gets rewritten at exactly the moment it starts mattering.

**System observability is not AI evaluation**. Spans answer
"how long did it take, where did it stall, what errored, which span blew p95" —
the infrastructure layer of that section's metric table, and nothing above it.
Whether the promised amount was written down as the customer said it, whether
the restatement was genuinely confirmed, and how often a case's forbidden
phrases were spoken are the job of the semantic traces in Postgres and the
``evals`` service. Whether the agent said anything outside the approved script
is neither of those: it is settled before the words leave, by
:func:`~trail.agent.compliance.assert_agent_text_is_approved` in the agent
service, and a span recording that it happened is evidence of the gate rather
than the gate. Teams that conflate the two buy an APM, believe
evaluation is handled, and discover months later that they have beautiful
latency dashboards and no idea whether the balance being read out to customers
is the one the system of record holds. Do not put extraction correctness in a
span attribute and call it evaluated.

Call :func:`setup_telemetry` once per process, at module scope, and hand it the
app::

    app = FastAPI(lifespan=lifespan)
    setup_telemetry(get_settings().service_name, app)

Passing ``app`` is what produces HTTP server spans, and the two constraints
behind that are worth knowing because both fail silently. Without an app,
:class:`~opentelemetry.instrumentation.fastapi.FastAPIInstrumentor` can only
swap the ``fastapi.FastAPI`` class, which does nothing for an app already built
*or* for a module that did ``from fastapi import FastAPI`` before the swap — the
common import style. And the instrumentor works by patching
``build_middleware_stack``, which Starlette calls on its way into the lifespan
scope, so a call from inside a lifespan startup hook is already too late.
Everything else — the provider, the exporter, :func:`span` — works from
anywhere.

Without a reachable collector the process must still run: with the endpoint
blank, or if the exporter cannot be constructed, this module installs no tracer
provider at all and every span becomes a no-op. A local run without Langfuse is
a supported mode, not a crash.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Tracer

from trail.config import get_settings

__all__ = [
    "configure_logging",
    "current_trace_id",
    "flush_telemetry",
    "setup_telemetry",
    "span",
    "trace_url",
    "tracer",
]

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging() -> None:
    """Install a root log handler, so both services print the same way.

    Uvicorn's default ``dictConfig`` configures only the ``uvicorn.*`` loggers
    and adds no root handler, so without this a service's own ``logger.info``
    is discarded and its ``logger.critical`` reaches stderr through
    ``logging.lastResort`` — unformatted, untimestamped, and easy to lose. The
    lines that matter most here are the CRITICAL compliance violations, so it
    lives in one place both services call rather than in one of them.

    Idempotent by way of ``basicConfig``, which does nothing when the root
    logger already has a handler.
    """
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)


#: The package tracer. Obtained at import time, which is safe: with no provider
#: installed yet this is a proxy that resolves to whichever provider
#: :func:`setup_telemetry` installs, and stays a no-op if none ever is.
tracer: Tracer = trace.get_tracer("trail")

#: Prefix applied to attribute names that do not already carry a namespace, so
#: ``span("agent.turn", call_id=...)`` emits the documented ``trail.call_id``.
_ATTRIBUTE_NAMESPACE = "trail"

_initialised = False


#: Langfuse's promotion rule is 'any span carrying a model attribute is a
#: generation'. The LLM span already carries everything needed under this
#: repo's own ``trail.*`` namespace; this maps those keys onto the GenAI
#: semantic convention on the way out, so the instrumentation stays
#: vendor-neutral and unedited.
_GENAI_ALIASES = {
    "trail.model": "gen_ai.request.model",
    "trail.input_tokens": "gen_ai.usage.input_tokens",
    "trail.output_tokens": "gen_ai.usage.output_tokens",
    "trail.cost_usd": "gen_ai.usage.cost",
}

#: What turns a wall of spans into something a person can read, and the reason
#: it lives here rather than at the call sites: every name on the right is
#: Langfuse's, every name on the left is ours. Moving to another backend is a
#: new table in this file, not an edit to the middleware.
#:
#: * ``observation.type`` — one of Langfuse's own kinds, **lowercase**:
#:   ``span``, ``generation``, ``event``, ``embedding``, ``agent``, ``tool``,
#:   ``chain``, ``retriever``, ``guardrail``, ``evaluator``. The ingestion
#:   mapper looks the value up in a lowercase table and silently falls back
#:   to ``SPAN`` on a miss, so an uppercase value is not an error — it is a
#:   correctly ingested span with the wrong kind. ``guardrail`` being one of
#:   them is why this project's gates render as gates.
#: * ``observation.input`` / ``.output`` — what went in and what came out.
#:   Without them the trace shows timings and nothing to interpret, which is
#:   the difference between "it took 940ms" and "it took 940ms answering this".
#: * ``session.id`` — groups every turn of one thread into a conversation.
#: * ``internal.as_root`` — promotes a span to the trace root. Without it the
#:   root is the ASGI request and every trace is titled with the HTTP route.
#: * ``level`` / ``status_message`` — a blocked turn is a warning, not a
#:   success with an odd payload.
_LANGFUSE_ALIASES = {
    "trail.observation_type": "langfuse.observation.type",
    "trail.input": "langfuse.observation.input",
    "trail.output": "langfuse.observation.output",
    "trail.level": "langfuse.observation.level",
    "trail.status_message": "langfuse.observation.status_message",
    "trail.session_id": "langfuse.session.id",
    "trail.trace_name": "langfuse.trace.name",
    "trail.trace_input": "langfuse.trace.input",
    "trail.trace_output": "langfuse.trace.output",
    "trail.as_root": "langfuse.internal.as_root",
}


def _with_genai_aliases(span_: ReadableSpan) -> ReadableSpan:
    attributes = span_.attributes or {}
    aliased = {
        new: attributes[old]
        for old, new in _LANGFUSE_ALIASES.items()
        if old in attributes
    }
    # The GenAI block is gated on `trail.model` because those four keys only
    # mean anything together: a span with usage counts and no model is not a
    # generation, and promoting it to one would put a cost on something that
    # never called a model.
    if "trail.model" in attributes:
        aliased.update(
            {
                new: attributes[old]
                for old, new in _GENAI_ALIASES.items()
                if old in attributes
            }
        )
    if not aliased:
        return span_
    return ReadableSpan(
        name=span_.name,
        context=span_.get_span_context(),
        parent=span_.parent,
        resource=span_.resource,
        attributes={**attributes, **aliased},
        events=span_.events,
        links=span_.links,
        kind=span_.kind,
        status=span_.status,
        start_time=span_.start_time,
        end_time=span_.end_time,
        instrumentation_scope=span_.instrumentation_scope,
    )


class _GenAIExporter(SpanExporter):
    """Wraps the real exporter, aliasing ``trail.*`` attributes on the way out.

    See :data:`_GENAI_ALIASES` and :data:`_LANGFUSE_ALIASES`. Every
    vendor-specific attribute name in this repository is in those two tables
    and nowhere else, so the middleware that produces the spans names only
    things this project owns. Swapping backends is a new table here.
    """

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Any) -> SpanExportResult:
        return self._inner.export([_with_genai_aliases(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


def setup_telemetry(service_name: str, app: FastAPI | None = None) -> None:
    """Install the tracer provider and the OTLP exporter, and instrument HTTP.

    Idempotent: the second and later calls return immediately, so a process that
    imports both service modules does not stack exporters or trip OTel's
    "overriding of current TracerProvider is not allowed" error.

    Args:
        service_name: ``service.name`` on the resource — ``trail-agent`` or
            ``trail-evals``. This is what separates the two services in the
            Langfuse UI, so it must differ per container.
        app: the FastAPI application to instrument, if this process serves one.
            Omitting it falls back to patching the ``fastapi.FastAPI`` class,
            which only reaches apps constructed afterwards *and* imported as
            ``fastapi.FastAPI`` rather than ``from fastapi import FastAPI`` —
            see the module docstring. The CLI, which serves nothing, passes
            nothing.
    """
    global _initialised
    if _initialised:
        return

    endpoint = get_settings().otel_exporter_otlp_endpoint.strip()
    if not endpoint:
        logger.info(
            "TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT is empty; tracing disabled "
            "(spans become no-ops)"
        )
        _instrument_libraries(app)
        _initialised = True
        return

    try:
        exporter: SpanExporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=_parse_headers(get_settings().otel_exporter_otlp_headers),
        )
    except Exception:  # pragma: no cover - depends on a malformed endpoint
        # A bad endpoint must not take the service down with it. The exporter
        # itself retries in a background thread and never raises into a request,
        # so this only covers construction.
        logger.warning(
            "could not construct the OTLP exporter for %s; tracing disabled",
            endpoint,
            exc_info=True,
        )
        _instrument_libraries(app)
        _initialised = True
        return

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(_GenAIExporter(exporter)))
    trace.set_tracer_provider(provider)

    _instrument_libraries(app)
    _initialised = True
    logger.info("tracing enabled for %s, exporting to %s", service_name, endpoint)


def _instrument_libraries(app: FastAPI | None) -> None:
    """Instrument the FastAPI app (or the class) and every httpx client.

    Runs even when tracing is disabled: with no provider installed the
    instrumentation records nothing, and doing it unconditionally keeps the two
    paths behaving identically apart from whether spans are recorded.

    httpx instrumentation is what makes one client's calls to the agent appear
    as a single trace spanning both processes rather than two unrelated ones.

    Two exclusions, and between them they are the difference between a readable
    trace list and a wall.

    ``exclude_spans`` drops the per-chunk ASGI spans. ASGI emits one
    ``http send`` span per response chunk and the streaming endpoint sends one
    chunk per stage frame, so a turn that did six interesting things arrives as
    six useful spans buried under a dozen identical
    ``POST …/turns/stream http send`` rows carrying nothing a reader wants.

    ``excluded_urls`` drops two routes entirely.

    ``healthz`` because the compose healthcheck calls it every ten seconds for
    as long as the stack is up: leave it in and the overwhelming majority of
    every trace list is a liveness probe, and finding a conversation means
    filtering past hundreds of them.

    The turn route because otherwise **two spans claim to be the trace root** —
    the ASGI request, which has no parent, and ``trail.turn``, which asks to be
    one. Both create a trace record, and the one that wins decides whether the
    trace is called "quais serviços sobem?" or
    ``POST /threads/{thread_id}/turns/stream``. The second is true of every
    turn this service has ever served and therefore identifies none of them.
    What is lost is a span holding the route, the status code and a latency
    that ``trail.turn`` already measures more precisely.
    """
    excluded = "healthz,threads/[^/]+/turns"
    if app is not None:
        FastAPIInstrumentor.instrument_app(
            app, exclude_spans=["send", "receive"], excluded_urls=excluded
        )
    else:
        FastAPIInstrumentor().instrument(
            exclude_spans=["send", "receive"], excluded_urls=excluded
        )
    HTTPXClientInstrumentor().instrument()


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a span named ``name``, attach ``attributes``, and end it.

    Exceptions are recorded on the span, mark it as errored, and propagate —
    swallowing them here would hide the failure from both the caller and the
    trace.

    Attribute names are namespaced automatically: ``call_id=...`` is emitted as
    ``trail.call_id``, while a name that already contains a ``.`` is used as
    given. Both call styles therefore produce the keys in the INTERFACES table,
    which matters because comparing agent and evals traces means comparing
    attribute names.

    Values are coerced to something OTel accepts — a :class:`~uuid.UUID` passed
    raw would otherwise be dropped with a warning and the attribute would simply
    be missing from the trace. ``None`` values are skipped: an absent attribute
    and an attribute set to the string ``"None"`` are not the same thing.

    ::

        with span("agent.turn", call_id=call_id, step=step) as current:
            ...
    """
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is None:
                continue
            current.set_attribute(_attribute_name(key), _attribute_value(value))
        yield current


# --------------------------------------------------------------------------
# Handing one trace to a human
# --------------------------------------------------------------------------
#
# Three functions that exist for one purpose: let a response carry a link a
# person can click and land on the trace that produced it. Everything above is
# about spans reaching a collector; this is about a laptop reaching Langfuse.


def current_trace_id() -> str | None:
    """The active trace's id, as the 32 lowercase hex digits Langfuse's URL wants.

    **Call this from inside the** ``with span(...)`` **block.** Outside it there
    is no current span, the context is invalid, and this returns ``None`` — the
    same answer it gives when tracing is disabled, which makes the mistake
    indistinguishable from the supported configuration and therefore silent.

    ``None`` is returned for both of the ways there is nothing to link to: no
    provider was installed (:func:`setup_telemetry` found an empty endpoint, so
    every span is a no-op whose context carries ``INVALID_TRACE_ID``), and no
    span is current. A caller that renders a URL from this must handle ``None``
    rather than format a link to trace ``0000…0000``, which resolves to a
    Langfuse page that does not exist.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def trace_url(trace_id: str | None) -> str | None:
    """The Langfuse deep link for ``trace_id``, or ``None`` when there is no trace.

    Absolute, and it has to be: the browser reaches Langfuse directly rather than
    through the service that produced the trace, so a relative path would resolve
    against the agent's own origin and 404. The base comes from
    :attr:`~trail.config.Settings.langfuse_ui_base_url`, which is the
    browser-reachable address and not the container one — see that field.
    Langfuse also scopes every trace URL by project
    (:attr:`~trail.config.Settings.langfuse_project_id`), so the link needs
    both. The trace id is the 32-hex OTel trace id, which Langfuse adopts
    verbatim for OTLP-ingested traces.
    """
    if not trace_id:
        return None
    settings = get_settings()
    if settings.otel_mode == "adot":
        region = settings.aws_region
        if not region:
            return None
        return (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#gen-ai-observability/traces?traceId={trace_id}"
        )
    base = settings.langfuse_ui_base_url.rstrip("/")
    return f"{base}/project/{settings.langfuse_project_id}/traces/{trace_id}"


async def flush_telemetry(timeout_ms: int = 2000) -> None:
    """Force the batch processor to export now, so a clicked link resolves.

    The exporter batches, and the default schedule is seconds — long enough that
    a user clicking the link in the response that *just* arrived opens Langfuse on
    a trace that has not been written yet, sees "not found", and concludes the
    tracing is broken.

    Two constraints, and both fail silently rather than loudly:

    * **Call this after the span's** ``with`` **block has exited.** An unended
      span is not exportable, so a flush from inside the block ships a trace
      missing its own root — the child spans arrive and the span the link was
      built from does not.
    * **Run it off the event loop.** :meth:`TracerProvider.force_flush` blocks
      the calling thread until the queue drains or ``timeout_ms`` elapses, and
      blocking the loop inside a request handler stalls every other request on
      the worker.

    The ``hasattr`` guard is the disabled-tracing path: with no provider
    installed the global is OTel's proxy, which has no ``force_flush``, and
    there is nothing queued to flush anyway.
    """
    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush is None:
        return
    await asyncio.to_thread(flush, timeout_ms)


def _attribute_name(key: str) -> str:
    return key if "." in key else f"{_ATTRIBUTE_NAMESPACE}.{key}"


def _attribute_value(value: Any) -> Any:
    # bool/int/float/str pass straight through, and StrEnum members are str.
    # Everything else — UUID above all — becomes its string form.
    return value if isinstance(value, (bool, int, float, str)) else str(value)


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse the OTel standard ``k=v,k=v`` header string.

    Empty is the supported no-auth case (an OTel Collector or a Jaeger
    behind this endpoint wants no header at all). A malformed pair is
    skipped rather than raised: telemetry may never be the reason a
    service fails to start.
    """
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        key, sep, value = pair.partition("=")
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers
