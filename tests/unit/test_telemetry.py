"""Everything ``test_trace_links.py`` does not already cover in ``telemetry.py``.

That file exercises the pure, always-safe-to-call functions in the tracing-
disabled configuration the unit tier runs in by default: ``current_trace_id``
outside a span, ``trace_url``, ``_parse_headers``, ``_with_genai_aliases``,
and a no-provider ``flush_telemetry``. This file covers what is left:
``configure_logging``, the ``_GenAIExporter`` wrapper's delegation,
``setup_telemetry`` wiring an exporter when an endpoint *is* configured (and
its idempotency guard), ``_instrument_libraries``, the ``span`` context
manager's namespacing/None-skipping/exception-recording, ``current_trace_id``
*inside* a span, and the ``flush_telemetry`` branch where a provider with
``force_flush`` is installed.

Global OTel state is sticky (``trace.set_tracer_provider`` works once per
process), so nothing here touches the real global tracer provider or the
real global instrumentors: ``trail.telemetry.tracer``,
``trail.telemetry.trace.set_tracer_provider``,
``trail.telemetry.trace.get_tracer_provider``, ``BatchSpanProcessor``,
``FastAPIInstrumentor`` and ``HTTPXClientInstrumentor`` are monkeypatched
per test, which ``monkeypatch`` restores automatically — so is the module's
own ``_initialised`` latch — keeping tests independent of each other and of
every other test module in the suite.
"""

from __future__ import annotations

import logging

import pytest
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanContext, SpanKind
from opentelemetry.trace.status import Status, StatusCode

from trail import telemetry
from trail.config import get_settings

pytestmark = pytest.mark.unit


def _span(attributes: dict[str, object]) -> ReadableSpan:
    """A minimal finished span, for feeding straight into ``_GenAIExporter``."""
    return ReadableSpan(
        name="chat gpt-5.6-luna",
        context=SpanContext(trace_id=1, span_id=1, is_remote=False),
        parent=None,
        resource=Resource.create({}),
        attributes=attributes,
        events=(),
        links=(),
        kind=SpanKind.INTERNAL,
        status=Status(),
        start_time=0,
        end_time=1,
        instrumentation_scope=None,
    )


def _local_tracer() -> tuple[object, InMemorySpanExporter]:
    """A tracer backed by its own private ``TracerProvider``.

    Deliberately not the process-wide provider :func:`trace.get_tracer_provider`
    would return — ``span()`` is exercised by swapping ``telemetry.tracer``
    itself, so this never touches (or fights over) the sticky global.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


# --------------------------------------------------------------------------
# configure_logging
# --------------------------------------------------------------------------


def test_configure_logging_installs_a_formatted_info_level_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uvicorn's dictConfig adds no root handler; this is the one that does."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])

    telemetry.configure_logging()

    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert root.handlers[0].formatter is not None
    assert root.handlers[0].formatter._fmt == telemetry._LOG_FORMAT


def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call must not stack a second handler on the root logger."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])

    telemetry.configure_logging()
    telemetry.configure_logging()

    assert len(root.handlers) == 1


# --------------------------------------------------------------------------
# _GenAIExporter
# --------------------------------------------------------------------------


class _RecordingExporter:
    """A stand-in for the real OTLP exporter that records what it was asked."""

    def __init__(self) -> None:
        self.exported: list[list[ReadableSpan]] = []
        self.shutdown_called = False
        self.force_flush_calls: list[int] = []

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        self.exported.append(list(spans))
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.force_flush_calls.append(timeout_millis)
        return True


def test_genai_exporter_export_aliases_spans_before_delegating() -> None:
    """The wrapper's whole job: alias, then hand off — never export unaliased."""
    inner = _RecordingExporter()
    exporter = telemetry._GenAIExporter(inner)
    span_ = _span({"trail.model": "gpt-5.6-luna", "trail.input_tokens": 3})

    result = exporter.export([span_])

    assert result == SpanExportResult.SUCCESS
    assert len(inner.exported) == 1
    (delegated,) = inner.exported[0]
    assert delegated.attributes["gen_ai.request.model"] == "gpt-5.6-luna"
    assert delegated.attributes["trail.model"] == "gpt-5.6-luna"


