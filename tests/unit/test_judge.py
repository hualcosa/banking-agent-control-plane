"""The judge check: a model's opinion, kept distinguishable from a measurement.

Driven by :class:`tests.fakes.ScriptedModel`, so the whole path — the binding,
the prompt, the verdict parse, the ledger — runs offline. What is scripted is
the judge's answer, which is the only part a real model would contribute.
"""

from __future__ import annotations

import httpx
import pytest

from tests.fakes import says, scripted
from trail.evals import judge as J
from trail.evals import metrics as m
from trail.evals.cases import Case, GoldenSet, Observation, contains
from trail.evals.runner import run_case, run_golden_set
from trail.runtime.events import sse

pytestmark = pytest.mark.unit


def session(
    *replies: str,
    usage: dict | None = None,
    model: str = "gpt-5.6-luna",
    self_evaluating: bool = True,
) -> J.JudgeSession:
    return J.JudgeSession(
        chat=scripted(*(says(reply, usage) for reply in replies)),
        ledger=J.JudgeLedger(model=model, self_evaluating=self_evaluating),
    )


def observation(answer: str) -> Observation:
    return Observation(case_id="c", turn=0, question="pergunta", answer=answer)


async def test_pass_leaves_no_finding() -> None:
    check = J.judge("A resposta admite não saber?")
    with J.bind_judge(session("PASS")):
        assert await check.run(observation("não conheço essa variável")) == []


async def test_fail_becomes_a_finding_marked_as_an_opinion() -> None:
    check = J.judge("A resposta admite não saber?")
    with J.bind_judge(session("FAIL: inventou um comportamento")):
        findings = await check.run(observation("ela define o limite de retentativas"))
    assert len(findings) == 1
    finding = findings[0]
    # `source` is the whole point: a substring test and a model's opinion are
    # not the same evidence, and a scorecard that hid the difference would be
    # claiming more than it measured.
    assert finding.source == "judge"
    assert finding.kind == "OMISSION"
    assert finding.detail == "inventou um comportamento"


async def test_a_rubric_about_invention_inherits_the_zero_tolerance_kind() -> None:
    check = J.judge("A resposta inventa uma variável?", kind="FABRICATION")
    with J.bind_judge(session("FAIL: inventou")):
        findings = await check.run(observation("TRAIL_RETRY_LIMIT existe"))
    assert findings[0].kind == "FABRICATION"


async def test_an_illegible_verdict_is_refused_not_read_charitably() -> None:
    """Guessing from a stray 'sim' is how a judge starts passing everything."""
    check = J.judge("qualquer coisa")
    with (
        J.bind_judge(session("acho que sim, no geral")),
        pytest.raises(ValueError, match="ilegível"),
    ):
        await check.run(observation("qualquer resposta"))


async def test_an_unbound_judge_says_so_instead_of_passing() -> None:
    check = J.judge("qualquer coisa")
    with pytest.raises(RuntimeError, match="no judge bound"):
        await check.run(observation("resposta"))


async def test_a_judge_that_raises_fails_its_own_check_loudly() -> None:
    """A broken grader must not read as a clean run."""
    body = "".join(
        [
            sse(
                "stage",
                {
                    "name": "model",
                    "kind": "model",
                    "label": "modelo",
                    "status": "done",
                    "ns": 1_000_000,
                    "detail": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cost_usd": 0.001,
                    },
                },
            ),
            sse("turn", {"thread_id": "t", "text": "resposta", "ns": 1_000_000}),
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/threads":
            return httpx.Response(201, json={"thread_id": "t-1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    case = Case("j", ["pergunta"], [J.judge("um critério")])
    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    ) as client:
        with J.bind_judge(session("resposta sem formato")):
            outcome = await run_case(client, case)

    assert [f.kind for f in outcome.findings] == ["ERROR"]
    assert not outcome.checks[0].passed


async def test_judge_spend_is_tallied_apart_from_the_agents() -> None:
    """Folding the grader's cost in would move a number when only the set changed."""
    body = "".join(
        [
            sse(
                "stage",
                {
                    "name": "model",
                    "kind": "model",
                    "label": "modelo",
                    "status": "done",
                    "ns": 1_000_000,
                    "detail": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cost_usd": 0.004,
                    },
                },
            ),
            sse("turn", {"thread_id": "t", "text": "resposta", "ns": 1_000_000}),
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/threads":
            return httpx.Response(201, json={"thread_id": "t-1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream"}
        )

    golden = GoldenSet(
        version="v1",
        cases=[Case("j", ["pergunta"], [J.judge("um critério"), contains("resposta")])],
    )
    judge_session = session("PASS", usage={"input_tokens": 900, "output_tokens": 3})
    async with httpx.AsyncClient(
        base_url="http://agent", transport=httpx.MockTransport(handle)
    ) as client:
        with J.bind_judge(judge_session):
            outcomes = await run_golden_set(golden, client=client)

    report = m.compute_metrics(outcomes, golden, judge=judge_session.ledger)
    # The agent's number, untouched by the grader's 900 tokens.
    assert report.metric("cost_per_turn_usd").value == 0.004
    assert judge_session.ledger.calls == 1
    assert judge_session.ledger.input_tokens == 900
    assert judge_session.ledger.self_evaluating


def test_the_judge_defaults_to_the_agents_own_model_and_says_so(make_settings) -> None:
    settings = make_settings(judge_model="")
    assert (settings.judge_model or settings.model) == settings.model
    # Which is a legitimate default and a known bias, so it is flagged rather
    # than left unstated — see the ledger's `self_evaluating`.
    assert J.JudgeLedger(model=settings.model, self_evaluating=True).self_evaluating

    other = make_settings(judge_model="gpt-5.6")
    assert other.judge_model != other.model
