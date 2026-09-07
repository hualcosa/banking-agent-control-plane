# Runbook — resolver um intent em `UNKNOWN`

Para quem foi acordado às 3h porque um PIX ficou no ar. Este documento é
suficiente por si só: se você seguir daqui até o fim, o intent sai de `UNKNOWN`
e você sabe dizer ao cliente se o dinheiro saiu ou não.

Três comandos, nesta ordem:

```bash
trail intents                       # o que está esperando uma pessoa
trail reconcile pix_4c1e0a7b93d2    # perguntar ao banco o que aconteceu
trail intents                       # confirmar que saiu da lista
```

Nenhum deles fala com o agente. Eles abrem o **mesmo Postgres** que o agente
escreve e constroem o próprio `ControlPlane` por cima — então funcionam com o
agente fora do ar, que é justamente o cenário em que você foi acordado.

---

## 1. O que significa `UNKNOWN`

`UNKNOWN` **não é erro**. É o estado honesto para "o banco foi chamado e a
resposta nunca chegou": o dinheiro **pode** ter saído, e ninguém neste processo
sabe.

A janela é esta, em `plane.py::_execute`:

```text
  grava execution_request  ──►  bank.create_pix(...)  ──►  grava o recibo
                            └──────── a janela ────────┘
```

Duas coisas caem nessa janela e **têm a mesma evidência**, por isso recebem a
mesma resposta:

| como acontece | quem move para `UNKNOWN` |
|---|---|
| o banco não responde (timeout) — no mock, todo valor terminado em `,13` | `_execute` capta `BankTimeout` e transiciona na hora |
| o processo morre no meio (deploy, OOM, kill) e o intent fica preso em `SUBMITTED` | o **sweep de restart**, na subida do processo seguinte (`examples/banking/agent.py::sweep_on_boot`) |

O sweep é o que garante que um container morto vire um evento recuperável: ele
varre `SUBMITTED`, move cada um para `UNKNOWN` e **imprime os ids no log da
subida**. Se você chegou aqui por causa de uma linha `restart sweep: N intent(s)
left in SUBMITTED moved to UNKNOWN`, é exatamente disto que se trata.

O que **nunca** acontece: um timeout virar `FAILED` automaticamente. Tratar
timeout como falha e repetir é como o cliente paga duas vezes.

## 2. O que `reconcile` faz — e o que ele não faz

**Faz:** pergunta ao banco pela **chave de idempotência** do intent (que é o
próprio `intent_id`) e escreve no ledger o que o banco respondeu.

- o banco tem o recibo → o intent vira `COMPLETED`, com o recibo anexado;
- o banco não conhece a chave → o intent vira `FAILED`.

**Não faz — nenhuma destas, em nenhuma circunstância:**

- **não paga de novo.** Não existe caminho de `reconcile` para o gateway de
  execução; ele só lê.
- **não estorna** nem cancela nada no banco.
- **não confirma** por ninguém: consentimento é do cliente, na conversa.
- **não mexe** em intent que não esteja em `UNKNOWN` — nesse caso só relata o
  estado atual.
- **não conserta** o agente. Se o agente está caindo, isto resolve o pagamento
  pendente, não a causa.

## 3. Antes de começar

1. `TRAIL_DATABASE_URL` tem que apontar para o **mesmo** Postgres do agente.
   De fora do compose o host é `localhost`; `make intents` e `make reconcile`
   já montam isso para você.
2. `TRAIL_CUSTOMER_ID` é o cliente cujos intents você enxerga. A lista é
   escopada por cliente, mesmo com o banco de dados compartilhado.
3. Precisa de um `.env` (copie de `.env.example`). Estes comandos não chamam
   modelo nenhum, mas leem a mesma configuração; sem a chave eles avisam qual
   variável falta em vez de estourar um traceback.
4. **Não** é preciso que o agente esteja de pé.

## 4. O procedimento

### 4.1 Listar o que espera uma pessoa

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
intent            estado            valor        destinatário    criado (UTC)         próximo passo
pix_4c1e0a7b93d2  UNKNOWN           R$ 1.250,13  contact_renata  2026-09-07 03:14:11  trail reconcile pix_4c1e0a7b93d2
pix_77b1c0e4aa10  AWAITING_STEP_UP  R$ 800,00    contact_joao    2026-09-07 03:22:11  trail step-up pix_77b1c0e4aa10
  1 em UNKNOWN — o banco sabe o que aconteceu e este processo não; ver docs/runbook.md
```

A coluna **próximo passo** já traz o comando pronto. A senha do DSN nunca é
impressa; o host é — confira que é o banco que você acha que é antes de agir.

Se não houver nada:

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  nada aguardando ação
```

### 4.2 Reconciliar — o banco pagou

```console
$ trail reconcile pix_4c1e0a7b93d2
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  perguntando ao banco pela chave de idempotência; nenhum pagamento novo é feito por este comando
  banco consultado: MockBank com 1 pagamento(s) conhecido(s)
  COMPLETED pix_4c1e0a7b93d2
  confirmado junto ao banco: o pagamento foi feito
  R$ 1.250,13 → Renata Silva  ·  estado COMPLETED
  recibo pay_31f0c9a2 · E7C4A1B0D9E3 · COMPLETED
```

**O que dizer ao cliente:** o PIX foi feito; o comprovante é o `end_to_end_id`
acima. Não repita o pagamento.

### 4.3 Reconciliar — o banco não tem registro

