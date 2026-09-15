"""Input and output guardrails, as middleware, and the dial that composes them.

A guardrail here is two things that are usually conflated: a **check**, which
is a pure function from text to a verdict, and a **gate**, which is the
middleware that runs the check and short-circuits the agent when it fails.
Keeping them apart is what makes the checks unit-testable offline — no graph,
no model, no network — and what lets a project supply its own check without
knowing anything about LangChain.

The gate hooks are the ones LangChain gives for exactly this:

``before_agent``
    Runs once per invocation, before the model sees anything. This is the input
    guardrail; it protects the *model* from a manipulated prompt.
``after_agent``
    Runs once, on the finished response, before it reaches the caller. This is
    the output guardrail; it protects *you* from what the model said.

Both are declared ``can_jump_to=["end"]`` and return ``{"jump_to": "end"}`` on
failure, which is LangChain's supported way to abandon the remaining graph and
answer with a replacement message. That replacement is the point: a guardrail
that raises gives the caller a 500, and a 500 is not an answer.

Which gates exist is decided by composition, not configuration — see
:data:`GUARDRAIL_MODES`. There is no ``if guardrails_enabled`` anywhere in this
file, and that is deliberate: the branch would have to be repeated at every
gate, and a gate that forgets it would be a guardrail that is on when the
operator believes it is off.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain.messages import AIMessage
from langgraph.runtime import Runtime

from trail.runtime.events import StageEvent, emit, ns_since
from trail.telemetry import span

# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One failed check, with enough context to act on it without re-running.

    ``rule`` names the policy the text breached rather than restating the
    check, so a violation that surfaces in a trace, a log line or a UI carries
    its own justification instead of sending the reader to find it.
    """

    check: str
    rule: str
    detail: str
    evidence: str


#: Evidence is truncated so a violation stays readable in a log line and a span
#: attribute. The full text is always available in the transcript.
_EVIDENCE_CHARS = 160


def evidence(text: str) -> str:
    """``text``, clipped to a length that fits in a log line."""
    return text if len(text) <= _EVIDENCE_CHARS else text[:_EVIDENCE_CHARS] + "…"


