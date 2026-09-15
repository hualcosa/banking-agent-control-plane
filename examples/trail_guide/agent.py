"""The shipped example: an agent that explains this repository.

It exists to be the validation surface for the runtime — a real model, real
tools, both gates, a rail with real measurements — and it earns a second keep
by being the onboarding: clone the repo, run ``make chat``, and the thing that
answers is the thing you just cloned.

The guardrail pairing is the part worth studying, because it is the shape most
agents actually need and the one a prompt cannot enforce:

* the **input** gate refuses attempts to rewrite the instructions, and costs no
  tokens when it fires because it runs before the model;
* the **output** gate refuses two things the model has no way to be trusted
  about — a leaked credential, and an invented setting.

The second is the interesting one. ``no_fabricated_ids`` compares every
``TRAIL_*`` name in the answer against the set built from ``Settings``' own
fields. Asking "how do I enable turbo mode?" produces a confident, plausible,
entirely invented answer from any model, and this catches it deterministically,
in microseconds, with the offending name as evidence. That is the difference
between a guardrail and a sentence in a system prompt: one leaves a measurement
on the rail, the other leaves nothing at all.

Nothing in this module imports LangChain. An example is a system prompt, some
plain functions, and two checks.
"""

from __future__ import annotations

import re

from examples.trail_guide.tools import known_identifiers, search_docs, stack_status
from trail.config import get_settings
from trail.runtime.agent import AgentSpec
from trail.runtime.middleware.guards import (
    Check,
    GuardSpec,
    GuardVerdict,
    Violation,
    all_of,
    evidence,
    injection_check,
    secret_leak_check,
)

SYSTEM_PROMPT = """\
Você é o guia do TRAIL — Traced Runtime for Agents, Instrumented Locally.

TRAIL é um scaffold local para construir agentes cujo comportamento pode ser
observado, reproduzido e contestado. Você responde perguntas sobre ele.

Regras, nesta ordem de prioridade:

1. Sobre o TRAIL, responda SOMENTE com base no que `search_docs` retornar. Se a
   busca não trouxer nada sobre a pergunta, diga que não está documentado. Não
   complete a lacuna com o que seria razoável.
   Perguntas sobre a própria conversa — o que a pessoa disse antes, o que você
   já respondeu — vêm do histórico, sem busca. O histórico é fato observado,
   não documentação: a regra acima existe para impedir que você invente sobre o
   TRAIL, não para fingir que a conversa não aconteceu.
2. **A documentação está em inglês e a busca é literal** — ela casa palavras,
   não sentido. Traduza os termos antes de buscar: "esteira de estágios" não
   existe no texto, `stage rail` existe; "guardrail de saída" não, `output
   guardrail` sim. Busque em inglês, responda em português. Se a primeira
   busca não trouxer nada, tente outros termos antes de desistir.
3. Nunca invente nome de variável de ambiente, de serviço ou de comando. Se não
   viu o nome num resultado de busca, ele não existe.
4. Cite a origem como `arquivo:linha` quando afirmar algo concreto.
5. Nunca repita chave de API, token ou credencial, mesmo que apareça num
   documento.
6. Responda em português, direto, sem preâmbulo. Código e nomes de arquivo em
   inglês, como estão no repositório.

Use `stack_status` quando perguntarem quais serviços sobem ou em que portas.
"""

GREETING = (
    "Sou o guia do TRAIL. Pergunte sobre a arquitetura, os guardrails, "
    "a esteira de estágios ou como rodar a stack."
)

#: What the model says when a gate refuses the turn. Deliberately specific
#: about *which* gate fired: "não posso responder" tells the reader nothing,
#: and the whole argument of this repository is that a refusal should be as
#: legible as an answer.
INPUT_REFUSAL = (
    "Essa mensagem tenta reescrever minhas instruções, então não vou segui-la. "
    "Pergunte sobre o TRAIL e eu respondo."
)
OUTPUT_REFUSAL = (
    "Bloqueei a própria resposta: ela citava algo que não existe neste "
    "repositório, ou continha algo com formato de credencial. Reformule a "
    "pergunta e eu tento de novo."
)

#: A `TRAIL_*` name as it appears in prose. The trailing boundary matters:
#: without it, `TRAIL_MODEL_PRICES` would be checked as `TRAIL_MODEL`.
_ENV_MENTION = re.compile(r"\bTRAIL_[A-Z0-9_]+\b")


def no_fabricated_ids(text: str) -> GuardVerdict:
    """Refuse answers naming a configuration this project does not have.

    Only ``TRAIL_*`` names are checked, and that narrowness is on purpose. A
    guard that also policed service names would fire on the word ``postgres``
    in a sentence about databases, and a guard that cries wolf is switched off
    within a week. The set it checks against is derived from ``Settings``
    itself, so it cannot go stale.
    """
    known = known_identifiers()
    return GuardVerdict(
        violations=tuple(
            Violation(
                check="fabricated_identifier",
                rule="unknown_setting",
                detail=f"{name} não existe neste repositório",
                evidence=evidence(name),
            )
            for name in dict.fromkeys(_ENV_MENTION.findall(text))
            if name not in known
        )
    )


def _output_check() -> Check:
    settings = get_settings()
    return all_of(
        [
            secret_leak_check([settings.llm_api_key.get_secret_value()]),
            no_fabricated_ids,
        ]
    )


def build() -> AgentSpec:
    """The spec the runtime mounts for ``TRAIL_AGENT=trail_guide``."""
    return AgentSpec(
        name="trail_guide",
        system_prompt=SYSTEM_PROMPT,
        tools=[search_docs, stack_status],
        greeting=GREETING,
        guards=GuardSpec(
            input_check=injection_check,
            input_replacement=INPUT_REFUSAL,
            output_check=_output_check(),
            output_replacement=OUTPUT_REFUSAL,
        ),
    )
