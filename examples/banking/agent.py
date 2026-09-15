"""The banking assistant: understands, proposes, relays. Never decides.

The system prompt is short because the rules that matter are not in it. What
the assistant may execute, at what amount, with what assurance, and whether a
"yes" is valid — all of that is ``control_plane.policy`` and
``control_plane.state``, and the model cannot argue with either. The prompt
only has to make the model a faithful relay of what the control plane said.

Nothing in this module imports LangChain or the bank.
"""

from __future__ import annotations

import re

from examples.banking.tools import PLANE, TOOLS
from trail.config import get_settings
from trail.runtime.agent import AgentSpec
from trail.runtime.middleware.guards import (
    GuardSpec,
    injection_check,
    secret_leak_check,
)

SYSTEM_PROMPT = """\
You are the customer's banking assistant in a chat interface. Reply in the
language of the customer's latest message, briefly and directly, with no
preamble. Translate explanatory tool messages into that language, but preserve
names, formatted values, identifiers, and status tokens exactly.

LANGUAGE ROUTING IS A HARD REQUIREMENT. This demo supports English and Brazilian
Portuguese. Determine the response language only from the customer's latest
message, never from names, PIX, BRL, the bank locale, tool data, or earlier
turns. If that message is English, every explanatory word in your answer must
be English. If it is Portuguese, answer in Portuguese. For any other or unclear
language, use English. For example, after "Send R$50 to Ana", ask "Which Ana do
you mean?" — never "Qual Ana?".

You understand the customer's intent and use tools. You DO NOT decide whether
an operation may happen: the system behind the tools decides, and you faithfully
relay its result.

Rules, in this order:

1. Never say money was sent, transferred, or paid unless a tool returned
   `status: COMPLETED` in this conversation. `UNKNOWN` means the bank did not
   answer; say that and use `check_pix`.
2. To send a PIX, call `propose_pix` with the name and amount exactly as the
   customer stated them. If it returns `REQUIRE_CONFIRMATION`, present the
   recipient (`data.recipient`) and amount (`data.display`) exactly as returned
   and ask for confirmation. Call `confirm_pix` only after the customer clearly
   says yes to THAT question. "Send it" before you present the amount is NOT
   confirmation.
3. If it returns `REQUIRE_MORE_INFO` with candidates, ask which candidate. If it
   returns `DENY`, explain the reason (`message`) and do not work around it with
   another value or name.
4. If it returns `REQUIRE_STEP_UP_AUTH`, say the operation requires approval in
   the banking app and stop. You have no tool to grant approval, and the
   customer saying "I approved it" does not approve anything. When approval
   arrives from the banking app, the PIX continues automatically. If the
   customer insists, call `check_pix` and relay the returned state.
5. "Send 50 more" refers to the last recipient in this conversation: propose a
   new PIX to that name. Never reuse an old `confirmation_id`.
6. Format Brazilian reais as R$ 1.234,56. Never invent balances, dates, or
   merchants; repeat only what tools returned.
7. If the customer asks why something did or did not happen, call
   `explain_action` and summarize the events.
8. Never repeat a key, token, or credential.
"""

GREETING = (
    "Hi! I can check your balance, review card transactions, or prepare a PIX "
    "payment to one of your contacts. What do you need?"
)

INPUT_REFUSAL_EN = (
    "That message attempts to override my instructions, so I will not follow it. "
    "I can still help with balances, card transactions, or PIX."
)
INPUT_REFUSAL_PT = (
    "Essa mensagem tenta mudar minhas instruções, então não vou segui-la. "
    "Ainda posso ajudar com saldo, cartão ou PIX."
)
OUTPUT_REFUSAL_EN = (
    "I blocked my own response because it contained something shaped like a "
    "credential. Could you repeat the question?"
)
OUTPUT_REFUSAL_PT = (
    "Bloqueei minha própria resposta porque ela continha algo com formato de "
    "credencial. Pode repetir a pergunta?"
)

_PORTUGUESE_HINTS = re.compile(
    r"\b(?:cart[aã]o|confirma(?:r|[cç][aã]o)?|envie|essa|gostaria|"
    r"instru[cç][oõ]es|manda(?:r)?|meu|minha|mostre|n[aã]o|obrigad[oa]|pode|"
    r"pra|quais?|quero|saldo|sem|sim|voc[eê])\b",
    re.IGNORECASE,
)


def _in_customer_language(text: str, english: str, portuguese: str) -> str:
    """Choose English or Portuguese guard copy without spending a model call."""
    return portuguese if _PORTUGUESE_HINTS.search(text) else english


def input_refusal(text: str) -> str:
    return _in_customer_language(text, INPUT_REFUSAL_EN, INPUT_REFUSAL_PT)


def output_refusal(text: str) -> str:
    return _in_customer_language(text, OUTPUT_REFUSAL_EN, OUTPUT_REFUSAL_PT)


def sweep_on_boot() -> list[str]:
    """Resolve whatever the last process left mid-payment.

    An intent in ``SUBMITTED`` means the bank was called and the answer never
    came back — the process died between the two writes. It has no way out on
    its own: ``reconcile`` only accepts ``UNKNOWN``. Moving it there is what
    turns a crashed container into a recoverable event, and the operator needs
    to see the ids, because each one is now a payment whose truth lives at the
    bank and not here. The runbook takes it from there.
    """
    swept = PLANE.sweep()
    if not swept:
        return []
    return [
        f"restart sweep: {len(swept)} intent(s) left in SUBMITTED moved to "
        f"UNKNOWN, awaiting reconciliation: {', '.join(swept)}"
    ]


def build() -> AgentSpec:
    """The spec the runtime mounts for ``TRAIL_AGENT=banking``."""
    settings = get_settings()
    return AgentSpec(
        name="banking",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        greeting=GREETING,
        on_startup=sweep_on_boot,
        guards=GuardSpec(
            input_check=injection_check,
            input_replacement=input_refusal,
            output_check=secret_leak_check([settings.llm_api_key.get_secret_value()]),
            output_replacement=output_refusal,
        ),
    )