def test_genai_exporter_shutdown_delegates() -> None:
    inner = _RecordingExporter()
    telemetry._GenAIExporter(inner).shutdown()
    assert inner.shutdown_called is True


def test_genai_exporter_force_flush_delegates_timeout_and_return_value() -> None:
    inner = _RecordingExporter()
    result = telemetry._GenAIExporter(inner).force_flush(1234)
    assert inner.force_flush_calls == [1234]
    assert result is True


# --------------------------------------------------------------------------
# setup_telemetry
# --------------------------------------------------------------------------


def test_setup_telemetry_is_a_no_op_once_initialised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second call in a process must not touch anything, not just the provider.

    A process that imports both service modules calls this twice; re-running
    the wiring would either double-instrument or trip OTel's "overriding of
    current TracerProvider is not allowed" error.
    """
    monkeypatch.setattr(telemetry, "_initialised", True)

    def _boom(app: object) -> None:
        raise AssertionError("must not run past the _initialised guard")

    monkeypatch.setattr(telemetry, "_instrument_libraries", _boom)

    telemetry.setup_telemetry("trail-agent")  # would raise if the guard failed


def test_setup_telemetry_with_no_endpoint_skips_the_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local run without a collector must not install a tracer provider."""
    monkeypatch.setattr(telemetry, "_initialised", False)
    monkeypatch.setenv("TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT", "")
    get_settings.cache_clear()

    def _boom(provider: object) -> None:
        raise AssertionError("no endpoint means no provider is installed")

    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", _boom)
    instrumented = []
    monkeypatch.setattr(
        telemetry, "_instrument_libraries", lambda app: instrumented.append(app)
    )

    telemetry.setup_telemetry("trail-agent")

    assert telemetry._initialised is True
    assert instrumented == [None]
    get_settings.cache_clear()


@pytest.mark.parametrize("app", [None, object()])
def test_setup_telemetry_with_an_endpoint_wires_exporter_processor_and_resource(
    monkeypatch: pytest.MonkeyPatch, app: object
) -> None:
    """The success path: headers parsed, span aliased on export, name on the resource.

    ``trace.set_tracer_provider`` and ``BatchSpanProcessor`` are swapped for
    recorders rather than exercised for real, because the global tracer
    provider can only be set once per process and this suite runs many tests.
    """
    monkeypatch.setattr(telemetry, "_initialised", False)
    monkeypatch.setenv("TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel.invalid:4318")
    monkeypatch.setenv("TRAIL_OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc==")
    get_settings.cache_clear()

    captured_provider: dict[str, object] = {}
    monkeypatch.setattr(
        telemetry.trace,
        "set_tracer_provider",
        lambda provider: captured_provider.__setitem__("provider", provider),
    )

    processors: list[object] = []

    class _RecordingProcessor:
        def __init__(self, exporter: object) -> None:
            self.exporter = exporter
            processors.append(self)

        def shutdown(self) -> None:  # pragma: no cover - atexit only
            pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:  # pragma: no cover
            return True

    monkeypatch.setattr(telemetry, "BatchSpanProcessor", _RecordingProcessor)
    instrumented = []
    monkeypatch.setattr(
        telemetry, "_instrument_libraries", lambda a: instrumented.append(a)
    )

    telemetry.setup_telemetry("trail-agent-test", app)

    assert telemetry._initialised is True
    assert instrumented == [app]

    provider = captured_provider["provider"]
    assert provider.resource.attributes[SERVICE_NAME] == "trail-agent-test"

    assert len(processors) == 1
    exporter = processors[0].exporter
    assert isinstance(exporter, telemetry._GenAIExporter)
    assert exporter._inner._endpoint == "http://otel.invalid:4318"
    assert exporter._inner._headers == {"Authorization": "Basic abc=="}

    get_settings.cache_clear()


# --------------------------------------------------------------------------
# _instrument_libraries
# --------------------------------------------------------------------------


