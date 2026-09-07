"""``trail chat`` and ``trail eval``, driven end to end against a fake service.

No real agent runs here: every test substitutes ``httpx.AsyncClient`` with one
wired to an ``httpx.MockTransport`` that replays scripted JSON or SSE bodies —
the same shape ``iter_sse`` parses in production, so the streaming path under
test is the real parser, not a second reading of the wire contract. Where the
CLI builds its own client (``chat``, ``evaluate``, ``health``), the narrowest
seam is patched: the ``httpx`` name inside ``trail.cli``'s namespace, so
nothing outside this module's tests is affected.

``_render_rail``, ``_render_cost`` and ``_render_trace`` are exercised directly
against an ``io.StringIO``-backed ``Console`` because they take one as a
parameter; ``chat``/``evaluate``/``health``/``main`` build their own, so those
are read back through ``capsys`` instead — Rich resolves ``sys.stdout``
dynamically on every print, so capturing works regardless of construction
order.
"""

from __future__ import annotations

import io
import types
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from rich.console import Console

from control_plane import Context, CreatePix, Intent, MemoryStore, MockBank
from trail import cli
from trail.config import get_settings
from trail.evals import store as store_module
from trail.evals.cases import (
    Case,
    GoldenSet,
    Threshold,
    calls_tools,
    contains,
    not_contains,
)
from trail.evals.metrics import Metric, RunReport
from trail.identity import HEADER as IDENTITY_HEADER
from trail.identity import verify
from trail.runtime.events import sse

pytestmark = pytest.mark.unit

#: The CLI signs with this; the fake service below verifies with it, which is
#: what makes "the header the CLI sends is one a service would accept" a claim
#: these tests can actually check rather than assert about a literal.
SECRET = "unit-test-identity-secret"


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command that talks to the agent signs an identity first.

    Without a secret the CLI refuses to run at all — which is itself a test
    below — so the rest of the suite needs one, exactly as a real terminal
    gets one from ``.env``.
    """
    monkeypatch.setenv("TRAIL_IDENTITY_SECRET", SECRET)
    monkeypatch.setenv("TRAIL_CUSTOMER_ID", "cust_123")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fakes: an httpx transport, a scripted stdin, an in-memory console
# ---------------------------------------------------------------------------


def fake_httpx(handler) -> types.SimpleNamespace:
    """A stand-in for the ``httpx`` name inside ``trail.cli``.

    ``chat``/``evaluate``/``health`` build their own client with no injection
    point, so this patches the narrowest possible seam — the module attribute
    ``trail.cli.httpx`` — rather than the shared ``httpx`` module every other
    file imports.
    """
    return types.SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), **kw
        ),
        HTTPError=httpx.HTTPError,
    )


def plain_console() -> Console:
    """A console that renders to an in-memory buffer, colour-free."""
    return Console(file=io.StringIO(), width=120, force_terminal=False, theme=cli.THEME)


def scripted_input(monkeypatch: pytest.MonkeyPatch, *lines: str) -> None:
    """Feed ``lines`` to ``builtins.input`` (what ``Console.input`` calls), then EOF."""
    remaining = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def turn_body(
    *,
    label: str = "entrada",
    ns: int = 1_500_000_000,
    tokens: tuple[int, int] = (10, 5),
    cost: float | None = 0.002,
    text: str = "resposta",
    trace_url: str = "http://langfuse/trace/abc",
) -> str:
    """One turn's SSE body: a start frame, a done frame, the answer, the trace."""
    return "".join(
        [
            sse(
                "stage",
                {
                    "name": "guard_in",
                    "kind": "guard_in",
                    "label": label,
                    "status": "start",
                },
            ),
            sse(
                "stage",
                {
                    "name": "guard_in",
                    "kind": "guard_in",
                    "label": label,
                    "status": "done",
                    "ns": 1_600,
                },
            ),
            sse(
                "stage",
                {
                    "name": "model",
                    "kind": "model",
                    "label": "modelo",
                    "status": "done",
                    "ns": ns,
                    "detail": {
                        "input_tokens": tokens[0],
                        "output_tokens": tokens[1],
                        "cost_usd": cost,
                    },
                },
            ),
            sse("turn", {"thread_id": "t", "text": text, "ns": ns}),
            sse("trace", {"trace_id": "abc", "trace_url": trace_url}),
        ]
    )