@dataclass(frozen=True)
class GuardVerdict:
    """The outcome of one guard: whether it passed, and what failed if not."""

    violations: tuple[Violation, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.violations

    def as_detail(self) -> dict[str, Any]:
        return {
            "violations": [
                {
                    "check": v.check,
                    "rule": v.rule,
                    "detail": v.detail,
                    "evidence": v.evidence,
                }
                for v in self.violations
            ]
        }


#: A check is a pure function from text to a verdict. Deliberately synchronous:
#: every check shipped here is deterministic and runs in microseconds, and a
#: model-based check that needs `await` can be wrapped rather than forcing every
#: trivial regex to be a coroutine.
Check = Callable[[str], GuardVerdict]
Replacement = str | Callable[[str], str]


def all_of(checks: Sequence[Check]) -> Check:
    """One check that runs every check and accumulates every violation.

    Accumulating rather than short-circuiting is the choice: a response can
    breach two policies at once, and reporting only the first would make the
    second look like it appeared after the first was fixed.
    """

    def run(text: str) -> GuardVerdict:
        found: list[Violation] = []
        for check in checks:
            found.extend(check(text).violations)
        return GuardVerdict(violations=tuple(found))

    return run


PASS = GuardVerdict()


def never(_: str) -> GuardVerdict:
    """A check that passes everything. The identity element of :func:`all_of`."""
    return PASS


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def _message_text(message: Any) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # Multimodal content arrives as a list of blocks; only the text ones can be
    # checked, and a guard that silently ignored them would be worse than one
    # that checks what it can.
    return " ".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _last_text(state: AgentState) -> str:
    messages = state.get("messages") or []
    return _message_text(messages[-1]) if messages else ""


def _last_user_text(state: AgentState) -> str:
    """The latest customer utterance, even after a model answer was appended."""
    messages = state.get("messages") or []
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            return _message_text(message)
    return ""


def _replacement_text(replacement: Replacement, source: str) -> str:
    return replacement(source) if callable(replacement) else replacement


#: Names the gate that substituted a message, carried in ``additional_kwargs``.
#: A flag rather than a text comparison, because matching on the refusal string
#: would re-fire on a legitimate answer that happened to quote it — and the
#: refusal wording belongs to the example, not to this module.
_REFUSED_BY = "trail_refused_by"


def _refused_by(state: AgentState) -> str | None:
    """Which gate already refused this turn, if any.

    ``jump_to: "end"`` does not stop ``after_agent`` from running again on the
    state the jump produced: the graph ends, the completion hooks fire, and a
    gate that did not notice would screen its own refusal, block it, replace it
    with an identical refusal, and report a second violation for a sentence it
    wrote itself.

    Knowing *which* gate is what separates the two silent cases. A gate that
    sees its own name has already reported and must say nothing more; a gate
    that sees another's had nothing to screen and should say so, because an
    absent cell and a skipped cell read the same and only one of them is true.
    """
    messages = state.get("messages") or []
    if not messages:
        return None
    value = getattr(messages[-1], "additional_kwargs", {}).get(_REFUSED_BY)
    return value if isinstance(value, str) else None


def _refuse(by: str, text: str) -> dict[str, Any]:
    """The state update that ends the turn with ``text`` as the answer."""
    return {
        "messages": [AIMessage(text, additional_kwargs={_REFUSED_BY: by})],
        "jump_to": "end",
    }


#: How much screened text reaches a span. A guard reads whole conversations;
#: a trace only has to show enough to recognise what was screened.
_SPAN_CHARS = 2_000


def _guard_span(name: str, text: str) -> Any:
    """A span for one gate, typed so Langfuse renders it as a gate.

    ``GUARDRAIL`` is one of Langfuse's own observation kinds, which is why the
    gates in this project appear in a trace as gates rather than as anonymous
    spans between two model calls — and why a blocked turn is legible in the
    tree without reading any attribute.
    """
    return span(
        name,
        **{
            "trail.observation_type": "guardrail",
            "trail.input": text[:_SPAN_CHARS],
        },
    )


def _record(active: Any, verdict: GuardVerdict) -> None:
    """Put the verdict on the span, as a level rather than only as a payload.

    A blocked turn is a WARNING. Recorded as a plain attribute it is a
    successful span with an unusual field, which is indistinguishable at a
    glance from every other successful span — and a guardrail nobody can see
    fire is the failure this whole scaffold argues against.
    """
    if verdict.passed:
        active.set_attribute("trail.output", "passed")
        return
    active.set_attribute("trail.level", "WARNING")
    active.set_attribute(
        "trail.status_message",
        "; ".join(f"{v.check}: {v.rule}" for v in verdict.violations)[:200],
    )
    active.set_attribute(
        "trail.output", json.dumps(verdict.as_detail(), ensure_ascii=False)
    )


class InputGuard(AgentMiddleware):
    """Screens the incoming message before the model is called at all.

    Blocking here rather than at ``before_model`` means a refused turn costs no
    tokens: the model and the tools never run. It also means they emit no
    frames of their own, so this hook emits their skips — an absent cell and a
    skipped cell look the same to a reader who was not watching, and only one
    of them is a claim.
    """

    name = "guard_in"

    def __init__(self, check: Check, replacement: Replacement, label: str = "input"):
        super().__init__()
        self.check = check
        self.replacement = replacement
        self.label = label

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        started = time.perf_counter_ns()
        text = _last_text(state)
        with _guard_span("trail.guard.in", text) as active:
            verdict = self.check(text)
            _record(active, verdict)
        if verdict.passed:
            emit(
                StageEvent(
                    name=self.name,
                    kind="guard_in",
                    label=self.label,
                    status="done",
                    ns=ns_since(started),
                )
            )
            return None
        emit(
            StageEvent(
                name=self.name,
                kind="guard_in",
                label=self.label,
                status="blocked",
                ns=ns_since(started),
                detail=verdict.as_detail(),
            )
        )
        emit(StageEvent(name="model", kind="model", label="model", status="skip"))
        return _refuse(self.name, _replacement_text(self.replacement, text))


class OutputGuard(AgentMiddleware):
    """Screens the finished response before it reaches the caller.

    ``after_agent`` and not ``after_model``: the model is called once per tool
    round, and screening every intermediate turn would reject a tool call for
    saying something the final answer never says. The cost is that a blocked
    response has already been paid for — which is the honest trade, because the
    alternative is a gate that fires on text nobody was going to read.

    When the input gate already refused the turn, this one reports a skip
    rather than screening the refusal: the text it would be checking is a
    sentence this repository wrote, and passing it would be a measurement of
    nothing.
    """

    name = "guard_out"

    def __init__(self, check: Check, replacement: Replacement, label: str = "output"):
        super().__init__()
        self.check = check
        self.replacement = replacement
        self.label = label

    @hook_config(can_jump_to=["end"])
    async def aafter_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        refused_by = _refused_by(state)
        if refused_by == self.name:
            # Our own refusal, coming back around after the jump. Already
            # reported; saying it twice would double-count the violation.
            return None
        if refused_by is not None:
            emit(
                StageEvent(
                    name=self.name, kind="guard_out", label=self.label, status="skip"
                )
            )
            return None
        started = time.perf_counter_ns()
        text = _last_text(state)
        with _guard_span("trail.guard.out", text) as active:
            verdict = self.check(text)
            _record(active, verdict)
        if verdict.passed:
            emit(
                StageEvent(
                    name=self.name,
                    kind="guard_out",
                    label=self.label,
                    status="done",
                    ns=ns_since(started),
                )
            )
            return None
        emit(
            StageEvent(
                name=self.name,
                kind="guard_out",
                label=self.label,
                status="blocked",
                ns=ns_since(started),
                detail=verdict.as_detail(),
            )
        )
        return _refuse(
            self.name,
            _replacement_text(self.replacement, _last_user_text(state)),
        )


# --------------------------------------------------------------------------
# The dial
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardSpec:
    """What an example agent offers as its two gates, and what to say instead.

    Both checks are always supplied. Which of them is *mounted* is the mode's
    decision, not the agent's — so switching a gate off is an operator action
    that leaves a visible skip, rather than an agent rewrite.
    """

    input_check: Check = never
    input_replacement: Replacement = "Não posso seguir com essa mensagem."
    output_check: Check = never
    output_replacement: Replacement = "Não posso responder isso."


def _both(spec: GuardSpec) -> list[AgentMiddleware]:
    return [
        InputGuard(spec.input_check, spec.input_replacement),
        OutputGuard(spec.output_check, spec.output_replacement),
    ]


def _input_only(spec: GuardSpec) -> list[AgentMiddleware]:
    return [InputGuard(spec.input_check, spec.input_replacement)]


def _output_only(spec: GuardSpec) -> list[AgentMiddleware]:
    return [OutputGuard(spec.output_check, spec.output_replacement)]


def _none(_: GuardSpec) -> list[AgentMiddleware]:
    return []


#: The whole guardrail configuration. Turning a gate on or off is list
#: membership — there is no flag threaded through the gates themselves, so a
#: gate cannot disagree with the mode.
GUARDRAIL_MODES: dict[str, Callable[[GuardSpec], list[AgentMiddleware]]] = {
    "both": _both,
    "input": _input_only,
    "output": _output_only,
    "none": _none,
}

#: Which gates each mode leaves out, so the trace middleware can emit their
#: skip frames. Derived by hand rather than by inspecting the built list,
#: because the rail must be able to say "guard_out was switched off" before any
#: middleware has run.
OMITTED_BY_MODE: dict[str, tuple[tuple[str, str, str], ...]] = {
    "both": (),
    "input": (("guard_out", "guard_out", "output"),),
    "output": (("guard_in", "guard_in", "input"),),
    "none": (
        ("guard_in", "guard_in", "input"),
        ("guard_out", "guard_out", "output"),
    ),
}


def build_guards(mode: str, spec: GuardSpec) -> list[AgentMiddleware]:
    """The middleware list for ``mode``, or a loud failure naming the valid set."""
    try:
        return GUARDRAIL_MODES[mode](spec)
    except KeyError:
        valid = ", ".join(sorted(GUARDRAIL_MODES))
        raise ValueError(
            f"unknown guardrail mode {mode!r}; TRAIL_GUARDRAILS must be one of: {valid}"
        ) from None


def omitted_by(mode: str) -> tuple[tuple[str, str, str], ...]:
    """The ``(name, kind, label)`` of every gate this mode leaves out.

    Consumed by the trace middleware, which emits each one as a ``skip`` frame
    from the hook where that gate would have run — so a switched-off guardrail
    lands in pipeline order rather than being yielded up front and re-sorted by
    the client. Any sort able to place a skip is also able to scramble the real
    interleaving of model and tool calls, which is the ordering a reader is
    actually reading for.

    A gate that reported nothing would be indistinguishable on the rail from
    one that ran and passed, and the difference between those two is the only
    thing an operator reading the rail needs.
    """
    return OMITTED_BY_MODE.get(mode, ())


# --------------------------------------------------------------------------
# Checks that are not domain-specific
# --------------------------------------------------------------------------

#: Prompt-injection shapes, in the two languages this repo is written in. This
#: is a floor, not a defence: a determined attacker rewrites around a regex.
#: It exists because the cheap deterministic layer belongs underneath the
#: expensive probabilistic one, not instead of it — and because a check with a
#: measurable latency on the rail teaches more about guardrails than a sentence
#: in a system prompt that leaves no trace at all.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|esque[çc]a|ignore?e?)\b[^.\n]{0,40}"
            r"\b(previous|prior|above|all|your|as|suas?|todas?)\b[^.\n]{0,40}"
            # Plurals matter: the phrase people actually type is "ignore all
            # previous instruction*s*", and a pattern without the `s?` matches
            # only the singular nobody writes.
            r"\b(instructions?|prompts?|rules?|instru[çc][õo]es|regras?|ordens?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"\b(reveal|print|show|repeat|output|imprim[ae]|mostre|revele|repita)\b"
            r"[^.\n]{0,40}\b(system\s+prompt|initial\s+instructions|your\s+prompt|"
            r"system\s+message|prompt\s+de\s+sistema|suas\s+instru[çc][õo]es)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\b(you\s+are\s+now|from\s+now\s+on\s+you|act\s+as\s+if\s+you|"
            r"agora\s+voc[êe]\s+[ée]|a\s+partir\s+de\s+agora\s+voc[êe])\b",
            re.IGNORECASE,
        ),
    ),
)


