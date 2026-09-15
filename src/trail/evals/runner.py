"""Driving the golden set over the same HTTP endpoint a person drives.

`POST /threads`, then `POST /threads/{id}/turns/stream` per turn, reading the
frames with `runtime.events.iter_sse` — the CLI's parser, not a second one. If
this module reached into the agent, or asked the service for a field a browser
never gets, the numbers it produced would describe a system that does not ship.

Two rules govern failure, and both exist to keep the denominator honest:

* **A case never raises.** A connection refused, a 502 from the model, a
  malformed frame: each becomes an ``ERROR`` finding on a real
  :class:`~trail.evals.cases.CaseOutcome`, and that outcome stays in the set.
  A harness that dropped its failures would report the pass rate of the cases
  that happened to work.
* **An errored case fails its checks rather than skipping them.** A turn that
  never answered did not ground itself in a tool and did not trigger the gate
  it was supposed to trigger. Recording those checks as "not run" would shrink
  every denominator by exactly the cases that went worst.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence

import httpx

from trail.evals.cases import (
    Case,
    CaseOutcome,
    Check,
    CheckResult,
    Finding,
    GoldenSet,
    Observation,
)
from trail.runtime.events import iter_sse

logger = logging.getLogger(__name__)

#: A real turn waits on a model and possibly several tool rounds. The integration
#: tier uses the same number for the same reason: a timeout tuned for a fast
#: local answer turns a slow model into a fake regression.
TURN_TIMEOUT = 180.0

#: Cases run concurrently against one service. Four is enough to stop the run
#: being a serial wait on the model, and low enough that the latency
#: percentiles measure the agent rather than the queue behind it.
# ponytail: fixed default, raise it when a golden set outgrows a coffee break.
DEFAULT_CONCURRENCY = 4


async def drive_turn(
    client: httpx.AsyncClient, thread_id: str, case_id: str, turn: int, question: str
) -> Observation:
    """Run one turn and collect its frames into an :class:`Observation`.

    ``start`` frames are dropped. They exist so a live client can show which
    stage is running; a completed stage carries the same name plus the latency,
    and keeping both would double every count on the rail.
    """
    stages: list[dict] = []
    answer = ""
    error: dict | None = None
    ns: int | None = None
    trace_url: str | None = None

    async with client.stream(
        "POST",
        f"/threads/{thread_id}/turns/stream",
        json={"message": question},
        timeout=TURN_TIMEOUT,
    ) as response:
        response.raise_for_status()
        async for event, data in iter_sse(response.aiter_lines()):
            if event == "stage":
                if data.get("status") != "start":
                    stages.append(data)
            elif event == "turn":
                answer = data.get("text", "")
                ns = data.get("ns")
            elif event == "error":
                error = data
            elif event == "trace":
                trace_url = data.get("trace_url")

    return Observation(
        case_id=case_id,
        turn=turn,
        question=question,
        answer=answer,
        stages=stages,
        error=error,
        ns=ns,
        trace_url=trace_url,
    )


async def apply_checks(
    case: Case,
    obs: Observation,
    checks: Sequence[Check] | None = None,
    *,
    label: str = "",
) -> tuple[list[Finding], list[CheckResult]]:
    """Run every check against ``obs``, awaiting the ones that are coroutines.

    This is the seam the whole design turns on. A deterministic check returns a
    list; a judge check returns a coroutine that resolves to one. Neither knows
    about the other, and a case is free to declare one, the other, or both.

    ``checks`` defaults to the case's last-turn checks; the per-turn ones are
    passed in explicitly with a ``label`` so that a result listing says which
    turn a check belonged to. The findings need no such help — a
    :class:`Finding` already carries ``obs.turn``.
    """
    findings: list[Finding] = []
    results: list[CheckResult] = []
    for check in case.checks if checks is None else checks:
        try:
            produced = check.run(obs)
            if inspect.isawaitable(produced):
                produced = await produced
        except Exception as exc:
            # A check that raises is a bug in the check, not a verdict on the
            # agent — but it cannot be silent either, or a broken judge reads
            # as a clean run. It fails its own check and says why.
            logger.warning("check %s raised on %s", check.name, case.id, exc_info=exc)
            produced = [
                Finding(
                    case_id=case.id,
                    turn=obs.turn,
                    kind="ERROR",
                    check=check.name,
                    source="check",
                    expected="o check executa",
                    actual=f"{type(exc).__name__}: {exc}"[:200],
                    detail="o check falhou, o agente não foi avaliado neste ponto",
                )
            ]
        findings.extend(produced)
        results.append(
            CheckResult(
                name=f"{label}{check.name}", metric=check.metric, passed=not produced
            )
        )
    return findings, results


def _failed_everything(case: Case, obs: Observation, reason: str) -> CaseOutcome:
    """The outcome for a case whose conversation never completed.

    Every declared check is recorded as failed — see this module's second rule.
    """
    return CaseOutcome(
        case_id=case.id,
        observations=[obs],
        findings=[
            Finding(
                case_id=case.id,
                turn=obs.turn,
                kind="ERROR",
                check="turn",
                source="check",
                expected="o turno responde",
                actual=reason[:200],
                detail="o turno não chegou a produzir resposta",
            )
        ],
        checks=[
            CheckResult(name=c.name, metric=c.metric, passed=False)
            for c in case.all_checks()
        ],
    )


async def run_case(client: httpx.AsyncClient, case: Case) -> CaseOutcome:
    """Drive one case to completion and score it. Never raises.

    The thread is deleted on the way out, always. An eval run that left its
    threads behind would fill the sidebar with fifteen conversations nobody
    held — the clutter `c3b932a` removed once already, reintroduced from the
    other end.
    """
    thread_id: str | None = None
    observations: list[Observation] = []
    try:
        opened = await client.post("/threads", timeout=30.0)
        opened.raise_for_status()
        thread_id = opened.json()["thread_id"]

        findings: list[Finding] = []
        results: list[CheckResult] = []
        for index, question in enumerate(case.turns):
            obs = await drive_turn(client, thread_id, case.id, index, question)
            observations.append(obs)
            if obs.error is not None:
                return _failed_everything(
                    case, obs, f"{obs.error.get('status')} {obs.error.get('detail')}"
                )
            # Whatever this turn had to be true *as it happened*. A regression
            # in the middle of a conversation is invisible from the last turn.
            turn_findings, turn_results = await apply_checks(
                case, obs, case.checks_for(index), label=f"turno {index}: "
            )
            findings.extend(turn_findings)
            results.extend(turn_results)

        # And `checks` against the last turn: a case's earlier turns exist to
        # build the context its final question is asked in.
        last_findings, last_results = await apply_checks(case, observations[-1])
        findings.extend(last_findings)
        results.extend(last_results)
        return CaseOutcome(
            case_id=case.id,
            observations=observations,
            findings=findings,
            checks=results,
        )
    except Exception as exc:
        logger.warning("case %s failed", case.id, exc_info=exc)
        last = (
            observations[-1]
            if observations
            else Observation(
                case_id=case.id, turn=0, question=case.turns[0] if case.turns else ""
            )
        )
        return _failed_everything(case, last, f"{type(exc).__name__}: {exc}")
    finally:
        if thread_id is not None:
            try:
                await client.delete(f"/threads/{thread_id}", timeout=30.0)
            except Exception:
                logger.debug("could not delete eval thread %s", thread_id)


async def run_golden_set(
    golden: GoldenSet,
    *,
    client: httpx.AsyncClient,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_done: Callable[[CaseOutcome], None] | None = None,
) -> list[CaseOutcome]:
    """Run every case, bounded, and return the outcomes in declaration order.

    ``client`` is injected rather than built here, which is what lets the unit
    tier drive the whole runner against an ``httpx.MockTransport`` — no server,
    no key, no Docker — and still exercise the real SSE parsing.
    """
    limit = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> CaseOutcome:
        async with limit:
            outcome = await run_case(client, case)
        if on_done is not None:
            on_done(outcome)
        return outcome

    return list(await asyncio.gather(*(one(case) for case in golden.cases)))
