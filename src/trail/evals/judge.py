"""A check whose verdict comes from a model instead of from a substring.

Some things a golden set wants to assert are not substrings. "Admits it does
not know, instead of inventing a behaviour" is a property of an answer, not a
token in it, and a `contains()` written to approximate it would pass on the
wrong sentence and fail on the right one.

So: a judge check. It has the same type as every other check, produces the same
:class:`~trail.evals.cases.Finding`, and lands in the same taxonomy — with
``source="judge"``, because a model's opinion and a regex are not the same
evidence and a scorecard that hid the difference would be claiming more than it
measured.

**The model is bound by the runner, not by the case.** A case declares
``judge("...")`` and says nothing about which model, which key, or what it
costs; `bind_judge` supplies those for the duration of a run. That is the same
mechanism `events.emit` uses for the stream writer, for the same reason: the
thing being declared should not have to carry the machinery that runs it.

Two accounting rules, both about not flattering the agent:

* **Judge tokens never enter the agent's cost.** ``cost_per_turn_usd`` is what
  the agent spent answering. Folding the grader's spend into it would make an
  agent look more expensive for being measured, and would move the number when
  only the golden set changed.
* **A judge that is the agent is flagged.** With ``TRAIL_JUDGE_MODEL`` unset
  the same model grades its own output, which is a known and well-documented
  bias. That is a legitimate default — it needs no second key — but the
  scorecard says so on every run rather than letting it pass unstated.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from trail import costs
from trail.config import Settings
from trail.evals.cases import Check, Finding, FindingKind, Observation

logger = logging.getLogger(__name__)

#: The judge is asked for one token, not for prose. A rubric that needs a
#: paragraph to answer is a rubric that should have been two rubrics, and a
#: free-form grader is one whose output has to be parsed by another grader.
PROMPT = """Você é um avaliador. Julgue a resposta de um agente contra UM critério.

Responda EXATAMENTE em uma destas duas formas, sem mais nada:
PASS
FAIL: <motivo em uma frase>

Critério: {rubric}

Pergunta feita ao agente:
{question}

Resposta do agente:
{answer}
"""


@dataclass
class JudgeLedger:
    """What the grading cost, kept apart from what the agent cost."""

    model: str
    #: ``True`` when the judge and the agent are the same model.
    self_evaluating: bool = False
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None

    def record(self, usage: costs.Usage) -> None:
        self.calls += 1
        self.input_tokens += usage.total_input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd


@dataclass
class JudgeSession:
    """The model a judge check reaches, and the tally of what it spent."""

    chat: Any
    ledger: JudgeLedger = field(default_factory=lambda: JudgeLedger(model=""))


_SESSION: ContextVar[JudgeSession | None] = ContextVar("trail_judge", default=None)


@contextmanager
def bind_judge(session: JudgeSession) -> Iterator[JudgeSession]:
    """Make ``session`` the judge for the duration of the block.

    A ``ContextVar`` rather than a parameter threaded through every check, and
    rather than a module global: the runner drives cases concurrently, and a
    global would be one tally shared by tasks that should each see the binding
    their own run installed.
    """
    token = _SESSION.set(session)
    try:
        yield session
    finally:
        _SESSION.reset(token)


def build_session(settings: Settings) -> JudgeSession:
    """The judge for this run, from configuration.

    ``TRAIL_JUDGE_MODEL`` empty means "the agent's own model" — the default,
    because requiring a second model to run the suite at all would make the
    judge checks the reason nobody runs it.
    """
    from trail.runtime.agent import build_model

    judge_model = settings.judge_model or settings.model
    chat = build_model(settings.model_copy(update={"model": judge_model}))
    return JudgeSession(
        chat=chat,
        ledger=JudgeLedger(
            model=judge_model,
            self_evaluating=not settings.judge_model,
        ),
    )


def _parse(text: str) -> tuple[bool, str]:
    """``(passed, reason)`` from the judge's reply.

    A reply that is neither ``PASS`` nor ``FAIL:`` is not read charitably. A
    grader that ignored its own format has not graded anything, and guessing
    from a stray "sim" is how a judge silently starts passing everything.
    """
    stripped = text.strip()
    upper = stripped.upper()
    if upper.startswith("PASS"):
        return True, ""
    if upper.startswith("FAIL"):
        return False, stripped[4:].lstrip(": ").strip() or "sem motivo declarado"
    raise ValueError(f"veredito ilegível do juiz: {stripped[:120]!r}")


def judge(
    rubric: str,
    *,
    kind: FindingKind = "OMISSION",
    metric: str = "",
) -> Check:
    """A check that asks a model whether ``rubric`` holds for the answer.

    ``kind`` defaults to ``OMISSION`` — the usual rubric asks whether the answer
    contains something it should. Pass ``FABRICATION`` for a rubric about
    something the answer must *not* claim, and the failure inherits the
    zero-tolerance tier that goes with it.
    """

    async def run(obs: Observation) -> list[Finding]:
        session = _SESSION.get()
        if session is None:
            raise RuntimeError(
                "a judge check ran with no judge bound; "
                "the runner must wrap the run in evals.judge.bind_judge()"
            )
        message = await session.chat.ainvoke(
            PROMPT.format(rubric=rubric, question=obs.question, answer=obs.answer)
        )
        session.ledger.record(costs.usage_from_message(message, session.ledger.model))
        content = (
            message.content
            if isinstance(message.content, str)
            else str(message.content)
        )
        passed, reason = _parse(content)
        if passed:
            return []
        return [
            Finding(
                case_id=obs.case_id,
                turn=obs.turn,
                kind=kind,
                check=f"judge({rubric[:60]})",
                source="judge",
                expected=rubric,
                actual=obs.answer[:200],
                detail=reason,
            )
        ]

    return Check(name=f"judge({rubric[:60]})", run=run, metric=metric)
