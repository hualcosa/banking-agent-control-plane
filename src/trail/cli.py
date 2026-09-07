"""``trail chat`` — a conversation, and the pipeline behind it.

``trail eval`` is the other half: the same client, driving a golden set instead
of a person's questions, over the same endpoint. Both live here because they
are the same claim — a client with a private code path measures a system that
does not exist in production — and keeping them in one module is what makes
that hard to quietly stop being true.

The point of this client is the rail. Anything can print a model's answer; what
this prints alongside it is what the turn actually did — which gates ran, which
were switched off, how long the model took, what it cost, and a link to the
span tree. That is the claim the repository makes, and a client that showed
only the answer would leave it unverifiable from the terminal.

It speaks HTTP to the service and never imports the agent. Same interface a
browser uses, same one the eval harness drives.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from datetime import UTC, datetime
from typing import Any

import httpx
from rich.console import Console
from rich.text import Text
from rich.theme import Theme

from trail.config import Settings, get_settings
from trail.evals import metrics as scoring
from trail.evals import report as scorecard
from trail.evals import store
from trail.evals.cases import CaseOutcome
from trail.evals.judge import bind_judge, build_session
from trail.evals.metrics import RunReport
from trail.evals.runner import run_golden_set
from trail.identity import HEADER as IDENTITY_HEADER
from trail.identity import sign as sign_identity
from trail.runtime.events import duration, iter_sse
from trail.runtime.registry import load_golden

DEFAULT_BASE_URL = os.environ.get("TRAIL_AGENT_BASE_URL", "http://localhost:8000")

THEME = Theme(
    {
        "agent": "bold cyan",
        "user": "bold white",
        "meta": "dim",
        "ok": "green",
        "skip": "dim strike",
        "blocked": "bold red",
        "rule": "dim",
    }
)

#: A distinct glyph per status, and `blocked` must not share one with `done`.
#: Colour alone is not a difference: it is lost in a pipe, in a screenshot, and
#: to a reader who cannot distinguish red from grey — and a gate that fired
#: rendering identically to a gate that passed is the exact failure this whole
#: design exists to prevent.
MARK = {"done": "▪", "skip": "▫", "blocked": "✗", "start": "▪"}


class CliError(Exception):
    """An error the user can act on, rendered as a message plus a hint."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def identity_headers() -> dict[str, str]:
    """The signed identity this client presents on every request.

    The CLI is standing in for the channel — the piece that, in production,
    has actually authenticated the person — so it is the piece that signs.
    ``TRAIL_CUSTOMER_ID`` is who it claims to be and ``TRAIL_IDENTITY_SECRET``
    is what makes the claim checkable; with no secret there is nothing to sign
    with, and saying so here is far cheaper to debug than a 401 from a service
    that (correctly) refuses to explain itself.

    Milestone 4 replaces this with a Cognito login: the same header slot, a
    token instead of a MAC, and this function is the only part that changes.
    """
    settings = get_settings()
    secret = settings.identity_secret.get_secret_value()
    if not secret:
        raise CliError(
            "TRAIL_IDENTITY_SECRET não está definido: sem ele não há como "
            "assinar a identidade e o agente recusa todo pedido com 401",
            hint="copie .env.example para .env, ou exporte TRAIL_IDENTITY_SECRET",
        )
    return {IDENTITY_HEADER: sign_identity(settings.customer_id, secret)}


def _render_rail(console: Console, stages: list[dict[str, Any]]) -> None:
    """Print the rail in arrival order, which is pipeline order.

    Deliberately unsorted. The skips for gates the dial left out are emitted
    from the hook where each would have run, so they already land in place —
    and any sort able to reposition them is also able to scramble the real
    interleaving of model and tool calls, which is the ordering a reader is
    reading for.
    """
    text = Text()
    for stage in stages:
        status = stage["status"]
        style = {"skip": "skip", "blocked": "blocked"}.get(status, "meta")
        text.append(f"{MARK.get(status, '·')}{stage['label']} ", style=style)
        if status == "skip":
            text.append("pulado  ", style="skip")
        elif status == "blocked":
            text.append("BLOQUEADO  ", style="blocked")
        elif stage.get("ns") is not None:
            text.append(f"{duration(stage['ns'])}  ", style="meta")
    console.print("  ", text)

    for stage in stages:
        for violation in (stage.get("detail") or {}).get("violations", []):
            console.print(
                f"  ↳ [blocked]{violation['check']}[/] · {violation['detail']}"
                f" · [meta]{violation['evidence']}[/]"
            )


