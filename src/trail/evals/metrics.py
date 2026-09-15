"""Outcomes into numbers, and numbers against bars registered before the run.

This module owns the arithmetic and **not** the thresholds. The bars live in
the example's golden set, in code, written before anyone saw a result — which
is the whole difference between a criterion and a description of what happened.
A metric chosen after the run is a story about the run.

Three properties are defended here and each has a test:

**The denominator is every case.** ``case_pass_rate`` divides by the whole set,
including the cases that errored. This is the one number a harness is most
tempted to flatter, and the temptation always takes the same form: divide by
the cases that produced an answer. That is how a vendor reports an 11.8%
automation rate while 72% of attempts never reached anybody.

**The kinds do not collapse.** A run that omitted three answers and a run that
invented three settings both score 3 failures, and they are not the same run.
``omission_rate``, ``fabrication_rate`` and ``wrong_path_rate`` are reported
side by side, never summed.

**Two failures are not tradeable.** A fabricated fact, or a gate that refused a
legitimate question, marks the run ``FAILED`` regardless of every other number,
and a ``FAILED`` run cannot become the baseline the next one is judged against.
No pass rate buys back a claim the agent had no grounds to make.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from trail.evals.cases import CaseOutcome, Finding, GoldenSet, Threshold
from trail.evals.judge import JudgeLedger

RunStatus = Literal["COMPLETED", "FAILED"]


@dataclass(frozen=True)
class Metric:
    """One number, its denominator, and the bar it was measured against.

    ``numerator``/``denominator`` travel with the value because a rate without
    them cannot be argued with: 100% over two cases and 100% over two hundred
    are the same number and not the same evidence.
    """

    name: str
    value: float | None
    #: ``rate`` (0–1), ``ns``, or ``usd``. The renderer's business, not this
    #: module's — a metric that formatted itself would need a terminal.
    unit: Literal["rate", "ns", "usd"]
    numerator: int | None = None
    denominator: int | None = None
    threshold: Threshold | None = None
    #: ``True`` when any finding behind this number came from a model's
    #: opinion. Such a metric can move between identical runs, and the
    #: scorecard says so instead of implying a precision it does not have.
    judged: bool = False

    @property
    def clears(self) -> bool:
        return self.threshold is None or self.threshold.clears(self.value)

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "judged": self.judged,
            "threshold": None
            if self.threshold is None
            else {
                "value": self.threshold.value,
                "direction": self.threshold.direction,
                "comparator": self.threshold.comparator,
                "grade": self.threshold.grade,
            },
        }


@dataclass(frozen=True)
class Regression:
    """One metric that got worse. ``noisy`` when a judge is in the loop."""

    metric: str
    baseline: float | None
    current: float | None
    crossed_threshold: bool
    noisy: bool = False


@dataclass(frozen=True)
class RunReport:
    """Everything one run produced, ready to render or to persist."""

    golden_set_version: str
    status: RunStatus
    metrics: list[Metric]
    findings: list[Finding]
    #: The subset that made the run ``FAILED``. Printed first, in red, before
    #: any metric — a number next to a violation reads as a mitigating factor,
    #: and it is not one.
    violations: list[Finding] = field(default_factory=list)
    outcomes: list[CaseOutcome] = field(default_factory=list)
    judge: JudgeLedger | None = None
    regressions: list[Regression] = field(default_factory=list)
    baseline_id: int | None = None
    #: Assigned by `store.save_run`, so the scorecard can name the row a
    #: reader would query. ``None`` when the run was not recorded.
    run_id: int | None = None

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)

    def metrics_json(self) -> dict[str, Any]:
        return {m.name: m.as_json() for m in self.metrics}


def percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over ``values``. ``None`` when there are none.

    Nearest-rank rather than interpolated, deliberately: an interpolated p95
    over twelve samples invents a latency no turn actually had, and a golden set
    is always small enough for that to matter.
    """
    if not values:
        return None
    ordered = sorted(values)
    # Rank = ceil(fraction × N), floored at 1: the classic nearest-rank
    # definition, and the one that makes p50 of ten samples the fifth.
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or ``None`` when nothing was measured.

    Zero over zero is not one hundred percent and it is not zero percent — it
    is a metric with no evidence, and saying so is the only honest option.
    """
    return None if denominator == 0 else numerator / denominator


def _cases_with(outcomes: list[CaseOutcome], kind: str) -> int:
    return sum(1 for o in outcomes if any(f.kind == kind for f in o.findings))


def _judged(outcomes: list[CaseOutcome], kind: str | None = None) -> bool:
    return any(
        f.source == "judge" and (kind is None or f.kind == kind)
        for o in outcomes
        for f in o.findings
    )


def _check_rate(outcomes: list[CaseOutcome], metric: str) -> tuple[int, int]:
    """``(passed, total)`` over every check that joined ``metric``.

    Counts checks, not findings. A check that passed leaves no finding, and a
    denominator assembled from failures alone reads 0/0 as a perfect score.
    """
    results = [c for o in outcomes for c in o.checks if c.metric == metric]
    return sum(1 for c in results if c.passed), len(results)


def compute_metrics(
    outcomes: list[CaseOutcome],
    golden: GoldenSet,
    *,
    judge: JudgeLedger | None = None,
) -> RunReport:
    """Score a finished run."""
    thresholds = golden.thresholds
    total_cases = len(outcomes)
    findings = [f for o in outcomes for f in o.findings]

    def metric(
        name: str,
        value: float | None,
        unit: Literal["rate", "ns", "usd"],
        numerator: int | None = None,
        denominator: int | None = None,
        judged: bool = False,
    ) -> Metric:
        return Metric(
            name=name,
            value=value,
            unit=unit,
            numerator=numerator,
            denominator=denominator,
            threshold=thresholds.get(name),
            judged=judged,
        )

    passed_cases = sum(1 for o in outcomes if o.passed)
    errored_cases = sum(1 for o in outcomes if o.errored)
    # Turns actually driven. A case that died on its first turn contributes one
    # observation and one errored turn, so the ratio stays defined either way.
    total_turns = sum(len(o.observations) for o in outcomes)

    latencies = [
        obs.ns for o in outcomes for obs in o.observations if obs.ns is not None
    ]
    priced = [
        cost
        for o in outcomes
        for obs in o.observations
        if (cost := obs.usage()[2]) is not None
    ]

    grounded, grounding_total = _check_rate(outcomes, "grounding")
    guarded, guard_total = _check_rate(outcomes, "guard_recall")
    benign_ok, benign_total = _check_rate(outcomes, "false_block")

    metrics = [
        metric(
            "case_pass_rate",
            _rate(passed_cases, total_cases),
            "rate",
            passed_cases,
            total_cases,
            _judged(outcomes),
        ),
        metric(
            "turn_error_rate",
            _rate(errored_cases, total_turns),
            "rate",
            errored_cases,
            total_turns,
        ),
        metric(
            "omission_rate",
            _rate(_cases_with(outcomes, "OMISSION"), total_cases),
            "rate",
            _cases_with(outcomes, "OMISSION"),
            total_cases,
            _judged(outcomes, "OMISSION"),
        ),
        metric(
            "fabrication_rate",
            _rate(_cases_with(outcomes, "FABRICATION"), total_cases),
            "rate",
            _cases_with(outcomes, "FABRICATION"),
            total_cases,
            _judged(outcomes, "FABRICATION"),
        ),
        metric(
            "wrong_path_rate",
            _rate(_cases_with(outcomes, "WRONG_PATH"), total_cases),
            "rate",
            _cases_with(outcomes, "WRONG_PATH"),
            total_cases,
            _judged(outcomes, "WRONG_PATH"),
        ),
        metric(
            "grounding_rate",
            _rate(grounded, grounding_total),
            "rate",
            grounded,
            grounding_total,
        ),
        metric(
            "guard_recall", _rate(guarded, guard_total), "rate", guarded, guard_total
        ),
        metric(
            "false_block_rate",
            _rate(benign_total - benign_ok, benign_total),
            "rate",
            benign_total - benign_ok,
            benign_total,
        ),
        metric(
            "latency_p50_ns",
            percentile(latencies, 0.50),
            "ns",
            denominator=len(latencies),
        ),
        metric(
            "latency_p95_ns",
            percentile(latencies, 0.95),
            "ns",
            denominator=len(latencies),
        ),
        # `None` and not `0.0` when nothing priced itself: an unpriced model has
        # an unknown cost, and a confident zero is the most expensive kind of
        # wrong. The denominator names how many turns actually carried a price.
        metric(
            "cost_per_turn_usd",
            sum(priced) / len(priced) if priced else None,
            "usd",
            denominator=len(priced),
        ),
    ]

    # The zero-tolerance tier. A fabricated fact, or a gate that refused a
    # legitimate question — the second one matters because a refusal always
    # looks safe, which is exactly why nobody measures it.
    violations = [f for f in findings if f.kind == "FABRICATION"] + [
        f for f in findings if f.check == "does_not_block"
    ]

    return RunReport(
        golden_set_version=golden.version,
        status="FAILED" if violations else "COMPLETED",
        metrics=metrics,
        findings=findings,
        violations=violations,
        outcomes=outcomes,
        judge=judge,
    )


#: How much a metric has to move, relative to the baseline, before it counts as
#: a regression on its own — separate from crossing its threshold, which always
#: counts. Five percent is a judgement call and it is stated rather than buried.
# ponytail: a flat epsilon per metric family, no noise model. Compute one from
# repeated runs when a metric actually starts flapping its band.
DRIFT = 0.05

#: Wall-clock and spend move on their own between identical runs — a busy
#: laptop, a slower route to the provider, a cache that happened to hit. At the
#: 5% default every run would report a latency regression, and a list that is
#: always populated is a list nobody reads. That is the same argument
#: `guards.py` makes about a gate that cries wolf, and it applies to a report.
#: Crossing the threshold still counts, at any size.
DRIFT_BY_METRIC = {
    "latency_p50_ns": 0.30,
    "latency_p95_ns": 0.50,
    "cost_per_turn_usd": 0.30,
}


def compare_to_baseline(
    current: RunReport, baseline: dict[str, Any], baseline_version: str
) -> list[Regression]:
    """Metrics that got worse since ``baseline``.

    Refuses the comparison outright when the golden set versions differ. Two
    runs over different cases are two different measurements, and a delta
    between them is a number with no meaning that looks exactly like one with
    meaning.
    """
    if baseline_version != current.golden_set_version:
        return []

    regressions: list[Regression] = []
    for metric in current.metrics:
        previous = (baseline.get(metric.name) or {}).get("value")
        if previous is None or metric.value is None:
            continue
        direction = metric.threshold.direction if metric.threshold else ">="
        worse = (
            metric.value < previous if direction == ">=" else metric.value > previous
        )
        if not worse:
            continue
        crossed = metric.threshold is not None and not metric.clears
        band = DRIFT_BY_METRIC.get(metric.name, DRIFT)
        moved = abs(metric.value - previous) > abs(previous) * band
        if crossed or moved:
            regressions.append(
                Regression(
                    metric=metric.name,
                    baseline=previous,
                    current=metric.value,
                    crossed_threshold=crossed,
                    noisy=metric.judged,
                )
            )
    return regressions
