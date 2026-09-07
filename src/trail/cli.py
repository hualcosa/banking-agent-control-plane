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

`chat` and `eval` speak HTTP to the service and never import the agent. Same
interface a browser uses, same one the eval harness drives.

The other three commands — ``step-up``, ``intents`` and ``reconcile`` — do the
opposite on purpose, and the reason is the whole point of them. They never
touch the agent: they open the *shared* Postgres directly, build their own
``ControlPlane`` over it, and call the plane's methods from this process.

That is what "out of band" means. A step-up that arrived over the same HTTP
connection as the conversation would prove nothing — it is the same channel,
carrying the same trust. A mobile app's biometric callback reaches the control
plane through its own path, and so does this; the CLI is standing in for that
path exactly as it stands in for the channel when it signs an identity. The
operator's two commands are out of band for a blunter reason: at 3am the agent
may be the thing that is down, and an intent stuck in ``UNKNOWN`` still has to
be resolvable. Neither needs the service to be up, and neither has a private
code path into it — they use the same public methods ``examples/banking``
calls.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from control_plane import Context, ControlPlane, MockBank, Outcome, brl
from control_plane.pgstore import PgStore
from control_plane.state import new_id
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


# ---------------------------------------------------------------------------
# Out of band: step-up, and the operator's two commands
#
# Everything below reaches the same Postgres the agent writes to and builds its
# own ControlPlane over it. No HTTP, no agent import, no private method — the
# same public surface `examples/banking/tools.py` calls, from a second process.
# ---------------------------------------------------------------------------

#: What an intent in each of these states is waiting for, and the command that
#: gives it. ``trail intents`` prints this next to every row: an operator who
#: has just been woken up should not have to remember which state maps to which
#: verb, and a state with no next step is a state that should not be listed.
NEXT_STEP: dict[str, str] = {
    "UNKNOWN": "trail reconcile {id}",
    "AWAITING_STEP_UP": "trail step-up {id}",
    "AWAITING_CONFIRMATION": "confirme na conversa (a sessão que propôs)",
    "SUBMITTED": "em voo; se o processo caiu, o sweep move para UNKNOWN no boot",
    "PENDING": "o banco ainda não liquidou; aguarde e liste de novo",
}

#: Listed in this order, worst first. ``UNKNOWN`` leads because it is the only
#: state whose resolution needs a human and whose truth lives at the bank.
ATTENTION_STATES: list[str] = [
    "UNKNOWN",
    "SUBMITTED",
    "PENDING",
    "AWAITING_STEP_UP",
    "AWAITING_CONFIRMATION",
]


def redact(dsn: str) -> str:
    """The DSN with the password replaced, safe to print.

    Printed on every out-of-band command, because the first question about a
    surprising list of intents is which database it came from — and the second
    is whether that was the production one.
    """
    try:
        parts = urlsplit(dsn)
    except ValueError:  # pragma: no cover - urlsplit is total for str inputs
        return "?"
    if not parts.hostname:
        return dsn
    user = f"{parts.username}:***@" if parts.username else ""
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, f"{user}{parts.hostname}{port}", parts.path, "", "")
    )


