"""The observability metadata a response carries, and the link built from it.

Three small pure things, and all three fail silently when they are wrong, which
is the only reason they are worth a test file. A trace id read from outside its
span is ``None`` and the link disappears; a link built against the *collector's*
address is a URL a browser cannot resolve; a flush that raises because no
provider is installed would take a request down for the sake of telemetry.

Nothing here starts a span or installs a tracer provider. These tests run in the
configuration the offline tier always runs in — tracing disabled, no collector,
no exporter — which is also the configuration the null answers describe.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind
from opentelemetry.trace.status import Status

from trail.config import get_settings
from trail.telemetry import (
    _parse_headers,
    _with_genai_aliases,
    current_trace_id,
    flush_telemetry,
    trace_url,
)

pytestmark = pytest.mark.unit


def test_there_is_no_trace_id_outside_a_span() -> None:
    """The mistake this returns ``None`` for is calling it after the block exits.

    OTel answers a request for the current span with a non-recording sentinel
    whose context is invalid rather than with an error, so the wrong call site
    produces a plausible object and a trace id of zero. ``None`` is the honest
    reading of that, and it is the same answer tracing-disabled gives — which is
    exactly why the call site matters and is commented where it is made.
    """
    assert current_trace_id() is None


def test_a_null_trace_id_produces_no_link() -> None:
    """A link to trace ``0000…`` is a 404 in Langfuse dressed as a feature."""
    assert trace_url(None) is None
    assert trace_url("") is None


def test_the_link_points_at_the_browser_reachable_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base is configuration, and it is *not* the OTLP endpoint.

    ``TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT`` names a compose service and is
    resolved inside the compose network; this link is clicked from a laptop. The
    two settings look interchangeable and the failure mode of confusing them is a
    dead link rather than an error, so the test pins that they are separate
    values and that this one is the one the URL is built from.
    """
    monkeypatch.setenv("TRAIL_LANGFUSE_UI_BASE_URL", "http://langfuse.example:3000")
    monkeypatch.setenv("TRAIL_LANGFUSE_PROJECT_ID", "trail")
    monkeypatch.setenv(
        "TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://langfuse-web:3000/api/public/otel/v1/traces",
    )
    get_settings.cache_clear()

    trace_id = "4bf92f3577b34da6" + "a3ce929d0e0e4736"
    assert trace_url(trace_id) == (
        f"http://langfuse.example:3000/project/trail/traces/{trace_id}"
    )

    get_settings.cache_clear()


def test_a_trailing_slash_on_the_base_does_not_double_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``http://host:3000//project/…`` is not a URL Langfuse routes."""
    monkeypatch.setenv("TRAIL_LANGFUSE_UI_BASE_URL", "http://localhost:3000/")
    monkeypatch.setenv("TRAIL_LANGFUSE_PROJECT_ID", "trail")
    get_settings.cache_clear()

    assert trace_url("abc") == "http://localhost:3000/project/trail/traces/abc"

    get_settings.cache_clear()


def test_adot_mode_links_to_agentcore_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On AgentCore the trace lives in CloudWatch, not a local Langfuse."""
    monkeypatch.setenv("TRAIL_OTEL_MODE", "adot")
    monkeypatch.setenv("AWS_REGION", "sa-east-1")
    get_settings.cache_clear()

    assert trace_url("abc") == (
        "https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1"
        "#gen-ai-observability/traces?traceId=abc"
    )

    get_settings.cache_clear()


def test_adot_mode_without_a_region_produces_no_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console URL for region ``""`` is a dead link, so there is none."""
    monkeypatch.setenv("TRAIL_OTEL_MODE", "adot")
    monkeypatch.delenv("AWS_REGION", raising=False)
    get_settings.cache_clear()

    assert trace_url("abc") is None

    get_settings.cache_clear()


def test_parse_headers_keeps_base64_padding_intact() -> None:
    """A ``split("=")`` regression would truncate the Basic-auth value.

    The value ends in ``==`` — base64 padding. Parsing with ``split("=")``
    instead of ``partition("=")`` would drop everything after the first ``=``
    and turn the password into an empty string, which is a silent 401 at
    ingestion time rather than a raised error. This is the regression guard.
    """
    headers = _parse_headers("Authorization=Basic ab==,x-langfuse-ingestion-version=4")
    assert headers["Authorization"] == "Basic ab=="
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_parse_headers_is_empty_for_the_no_auth_case() -> None:
    """Empty is supported: an OTel Collector or a Jaeger behind this wants none."""
    assert _parse_headers("") == {}


def _span(attributes: dict[str, object]) -> ReadableSpan:
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


def test_genai_aliases_are_added_alongside_the_originals() -> None:
    """Langfuse promotes a span to a typed generation on ``gen_ai.request.model``.

    The ``trail.*`` attributes stay, so nothing that reads them today breaks;
    the aliases are additive.
    """
    span_ = _span({"trail.model": "gpt-5.6-luna", "trail.input_tokens": 10})
    aliased = _with_genai_aliases(span_)

    assert aliased.attributes["gen_ai.request.model"] == "gpt-5.6-luna"
    assert aliased.attributes["gen_ai.usage.input_tokens"] == 10
    assert aliased.attributes["trail.model"] == "gpt-5.6-luna"


def test_a_span_without_a_model_attribute_is_returned_unchanged() -> None:
    """Identity for every non-LLM span — this must not touch the agent's turn spans."""
    span_ = _span({"trail.call_id": "abc"})
    assert _with_genai_aliases(span_) is span_


async def test_flushing_without_a_provider_is_a_no_op() -> None:
    """Telemetry may never be the reason a turn fails.

    With tracing disabled the global provider is OTel's proxy, which has no
    ``force_flush`` at all — so the guard is a ``hasattr`` check and not a
    ``try``. Awaiting this in that configuration must simply return.
    """
    await flush_telemetry(timeout_ms=1)


def test_bedrock_converse_model_id_names_the_model() -> None:
    """``ChatBedrockConverse`` sets only ``model_id``; the class name prices nothing."""
    from types import SimpleNamespace

    from trail.runtime.middleware.trace import _model_name

    model = SimpleNamespace(
        model_name=None, model=None, model_id="openai.gpt-oss-120b-1:0"
    )
    assert _model_name(SimpleNamespace(model=model)) == "openai.gpt-oss-120b-1:0"