```console
$ trail reconcile pix_4c1e0a7b93d2
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  perguntando ao banco pela chave de idempotência; nenhum pagamento novo é feito por este comando
  banco consultado: MockBank com 0 pagamento(s) conhecido(s)
  FAILED pix_4c1e0a7b93d2
  não executado
  R$ 1.250,13 → Renata Silva  ·  estado FAILED
```

⚠️ **Leia o contador antes de acreditar no `FAILED`.** Ver a seção 6: com
`0 pagamento(s) conhecido(s)`, `FAILED` significa "este livro não tem registro",
que só é a mesma frase que "o dinheiro não saiu" quando o banco consultado é o
banco de verdade.

Com um banco que realmente responde, `FAILED` é a resolução correta: nada foi
debitado, e o cliente pode pedir o PIX de novo pela conversa (um pedido novo é
um intent novo — nada é "retentado" por aqui).

### 4.4 Rodar duas vezes é seguro

```console
$ trail reconcile pix_4c1e0a7b93d2
  ...
  COMPLETED pix_4c1e0a7b93d2
  R$ 1.250,13 → Renata Silva  ·  estado COMPLETED
  recibo pay_31f0c9a2 · E7C4A1B0D9E3 · COMPLETED
```

Um intent já liquidado só relata o próprio estado. Nada é reexecutado.

### 4.5 Confirmar que a fila esvaziou

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  nada aguardando ação
```

## 5. Como ler a saída

| primeira palavra | significa | o que fazer |
|---|---|---|
| `COMPLETED` | o banco tem o recibo: o dinheiro saiu | informar o cliente; nada a repetir |
| `FAILED` | o banco não conhece a chave (leia o contador de pagamentos, seção 6) | o cliente pode pedir de novo pela conversa |
| `DENY` · `referência desconhecida para esta sessão` | o id não existe **ou** não é deste `TRAIL_CUSTOMER_ID` | confira o id e o cliente configurado |
| `DENY` com outro texto | o intent não está em `UNKNOWN` | releia `trail intents`: ele já foi resolvido |

Códigos de saída: `0` quando a reconciliação aconteceu (tanto `COMPLETED`
quanto `FAILED`), `1` quando o comando foi recusado, `1` com mensagem em
`stderr` quando a configuração ou o banco de dados não permitiram nem tentar.

## 6. Limite desta versão (leia antes de confiar num `FAILED`)

**O banco é um mock e vive na memória do processo que pagou.** O `MockBank`
guarda os pagamentos num dicionário em processo — e, desde o escopo por
conversa, num dicionário por conversa dentro do agente. O `trail reconcile`
abre o **seu próprio** `MockBank`, que nasce vazio: ele nunca vai encontrar o
recibo de um pagamento feito pelo agente.

Por isso o comando imprime `banco consultado: MockBank com N pagamento(s)
conhecido(s)`. A regra operacional é curta:

> **Com `0 pagamento(s) conhecido(s)`, um `FAILED` não é prova de que o dinheiro
> não saiu.** É prova de que o livro consultado nunca viu pagamento nenhum.

Enquanto isso for verdade, a pergunta "o banco pagou?" só tem resposta
confiável de dentro do processo do agente (a tool `check_pix`, na conversa que
propôs o PIX). O comando desta seção passa a valer integralmente quando o banco
deixa de ser um mock em processo e vira um serviço que os dois processos podem
perguntar — é o que o T21 (`UNKNOWN` forçado em produção) exige de fato.

**Ownership do caminho fora de banda.** `step_up` e `reconcile` resolvem o
intent por `customer_id` + `intent_id` (`ControlPlane._owned_by_customer`), e é
isso que permite operá-los de outro processo — um operador não está na thread
da conversa do cliente. Duas consequências que valem para o seu turno:

- **cliente errado, id certo, mesma resposta:** um `intent_id` de outro
  `TRAIL_CUSTOMER_ID` responde `referência desconhecida para esta sessão`.
  Confira a primeira linha da saída antes de suspeitar do id.
- **consentimento não afrouxou:** `confirm` continua exigindo cliente **e**
  sessão. Nenhum comando deste runbook confirma um pagamento.

## 7. O outro comando fora de banda: `trail step-up`

Não faz parte da recuperação de `UNKNOWN`; está aqui porque aparece na mesma
lista. É a simulação do **callback do aplicativo**: quando a política exigiu
autenticação forte (`AWAITING_STEP_UP`), a garantia chega por um canal que não
é a conversa.

```console
$ trail step-up pix_77b1c0e4aa10
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  REQUIRE_CONFIRMATION pix_77b1c0e4aa10
  apresente exatamente este valor e destinatário e peça confirmação explícita
  R$ 800,00 → João Pereira  ·  estado AWAITING_CONFIRMATION
  a autenticação foi registrada; a confirmação continua sendo do cliente, na conversa onde o PIX foi proposto
```

O step-up concede **garantia**, nunca **consentimento**: a política é reavaliada
e o cliente ainda precisa confirmar na conversa em que o PIX foi proposto. Não
existe caminho por aqui que mova dinheiro.

## 8. Quando escalar

- `trail intents` lista o mesmo intent em `SUBMITTED` depois de o agente ter
  subido: o sweep não rodou — verifique o log de boot do agente.
- O cliente relata **dois** débitos para um mesmo pedido: pare, não reconcilie
  mais nada e leve o `intent_id` para quem cuida do ledger; a chave de
  idempotência é o `intent_id` e um débito duplo contradiz a invariante I1.
- `trail intents` mostra um DSN que não é o do ambiente que você pretende
  operar: pare e corrija `TRAIL_DATABASE_URL` antes de qualquer comando de
  escrita.
