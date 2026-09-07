# ADR 0003 — Uma confirmação expira, e a ação não pode mudar embaixo dela

**Status:** aceito · **Data:** 2026-09-07 · **Evidência:** `tests/unit/test_recovery.py`, `tests/unit/test_invariants.py` (células `expired_yes` e `mutated_action`)

## Contexto

O V0 emitia um `confirmation_id` aleatório e o aceitava para sempre, contra qualquer versão da ação. `Intent.confirmed_at` era gravado e nunca lido.

O efeito não era um bug: era a **ausência de um fato**. "Confirmação velha" e "ação mutada" não eram cenários que o sistema errava — eram cenários sobre os quais ele não conseguia errar, porque não guardava nada com que comparar. Duas células da matriz do marco 3 eram, literalmente, inexprimíveis.

## Decisão

Dois campos e duas checagens em `confirm`:

- **TTL de 5 minutos** (`confirmation_issued_at`). Um "sim" é consentimento para mover dinheiro **agora**, com o cliente ainda na conversa. Expirado, o intent é **cancelado** — não fica esperando um token que não vai rejuvenescer.
- **Digest da ação com chave** (`action_digest`): HMAC sobre o JSON canônico da ação. Se o digest diverge, `DENY` e nada executa.

O digest é **com chave, não hash puro**, e essa é a parte que importa: um hash simples diz que a ação mudou, mas quem consegue reescrever a ação também consegue recalcular o hash para bater. A chave é o que faz as duas escritas exigirem duas capacidades.

A chave vem do ambiente (`TRAIL_CONFIRMATION_SECRET`), não de um valor aleatório por processo — aleatório invalidaria toda confirmação pendente a cada restart, o que é uma indisponibilidade fantasiada de controle de segurança.

## Consequências

TTL curto tem custo de usabilidade: um cliente que sai para conferir o saldo e volta em seis minutos precisa propor de novo. Aceito, e ajustável num lugar só (`state.CONFIRMATION_TTL`).

A verificação da matriz mostrou o valor real destas duas checagens de um jeito que os testes unitários não mostravam: removendo a checagem de TTL, uma célula fica vermelha; removendo o digest, outra. E a primeira tentativa de detectar expiração **procurava o evento `confirmation_expired` que o próprio plane escreve** — ou seja, apagar a checagem apagava também a evidência dela. A matriz derivaria "verde" de um sistema quebrado. Hoje a invariante calcula `confirmed_at - issued_at` por conta própria.
