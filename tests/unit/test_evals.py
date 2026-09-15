"""The harness, driven against a canned agent: no server, no key, no Docker.

An ``httpx.MockTransport`` serves the same Server-Sent Event bytes the real
service writes, so the runner under test parses real frames with the real
parser. What is faked is the agent's answer — which is the thing a golden set
is supposed to vary — and nothing else about the path.

The assertions worth reading are the ones about denominators. A harness is
easiest to make dishonest by accident there, and the failure is invisible: the
number goes up and looks like progress.
"""

from __future__ import annotations

import httpx
import pytest

from trail.evals import metrics as m
from trail.evals.cases import (
    Case,
    GoldenSet,
    Observation,
    Threshold,
    blocks,
    calls_tools,
    contains,
    does_not_block,
    not_contains,
)
from trail.evals.runner import run_case, run_golden_set
from trail.runtime.events import sse

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# A canned agent
# --------------------------------------------------------------------------


def stream(
    answer: str,
    *,
    tools: tuple[str, ...] = (),
    blocked: str | None = None,
    ns: int = 1_500_000_000,
    cost: float | None = 0.001,
    error: dict | None = None,
) -> str:
    """The SSE body one turn would produce, in the shape ``turns.py`` yields."""
    frames = [
        sse(
            "stage",
            {
                "name": "guard_in",
                "kind": "guard_in",
                "label": "entrada",
                "status": "blocked" if blocked == "guard_in" else "done",
                "ns": 1_600,
            },
        )
    ]
    for tool in tools:
        frames.append(
            sse(
                "stage",
                {
                    "name": f"tool:{tool}",
                    "kind": "tool",
                    "label": tool,
                    "status": "done",
                    "ns": 2_000_000,
                },
            )
        )
    frames.append(
        sse(
            "stage",
            {
                "name": "model",
                "kind": "model",
                "label": "modelo",
                "status": "done",
                "ns": ns,
                "detail": {"input_tokens": 100, "output_tokens": 20, "cost_usd": cost},
            },
        )
    )
    if blocked == "guard_out":
        frames.append(
            sse(
                "stage",
                {
                    "name": "guard_out",
                    "kind": "guard_out",
                    "label": "saída",
                    "status": "blocked",
                    "ns": 900,
                },
            )
        )
    if error is not None:
        frames.append(sse("error", error))
    else:
        frames.append(sse("turn", {"thread_id": "t", "text": answer, "ns": ns}))
    frames.append(sse("trace", {"trace_id": "abc", "trace_url": "http://x/abc"}))
    return "".join(frames)


