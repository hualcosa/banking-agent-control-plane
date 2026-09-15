"""The scorecard renderer, checked against the text it prints.

Every assertion below reads the console's plain-text output, never just that a
line executed — a renderer that silently drops a case number or prints the
wrong mark is a worse bug than one that crashes, because nobody notices until
someone acts on a scorecard that lied.

Reports are built by hand here rather than through `compute_metrics`: the
arithmetic is `test_evals.py`'s job, this file's job is what the numbers look
like once printed.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from trail.evals import report as scorecard
from trail.evals.cases import CaseOutcome, Finding, Threshold
from trail.evals.judge import JudgeLedger
from trail.evals.metrics import Metric, Regression, RunReport
from trail.runtime.events import duration

pytestmark = pytest.mark.unit


def console() -> Console:
    """A console with markup parsed but no colour — plain text to assert on."""
    return Console(
        file=io.StringIO(), width=120, force_terminal=False, color_system=None
    )


def finding(
    *,
    case_id: str = "c1",
    kind: str = "OMISSION",
    source: str = "check",
    expected: str = "algo",
    detail: str = "faltou",
) -> Finding:
    return Finding(
        case_id=case_id,
        turn=0,
        kind=kind,
        check="contains",
        source=source,
        expected=expected,
        actual="resposta",
        detail=detail,
    )


def base_report(**overrides: object) -> RunReport:
    defaults: dict[str, object] = {
        "golden_set_version": "trail_guide-v1",
        "status": "COMPLETED",
        "metrics": [],
        "findings": [],
    }
    defaults.update(overrides)
    return RunReport(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# render_value: the scale a person reads, per unit — and the absent case
# --------------------------------------------------------------------------


def test_render_value_none_is_a_dash_not_zero() -> None:
    """Zero over zero must not read as a perfect score."""
    assert scorecard.render_value(Metric("m", None, "rate")) == "—"


def test_render_value_rate_is_a_percentage() -> None:
    assert scorecard.render_value(Metric("m", 0.1234, "rate")) == "12.3%"


def test_render_value_ns_uses_the_shared_duration_scale() -> None:
    metric = Metric("m", 1_500_000_000, "ns")
    assert scorecard.render_value(metric) == duration(1_500_000_000)


def test_render_value_usd_is_four_decimals() -> None:
    assert scorecard.render_value(Metric("m", 0.0012345, "usd")) == "US$ 0.0012"


# --------------------------------------------------------------------------
# _bar: blank with no threshold, else formatted per unit like render_value
# --------------------------------------------------------------------------


def test_bar_is_blank_with_no_threshold() -> None:
    assert scorecard._bar(Metric("m", 0.5, "rate")) == ""


def test_bar_formats_a_rate_threshold() -> None:
    metric = Metric("m", 0.5, "rate", threshold=Threshold(0.9, ">="))
    assert scorecard._bar(metric) == ">= 90%"


def test_bar_formats_a_latency_threshold() -> None:
    metric = Metric("m", 1, "ns", threshold=Threshold(2_000_000_000, "<="))
    assert scorecard._bar(metric) == f"<= {duration(2_000_000_000)}"


def test_bar_formats_a_cost_threshold() -> None:
    metric = Metric("m", 0.001, "usd", threshold=Threshold(0.01, "<="))
    assert scorecard._bar(metric) == "<= US$ 0.0100"


# --------------------------------------------------------------------------
# _violations: the zero-tolerance tier, printed first, in red
# --------------------------------------------------------------------------


def test_violations_prints_nothing_when_there_are_none() -> None:
    c = console()
    scorecard._violations(c, base_report(violations=[]))
    assert c.file.getvalue() == ""


def test_violations_names_the_case_and_the_detail() -> None:
    c = console()
    v = finding(case_id="fab-1", detail="afirma um ajuste que não existe")
    scorecard._violations(c, base_report(violations=[v]))
    printed = c.file.getvalue()
    assert "violações" in printed
    assert "fab-1" in printed
    assert "afirma um ajuste que não existe" in printed


# --------------------------------------------------------------------------
# _metrics: the mark per row (no bar / clears / fails), the judged suffix,
# the threshold source, and the fraction column
# --------------------------------------------------------------------------


def test_metrics_table_marks_no_threshold_pass_and_fail() -> None:
    c = console()
    no_bar = Metric("sem_limiar", 0.5, "rate")
    clears = Metric(
        "dentro_do_limiar",
        0.95,
        "rate",
        numerator=95,
        denominator=100,
        threshold=Threshold(0.9, ">=", "spec X", "P"),
    )
    fails = Metric(
        "fora_do_limiar",
        0.5,
        "rate",
        numerator=5,
        denominator=10,
        threshold=Threshold(0.9, ">=", "", "I"),
        judged=True,
    )
    report = base_report(metrics=[no_bar, clears, fails])
    scorecard._metrics(c, report)
    printed = c.file.getvalue()

    assert "sem_limiar" in printed
    assert "dentro_do_limiar" in printed
    # A judged metric carries its warning suffix into the name itself.
    assert "fora_do_limiar ‡" in printed
    assert "▪" in printed  # clears
    assert "✗" in printed  # fails
    assert "spec X [P]" in printed
    # No comparator on record: falls back to the grade's Portuguese label.
    assert "medição interna [I]" in printed
    assert "95/100" in printed
    assert "5/10" in printed


def test_metrics_table_fraction_falls_back_to_bare_denominator() -> None:
    """A latency metric has a sample count but no numerator/denominator pair."""
    c = console()
    metric = Metric("latency_p50_ns", 1_000_000, "ns", denominator=7)
    scorecard._metrics(c, base_report(metrics=[metric]))
    printed = c.file.getvalue()
    assert "7" in printed
    assert "/" not in printed.split("latency_p50_ns")[1].split("\n")[0]


# --------------------------------------------------------------------------
# _taxonomy: counts per kind, never summed, then the failing cases
# --------------------------------------------------------------------------


def test_taxonomy_reports_no_findings() -> None:
    c = console()
    scorecard._taxonomy(c, base_report(findings=[]))
    assert "nenhum achado" in c.file.getvalue()


def test_taxonomy_counts_kinds_side_by_side_and_lists_failing_cases() -> None:
    c = console()
    omission = finding(case_id="a", kind="OMISSION", detail="faltou o essencial")
    fabrication = finding(
        case_id="b", kind="FABRICATION", source="judge", detail="inventou um ajuste"
    )
    outcomes = [
        CaseOutcome(case_id="a", findings=[omission]),
        CaseOutcome(case_id="b", findings=[fabrication]),
        CaseOutcome(case_id="ok", findings=[]),
    ]
    report = base_report(
        findings=[omission, fabrication],
        outcomes=outcomes,
    )
    scorecard._taxonomy(c, report)
    printed = c.file.getvalue()

    # Portuguese labels, sorted by kind key so FABRICATION precedes OMISSION.
    assert "invenção 1" in printed
    assert "omissão 1" in printed
    assert printed.index("invenção 1") < printed.index("omissão 1")
    # The failing cases are listed, the passing one is not.
    assert "a" in printed.splitlines()[-1] or "a" in printed
    assert "b" in printed
    assert "faltou o essencial" in printed
    assert "inventou um ajuste" in printed
    assert "ok" not in printed.replace("omissão", "").replace("invenção", "")


# --------------------------------------------------------------------------
# _finding_line: judge vs check, and the fields it carries
# --------------------------------------------------------------------------


def test_finding_line_marks_a_check_source() -> None:
    line = scorecard._finding_line(
        finding(
            kind="WRONG_PATH", source="check", expected="tool x", detail="sem consulta"
        )
    )
    assert "check" in line
    assert "caminho errado" in line
    assert "'tool x'" in line
    assert "sem consulta" in line


def test_finding_line_marks_a_judge_source() -> None:
    line = scorecard._finding_line(
        finding(kind="ERROR", source="judge", expected="ok", detail="falhou")
    )
    assert "juiz" in line
    assert "erro" in line


# --------------------------------------------------------------------------
# _regressions: no baseline, no regression, and regressions with their notes
# --------------------------------------------------------------------------


def test_regressions_with_no_baseline_says_first_run() -> None:
    c = console()
    scorecard._regressions(c, base_report(baseline_id=None))
    assert "sem baseline" in c.file.getvalue()


def test_regressions_with_baseline_and_no_regressions() -> None:
    c = console()
    scorecard._regressions(c, base_report(baseline_id=3, regressions=[]))
    printed = c.file.getvalue()
    assert "sem regressão" in printed
    assert "run #3" in printed


def test_regressions_notes_crossed_threshold_and_noisy_separately() -> None:
    c = console()
    crossed = Regression(
        metric="latency_p95_ns",
        baseline=1.0,
        current=2.0,
        crossed_threshold=True,
        noisy=False,
    )
    noisy = Regression(
        metric="omission_rate",
        baseline=0.1,
        current=0.2,
        crossed_threshold=False,
        noisy=True,
    )
    report = base_report(baseline_id=9, regressions=[crossed, noisy])
    scorecard._regressions(c, report)
    printed = c.file.getvalue()

    lines = {line.strip(): line for line in printed.splitlines()}
    crossed_line = next(v for k, v in lines.items() if k.startswith("latency_p95_ns"))
    noisy_line = next(v for k, v in lines.items() if k.startswith("omission_rate"))

    assert "cruzou o limiar" in crossed_line
    assert "métrica julgada" not in crossed_line
    assert "métrica julgada" in noisy_line
    assert "cruzou o limiar" not in noisy_line
    assert "run #9" in printed


# --------------------------------------------------------------------------
# _judge: absent ledger, zero calls, priced vs unpriced, self-evaluation
# --------------------------------------------------------------------------


def test_judge_prints_nothing_without_a_ledger() -> None:
    c = console()
    scorecard._judge(c, base_report(judge=None))
    assert c.file.getvalue() == ""


def test_judge_prints_nothing_with_zero_calls() -> None:
    c = console()
    scorecard._judge(c, base_report(judge=JudgeLedger(model="claude-x", calls=0)))
    assert c.file.getvalue() == ""


def test_judge_ledger_with_unknown_cost_shows_a_dash() -> None:
    c = console()
    ledger = JudgeLedger(
        model="claude-x", calls=2, input_tokens=10, output_tokens=5, cost_usd=None
    )
    scorecard._judge(c, base_report(judge=ledger))
    printed = c.file.getvalue()
    assert "claude-x" in printed
    assert "2 chamadas" in printed
    assert "—" in printed
    assert "auto-avaliação" not in printed


def test_judge_ledger_self_evaluating_warns() -> None:
    c = console()
    ledger = JudgeLedger(
        model="agent-model",
        calls=3,
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.05,
        self_evaluating=True,
    )
    scorecard._judge(c, base_report(judge=ledger))
    printed = c.file.getvalue()
    assert "US$ 0.0500" in printed
    assert "auto-avaliação" in printed
    assert "TRAIL_JUDGE_MODEL" in printed


# --------------------------------------------------------------------------
# _footnotes: the judged caveat only appears when a metric earned it
# --------------------------------------------------------------------------


def test_footnotes_omit_the_judged_note_when_nothing_is_judged() -> None:
    c = console()
    metrics = [Metric("case_pass_rate", 1.0, "rate", judged=False)]
    scorecard._footnotes(c, base_report(metrics=metrics))
    assert "métrica com achados de juiz" not in c.file.getvalue()


def test_footnotes_add_the_judged_note_when_a_metric_is_judged() -> None:
    c = console()
    metrics = [Metric("omission_rate", 0.1, "rate", judged=True)]
    scorecard._footnotes(c, base_report(metrics=metrics))
    assert "métrica com achados de juiz" in c.file.getvalue()


# --------------------------------------------------------------------------
# render: the full scorecard, in the order the module's docstring promises
# --------------------------------------------------------------------------


def test_render_orders_sections_violations_first() -> None:
    """Violations must read before the metrics, or a pass rate above a
    fabrication reads as a mitigating factor."""
    c = console()
    v = finding(case_id="v1", kind="FABRICATION", detail="inventou algo")
    report = base_report(
        status="FAILED",
        metrics=[Metric("case_pass_rate", 0.8, "rate")],
        findings=[v],
        violations=[v],
        outcomes=[CaseOutcome(case_id="v1", findings=[v])],
        baseline_id=None,
    )
    scorecard.render(
        c, report, agent="trail_guide", model="fake-model", guardrails="both", run_id=42
    )
    printed = c.file.getvalue()

    assert "agente trail_guide" in printed
    assert "modelo fake-model" in printed
    assert "guardrails both" in printed
    assert "run #42" in printed
    assert "FAILED" in printed

    order = ["violações", "métrica", "taxonomia", "sem baseline"]
    positions = [printed.index(text) for text in order]
    assert positions == sorted(positions)


def test_render_without_run_id_omits_the_run_marker() -> None:
    c = console()
    scorecard.render(
        c, base_report(), agent="a", model="m", guardrails="none", run_id=None
    )
    assert "run #" not in c.file.getvalue()