def error_body(status: int = 502, detail: str = "upstream indisponível") -> str:
    return sse("error", {"status": status, "detail": detail})


def open_thread_handler(turn_response: str, *, healthy: bool = True):
    """A handler answering ``POST /threads``, ``/healthz`` and one turn."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/threads" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "thread_id": "t-1",
                    "agent": "fake",
                    "greeting": "oi, como posso ajudar?",
                    "guardrails": "both",
                },
            )
        if request.url.path == "/healthz":
            return httpx.Response(200 if healthy else 503, json={"ok": healthy})
        return httpx.Response(
            200, text=turn_response, headers={"content-type": "text/event-stream"}
        )

    return handle


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_chat_defaults_to_the_env_base_url() -> None:
    args = cli.build_parser().parse_args(["chat"])
    assert args.command == "chat"
    assert args.base_url == cli.DEFAULT_BASE_URL


def test_eval_concurrency_defaults_to_four_and_can_be_overridden() -> None:
    default = cli.build_parser().parse_args(["eval"])
    overridden = cli.build_parser().parse_args(["eval", "--concurrency", "9"])
    assert default.concurrency == 4
    assert overridden.concurrency == 9


def test_health_base_url_can_be_overridden() -> None:
    args = cli.build_parser().parse_args(["health", "--base-url", "http://elsewhere"])
    assert args.base_url == "http://elsewhere"


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_parser_rejects_an_unknown_command() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["bogus"])


# ---------------------------------------------------------------------------
# _render_rail — the rail is arrival order, and blocked must not read as done
# ---------------------------------------------------------------------------


def test_render_rail_marks_done_skip_and_blocked_with_distinct_glyphs() -> None:
    console = plain_console()
    stages = [
        {
            "name": "guard_in",
            "kind": "guard_in",
            "label": "entrada",
            "status": "done",
            "ns": 1_600,
        },
        {"name": "extra", "kind": "guard_in", "label": "extração", "status": "skip"},
        {
            "name": "guard_out",
            "kind": "guard_out",
            "label": "saída",
            "status": "blocked",
        },
    ]
    cli._render_rail(console, stages)
    out = console.file.getvalue()
    assert "▪entrada" in out
    assert "1.6 µs" in out
    assert "▫extração" in out and "pulado" in out
    assert "✗saída" in out and "BLOQUEADO" in out
    # Blocked and done must not share a glyph — the whole point of the marks.
    assert cli.MARK["blocked"] != cli.MARK["done"]


def test_render_rail_prints_each_violation_under_its_stage() -> None:
    console = plain_console()
    stages = [
        {
            "name": "guard_out",
            "kind": "guard_out",
            "label": "saída",
            "status": "blocked",
            "detail": {
                "violations": [
                    {"check": "pii", "detail": "vazou email", "evidence": "a@b.com"}
                ]
            },
        }
    ]
    cli._render_rail(console, stages)
    out = console.file.getvalue()
    assert "pii" in out
    assert "vazou email" in out
    assert "a@b.com" in out


# ---------------------------------------------------------------------------
# _render_cost
# ---------------------------------------------------------------------------


def test_render_cost_prints_tokens_price_and_total_wall_time() -> None:
    console = plain_console()
    stages = [
        {
            "kind": "model",
            "status": "done",
            "detail": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0015},
        }
    ]
    cli._render_cost(console, stages, total_ns=1_500_000_000)
    out = console.file.getvalue()
    assert "100 in" in out
    assert "20 out" in out
    assert "US$ 0.0015" in out
    assert "total 1.50 s" in out


def test_render_cost_shows_a_dash_for_an_unpriced_model_not_a_zero() -> None:
    """A confident $0.00 would be the most expensive kind of wrong."""
    console = plain_console()
    stages = [
        {
            "kind": "model",
            "status": "done",
            "detail": {"input_tokens": 5, "output_tokens": 1, "cost_usd": None},
        }
    ]
    cli._render_cost(console, stages, total_ns=None)
    out = console.file.getvalue()
    assert "—" in out
    assert "US$" not in out
    assert "total" not in out


def test_render_cost_is_silent_when_nothing_was_priced() -> None:
    console = plain_console()
    cli._render_cost(console, [{"kind": "tool", "status": "done"}], total_ns=100)
    assert console.file.getvalue() == ""


def test_render_cost_ignores_stages_that_are_not_a_completed_model_call() -> None:
    console = plain_console()
    stages = [
        {"kind": "model", "status": "start"},
        {"kind": "guard_in", "status": "done", "detail": {"input_tokens": 999}},
    ]
    cli._render_cost(console, stages, total_ns=None)
    assert console.file.getvalue() == ""


# ---------------------------------------------------------------------------
# _render_trace
# ---------------------------------------------------------------------------


def test_render_trace_prints_the_link_unbroken() -> None:
    console = plain_console()
    url = "http://langfuse/trace/deadbeefdeadbeefdeadbeefdeadbeef"
    cli._render_trace(console, url)
    out = console.file.getvalue()
    assert "trace:" in out
    assert url in out
    # Soft-wrapped: the id must survive as one contiguous line, not be split by
    # a hard newline the way a wrapped plain string would be.
    assert "\n" not in out.strip()


# ---------------------------------------------------------------------------
# _turn — the streaming path
# ---------------------------------------------------------------------------


async def test_turn_renders_answer_rail_cost_and_trace() -> None:
    body = turn_body(text="a resposta completa", tokens=(10, 5), cost=0.002)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/threads/t-1/turns/stream"
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    console = plain_console()
    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    ) as client:
        await cli._turn(client, console, "t-1", "oi")
    out = console.file.getvalue()
    assert "a resposta completa" in out
    assert "▪entrada" in out
    assert "10 in" in out and "5 out" in out
    assert "http://langfuse/trace/abc" in out


async def test_turn_renders_the_error_frame_as_a_failure_line() -> None:
    body = error_body(status=502, detail="upstream indisponível")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    console = plain_console()
    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    ) as client:
        await cli._turn(client, console, "t-1", "oi")
    out = console.file.getvalue()
    assert "falha 502" in out
    assert "upstream indisponível" in out


# ---------------------------------------------------------------------------
# chat()
# ---------------------------------------------------------------------------


async def test_chat_prints_greeting_and_thread_then_quits(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "httpx", fake_httpx(open_thread_handler(turn_body())))
    scripted_input(monkeypatch, "sair")
    code = await cli.chat("http://agent")
    out = capsys.readouterr().out
    assert code == 0
    assert "oi, como posso ajudar?" in out
    assert "fake" in out  # the agent name from /threads
    assert "t-1"[:8] in out  # thread id is shown truncated


async def test_chat_runs_a_turn_before_quitting(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "httpx",
        fake_httpx(open_thread_handler(turn_body(text="tudo certo por aqui"))),
    )
    scripted_input(monkeypatch, "como você está?", "quit")
    code = await cli.chat("http://agent")
    out = capsys.readouterr().out
    assert code == 0
    assert "tudo certo por aqui" in out


async def test_chat_ignores_blank_input_and_does_not_call_the_agent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/threads":
            return httpx.Response(
                201,
                json={
                    "thread_id": "t-2",
                    "agent": "fake",
                    "greeting": "oi",
                    "guardrails": "both",
                },
            )
        return httpx.Response(
            200, text=turn_body(), headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    scripted_input(monkeypatch, "   ", "exit")
    code = await cli.chat("http://agent")
    assert code == 0
    # Only the thread was opened; the blank line never reached a turn.
    assert calls == ["/threads"]


async def test_chat_returns_0_on_eof_with_no_message_typed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "httpx", fake_httpx(open_thread_handler(turn_body())))
    scripted_input(monkeypatch)  # exhausted immediately -> EOFError
    code = await cli.chat("http://agent")
    assert code == 0


async def test_chat_raises_cli_error_with_a_hint_when_the_agent_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(cli, "httpx", fake_httpx(refuse))
    with pytest.raises(cli.CliError) as excinfo:
        await cli.chat("http://agent")
    assert "http://agent" in str(excinfo.value)
    assert "make up" in excinfo.value.hint


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def golden(*cases: Case, **thresholds: Threshold) -> GoldenSet:
    return GoldenSet(version="fixture-v1", cases=cases, thresholds=thresholds)


def eval_transport(by_question: dict[str, str], *, healthy: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200 if healthy else 503, json={"ok": healthy})
        if request.url.path == "/threads" and request.method == "POST":
            return httpx.Response(201, json={"thread_id": "t-eval"})
        if request.method == "DELETE":
            return httpx.Response(204)
        import json as _json

        question = _json.loads(request.content)["message"]
        return httpx.Response(
            200,
            text=by_question.get(question, turn_body(text="")),
            headers={"content-type": "text/event-stream"},
        )

    return handle


async def test_evaluate_announces_each_outcome_with_a_pass_or_fail_mark(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = (
        Case("ok", ["pergunta boa"], [contains("tudo bem")]),
        Case("tool", ["usa a ferramenta"], [calls_tools("search_docs")]),
    )
    gs = golden(*cases)
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(
        cli,
        "httpx",
        fake_httpx(
            eval_transport(
                {
                    "pergunta boa": turn_body(text="tudo bem por aqui"),
                    "usa a ferramenta": turn_body(text="ok"),
                }
            )
        ),
    )
    code = await cli.evaluate("http://agent", concurrency=2)
    out = capsys.readouterr().out
    # WRONG_PATH alone (unlike FABRICATION) does not fail the run and no
    # threshold was registered, so the run still exits clean.
    assert code == 0
    assert "▪ ok" in out
    assert "✗ tool" in out
    assert "concorrência" in out and "2" in out


async def test_evaluate_returns_0_when_every_case_passes_and_no_bar_is_crossed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = (Case("ok", ["pergunta"], [contains("tudo bem")]),)
    gs = golden(*cases)
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(
        cli,
        "httpx",
        fake_httpx(eval_transport({"pergunta": turn_body(text="tudo bem por aqui")})),
    )
    code = await cli.evaluate("http://agent")
    assert code == 0


async def test_evaluate_returns_1_when_a_fabrication_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = (Case("b", ["pergunta"], [not_contains("TRAIL_RETRY_LIMIT")]),)
    gs = golden(*cases)
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(
        cli,
        "httpx",
        fake_httpx(
            eval_transport({"pergunta": turn_body(text="TRAIL_RETRY_LIMIT existe")})
        ),
    )
    code = await cli.evaluate("http://agent")
    assert code == 1


async def test_evaluate_returns_1_when_a_registered_threshold_is_crossed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A regression that stays inside its bar does not fail; crossing it does."""
    cases = (Case("a", ["pergunta"], [contains("tudo bem")]),)
    gs = golden(*cases, case_pass_rate=Threshold(1.0, ">="))
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(
        cli,
        "httpx",
        fake_httpx(eval_transport({"pergunta": turn_body(text="não sei")})),
    )
    code = await cli.evaluate("http://agent")
    # 0/1 case_pass_rate never clears a >=100% bar, and the status itself is
    # COMPLETED (an OMISSION alone is not a violation) — so the crossed bar is
    # the only thing that can be making this non-zero.
    assert code == 1