def injection_check(text: str) -> GuardVerdict:
    """Refuse messages that try to rewrite the agent's instructions."""
    found = [
        Violation(
            check="prompt_injection",
            rule=name,
            detail="the message attempts to override the agent instructions",
            evidence=evidence(match.group(0)),
        )
        for name, pattern in _INJECTION_PATTERNS
        if (match := pattern.search(text))
    ]
    return GuardVerdict(violations=tuple(found))


#: Shapes that look like credentials regardless of which provider issued them.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


def secret_leak_check(literals: Sequence[str] = ()) -> Check:
    """Refuse responses containing a credential — by shape, or verbatim.

    ``literals`` are the exact secrets this process holds, so the check catches
    the real key even when it does not match any known shape. They are compared
    but never reported: the evidence is the pattern name, because a violation
    that quotes the secret it caught has leaked it into every log that records
    the violation.
    """
    real = tuple(s for s in literals if s and len(s) >= 8)

    def run(text: str) -> GuardVerdict:
        found = [
            Violation(
                check="secret_leak",
                rule=name,
                detail="the response contains something shaped like a credential",
                evidence=f"<{name} redacted>",
            )
            for name, pattern in _SECRET_PATTERNS
            if pattern.search(text)
        ]
        if any(secret in text for secret in real):
            found.append(
                Violation(
                    check="secret_leak",
                    rule="configured_secret",
                    detail="the response contains a secret from this configuration",
                    evidence="<secret redacted>",
                )
            )
        return GuardVerdict(violations=tuple(found))

    return run