def _render_cost(
    console: Console, stages: list[dict[str, Any]], total_ns: int | None
) -> None:
    tokens_in = tokens_out = 0
    cost: float | None = None
    for stage in stages:
        detail = stage.get("detail") or {}
        if stage["kind"] != "model" or stage["status"] != "done":
            continue
        tokens_in += detail.get("input_tokens") or 0
        tokens_out += detail.get("output_tokens") or 0
        if detail.get("cost_usd") is not None:
            cost = (cost or 0.0) + detail["cost_usd"]
    if not tokens_in and not tokens_out:
        return
    # `—` and not `$0.00`: an unpriced model has an unknown cost, and a
    # confident zero is the most expensive kind of wrong.
    money = f"US$ {cost:.4f}" if cost is not None else "—"
    parts = [f"{tokens_in} in", f"{tokens_out} out", money]
    # The turn's own wall time, which is not the sum of the cells: the graph
    # spends time between them. Showing both is how the gap becomes visible
    # instead of being something a reader has to compute and then doubt.
    if total_ns is not None:
        parts.append(f"total {duration(total_ns)}")
    console.print(f"  [meta]{' · '.join(parts)}[/]")


def _render_trace(console: Console, url: str) -> None:
    """Print the trace link so that clicking it opens the whole trace.

    ``soft_wrap`` is the fix and it is not cosmetic. Rich wraps to the console
    width by inserting a **real newline**, which splits a 32-character trace id
    across two lines; a terminal click then follows only the first, Langfuse
    looks up an id that does not exist, and the page sits on "Loading…"
    forever. With soft wrap the byte stream holds one unbroken line and the
    terminal's own reflow leaves the URL intact.

    The OSC 8 hyperlink is belt to that braces: terminals that support it make
    the link clickable regardless of where the line happens to fold.
    """
    console.print(f"  [meta]trace:[/] [link={url}]{url}[/link]", soft_wrap=True)


async def _turn(
    client: httpx.AsyncClient, console: Console, thread_id: str, message: str
) -> None:
    stages: list[dict[str, Any]] = []
    async with client.stream(
        "POST", f"/threads/{thread_id}/turns/stream", json={"message": message}
    ) as response:
        response.raise_for_status()
        # A live "…" for each stage as it starts, so the wait is visibly the
        # pipeline working rather than the terminal hanging. `start` frames are
        # shown and then discarded; only completed ones join the rail, which is
        # reprinted whole once the answer arrives.
        with console.status("", spinner="dots") as spinner:
            async for event, data in iter_sse(response.aiter_lines()):
                if event == "stage":
                    if data["status"] == "start":
                        spinner.update(f"[meta]{data['label']}…[/]")
                    else:
                        stages.append(data)
                elif event == "turn":
                    spinner.stop()
                    console.print()
                    console.print(Text(data["text"], style="agent"))
                    console.print()
                    _render_rail(console, stages)
                    _render_cost(console, stages, data.get("ns"))
                elif event == "error":
                    spinner.stop()
                    console.print(
                        f"  [blocked]falha {data['status']}[/] · {data['detail']}"
                    )
                elif event == "trace" and data.get("trace_url"):
                    _render_trace(console, data["trace_url"])


