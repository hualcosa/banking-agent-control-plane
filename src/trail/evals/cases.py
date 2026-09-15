"""What a case is, what a turn looked like, and what went wrong with it.

The shape here is `middleware/guards.py`'s, one level up. There, a check is a
pure function from text to a verdict and the gate that runs it does not know
what it checks. Here, a check is a function from one :class:`Observation` to a
list of :class:`Finding`, and the runner that applies it does not know whether
it is a regex or a language model.

That is the whole reason the two kinds compose. A deterministic check is a
plain synchronous function — no network, no key, unit-testable offline. A judge
check (`evals/judge.py`) is a coroutine that calls a model. The runner awaits
only what is awaitable, so neither has to pretend to be the other, and a
finding from a substring test and a finding from a model's opinion land in the
same taxonomy with the same fields. A case declares whichever it wants: one,
the other, or both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

#: The taxonomy. Four kinds, scored separately and never collapsed into a
#: single pass/fail — a run that omits three answers and a run that invents
#: three settings are not the same run, and one number cannot say which
#: happened. `FABRICATION` is the zero-tolerance one: see `metrics.py`.
FindingKind = Literal["OMISSION", "FABRICATION", "WRONG_PATH", "ERROR"]

#: Whether a finding came from a deterministic check or from a model's opinion.
#: On the wire and in the database, so a reader can weigh the two differently
#: without going back to read the case.
Source = Literal["check", "judge"]


@dataclass(frozen=True)
class Finding:
    """One thing that was wrong with one turn.

    ``expected`` and ``actual`` are strings rather than a typed union for the
    same reason ``StageEvent.detail`` is an open dict: a substring check, a
    tool-call check and a judge have nothing in common to type, and closing the
    shape would mean a schema change before a new kind of check can say
    anything about itself.
    """

    case_id: str
    turn: int
    kind: FindingKind
    check: str
    source: Source
    expected: str
    actual: str
    detail: str = ""


@dataclass(frozen=True)
class Observation:
    """One turn, exactly as it came off the wire.

    Nothing here is computed by the agent for the harness's benefit. These are
    the four frame types ``runtime/turns.py`` already yields, collected: the
    ``stage`` frames in arrival order, the ``turn`` frame's text and wall time,
    the ``error`` frame if there was one, the ``trace`` link. A harness that
    needed a field the CLI does not get would be measuring a different system.
    """

    case_id: str
    turn: int
    question: str
    answer: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    ns: int | None = None
    trace_url: str | None = None

    def tools_called(self) -> set[str]:
        """Tool names that actually ran, from the ``tool:`` stage frames."""
        return {
            stage["name"].removeprefix("tool:")
            for stage in self.stages
            if stage.get("kind") == "tool" and stage.get("status") == "done"
        }

    def blocked_by(self) -> set[str]:
        """Names of the gates that fired on this turn.

        ``status == "blocked"`` and not "a guard frame exists": a gate that ran
        and passed emits a frame too, and so does one the dial switched off.
        Reading presence instead of status is how a harness ends up reporting
        that every guardrail fires on every turn.
        """
        return {
            stage["name"]
            for stage in self.stages
            if stage.get("kind") in ("guard_in", "guard_out")
            and stage.get("status") == "blocked"
        }

    def usage(self) -> tuple[int, int, float | None]:
        """``(input_tokens, output_tokens, cost_usd)`` summed over model calls.

        Cost is ``None`` when no completed model call priced itself, never
        ``0.0`` — the same rule ``costs.py`` applies to an unpriced model, for
        the same reason: a confident zero is the most expensive kind of wrong,
        and a turn the input gate refused genuinely has no model cost to report.
        """
        tokens_in = tokens_out = 0
        cost: float | None = None
        for stage in self.stages:
            if stage.get("kind") != "model" or stage.get("status") != "done":
                continue
            detail = stage.get("detail") or {}
            tokens_in += detail.get("input_tokens") or 0
            tokens_out += detail.get("output_tokens") or 0
            if detail.get("cost_usd") is not None:
                cost = (cost or 0.0) + detail["cost_usd"]
        return tokens_in, tokens_out, cost


#: What a check does. Sync or async, and the runner does not care which — see
#: this module's docstring.
Runner = Callable[[Observation], "list[Finding] | Awaitable[list[Finding]]"]


@dataclass(frozen=True)
class Check:
    """A named assertion about one turn.

    ``metric`` is the load-bearing field and it exists so that `metrics.py`
    stays pure arithmetic. A rate needs a denominator, and "how many cases
    required a tool call" is knowable only from the checks a case declared. The
    check carries that itself rather than making the metrics module introspect
    case bodies to guess.
    """

    name: str
    run: Runner
    #: Which metric's denominator this check joins: ``grounding``,
    #: ``guard_recall``, or ``""`` for one that only feeds the taxonomy.
    metric: str = ""


@dataclass(frozen=True)
class CheckResult:
    """Whether one check passed, kept even when it did.

    Findings alone cannot produce a rate: they are the numerator. A check that
    passed leaves no finding and would leave no trace, and a denominator
    assembled only from failures is how a metric ends up reading 0/0 = 100%.
    """

    name: str
    metric: str
    passed: bool


@dataclass(frozen=True)
class Case:
    """One scripted conversation, and what must be true along and at the end of it.

    ``turns`` is a list because context matters — the second question is often
    only interesting after the first. ``checks`` run against the **last** turn,
    which is the common case: earlier turns exist to build the context the final
    question is asked in.

    ``turn_checks`` is the escape hatch for the conversations where that is not
    true. A two-turn case that must not claim a transfer was sent *before* the
    confirmation is a real assertion about turn 0, and splitting it into two
    cases loses the only thing that made it interesting — that the same thread
    kept its head between the two questions. Keyed by turn index so the check
    sits next to the number of the turn it judges, and so a case can assert on
    several turns without the reader counting positions in a parallel list.
    """

    id: str
    turns: Sequence[str]
    checks: Sequence[Check] = ()
    #: Turn index → the checks that turn must pass, applied as that turn
    #: completes. A turn with no entry is asserted on by nothing, exactly as
    #: before; ``checks`` is unaffected and still judges the last turn.
    turn_checks: Mapping[int, Sequence[Check]] = field(default_factory=dict)
    #: Free text, printed next to a failure. Why this case is in the set at
    #: all, which is the thing nobody remembers six months later.
    note: str = ""

    def checks_for(self, turn: int) -> Sequence[Check]:
        """The per-turn checks declared for ``turn``, if any."""
        return self.turn_checks.get(turn, ())

    def all_checks(self) -> list[Check]:
        """Every check the case declares, per-turn ones included.

        The denominator an errored case still owes — see `runner`'s second rule.
        """
        return [*self.checks, *(c for turn in self.turn_checks.values() for c in turn)]


@dataclass(frozen=True)
class CaseOutcome:
    """What one case did. Never an exception — see :func:`runner.run_case`."""

    case_id: str
    observations: list[Observation] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    @property
    def errored(self) -> bool:
        return any(f.kind == "ERROR" for f in self.findings)


@dataclass(frozen=True)
class GoldenSet:
    """A version-stamped set of cases, plus the bars they are judged against.

    The version pins both halves of a comparison. A baseline run scored against
    a different set of cases is not a baseline; `metrics.compare_to_baseline`
    refuses that comparison rather than printing a delta between two different
    measurements.
    """

    version: str
    cases: Sequence[Case]
    #: Metric name → the bar it must clear. Registered by the *example*, in
    #: code, before the run — which is what makes it a criterion rather than a
    #: description of whatever happened.
    thresholds: dict[str, Threshold] = field(default_factory=dict)


@dataclass(frozen=True)
class Threshold:
    """A pre-registered bar, and where the number came from.

    ``comparator`` and ``grade`` are not decoration. A threshold with no stated
    source is a guess wearing a number's clothes, and the scorecard prints both
    next to the value so a reader can discount the ones that deserve it.
    ``grade`` is one of ``P`` (published), ``I`` (internal measurement),
    ``V`` (verified here), ``D`` (declared, no comparator).
    """

    value: float
    #: ``">="`` or ``"<="``. Which direction clears the bar.
    direction: Literal[">=", "<="] = ">="
    comparator: str = ""
    grade: Literal["P", "I", "V", "D"] = "D"

    def clears(self, observed: float | None) -> bool:
        """``True`` when ``observed`` clears the bar.

        A ``None`` observation does not clear anything and does not fail
        anything either — it is a measurement that was not taken. Callers
        distinguish the two; this returns ``True`` so an unpriced model does
        not manufacture a failure out of a missing price list.
        """
        if observed is None:
            return True
        return (
            observed >= self.value if self.direction == ">=" else observed <= self.value
        )


# --------------------------------------------------------------------------
# The deterministic checks
# --------------------------------------------------------------------------
#
# Substring, case-folded, and not normalised any further. The agent does not
# normalise its own output, so a scorer that stripped accents or collapsed
# synonyms would be measuring a text nobody was ever shown.


def contains(*needles: str) -> Check:
    """The answer must contain every needle. A miss is an ``OMISSION``."""

    def run(obs: Observation) -> list[Finding]:
        haystack = obs.answer.casefold()
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind="OMISSION",
                check="contains",
                source="check",
                expected=needle,
                actual=obs.answer[:200],
                detail="a resposta não menciona o que a pergunta pedia",
            )
            for needle in needles
            if needle.casefold() not in haystack
        ]

    return Check(name=f"contains({', '.join(needles)})", run=run)


def not_contains(*needles: str) -> Check:
    """The answer must contain none of them. A hit is a ``FABRICATION``.

    This is the zero-tolerance check, and the kind is deliberate: the things a
    case forbids are invented setting names and leaked secrets. One hit fails
    the whole run — see `metrics.compute_metrics`.
    """

    def run(obs: Observation) -> list[Finding]:
        haystack = obs.answer.casefold()
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind="FABRICATION",
                check="not_contains",
                source="check",
                expected=f"ausência de {needle!r}",
                actual=obs.answer[:200],
                detail="a resposta afirma algo que não existe",
            )
            for needle in needles
            if needle.casefold() in haystack
        ]

    return Check(name=f"not_contains({', '.join(needles)})", run=run)


def calls_tools(*names: str) -> Check:
    """Every named tool must have run. Absence is a ``WRONG_PATH``.

    This is the grounding check: an answer that is right without having read
    anything is right by luck, and the next question will find that out. Reads
    the ``tool:`` stage frames, which is the same evidence the rail renders.
    """

    def run(obs: Observation) -> list[Finding]:
        called = obs.tools_called()
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind="WRONG_PATH",
                check="calls_tools",
                source="check",
                expected=f"ferramenta {name}",
                actual=", ".join(sorted(called)) or "nenhuma ferramenta",
                detail="respondeu sem consultar a fonte",
            )
            for name in names
            if name not in called
        ]

    return Check(name=f"calls_tools({', '.join(names)})", run=run, metric="grounding")


def blocks(guard: str = "guard_in") -> Check:
    """The named gate must have fired. Not firing is a ``WRONG_PATH``."""

    def run(obs: Observation) -> list[Finding]:
        if guard in obs.blocked_by():
            return []
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind="WRONG_PATH",
                check="blocks",
                source="check",
                expected=f"{guard} bloqueia",
                actual=obs.answer[:200],
                detail="a guardrail deixou passar o que deveria barrar",
            )
        ]

    return Check(name=f"blocks({guard})", run=run, metric="guard_recall")


def does_not_block() -> Check:
    """No gate may fire. A false block is a ``FABRICATION``-tier failure.

    Deliberately the same severity as an invented fact, and this is an opinion
    worth stating: a guardrail that refuses ordinary questions is not a cautious
    system, it is a broken one, and it fails in the direction nobody measures
    because a refusal always *looks* safe.
    """

    def run(obs: Observation) -> list[Finding]:
        fired = obs.blocked_by()
        if not fired:
            return []
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind="WRONG_PATH",
                check="does_not_block",
                source="check",
                expected="nenhuma guardrail dispara",
                actual=", ".join(sorted(fired)),
                detail="falso positivo: a guardrail barrou uma pergunta legítima",
            )
        ]

    return Check(name="does_not_block()", run=run, metric="false_block")
