"""The scorecard, in a terminal, with no build step.

The ordering is the argument. Violations print **first**, in red, before any
metric — a pass rate sitting above a fabricated fact reads as a mitigating
factor, and it is not one. Then the metrics, each beside the bar it was
measured against and where that bar came from. Then what regressed. Then the
footnotes that say which numbers are soft.

A metric with no comparator is printed as ``grade D`` rather than dressed up.
That is the whole reason the grades exist: a threshold with no stated source is
a guess wearing a number's clothes, and the alternative to admitting it is a
scorecard that looks equally confident about everything on it.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from trail.evals.cases import Finding
from trail.evals.metrics import Metric, RunReport
from trail.runtime.events import duration

GRADES = {
    "P": "publicado",
    "I": "medição interna",
    "V": "verificado aqui",
    "D": "declarado, sem comparador",
}

KIND_LABEL = {
    "OMISSION": "omissão",
    "FABRICATION": "invenção",
    "WRONG_PATH": "caminho errado",
    "ERROR": "erro",
}


def render_value(metric: Metric) -> str:
    """One metric's value at the scale a person reads it.

    ``—`` for a measurement that was not taken, never ``0`` and never ``100%``.
    Zero over zero is not a perfect score; it is an absence of evidence, and the
    two must not render the same.
    """
    if metric.value is None:
        return "—"
    if metric.unit == "rate":
        return f"{metric.value * 100:.1f}%"
    if metric.unit == "ns":
        return duration(int(metric.value))
    return f"US$ {metric.value:.4f}"


def _bar(metric: Metric) -> str:
    if metric.threshold is None:
        return ""
    bar = metric.threshold
    if metric.unit == "rate":
        return f"{bar.direction} {bar.value * 100:.0f}%"
    if metric.unit == "ns":
        return f"{bar.direction} {duration(int(bar.value))}"
    return f"{bar.direction} US$ {bar.value:.4f}"


def render(
    console: Console,
    report: RunReport,
    *,
    agent: str,
    model: str,
    guardrails: str,
    run_id: int | None = None,
) -> None:
    """Print the whole scorecard."""
    header = (
        f"[meta]agente[/] {agent}  [meta]modelo[/] {model}  "
        f"[meta]guardrails[/] {guardrails}  "
        f"[meta]golden set[/] {report.golden_set_version}"
    )
    if run_id is not None:
        header += f"  [meta]run[/] #{run_id}"
    console.print()
    console.print(header)

    status_style = "ok" if report.status == "COMPLETED" else "blocked"
    console.print(f"  [{status_style}]{report.status}[/]")
    console.print()

    _violations(console, report)
    _metrics(console, report)
    _taxonomy(console, report)
    _regressions(console, report)
    _judge(console, report)
    _footnotes(console, report)


def _violations(console: Console, report: RunReport) -> None:
    """The zero-tolerance tier, first and in red."""
    if not report.violations:
        return
    console.print("  [blocked]violações — nenhuma métrica compensa estas[/]")
    for finding in report.violations:
        console.print(
            f"    [blocked]✗[/] {finding.case_id} · {finding.check} · {finding.detail}"
        )
        console.print(f"      [meta]{finding.actual}[/]")
    console.print()


def _metrics(console: Console, report: RunReport) -> None:
    table = Table(box=None, pad_edge=False, show_edge=False)
    table.add_column("", width=2)
    table.add_column("métrica")
    table.add_column("valor", justify="right")
    table.add_column("n", justify="right", style="dim")
    table.add_column("limiar", justify="right")
    table.add_column("origem do limiar", style="dim")

    for metric in report.metrics:
        if metric.threshold is None:
            mark, style = " ", "dim"
        elif metric.clears:
            mark, style = "▪", "ok"
        else:
            mark, style = "✗", "blocked"
        name = metric.name + (" ‡" if metric.judged else "")
        fraction = (
            f"{metric.numerator}/{metric.denominator}"
            if metric.numerator is not None and metric.denominator is not None
            else (str(metric.denominator) if metric.denominator else "")
        )
        source = ""
        if metric.threshold is not None:
            source = metric.threshold.comparator or GRADES[metric.threshold.grade]
            source = f"{source} [{metric.threshold.grade}]"
        table.add_row(
            Text(mark, style=style),
            name,
            render_value(metric),
            fraction,
            _bar(metric),
            source,
        )
    console.print(table)
    console.print()


def _taxonomy(console: Console, report: RunReport) -> None:
    """Counts per kind, then the failing cases.

    Never a single failure count: a run that omitted three answers and a run
    that invented three settings score the same total and are not the same run.
    """
    if not report.findings:
        console.print("  [ok]nenhum achado[/]")
        console.print()
        return

    counts: dict[str, int] = {}
    for finding in report.findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    summary = " · ".join(
        f"{KIND_LABEL.get(kind, kind)} {count}"
        for kind, count in sorted(counts.items())
    )
    console.print(f"  [meta]taxonomia[/] {summary}")

    for outcome in report.outcomes:
        if outcome.passed:
            continue
        console.print(f"  [blocked]✗[/] {outcome.case_id}")
        for finding in outcome.findings:
            console.print(f"      {_finding_line(finding)}")
    console.print()


def _finding_line(finding: Finding) -> str:
    # The source marker is not decoration: `judge` means a model's opinion, and
    # a reader deciding whether to act on a finding needs to know which it is.
    mark = "[meta]juiz[/]" if finding.source == "judge" else "[meta]check[/]"
    return (
        f"{mark} {KIND_LABEL.get(finding.kind, finding.kind)} · "
        f"esperado {finding.expected!r} · {finding.detail}"
    )


def _regressions(console: Console, report: RunReport) -> None:
    if report.baseline_id is None:
        console.print("  [meta]sem baseline: primeira execução deste golden set[/]")
        console.print()
        return
    if not report.regressions:
        console.print(f"  [ok]sem regressão[/] [meta]vs run #{report.baseline_id}[/]")
        console.print()
        return

    console.print(f"  [blocked]regressões[/] [meta]vs run #{report.baseline_id}[/]")
    for regression in report.regressions:
        note = " (cruzou o limiar)" if regression.crossed_threshold else ""
        # A judged metric can move between identical runs. Saying so beside the
        # delta is cheaper than a reader chasing a regression the grader
        # invented.
        note += " [meta]‡ métrica julgada, move sozinha[/]" if regression.noisy else ""
        console.print(
            f"    {regression.metric}: {regression.baseline:.4g} → "
            f"{regression.current:.4g}{note}"
        )
    console.print()


def _judge(console: Console, report: RunReport) -> None:
    ledger = report.judge
    if ledger is None or ledger.calls == 0:
        return
    cost = f"US$ {ledger.cost_usd:.4f}" if ledger.cost_usd is not None else "—"
    console.print(
        f"  [meta]juiz[/] {ledger.model} · {ledger.calls} chamadas · "
        f"{ledger.input_tokens} in · {ledger.output_tokens} out · {cost}"
        f"  [meta](fora do custo do agente)[/]"
    )
    if ledger.self_evaluating:
        console.print(
            "  [blocked]⚠ auto-avaliação[/] o juiz é o próprio modelo do agente; "
            "defina TRAIL_JUDGE_MODEL para um grader independente"
        )
    console.print()


def _footnotes(console: Console, report: RunReport) -> None:
    notes = [
        "denominador de case_pass_rate é o conjunto inteiro, casos que quebraram "
        "inclusive — dividir pelos que responderam é como uma taxa de automação "
        "esconde as ligações que ninguém atendeu",
        "invenção e falso bloqueio reprovam a execução sozinhos; uma execução "
        "FAILED não pode servir de baseline",
    ]
    if any(metric.judged for metric in report.metrics):
        notes.append(
            "‡ métrica com achados de juiz: um modelo opinou, o número pode se "
            "mover entre execuções idênticas"
        )
    console.print("  [meta]" + "\n  ".join(f"· {note}" for note in notes) + "[/]")