async def chat(base_url: str) -> int:
    console = Console(theme=THEME)
    headers = identity_headers()
    async with httpx.AsyncClient(
        base_url=base_url, timeout=120.0, headers=headers
    ) as client:
        try:
            opened = await client.post("/threads")
            opened.raise_for_status()
        except httpx.HTTPError as exc:
            raise CliError(
                f"não consegui falar com o agente em {base_url}: {exc}",
                hint="a stack está de pé? `make up`",
            ) from exc

        thread = opened.json()
        console.print(
            f"[meta]agente[/] {thread['agent']}  "
            f"[meta]guardrails[/] {thread['guardrails']}  "
            f"[meta]thread[/] {thread['thread_id'][:8]}"
        )
        console.print()
        console.print(Text(thread["greeting"], style="agent"))

        while True:
            console.print()
            try:
                message = console.input("[user]› [/]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0
            if not message:
                continue
            if message in {"sair", "exit", "quit"}:
                return 0
            await _turn(client, console, thread["thread_id"], message)


async def evaluate(base_url: str, concurrency: int = 4) -> int:
    """Run the mounted example's golden set and print the scorecard.

    The whole command is a composition and deliberately holds no logic of its
    own: the registry resolves the golden set, the runner drives it over HTTP,
    `metrics` scores it against bars the example registered, `store` files it
    and finds the baseline, `report` renders it. Anything decided here would be
    a threshold living in a client.
    """
    console = Console(theme=THEME)
    settings = get_settings()
    try:
        golden = load_golden(settings.agent)
    except (ValueError, ModuleNotFoundError) as exc:
        raise CliError(
            f"o exemplo {settings.agent!r} não traz um golden set: {exc}",
            hint="um exemplo é medível quando expõe examples/<pacote>/golden.py",
        ) from exc

    console.print(
        f"[meta]golden set[/] {golden.version}  "
        f"[meta]{len(golden.cases)} casos[/]  [meta]concorrência[/] {concurrency}"
    )

    def announce(outcome: CaseOutcome) -> None:
        mark = "[ok]▪[/]" if outcome.passed else "[blocked]✗[/]"
        console.print(f"  {mark} {outcome.case_id}")

    # Built and bound unconditionally. Constructing the model costs nothing —
    # no call is made until a case actually declares a judge check — and the
    # alternative is inspecting case bodies to guess whether one does.
    session = build_session(settings)
    started_at = datetime.now(UTC)
    async with httpx.AsyncClient(
        base_url=base_url, timeout=180.0, headers=identity_headers()
    ) as client:
        try:
            (await client.get("/healthz", timeout=10.0)).raise_for_status()
        except httpx.HTTPError as exc:
            raise CliError(
                f"não consegui falar com o agente em {base_url}: {exc}",
                hint="a stack está de pé? `make up`",
            ) from exc
        with bind_judge(session):
            outcomes = await run_golden_set(
                golden, client=client, concurrency=concurrency, on_done=announce
            )

    report = scoring.compute_metrics(outcomes, golden, judge=session.ledger)
    report = await _persist(console, report, settings, started_at)
    scorecard.render(
        console,
        report,
        agent=settings.agent,
        model=settings.model,
        guardrails=settings.guardrails,
        run_id=report.run_id,
    )
    # The exit code is the criterion, so `make eval` can gate a merge. A FAILED
    # run or a crossed threshold is a non-zero exit; a regression that stayed
    # inside its bar is reported and does not fail the command.
    crossed = [m for m in report.metrics if not m.clears]
    return 1 if report.status == "FAILED" or crossed else 0


async def _persist(
    console: Console, report: RunReport, settings: Settings, started_at: datetime
) -> RunReport:
    """File the run, attach the baseline comparison, return the report.

    Storage failure never costs a run: the scorecard still prints, with a line
    saying it was not recorded. Losing an afternoon's numbers because Postgres
    was down would be the most annoying possible failure mode for a harness.
    """
    try:
        connection = await store.connect(settings.database_url)
    except Exception as exc:
        console.print(
            f"  [blocked]não registrado[/] [meta]{type(exc).__name__}: "
            f"{exc}; sem baseline e sem histórico[/]"
        )
        return report

    async with connection:
        baseline = await store.latest_baseline(connection, report.golden_set_version)
        if baseline is not None:
            report = dataclasses.replace(
                report,
                baseline_id=baseline.id,
                regressions=scoring.compare_to_baseline(
                    report, baseline.metrics, baseline.golden_set_version
                ),
            )
        run_id = await store.save_run(
            connection,
            report,
            agent=settings.agent,
            model=settings.model,
            guardrails=settings.guardrails,
            judge_model=settings.judge_model,
            started_at=started_at,
        )
    return dataclasses.replace(report, run_id=run_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trail", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    chat_cmd = sub.add_parser("chat", help="hold a conversation with the agent")
    chat_cmd.add_argument("--base-url", default=DEFAULT_BASE_URL)

    eval_cmd = sub.add_parser(
        "eval", help="run the mounted example's golden set against the agent"
    )
    eval_cmd.add_argument("--base-url", default=DEFAULT_BASE_URL)
    eval_cmd.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="cases in flight at once; raise it and the latency percentiles "
        "start measuring the queue rather than the agent",
    )

    health = sub.add_parser("health", help="check that the agent is up")
    health.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser


async def health(base_url: str) -> int:
    console = Console(theme=THEME)
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        try:
            response = await client.get("/healthz")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CliError(f"{base_url} não respondeu: {exc}") from exc
    console.print(f"[ok]ok[/] {base_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = {"chat": chat, "eval": evaluate, "health": health}[args.command]
    extra = {"concurrency": args.concurrency} if args.command == "eval" else {}
    try:
        return asyncio.run(runner(args.base_url, **extra))
    except CliError as exc:
        console = Console(theme=THEME, stderr=True)
        console.print(f"[blocked]erro[/] {exc}")
        if exc.hint:
            console.print(f"[meta]{exc.hint}[/]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