def test_instrument_libraries_instruments_the_passed_app_and_excludes_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an app: instrument it directly, drop chunk spans and the two noisy routes."""
    calls: dict[str, object] = {}

    class _FakeFastAPIInstrumentor:
        @staticmethod
        def instrument_app(app: object, exclude_spans=None, excluded_urls=None) -> None:
            calls["instrument_app"] = (app, exclude_spans, excluded_urls)

    monkeypatch.setattr(telemetry, "FastAPIInstrumentor", _FakeFastAPIInstrumentor)

    httpx_calls = []
    monkeypatch.setattr(
        telemetry,
        "HTTPXClientInstrumentor",
        lambda: type("H", (), {"instrument": lambda self: httpx_calls.append(True)})(),
    )

    sentinel_app = object()
    telemetry._instrument_libraries(sentinel_app)

    app, exclude_spans, excluded_urls = calls["instrument_app"]
    assert app is sentinel_app
    assert exclude_spans == ["send", "receive"]
    assert "healthz" in excluded_urls
    assert "threads/[^/]+/turns" in excluded_urls
    assert httpx_calls == [True]


def test_instrument_libraries_patches_the_class_when_no_app_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an app: fall back to ``FastAPIInstrumentor().instrument(...)``."""
    calls: dict[str, object] = {}

    class _FakeFastAPIInstrumentor:
        def instrument(self, **kwargs: object) -> None:
            calls["instrument_kwargs"] = kwargs

    monkeypatch.setattr(telemetry, "FastAPIInstrumentor", _FakeFastAPIInstrumentor)
    monkeypatch.setattr(
        telemetry,
        "HTTPXClientInstrumentor",
        lambda: type("H", (), {"instrument": lambda self: None})(),
    )

    telemetry._instrument_libraries(None)

    assert calls["instrument_kwargs"]["exclude_spans"] == ["send", "receive"]
    assert "healthz" in calls["instrument_kwargs"]["excluded_urls"]


# --------------------------------------------------------------------------
# span()
# --------------------------------------------------------------------------


def test_span_namespaces_bare_names_and_skips_none_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``call_id=`` becomes ``trail.call_id``; a dotted name passes through; None is dropped."""
    local_tracer, exporter = _local_tracer()
    monkeypatch.setattr(telemetry, "tracer", local_tracer)

    with telemetry.span(
        "agent.turn", call_id="abc", step=None, **{"already.dotted": 1}
    ):
        pass

    (finished,) = exporter.get_finished_spans()
    assert finished.attributes["trail.call_id"] == "abc"
    assert finished.attributes["already.dotted"] == 1
    assert "trail.step" not in finished.attributes
    assert "step" not in finished.attributes


def test_span_records_exceptions_and_still_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed turn must show up as an errored span, not a silently swallowed one."""
    local_tracer, exporter = _local_tracer()
    monkeypatch.setattr(telemetry, "tracer", local_tracer)

    with pytest.raises(ValueError, match="boom"), telemetry.span("agent.turn"):
        raise ValueError("boom")

    (finished,) = exporter.get_finished_spans()
    assert finished.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in finished.events)


# --------------------------------------------------------------------------
# current_trace_id (inside a span)
# --------------------------------------------------------------------------


def test_current_trace_id_reads_the_active_spans_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the ``with span(...)`` block, this is the trace id Langfuse expects."""
    local_tracer, _exporter = _local_tracer()
    monkeypatch.setattr(telemetry, "tracer", local_tracer)

    with telemetry.span("agent.turn") as current:
        trace_id = telemetry.current_trace_id()

    expected = format(current.get_span_context().trace_id, "032x")
    assert trace_id == expected
    assert len(trace_id) == 32


# --------------------------------------------------------------------------
# flush_telemetry (with a provider installed)
# --------------------------------------------------------------------------


async def test_flush_telemetry_calls_force_flush_on_an_installed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch skipped by default: a provider *is* there, so it must be flushed off-thread."""
    calls: list[int] = []

    class _FakeProvider:
        def force_flush(self, timeout_millis: int) -> bool:
            calls.append(timeout_millis)
            return True

    monkeypatch.setattr(telemetry.trace, "get_tracer_provider", lambda: _FakeProvider())

    await telemetry.flush_telemetry(timeout_ms=250)

    assert calls == [250]
