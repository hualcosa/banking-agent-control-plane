"""The harness against the running stack: a real agent, a real judge, real rows.

The unit tier proves the arithmetic. This proves the two claims the arithmetic
cannot: that the golden set actually drives the shipped HTTP endpoint, and that
a run survives the round trip through Postgres and comes back comparable.

Deliberately small. A full golden set here would be a slow, expensive
duplicate of `make eval`, which is the command that exists to run it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from trail.config import get_settings
from trail.evals import metrics as scoring
from trail.evals import store
from trail.evals.cases import Case, GoldenSet, Threshold, contains, does_not_block
from trail.evals.judge import bind_judge, build_session
from trail.evals.runner import run_golden_set
from trail.runtime.registry import load_golden

pytestmark = pytest.mark.integration


async def test_the_golden_set_drives_the_shipped_endpoint(live_agent: str) -> None:
    """One real case, one real answer, scored against a real bar."""
    golden = GoldenSet(
        version="integration-smoke",
        cases=[
            Case(
                id="what_is_trail",
                turns=["o que é o TRAIL, em uma frase?"],
                checks=[does_not_block(), contains("trail")],
            )
        ],
        thresholds={"case_pass_rate": Threshold(1.0, ">=", grade="V")},
    )
    async with httpx.AsyncClient(base_url=live_agent, timeout=180.0) as client:
        outcomes = await run_golden_set(golden, client=client, concurrency=1)

    report = scoring.compute_metrics(outcomes, golden)
    assert report.status == "COMPLETED"
    assert not report.findings, [f.detail for f in report.findings]
    # The evidence came off the wire, not from a test-only field.
    observation = outcomes[0].observations[0]
    assert observation.stages and observation.ns and observation.trace_url


async def test_the_judge_grades_a_real_answer(
    live_agent: str, real_credentials: str
) -> None:
    from trail.evals.judge import judge

    settings = get_settings()
    golden = GoldenSet(
        version="integration-judge",
        cases=[
            Case(
                id="judged",
                turns=["o que é o TRAIL, em uma frase?"],
                checks=[judge("A resposta descreve o TRAIL, seja lá como?")],
            )
        ],
    )
    session = build_session(settings)
    async with httpx.AsyncClient(base_url=live_agent, timeout=180.0) as client:
        with bind_judge(session):
            outcomes = await run_golden_set(golden, client=client, concurrency=1)

    assert session.ledger.calls == 1
    assert session.ledger.input_tokens > 0
    # Whatever the verdict, the grader's spend stays out of the agent's number.
    report = scoring.compute_metrics(outcomes, golden, judge=session.ledger)
    agent_cost = report.metric("cost_per_turn_usd").value
    assert agent_cost is None or agent_cost < 1.0


async def test_a_run_survives_the_round_trip_through_postgres() -> None:
    """Written, read back, and eligible as the next run's baseline."""
    settings = get_settings()
    try:
        connection = await store.connect(settings.database_url)
    except Exception as exc:
        pytest.skip(f"postgres não alcançável em {settings.database_url}: {exc}")

    golden = load_golden(settings.agent)
    report = scoring.compute_metrics([], golden)
    async with connection:
        run_id = await store.save_run(
            connection,
            report,
            agent=settings.agent,
            model=settings.model,
            guardrails=settings.guardrails,
            judge_model=settings.judge_model,
            started_at=datetime.now(UTC),
        )
        baseline = await store.latest_baseline(connection, golden.version)
        assert baseline is not None
        # Most recent COMPLETED run of the same set — which is this one.
        assert baseline.id == run_id
        assert set(baseline.metrics) == {m.name for m in report.metrics}

        # A different golden set is a different measurement, and gets no
        # baseline rather than a meaningless delta.
        assert await store.latest_baseline(connection, "no-such-set-v0") is None

        await connection.execute("DELETE FROM eval_runs WHERE id = %s", (run_id,))