async def test_evaluate_raises_cli_error_when_the_example_has_no_golden_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> None:
        raise ValueError(f"no golden set for {name}")

    monkeypatch.setattr(cli, "load_golden", missing)
    with pytest.raises(cli.CliError) as excinfo:
        await cli.evaluate("http://agent")
    assert "golden.py" in excinfo.value.hint


async def test_evaluate_raises_cli_error_when_the_health_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gs = golden(Case("a", ["q"], [contains("x")]))
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(cli, "httpx", fake_httpx(eval_transport({}, healthy=False)))
    with pytest.raises(cli.CliError) as excinfo:
        await cli.evaluate("http://agent")
    assert "make up" in excinfo.value.hint


# ---------------------------------------------------------------------------
# _persist — storage never costs a run
# ---------------------------------------------------------------------------


def report_with_pass_rate(value: float) -> RunReport:
    return RunReport(
        golden_set_version="fixture-v1",
        status="COMPLETED",
        metrics=[
            Metric(
                "case_pass_rate",
                value,
                "rate",
                threshold=Threshold(0.5, ">="),
            )
        ],
        findings=[],
    )


async def test_persist_saves_the_run_and_attaches_the_baseline(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_connect(url: str) -> FakeConnection:
        return FakeConnection()

    async def fake_latest_baseline(connection, version):
        return store_module.Baseline(
            id=7,
            golden_set_version=version,
            metrics={"case_pass_rate": {"value": 0.95}},
        )

    saved_kwargs: dict = {}

    async def fake_save_run(connection, report, **kwargs):
        saved_kwargs.update(kwargs)
        return 42

    monkeypatch.setattr(store_module, "connect", fake_connect)
    monkeypatch.setattr(store_module, "latest_baseline", fake_latest_baseline)
    monkeypatch.setattr(store_module, "save_run", fake_save_run)

    console = plain_console()
    report = report_with_pass_rate(0.6)
    result = await cli._persist(console, report, settings, datetime.now(UTC))

    assert result.run_id == 42
    assert result.baseline_id == 7
    # 0.6 regressed from 0.95 and the bar (>=0.5) still clears -> reported, but
    # crossed_threshold is what evaluate() uses to fail the run, not this alone.
    assert [r.metric for r in result.regressions] == ["case_pass_rate"]
    assert saved_kwargs["agent"] == settings.agent
    assert console.file.getvalue() == ""  # no "não registrado" line on success


async def test_persist_has_nothing_to_compare_when_there_is_no_baseline(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_connect(url: str) -> FakeConnection:
        return FakeConnection()

    async def fake_latest_baseline(connection, version):
        return None

    async def fake_save_run(connection, report, **kwargs):
        return 5

    monkeypatch.setattr(store_module, "connect", fake_connect)
    monkeypatch.setattr(store_module, "latest_baseline", fake_latest_baseline)
    monkeypatch.setattr(store_module, "save_run", fake_save_run)

    console = plain_console()
    report = report_with_pass_rate(0.6)
    result = await cli._persist(console, report, settings, datetime.now(UTC))

    assert result.run_id == 5
    assert result.baseline_id is None
    assert result.regressions == []


async def test_persist_tolerates_a_database_that_is_down(
    monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    """Losing an afternoon's numbers because Postgres was down is the worst failure."""

    async def fake_connect(url: str):
        raise ConnectionError("could not connect")

    monkeypatch.setattr(store_module, "connect", fake_connect)

    console = plain_console()
    report = report_with_pass_rate(0.6)
    result = await cli._persist(console, report, settings, datetime.now(UTC))

    assert result is report  # unchanged: no run_id, no baseline
    out = console.file.getvalue()
    assert "não registrado" in out
    assert "ConnectionError" in out


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


async def test_health_up_prints_ok_and_returns_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    code = await cli.health("http://agent")
    out = capsys.readouterr().out
    assert code == 0
    assert "ok" in out
    assert "http://agent" in out


async def test_health_down_raises_a_cli_error_naming_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False})

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    with pytest.raises(cli.CliError) as excinfo:
        await cli.health("http://agent")
    assert "http://agent" in str(excinfo.value)
    # health() passes no hint — a bare CliError must still be handled.
    assert excinfo.value.hint == ""


# ---------------------------------------------------------------------------
# main() — dispatch, exit codes, and error rendering
# ---------------------------------------------------------------------------


def test_main_health_up_returns_0(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    code = cli.main(["health", "--base-url", "http://agent"])
    assert code == 0
    assert "ok" in capsys.readouterr().out


def test_main_prints_error_and_hint_to_stderr_and_returns_1(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(cli, "httpx", fake_httpx(refuse))
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError)
    )
    code = cli.main(["chat", "--base-url", "http://agent"])
    captured = capsys.readouterr()
    assert code == 1
    assert "erro" in captured.err
    assert "make up" in captured.err


def test_main_renders_a_bare_cli_error_without_a_hint_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    code = cli.main(["health", "--base-url", "http://agent"])
    captured = capsys.readouterr()
    assert code == 1
    # Exactly one line starts with "erro" — health()'s CliError carries no
    # hint, so the `if exc.hint:` branch must not print a second message.
    # (The error text itself may still wrap across terminal-width lines.)
    starts = [line for line in captured.err.splitlines() if line.startswith("erro")]
    assert len(starts) == 1


def test_main_passes_the_concurrency_flag_through_to_evaluate(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gs = golden(Case("a", ["q"], [contains("x")]))
    monkeypatch.setattr(cli, "load_golden", lambda name: gs)
    monkeypatch.setattr(
        cli, "httpx", fake_httpx(eval_transport({"q": turn_body(text="x aqui")}))
    )
    code = cli.main(["eval", "--base-url", "http://agent", "--concurrency", "3"])
    out = capsys.readouterr().out
    assert code == 0
    assert "concorrência" in out and "3" in out


# ---------------------------------------------------------------------------
# identity — the CLI is standing in for the channel, so the CLI signs
# ---------------------------------------------------------------------------


async def test_chat_signs_an_identity_header_a_service_would_accept(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request the CLI makes carries a header that verifies to cust_123.

    Verified rather than compared to a literal: the assertion is that the
    service's own ``verify`` resolves the CLI's header to the configured
    customer, which is the property ``make chat`` depends on.
    """
    seen: list[str | None] = []
    inner = open_thread_handler(turn_body())

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(IDENTITY_HEADER))
        return inner(request)

    monkeypatch.setattr(cli, "httpx", fake_httpx(handle))
    scripted_input(monkeypatch, "oi", "sair")
    assert await cli.chat("http://agent") == 0
    capsys.readouterr()

    assert len(seen) == 2  # POST /threads, then the turn
    assert all(verify(value, SECRET) == "cust_123" for value in seen)


async def test_chat_refuses_to_run_with_no_identity_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that sends an unverifiable header presents as a server bug.

    So it does not send one: it stops with a message naming the variable, and
    never opens a connection — the fake transport would raise if it did.
    """
    monkeypatch.setenv("TRAIL_IDENTITY_SECRET", "")
    get_settings.cache_clear()

    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the CLI must not reach the agent unsigned")

    monkeypatch.setattr(cli, "httpx", fake_httpx(explode))
    with pytest.raises(cli.CliError) as raised:
        await cli.chat("http://agent")
    assert "TRAIL_IDENTITY_SECRET" in str(raised.value)


# ---------------------------------------------------------------------------
# Out of band: step-up, intents, reconcile
#
# These three never speak HTTP, so there is no transport to fake — the seam is
# the storage. ``operator_cli`` swaps ``PgStore`` for a ``MemoryStore``, which
# satisfies the same protocol, so what runs below is the real ``ControlPlane``
# over real state: an assertion here is an assertion about the plane's answer
# and not about a mock's. It also replaces ``httpx`` with one that raises,
# which is how "out of band" stops being a claim in a docstring: a command
# that reached the agent would fail the test that says it does not.
# ---------------------------------------------------------------------------


def no_http() -> types.SimpleNamespace:
    """An ``httpx`` stand-in that refuses to build a client at all."""

    def explode(**kw: object) -> httpx.AsyncClient:  # pragma: no cover
        raise AssertionError("an out-of-band command must not reach the agent")

    return types.SimpleNamespace(AsyncClient=explode, HTTPError=httpx.HTTPError)


def operator_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bank: MockBank | None = None,
) -> MemoryStore:
    """Point the out-of-band commands at in-memory state and forbid HTTP."""
    store = MemoryStore()
    the_bank = MockBank() if bank is None else bank
    monkeypatch.setattr(cli, "PgStore", lambda dsn: store)
    monkeypatch.setattr(cli, "MockBank", lambda: the_bank)
    monkeypatch.setattr(cli, "httpx", no_http())
    # Rich wraps table cells to the console width, and a wrapped id is a
    # substring assertion that fails for a cosmetic reason.
    monkeypatch.setenv("COLUMNS", "200")
    return store


def an_intent(
    store: MemoryStore,
    *,
    intent_id: str,
    state: str = "UNKNOWN",
    customer: str = "cust_123",
    session: str = "thread_da_conversa",
    amount: str = "800.00",
    recipient: str = "contact_joao",
) -> Intent:
    """One intent in the store, in whatever state the test needs it in."""
    intent = Intent(
        id=intent_id,
        action=CreatePix(
            source_account="checking_001",
            recipient_id=recipient,
            amount=Decimal(amount),
        ),
        context=Context(customer_id=customer, session_id=session),
        state=state,  # type: ignore[arg-type]  # a literal from State
    )
    store.put(intent)
    return intent


def test_the_out_of_band_commands_take_no_base_url() -> None:
    """They do not know the agent exists, so there is nothing to point at."""
    parser = build = cli.build_parser()
    assert not hasattr(build.parse_args(["intents"]), "base_url")
    assert parser.parse_args(["step-up", "pix_1"]).intent_id == "pix_1"
    assert parser.parse_args(["reconcile", "pix_1"]).intent_id == "pix_1"


def test_redact_hides_the_password_and_keeps_everything_useful() -> None:
    assert (
        cli.redact("postgresql://trail:s3cr3t@db.internal:5432/trail")
        == "postgresql://trail:***@db.internal:5432/trail"
    )
    # No host to redact around, so nothing is invented.
    assert cli.redact("sqlite:///local.db") == "sqlite:///local.db"


def test_intents_lists_what_needs_a_person_and_names_the_command_for_each(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = operator_cli(monkeypatch)
    an_intent(store, intent_id="pix_parado", state="UNKNOWN")
    an_intent(store, intent_id="pix_esperando", state="AWAITING_STEP_UP")
    an_intent(store, intent_id="pix_pronto", state="COMPLETED")

    assert cli.main(["intents"]) == 0
    out = capsys.readouterr().out

    assert "trail reconcile pix_parado" in out
    assert "trail step-up pix_esperando" in out
    # A settled intent is not waiting on anybody and must not be in the way.
    assert "pix_pronto" not in out
    # UNKNOWN leads the list: it is the one whose truth lives at the bank.
    assert out.index("pix_parado") < out.index("pix_esperando")
    assert "1 em UNKNOWN" in out


def test_intents_shows_only_the_configured_customer(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database is shared; the list is not. Ownership is not the store's job."""
    store = operator_cli(monkeypatch)
    an_intent(store, intent_id="pix_meu", customer="cust_123")
    an_intent(store, intent_id="pix_alheio", customer="cust_999")

    assert cli.main(["intents"]) == 0
    out = capsys.readouterr().out
    assert "pix_meu" in out
    assert "pix_alheio" not in out


def test_intents_says_plainly_when_there_is_nothing_to_do(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_cli(monkeypatch)
    assert cli.main(["intents"]) == 0
    assert "nada aguardando" in capsys.readouterr().out


def test_step_up_for_another_customer_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loosening ownership to the customer must not loosen it past the customer.

    This is the guard on the change T10 asks for: the session may differ, the
    customer may not. It passes today and must still pass afterwards.
    """
    store = operator_cli(monkeypatch)
    an_intent(
        store, intent_id="pix_alheio", state="AWAITING_STEP_UP", customer="cust_999"
    )

    assert cli.main(["step-up", "pix_alheio"]) == 1
    assert "referência desconhecida" in capsys.readouterr().out
    assert store.get("pix_alheio").state == "AWAITING_STEP_UP"


def test_step_up_from_another_process_reaches_the_confirmation(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of T10, asserted end to end through the CLI.

    The CLI's session is new on every invocation, so this only reaches the
    confirmation because ``ControlPlane.step_up`` resolves the intent through
    ``_owned_by_customer`` — customer plus intent id — rather than through
    ``_same_principal``, which compares the session and would refuse every
    real out-of-band callback.

    R$ 800,00 to a recipient never paid before scores ``high`` — so policy asks
    for strong assurance below the amount threshold, and the amount stays under
    the nighttime cap, which keeps this test independent of the hour it runs
    at (the CLI builds its own plane and injects no clock).

    The last assertion is the half that must *not* loosen: step-up grants
    assurance, never consent. The confirmation still belongs to the session
    that proposed the payment.
    """
    store = operator_cli(monkeypatch)
    an_intent(store, intent_id="pix_forte", state="AWAITING_STEP_UP")

    code = cli.main(["step-up", "pix_forte"])
    out = capsys.readouterr().out

    assert code == 0
    assert "REQUIRE_CONFIRMATION" in out
    settled = store.get("pix_forte")
    assert settled.state == "AWAITING_CONFIRMATION"
    assert settled.context.assurance == "strong"
    assert settled.context.session_id == "thread_da_conversa"


def test_reconcile_names_how_many_payments_the_bank_it_asked_knows(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """V0's bank is per process, so the CLI's bank starts empty — and says so.

    A ``FAILED`` printed under "0 pagamento(s) conhecido(s)" is not evidence
    that the money did not move; it is evidence that the book being asked has
    never seen a payment. The runbook turns that distinction into a rule, and
    this test is what keeps the line on screen.
    """
    store = operator_cli(monkeypatch)
    an_intent(store, intent_id="pix_parado", state="UNKNOWN")

    cli.main(["reconcile", "pix_parado"])
    out = capsys.readouterr().out
    assert "MockBank" in out and "0 pagamento(s) conhecido(s)" in out
    assert "nenhum pagamento novo" in out


def test_reconcile_completes_an_intent_the_bank_had_already_paid(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 3am path: UNKNOWN in, the bank's own answer out, no second debit.

    Like step-up, this runs as a principal with a session the intent has never
    seen — an operator is not in the customer's chat thread — so it depends on
    ``reconcile`` resolving ownership through ``_owned_by_customer`` too.
    """
    bank = MockBank()
    receipt = {
        "payment_id": "pay_ja_feito",
        "end_to_end_id": "E123",
        "status": "COMPLETED",
        "amount": "800.00",
        "recipient_id": "contact_joao",
    }
    bank.payments["pix_parado"] = receipt  # the idempotency key is the intent id
    balance_before = bank.accounts["checking_001"]
    store = operator_cli(monkeypatch, bank=bank)
    an_intent(store, intent_id="pix_parado", state="UNKNOWN")

    code = cli.main(["reconcile", "pix_parado"])
    out = capsys.readouterr().out

    assert code == 0
    assert "COMPLETED" in out and "pay_ja_feito" in out
    assert store.get("pix_parado").state == "COMPLETED"
    # Reconciliation asks; it never pays.
    assert bank.payments == {"pix_parado": receipt}
    assert bank.accounts["checking_001"] == balance_before


def test_a_database_it_cannot_reach_is_a_message_and_never_the_password(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(dsn: str) -> object:
        raise OSError("connection refused")

    monkeypatch.setenv("TRAIL_DATABASE_URL", "postgresql://trail:s3cr3t@db:5432/trail")
    get_settings.cache_clear()
    monkeypatch.setattr(cli, "PgStore", refuse)

    assert cli.main(["intents"]) == 1
    err = capsys.readouterr().err
    assert "db:5432" in err and "***" in err
    assert "s3cr3t" not in err
    assert "make up" in err


def test_a_missing_api_key_names_the_variable_instead_of_raising(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """These commands never call a model, so the failure has to explain itself."""
    monkeypatch.delenv("TRAIL_LLM_API_KEY", raising=False)
    get_settings.cache_clear()

    assert cli.main(["intents"]) == 1
    assert "TRAIL_LLM_API_KEY" in capsys.readouterr().err