def settings_or_error() -> Settings:
    """Configuration, or a message that names the missing variable.

    ``Settings`` fails closed on a missing ``TRAIL_LLM_API_KEY`` — right for a
    process that is about to call a model, and baffling at 3am for a command
    that never will. The value is not used here; only its absence is, and it is
    cheaper to say so than to hand an operator a pydantic traceback.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        missing = ", ".join(
            f"TRAIL_{str(error['loc'][0]).upper()}" for error in exc.errors()
        )
        raise CliError(
            f"configuração incompleta: {missing}",
            hint="copie .env.example para .env; estes comandos não chamam o "
            "modelo, mas leem a mesma configuração",
        ) from exc


@contextmanager
def open_plane() -> Iterator[ControlPlane]:
    """A control plane over the shared database, closed when the block ends.

    This is the out-of-band path in one function: ``PgStore`` is how a separate
    process reaches the state the agent wrote, and a plane built over it decides
    exactly as the agent's does, because it is the same class.

    The bank is named here rather than left to ``ControlPlane``'s default, so
    that what it is stays visible: in V0 ``MockBank`` holds its payments in the
    memory of the process that made them, and this process has made none. Every
    answer ``reconcile`` gives from here is therefore only as good as that book
    — which is the single largest caveat in ``docs/runbook.md`` and the one
    thing that changes when the bank becomes a service.
    """
    settings = settings_or_error()
    try:
        store = PgStore(settings.database_url)
    except Exception as exc:
        raise CliError(
            f"não consegui abrir o banco em {redact(settings.database_url)}: {exc}",
            hint="a stack está de pé? `make up` — e TRAIL_DATABASE_URL aponta "
            "para ela? (de fora do compose, o host é localhost)",
        ) from exc
    try:
        yield ControlPlane(bank=MockBank(), store=store)
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def operator_context(settings: Settings, channel: str) -> Context:
    """The principal these commands act as: this customer, a brand-new session.

    The session is new on every invocation and that is not a detail to work
    around — it is the fact being modelled. A step-up that arrives from the
    mobile app is *by definition* not the conversation's session, so binding
    ownership to the session would refuse every real callback. What identifies
    the intent here is the customer plus the intent id, and the intent id is a
    12-hex token that only the ledger and its owner have seen.

    A confirmation is the opposite and stays as it is: consent belongs to the
    conversation it was given in.
    """
    return Context(
        customer_id=settings.customer_id,
        session_id=new_id("cli"),
        channel=channel,  # type: ignore[arg-type]  # validated by Context
    )


def _banner(console: Console, settings: Settings) -> None:
    console.print(
        f"[meta]cliente[/] {settings.customer_id}  "
        f"[meta]banco[/] {redact(settings.database_url)}"
    )


def _render_outcome(console: Console, outcome: Outcome) -> None:
    """One outcome, printed for someone who has to decide what to do next."""
    style = "blocked" if outcome.status in ("DENY", "FAILED") else "ok"
    console.print(f"  [{style}]{outcome.status}[/] {outcome.intent_id or ''}")
    if outcome.message:
        console.print(f"  {outcome.message}")
    data = outcome.data
    if data.get("display"):
        console.print(
            f"  [meta]{data['display']} → {data.get('recipient', '?')}"
            f"  ·  estado {data.get('state', '?')}[/]"
        )
    receipt = data.get("receipt")
    if receipt:
        console.print(
            f"  [meta]recibo[/] {receipt.get('payment_id')} · "
            f"{receipt.get('end_to_end_id')} · {receipt.get('status')}"
        )


def step_up(intent_id: str) -> int:
    """Grant strong assurance to one intent, from outside the conversation.

    The simulated mobile callback. It does not confirm anything and cannot:
    all it does is raise the assurance on this one intent and make the plane
    evaluate policy again — which is why the customer still has to say yes, in
    the conversation, afterwards.
    """
    console = Console(theme=THEME)
    settings = settings_or_error()
    _banner(console, settings)
    with open_plane() as plane:
        outcome = plane.step_up(operator_context(settings, "mobile"), intent_id)
    _render_outcome(console, outcome)
    if outcome.status == "REQUIRE_CONFIRMATION":
        console.print(
            "  [meta]a autenticação foi registrada; a confirmação continua "
            "sendo do cliente, na conversa onde o PIX foi proposto[/]"
        )
    return 1 if outcome.status == "DENY" else 0


def intents() -> int:
    """List the intents that are waiting on a person, worst first."""
    console = Console(theme=THEME)
    settings = settings_or_error()
    _banner(console, settings)
    with open_plane() as plane:
        waiting = [
            intent
            for intent in plane.store.unsettled(ATTENTION_STATES)
            if intent.context.customer_id == settings.customer_id
        ]

    if not waiting:
        console.print("  [ok]nada aguardando ação[/]")
        return 0

    waiting.sort(key=lambda i: (ATTENTION_STATES.index(i.state), i.created_at))
    table = Table(box=None, pad_edge=False, header_style="meta")
    for column in ("intent", "estado", "valor", "destinatário", "criado (UTC)"):
        table.add_column(column)
    table.add_column("próximo passo")
    for intent in waiting:
        table.add_row(
            intent.id,
            Text(
                intent.state, style="blocked" if intent.state == "UNKNOWN" else "meta"
            ),
            brl(intent.action.amount),
            intent.action.recipient_id,
            intent.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            NEXT_STEP.get(intent.state, "—").format(id=intent.id),
        )
    console.print(table)
    unknown = sum(1 for i in waiting if i.state == "UNKNOWN")
    if unknown:
        console.print(
            f"  [blocked]{unknown} em UNKNOWN[/] [meta]— o banco sabe o que "
            "aconteceu e este processo não; ver docs/runbook.md[/]"
        )
    return 0


def reconcile(intent_id: str) -> int:
    """Resolve one ``UNKNOWN`` intent by asking the bank. Never re-pays.

    The exit code is the resolution, not the verdict: ``COMPLETED`` and
    ``FAILED`` are both successful reconciliations — the bank paid, or it never
    heard of the key — and only a refusal (an id this customer does not own, an
    intent that is not ``UNKNOWN``) is a non-zero exit.

    How many payments the bank being asked actually knows about is printed
    before the answer, and it is not decoration. In V0 the bank is a
    ``MockBank`` in the memory of whichever process made the payment, so the
    one this process just built knows about none of them: a ``FAILED`` from
    here means "this book has no record", which is only the same sentence as
    "the money did not move" once the bank is something both processes ask.
    Printing the count is what keeps those two sentences distinguishable at
    3am — see ``docs/runbook.md``.
    """
    console = Console(theme=THEME)
    settings = settings_or_error()
    _banner(console, settings)
    console.print(
        "  [meta]perguntando ao banco pela chave de idempotência; "
        "nenhum pagamento novo é feito por este comando[/]"
    )
    with open_plane() as plane:
        known = len(plane.bank.payments)
        style = "ok" if known else "blocked"
        console.print(
            f"  [meta]banco consultado:[/] {type(plane.bank).__name__} com "
            f"[{style}]{known} pagamento(s) conhecido(s)[/]"
        )
        outcome = plane.reconcile(operator_context(settings, "web"), intent_id)
    _render_outcome(console, outcome)
    return 1 if outcome.status == "DENY" else 0


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

    # The three below take no --base-url, and the absence is the design: they
    # do not know the agent exists. They reach TRAIL_DATABASE_URL instead.
    step = sub.add_parser(
        "step-up",
        help="grant strong assurance to one intent, out of band (the mobile "
        "callback, simulated)",
    )
    step.add_argument("intent_id")

    sub.add_parser(
        "intents", help="list the intents waiting on a person (needs the database)"
    )

    rec = sub.add_parser(
        "reconcile",
        help="resolve an UNKNOWN intent by asking the bank; never pays again",
    )
    rec.add_argument("intent_id")
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


#: The commands that reach the database instead of the agent. Synchronous
#: because ``PgStore`` is: LangChain runs `def` tools in a threadpool and the
#: plane stayed synchronous for that reason, so a client that wraps it in an
#: event loop would be adding a loop to have nothing to await.
OUT_OF_BAND = {
    "step-up": lambda args: step_up(args.intent_id),
    "intents": lambda args: intents(),
    "reconcile": lambda args: reconcile(args.intent_id),
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in OUT_OF_BAND:
            return OUT_OF_BAND[args.command](args)
        runner = {"chat": chat, "eval": evaluate, "health": health}[args.command]
        extra = {"concurrency": args.concurrency} if args.command == "eval" else {}
        return asyncio.run(runner(args.base_url, **extra))
    except CliError as exc:
        console = Console(theme=THEME, stderr=True)
        console.print(f"[blocked]erro[/] {exc}")
        if exc.hint:
            console.print(f"[meta]{exc.hint}[/]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
