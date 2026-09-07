# ADR 0001 — Ownership: o que é da sessão e o que é do cliente

**Status:** aceito · **Data:** 2026-09-07 · **Evidência:** `tests/unit/test_control_plane.py`, `tests/unit/test_voice.py`, `tests/unit/test_invariants.py` (célula `borrowed_token`)

## Contexto

O V0 tinha uma regra só: toda operação sobre um intent exigia **mesmo `customer_id` E mesma `session_id`** (`_same_principal`). Isso é a defesa contra replay de confirmação entre conversas (adversário A4 do modelo de ameaças) e funcionou enquanto tudo acontecia num canal só.

Três tarefas quebraram essa premissa ao mesmo tempo:

- **T10** (`trail step-up`): a aprovação forte chega do aplicativo do banco — outro processo, outra sessão **por construção**. Com a regra antiga, 100% dos callbacks reais seriam recusados. A armadilha estava prevista no plano de execução (armadilha 5).
- **T19** (runbook): o operador que resolve um `UNKNOWN` às 3h da manhã não está na thread de conversa do cliente.
- **T24** (voz → texto): ler valor e destinatário por telefone e aceitar "sim" é a confirmação mais fraca que este sistema consegue oferecer — um "sim" mal ouvido sobre um valor mal ouvido compõe o erro.

## Decisão

Ownership deixa de ser uma regra e passa a ser **três**, escolhidas pelo que a operação faz:

| Operação | Escopo | Por quê |
|---|---|---|
| `confirm`, `cancel` | cliente **+ sessão** | Consentimento é dito numa conversa. Um "sim" não é transferível para outra. |
| `step_up`, `reconcile` | cliente **+ intent** | Elevar garantia e perguntar ao banco o que ele fez são seguros de fazer de fora; nenhum dos dois move dinheiro por si. |
| `explain` | quem abriu a trilha | A trilha contém o `confirmation_id`, que `confirm` aceita. |

Com **uma exceção nomeada**: um intent proposto no canal `voice` pode ser confirmado — e lido — pelo mesmo cliente de outro canal. A exceção é do canal de origem, não do canal de destino: um intent proposto por texto continua exigindo a mesma sessão.

## Consequências

O que **não** muda: a fronteira do cliente. Outro `customer_id` é recusado em todas as operações, em todos os canais, e há teste para isso em cada uma.

O que muda: uma sessão comprometida do mesmo cliente agora pode disparar `step_up` e `reconcile` em intents que não criou. O ganho é que o step-up out-of-band existe; a perda é real e está registrada aqui em vez de descoberta depois. Mitigação atual: nenhuma das duas operações move dinheiro, e ambas ficam no ledger com a sessão que as chamou.

O handoff de canal é gravado (`channel_handoff`), com canal de origem, canal de destino e sessão — porque "quem confirmou isto, e de onde" é exatamente a pergunta que uma auditoria faz depois.

## Alternativas descartadas

- **Afrouxar tudo para cliente+intent.** Reabre A4: um `confirmation_id` vazado passa a valer em qualquer conversa. O consentimento é justamente o que não pode ser emprestado.
- **Deixar `confirm` por sessão e o step-up morrer.** Foi o estado do V0 e é o que a armadilha 5 previu: o canal out-of-band nunca funcionaria.
- **Um token de handoff explícito** (o cliente pede transferência de canal). Mais seguro e mais caro; se o step-up ganhar volume real, é para cá que isto evolui.