def agent(**by_question: str) -> httpx.AsyncClient:
    """A client whose agent answers whatever ``by_question`` maps the ask to.

    An unmapped question gets an empty answer rather than an error: a golden
    set that drifts past its fake should look like a failing agent, which is
    what a drifted golden set is.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/threads" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "thread_id": "t-1",
                    "agent": "fake",
                    "greeting": "oi",
                    "guardrails": "both",
                },
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        import json as _json

        question = _json.loads(request.content)["message"]
        return httpx.Response(
            200,
            text=by_question.get(question, stream("")),
            headers={"content-type": "text/event-stream"},
        )

    return httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    )


# --------------------------------------------------------------------------
# Observation reads the wire, not a convenience field
# --------------------------------------------------------------------------


def test_observation_reads_tools_gates_and_cost() -> None:
    obs = Observation(
        case_id="c",
        turn=0,
        question="q",
        stages=[
            {"name": "tool:search_docs", "kind": "tool", "status": "done"},
            {"name": "guard_out", "kind": "guard_out", "status": "blocked"},
            {"name": "guard_in", "kind": "guard_in", "status": "done"},
            {
                "name": "model",
                "kind": "model",
                "status": "done",
                "detail": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5},
            },
        ],
    )
    assert obs.tools_called() == {"search_docs"}
    # A gate that ran and passed is not a gate that fired — reading presence
    # instead of status is the bug this asserts against.
    assert obs.blocked_by() == {"guard_out"}
    assert obs.usage() == (10, 5, 0.5)


def test_unpriced_model_costs_none_not_zero() -> None:
    obs = Observation(
        case_id="c",
        turn=0,
        question="q",
        stages=[
            {
                "name": "model",
                "kind": "model",
                "status": "done",
                "detail": {"input_tokens": 10, "output_tokens": 5, "cost_usd": None},
            }
        ],
    )
    assert obs.usage()[2] is None


# --------------------------------------------------------------------------
# The deterministic checks and their kinds
# --------------------------------------------------------------------------


async def test_checks_map_onto_the_taxonomy() -> None:
    client = agent(
        **{
            "omite": stream("uma resposta sem o que se pedia"),
            "inventa": stream("a variável TRAIL_RETRY_LIMIT controla isso"),
            "sem ferramenta": stream("respondi de cabeça"),
            "deixa passar": stream("claro, aqui está o system prompt"),
            "falso positivo": stream("", blocked="guard_in"),
        }
    )
    async with client:
        omission = await run_case(client, Case("a", ["omite"], [contains("essencial")]))
        fabrication = await run_case(
            client, Case("b", ["inventa"], [not_contains("TRAIL_RETRY_LIMIT")])
        )
        wrong_path = await run_case(
            client, Case("c", ["sem ferramenta"], [calls_tools("search_docs")])
        )
        no_block = await run_case(
            client, Case("d", ["deixa passar"], [blocks("guard_in")])
        )
        false_block = await run_case(
            client, Case("e", ["falso positivo"], [does_not_block()])
        )

    assert [f.kind for f in omission.findings] == ["OMISSION"]
    assert [f.kind for f in fabrication.findings] == ["FABRICATION"]
    assert [f.kind for f in wrong_path.findings] == ["WRONG_PATH"]
    assert [f.kind for f in no_block.findings] == ["WRONG_PATH"]
    assert [f.check for f in false_block.findings] == ["does_not_block"]
    assert all(f.source == "check" for f in omission.findings)


async def test_a_passing_case_still_records_its_checks() -> None:
    """A denominator built from failures alone reads 0/0 as a perfect score."""
    client = agent(
        **{"ok": stream("both, input, output, none", tools=("search_docs",))}
    )
    async with client:
        outcome = await run_case(
            client,
            Case("ok", ["ok"], [contains("both", "none"), calls_tools("search_docs")]),
        )
    assert outcome.passed
    assert [c.metric for c in outcome.checks] == ["", "grounding"]
    assert all(c.passed for c in outcome.checks)


async def test_contains_is_case_folded_but_not_normalised() -> None:
    client = agent(**{"q": stream("A pendência é de R$ 1.200")})
    async with client:
        folded = await run_case(client, Case("x", ["q"], [contains("PENDÊNCIA")]))
        stripped = await run_case(client, Case("y", ["q"], [contains("pendencia")]))
    assert folded.passed
    # No accent folding: the agent does not normalise its output, so a scorer
    # that did would be grading a text nobody was shown.
    assert not stripped.passed


# --------------------------------------------------------------------------
# Per-turn checks
# --------------------------------------------------------------------------


async def test_a_mid_conversation_regression_is_not_invisible() -> None:
    """The last turn is fine and the case still fails, because turn 0 was not.

    This is the whole point of ``turn_checks``: a case that confirms a transfer
    on the second question is worthless if the first one already claimed the
    money was gone, and reading only the last answer cannot see that.
    """
    client = agent(
        **{
            "manda 300 pra Renata": stream("pronto, já enviei os R$ 300,00"),
            "sim, pode mandar": stream("transferência concluída", tools=("confirm",)),
        }
    )
    case = Case(
        id="pix",
        turns=["manda 300 pra Renata", "sim, pode mandar"],
        checks=[contains("concluída"), calls_tools("confirm")],
        turn_checks={0: [not_contains("enviei")]},
    )
    async with client:
        outcome = await run_case(client, case)

    assert not outcome.passed
    # The last turn's own checks all passed — only the intermediate one failed.
    assert [f.kind for f in outcome.findings] == ["FABRICATION"]
    # Attributable: the finding names the turn, not just the case.
    assert (outcome.findings[0].case_id, outcome.findings[0].turn) == ("pix", 0)
    assert [(c.name, c.passed) for c in outcome.checks] == [
        ("turno 0: not_contains(enviei)", False),
        ("contains(concluída)", True),
        ("calls_tools(confirm)", True),
    ]


async def test_the_same_case_without_turn_checks_passes_unchanged() -> None:
    """Backwards compatibility, stated as the contrast it is."""
    client = agent(
        **{
            "manda 300 pra Renata": stream("pronto, já enviei os R$ 300,00"),
            "sim, pode mandar": stream("transferência concluída", tools=("confirm",)),
        }
    )
    case = Case(
        id="pix",
        turns=["manda 300 pra Renata", "sim, pode mandar"],
        checks=[contains("concluída"), calls_tools("confirm")],
    )
    async with client:
        outcome = await run_case(client, case)
    assert outcome.passed
    assert [c.name for c in outcome.checks] == [
        "contains(concluída)",
        "calls_tools(confirm)",
    ]


async def test_a_turn_check_passes_where_the_turn_is_right() -> None:
    client = agent(
        **{
            "manda 300 pra Renata": stream("confirma o PIX de R$ 300,00?"),
            "sim, pode mandar": stream("transferência concluída"),
        }
    )
    case = Case(
        id="pix_ok",
        turns=["manda 300 pra Renata", "sim, pode mandar"],
        checks=[contains("concluída")],
        turn_checks={0: [not_contains("enviei"), contains("confirma")]},
    )
    async with client:
        outcome = await run_case(client, case)
    assert outcome.passed
    assert all(c.passed for c in outcome.checks)
    assert len(outcome.checks) == 3


async def test_an_errored_case_fails_its_turn_checks_too() -> None:
    """Same rule as ``checks``: the worst cases must not shrink a denominator."""
    client = agent(
        **{"quebra": stream("", error={"status": 502, "detail": "upstream"})}
    )
    case = Case(
        id="boom",
        turns=["quebra", "segunda"],
        checks=[contains("x")],
        turn_checks={0: [calls_tools("search_docs")]},
    )
    async with client:
        outcome = await run_case(client, case)
    assert outcome.errored
    assert [(c.metric, c.passed) for c in outcome.checks] == [
        ("", False),
        ("grounding", False),
    ]


# --------------------------------------------------------------------------
# Failure never leaves the denominator
# --------------------------------------------------------------------------


async def test_an_errored_turn_becomes_a_finding_not_an_exception() -> None:
    client = agent(
        **{"quebra": stream("", error={"status": 502, "detail": "upstream"})}
    )
    async with client:
        outcome = await run_case(client, Case("boom", ["quebra"], [contains("x")]))
    assert outcome.errored
    assert [f.kind for f in outcome.findings] == ["ERROR"]


async def test_an_errored_case_fails_its_checks_rather_than_skipping_them() -> None:
    """Otherwise every denominator shrinks by exactly the cases that went worst."""
    client = agent(
        **{"quebra": stream("", error={"status": 502, "detail": "upstream"})}
    )
    async with client:
        outcome = await run_case(
            client, Case("boom", ["quebra"], [calls_tools("search_docs")])
        )
    assert [(c.metric, c.passed) for c in outcome.checks] == [("grounding", False)]


async def test_a_transport_failure_is_still_a_case() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(refuse)
    ) as client:
        outcome = await run_case(client, Case("down", ["oi"], [contains("x")]))
    assert outcome.errored
    assert "ConnectError" in outcome.findings[0].actual


async def test_the_runner_deletes_the_threads_it_opened() -> None:
    """An eval run that left its threads behind would refill the sidebar."""
    deleted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        if request.url.path == "/threads":
            return httpx.Response(201, json={"thread_id": "t-9"})
        return httpx.Response(
            200, text=stream("oi"), headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    ) as client:
        await run_case(client, Case("c", ["oi"]))
    assert deleted == ["/threads/t-9"]


# --------------------------------------------------------------------------
# The metrics
# --------------------------------------------------------------------------


def golden(*cases: Case, **thresholds: Threshold) -> GoldenSet:
    return GoldenSet(version="test-v1", cases=cases, thresholds=thresholds)


async def test_the_denominator_is_every_case_including_the_broken_ones() -> None:
    client = agent(
        **{
            "boa": stream("both e none"),
            "quebra": stream("", error={"status": 502, "detail": "upstream"}),
        }
    )
    cases = (
        Case("ok", ["boa"], [contains("both")]),
        Case("boom", ["quebra"], [contains("both")]),
    )
    async with client:
        outcomes = await run_golden_set(golden(*cases), client=client)
    report = m.compute_metrics(outcomes, golden(*cases))

    rate = report.metric("case_pass_rate")
    assert rate is not None
    # 1/2, not 1/1. The broken case is still in the set.
    assert (rate.numerator, rate.denominator) == (1, 2)
    assert rate.value == 0.5


async def test_the_kinds_are_reported_side_by_side_never_summed() -> None:
    client = agent(
        **{
            "omite": stream("nada"),
            "inventa": stream("TRAIL_RETRY_LIMIT existe"),
        }
    )
    cases = (
        Case("a", ["omite"], [contains("both")]),
        Case("b", ["inventa"], [not_contains("TRAIL_RETRY_LIMIT")]),
    )
    async with client:
        outcomes = await run_golden_set(golden(*cases), client=client)
    report = m.compute_metrics(outcomes, golden(*cases))

    assert report.metric("omission_rate").value == 0.5
    assert report.metric("fabrication_rate").value == 0.5
    assert report.metric("wrong_path_rate").value == 0.0


async def test_a_fabrication_fails_the_run_whatever_else_passed() -> None:
    client = agent(**{"inventa": stream("TRAIL_RETRY_LIMIT existe")})
    cases = (Case("b", ["inventa"], [not_contains("TRAIL_RETRY_LIMIT")]),)
    async with client:
        outcomes = await run_golden_set(golden(*cases), client=client)
    report = m.compute_metrics(outcomes, golden(*cases))
    assert report.status == "FAILED"
    assert [f.kind for f in report.violations] == ["FABRICATION"]


async def test_a_false_block_fails_the_run_too() -> None:
    """A refusal always looks safe, which is why it has to be measured."""
    client = agent(**{"legítima": stream("", blocked="guard_in")})
    cases = (Case("f", ["legítima"], [does_not_block()]),)
    async with client:
        outcomes = await run_golden_set(golden(*cases), client=client)
    report = m.compute_metrics(outcomes, golden(*cases))
    assert report.status == "FAILED"
    assert report.metric("false_block_rate").value == 1.0


def test_a_rate_with_no_evidence_is_none_not_a_hundred_percent() -> None:
    report = m.compute_metrics([], golden())
    assert report.metric("case_pass_rate").value is None
    assert report.metric("grounding_rate").value is None


async def test_cost_is_none_when_nothing_priced_itself() -> None:
    client = agent(**{"q": stream("oi", cost=None)})
    cases = (Case("c", ["q"]),)
    async with client:
        outcomes = await run_golden_set(golden(*cases), client=client)
    report = m.compute_metrics(outcomes, golden(*cases))
    assert report.metric("cost_per_turn_usd").value is None


def test_percentile_is_nearest_rank() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert m.percentile(values, 0.50) == 5
    assert m.percentile(values, 0.95) == 10
    # No interpolation: a p95 over a small set must be a latency some turn
    # actually had.
    assert m.percentile([7], 0.95) == 7
    assert m.percentile([], 0.5) is None


# --------------------------------------------------------------------------
# Thresholds and regression
# --------------------------------------------------------------------------


async def test_a_metric_is_measured_against_the_bar_the_example_registered() -> None:
    client = agent(**{"q": stream("nada")})
    cases = (Case("a", ["q"], [contains("both")]),)
    bars = {"case_pass_rate": Threshold(0.9, ">=", "previous release", "I")}
    gs = GoldenSet(version="test-v1", cases=cases, thresholds=bars)
    async with client:
        outcomes = await run_golden_set(gs, client=client)
    report = m.compute_metrics(outcomes, gs)
    assert not report.metric("case_pass_rate").clears
    assert report.metric("case_pass_rate").threshold.grade == "I"


def test_an_unmeasured_metric_does_not_manufacture_a_failure() -> None:
    bar = Threshold(0.01, "<=")
    assert bar.clears(None)


def test_regression_needs_the_same_golden_set() -> None:
    report = m.RunReport(
        golden_set_version="v2",
        status="COMPLETED",
        metrics=[m.Metric("case_pass_rate", 0.5, "rate")],
        findings=[],
    )
    baseline = {"case_pass_rate": {"value": 0.9}}
    # Different set, no comparison: it is a different measurement, and a delta
    # between two of those looks exactly like one that means something.
    assert m.compare_to_baseline(report, baseline, "v1") == []
    regressions = m.compare_to_baseline(report, baseline, "v2")
    assert [r.metric for r in regressions] == ["case_pass_rate"]


def test_a_judged_metric_carries_its_noise_into_the_regression() -> None:
    report = m.RunReport(
        golden_set_version="v1",
        status="COMPLETED",
        metrics=[
            m.Metric(
                "omission_rate",
                0.4,
                "rate",
                threshold=Threshold(0.1, "<="),
                judged=True,
            )
        ],
        findings=[],
    )
    regressions = m.compare_to_baseline(report, {"omission_rate": {"value": 0.1}}, "v1")
    assert regressions[0].noisy
    assert regressions[0].crossed_threshold


def test_movement_within_the_drift_band_is_not_a_regression() -> None:
    report = m.RunReport(
        golden_set_version="v1",
        status="COMPLETED",
        metrics=[
            m.Metric("case_pass_rate", 0.99, "rate", threshold=Threshold(0.9, ">="))
        ],
        findings=[],
    )
    assert m.compare_to_baseline(report, {"case_pass_rate": {"value": 1.0}}, "v1") == []


# --------------------------------------------------------------------------
# The example's own set, and the renderer
# --------------------------------------------------------------------------


def test_the_registry_resolves_a_golden_set_beside_the_agent() -> None:
    from trail.runtime.registry import load_golden, load_spec

    spec = load_spec("trail_guide")
    gs = load_golden("trail_guide")
    # Same name, same package: an example cannot be mounted under one name and
    # measured under another.
    assert spec.name == "trail_guide"
    assert gs.version.startswith("trail_guide-")
    assert gs.cases and gs.thresholds


def test_an_unknown_example_names_the_valid_set() -> None:
    from trail.runtime.registry import load_golden

    with pytest.raises(ValueError, match="trail_guide"):
        load_golden("no_such_agent")


def test_every_threshold_belongs_to_a_metric_the_harness_computes() -> None:
    """A bar registered for a metric nobody computes is a bar nobody enforces."""
    from trail.runtime.registry import load_golden

    gs = load_golden("trail_guide")
    computed = {m.name for m in m.compute_metrics([], gs).metrics}
    assert set(gs.thresholds) <= computed


def test_the_scorecard_renders_a_run_with_missing_measurements() -> None:
    """A `—` must not be a crash: half these numbers are absent on a bad run."""
    from rich.console import Console

    from trail.evals import report as scorecard
    from trail.runtime.registry import load_golden

    gs = load_golden("trail_guide")
    report = m.compute_metrics([], gs)
    console = Console(
        record=True, width=100, theme=__import__("trail.cli", fromlist=["THEME"]).THEME
    )
    scorecard.render(
        console, report, agent="trail_guide", model="fake", guardrails="both"
    )
    printed = console.export_text()
    assert "case_pass_rate" in printed
    assert "—" in printed
    assert "sem baseline" in printed


def test_latency_jitter_alone_is_not_a_regression() -> None:
    """A list that is populated on every run is a list nobody reads."""
    report = m.RunReport(
        golden_set_version="v1",
        status="COMPLETED",
        metrics=[
            m.Metric(
                "latency_p95_ns",
                6_000_000_000,
                "ns",
                threshold=Threshold(20_000_000_000, "<="),
            ),
        ],
        findings=[],
    )
    baseline = {"latency_p95_ns": {"value": 4_700_000_000}}
    assert m.compare_to_baseline(report, baseline, "v1") == []


def test_but_crossing_the_bar_is_a_regression_at_any_size() -> None:
    report = m.RunReport(
        golden_set_version="v1",
        status="COMPLETED",
        metrics=[
            m.Metric(
                "latency_p95_ns",
                20_100_000_000,
                "ns",
                threshold=Threshold(20_000_000_000, "<="),
            ),
        ],
        findings=[],
    )
    baseline = {"latency_p95_ns": {"value": 20_000_000_000}}
    regressions = m.compare_to_baseline(report, baseline, "v1")
    assert [r.crossed_threshold for r in regressions] == [True]
