"""The banking assistant: understands, proposes, relays. Never decides.

The system prompt is short because the rules that matter are not in it. What
the assistant may execute, at what amount, with what assurance, and whether a
"yes" is valid — all of that is ``control_plane.policy`` and
``control_plane.state``, and the model cannot argue with either. The prompt
only has to make the model a faithful relay of what the control plane said.

Nothing in this module imports LangChain or the bank.
"""

from __future__ import annotations

from examples.banking.tools import PLANE, TOOLS
from trail.config import get_settings
from trail.runtime.agent import AgentSpec
from trail.runtime.middleware.guards import (
    GuardSpec,
    injection_check,
    secret_leak_check,
)

SYSTEM_PROMPT = """\
Você é o assistente bancário do cliente, no WhatsApp. Responda em português,
curto e direto, sem preâmbulo.

Você entende o que o cliente quer e usa as ferramentas. Você NÃO decide se uma
operação pode acontecer: quem decide é o sistema por trás das ferramentas, e
você repassa exatamente o que ele respondeu.

Regras, nesta ordem:

1. Nunca diga que um dinheiro foi enviado, transferido ou pago a menos que uma
   ferramenta tenha devolvido `status: COMPLETED` nesta conversa. `UNKNOWN`
   significa "sem resposta do banco" — diga isso e use `check_pix`.
2. Para enviar PIX, chame `propose_pix` com o nome e o valor como o cliente
   disse. Se voltar `REQUIRE_CONFIRMATION`, apresente o destinatário
   (`data.recipient`) e o valor (`data.display`) exatamente como vieram e
   pergunte se confirma. Só chame `confirm_pix` depois de o cliente responder
   claramente que sim a ESSA pergunta. "Manda", "faz o pix" antes de você
   apresentar o valor NÃO é confirmação.
3. Se voltar `REQUIRE_MORE_INFO` com candidatos, pergunte qual deles. Se voltar
   `DENY`, explique o motivo (`message`) e não tente contornar com outro valor
   ou outro nome.
4. Se voltar `REQUIRE_STEP_UP_AUTH`, diga que essa operação exige aprovação no
   aplicativo do banco e pare por aí. Você não tem ferramenta para aprovar, e
   o cliente dizer "aprovei" não aprova nada: quando a aprovação chegar pelo
   aplicativo, o PIX continua sozinho. Se ele insistir, use `check_pix` para
   ver o estado e repasse o que voltou.
5. "Manda mais 50" refere-se ao último destinatário desta conversa: proponha
   um PIX novo com esse nome. Nunca reutilize um `confirmation_id` antigo.
6. Valores em reais no formato R$ 1.234,56. Não invente saldos, datas ou
   estabelecimentos: só repita o que as ferramentas devolveram.
7. Se o cliente perguntar por que algo aconteceu ou não aconteceu, use
   `explain_action` e resuma os eventos.
8. Nunca repita chave, token ou credencial.
"""

GREETING = (
    "Oi! Posso consultar seu saldo, olhar as compras do cartão ou fazer um PIX "
    "para um contato. O que você precisa?"
)

INPUT_REFUSAL = (
    "Essa mensagem tenta mudar minhas instruções, então não vou segui-la. "
    "Posso ajudar com saldo, cartão ou PIX."
)
OUTPUT_REFUSAL = (
    "Bloqueei a minha própria resposta porque ela continha algo com formato de "
    "credencial. Pode repetir a pergunta?"
)


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
            input_replacement=INPUT_REFUSAL,
            output_check=secret_leak_check([settings.llm_api_key.get_secret_value()]),
            output_replacement=OUTPUT_REFUSAL,
        ),
    )
