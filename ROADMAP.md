# Roadmap — Agent Control Plane (The Odd Trail)

Este arquivo é a referência das próximas sessões. Cada marco responde **uma pergunta**.
Só passamos pro próximo quando a pergunta do atual tem uma resposta com evidência
(um teste, um número, um diff), não só um texto.

## A ideia em uma frase

O LLM **propõe** uma ação. O control plane **decide** se ela pode acontecer.
O gateway **executa**. O LLM nunca encosta no dinheiro diretamente.

```text
linguagem natural → LLM → ação tipada → regras determinísticas → banco
```

## Onde estamos (depois de S1–S5 e do primeiro commit de voz, 21 commits desde `f043ea3`)

| Marco | Estado | Evidência |
|---|---|---|
| 1 — ameaças e metas | **feito** | `docs/threat-model.md`, revisado em S5: 23 adversários, 19 com teste nomeado, 4 lacunas honestas |
| 2 — endurecer o V1 | **feito** | `make lint && make test` (410 testes unitários, gate de 90%) + `make test-integration` (22 testes) |
| 3 — evidência de falha | **feito** | `make matrix`: 5 invariantes × 11 cenários × N=100, sem LLM |
| 4 — AWS + operação | **não começou** | o `docs/runbook.md` já existe (escrito em S5, junto do `trail intents`/`trail reconcile`), mas nunca foi executado contra produção porque não há produção |
| 5 — voz | **em andamento agora** | primeiro commit dentro; `git diff --stat before-voice -- src/control_plane/` = 63 inserções, 5 remoções em 3 arquivos — e são dois números, não um (ver o marco) |
| 6 — benchmarks e decisão final | não começou | — |

O `src/control_plane/` tem ações tipadas, máquina de estados, política (com duas
regras reais do BACEN e relógio injetado), idempotência, ledger, reconciliação,
TTL de confirmação, digest da ação com chave, sweep de restart e um `Store` com
implementação em Postgres. O que ainda é atalho está marcado no código:

```bash
grep -rn "ponytail:" src examples
```

Cada linha dessa lista é um atalho consciente do V0. Essa lista **é** o backlog do V1.
Duas delas — identidade fixa e "estado só em dicts" — saíram da lista nos marcos
2 e 3; a ressalva honesta é que o processo que serve o chat ainda monta o plane
sobre `MemoryStore` (só o CLI abre o `PgStore`), então a durabilidade está
provada no store e ainda não está ligada no serviço.

## As invariantes (o que nunca pode acontecer)

1. Nunca mover dinheiro duas vezes pelo mesmo pedido.
2. Nunca executar sem autorização válida.
3. Nunca executar uma ação diferente da que foi confirmada.
4. Ambiguidade → para, não chuta.
5. Estado desconhecido (`UNKNOWN`) → pergunta pro banco, nunca tenta de novo às cegas.

Toda sessão deve terminar com essas 5 ainda verdadeiras.

## O que NÃO vamos fazer (escopo fechado)

- Só 3 capacidades: `GET_BALANCE`, `GET_CARD_TRANSACTIONS`, `CREATE_PIX`.
- 1 conta, 1 cliente por sessão.
- Sem PIX agendado, sem bloqueio de cartão, sem multi-tenant.
- Banco mockado até o fim (a fronteira é o assunto, não o banco).
- `REVERSED` existe como estado mas não ganha fluxo (MED é outro projeto).

Regra de exceção: se um marco mostrar que a **abstração** precisa mudar
(ex.: voz precisa de um campo `confidence` na ação), isso entra. Feature nova, não.

---

## Marco 1 — Modelo de ameaças, invariantes e metas · **FEITO (S1)**

**Pergunta:** contra o que estamos nos defendendo, e o que significa "funciona"?

**Por que primeiro:** sem lista de adversários, "fronteira de confiança" é só uma
palavra. Sem meta de latência, o marco 4 não sabe que infra escolher.

**O que fazer:**
- Uma página listando adversários: injeção de prompt via descrição de transação,
  replay de confirmação entre sessões, sequestro de sessão, provedor de modelo
  comprometido, resposta maliciosa da API do banco.
- Ligar cada adversário a pelo menos uma invariante.
- Definir metas: p95 de latência por turno, custo por conversa, disponibilidade —
  e um teto de gasto mensal na AWS (USD). Estourar o teto exige um ADR.
- Escrever a lista de "não vamos fazer" (acima) no README.

**Terminou quando:** existe `docs/threat-model.md` com tabela adversário → invariante → teste que vai provar.

**Fechado.** O arquivo existe e é falsificável em um comando (o `grep` no fim
dele). Revisado ao fim de S5: 23 linhas, 19 cobertas, 4 lacunas — A2 (nada
screena saída de tool), A11 (fabricação no canal, medida com LLM), A14 (o recibo
do banco nunca é conferido contra a ação confirmada) e A22 (endpoints de thread
autenticam mas não escopam por cliente). Nenhuma tem tarefa no plano. A linha
A23 (transcrição errada tratada como instrução) entrou com a voz e já nasceu
coberta.

---

## Marco 2 — Endurecer o V1 · **FEITO (S1–S4)**

**Pergunta:** o V0 respeita a própria regra dele?

**A resposta honesta era não, em dois lugares.** O LLM repassava a aprovação de
step-up (a tool `approve_step_up`), o que quebra "o LLM nunca encosta no
dinheiro"; e `explain` não pedia contexto, então a trilha de auditoria — que
contém o `confirmation_id`, token que `confirm` aceita — era legível de
qualquer sessão. Os dois estão corrigidos (S1); o primeiro relatório do projeto
é sobre eles: onde o V0 errou, a correção e o teste que teria pegado.

**O que fazer** (a ordem interna importa: Postgres → identidade → step-up → regras BACEN —
o step-up out-of-band só funciona com storage compartilhado):
- Trocar dicts por Postgres (`state.py:20`, `plane.py:20`); o `execution_request`
  é commitado em transação própria ANTES da chamada ao banco.
- Identidade vem do canal, não do agente (`tools.py:16`).
- Step-up vira canal out-of-band: `trail step-up <intent_id>` no CLI (processo
  separado → Postgres compartilhado, a mesma estrutura de um callback de app).
  A tool `approve_step_up` já foi apagada (S1) — até o CLI existir, um intent em
  `AWAITING_STEP_UP` não tem saída pelo chat, e essa é a resposta certa. O
  step-up passa a se amarrar a `customer_id` + `intent_id` (cross-channel por
  natureza); `confirm` continua amarrado à sessão — isso vira ADR e pré-paga o
  marco 5.
- Ownership de leitura, feito em S1: `explain(ctx, intent_id)` devolve `[]` para
  quem não abriu a trilha, e uma trilha emprestada é indistinguível de um id
  inexistente.
- TTL de confirmação e digest da ação amarrado à confirmação — sem esses campos,
  duas células da matriz do marco 3 ("confirmação velha", "ação mutada") são
  inexpressáveis.
- Sweep de restart: intent preso em `SUBMITTED` vira `UNKNOWN` na subida do
  processo (a aresta já existe em `state.py:66`; `reconcile` faz o resto).
- Duas regras reais do BACEN na política (`policy.py:61`): limite noturno 20h–06h
  e limite por transação, com relógio injetado. ~30 linhas, e a política deixa
  de ser ilustrativa.

**Terminou quando:** o agente não tem mais nenhuma tool capaz de aprovar nada,
e o control plane sobrevive a reiniciar o processo no meio de um PIX.

**Fechado, item por item, com o comando que passa:**

| Item | Onde ficou | Gate |
|---|---|---|
| `approve_step_up` apagada (S1) | `examples/banking/tools.py` | `test_no_tool_can_grant_assurance` |
| `explain(ctx, …)` escopado (S1) | `plane.py:388` | trilha emprestada é indistinguível de id inexistente |
| Regras BACEN + relógio injetado (S1) | `policy.py`, `pix_nighttime_limit` | `tests/unit/test_policy.py` com relógio fixo |
| `Store` + `MemoryStore` (S2) | `control_plane/store.py` | os testes antigos passaram **sem edição** |
| `intents` e `ledger_events` (S2) | `db/schema.sql` | aplica duas vezes limpo |
| `PgStore` (S3) | `control_plane/pgstore.py` | `make test` ≥90% **e** `make test-integration` (22 testes) |
| Identidade vinda do canal (S3) | `src/trail/identity.py`, header `X-Trail-Identity` | header forjado → 401 sem oráculo; dois clientes isolados de ponta a ponta |
| Sweep de restart (S4) | `ControlPlane.sweep`, hook `AgentSpec.on_startup` | crash no meio do PIX → novo plane sobre o mesmo store → sweep → reconcile → **1 débito** |
| TTL de confirmação + digest com chave (S4) | `state.py`, `plane.confirm` | expirada → `CANCELLED`; digest divergente → `DENY` |
| Ownership: step-up e reconcile em cliente+intent, `confirm` na sessão (S4/S5) | `plane._owned_by_customer` | `test_step_up_is_bound_to_the_customer_not_the_session` · `test_consent_is_still_bound_to_the_conversation` |
| Step-up out-of-band de verdade (S5) | `trail step-up <intent_id>` | `test_step_up_from_another_process_reaches_the_confirmation` |

Fora do combinado, e vale registrar: CI roda `make lint` + `make test` em todo
PR (S1), e o provedor de modelo virou setting (`TRAIL_LLM_PROVIDER`, com
`langchain-aws` num extra `bedrock`) — o marco 4 não precisa de reescrita para
falar com o Bedrock.

**A ressalva honesta:** o `PgStore` está testado e é o que o CLI abre, mas o
processo que serve o chat ainda constrói `ControlPlane()` sobre `MemoryStore`
(`examples/banking/tools.py:56`). "Sobrevive a reiniciar o processo" está provado
no store e na integração, não no serviço. Ligar os dois é uma tarefa pequena e
não está feita.

---

## Marco 3 — Evidência de falha · **FEITO (S3–S5)**

**Pergunta:** as invariantes sobrevivem a crash e a adversário?

**Por que depois do marco 2:** testar crash em cima de dict na memória não prova
nada. Precisa do Postgres primeiro.

**O que fazer:**
- Matriz **invariante × cenário** com número em cada célula.
- Injeção de crash **abaixo do agente** (direto no control plane, determinístico):
  crash entre chamar o banco e gravar o ledger, timeout depois de `SUBMITTED`,
  confirmação de ação mutada, confirmação velha, pedido duplicado.
- Golden set adversarial **acima do agente** (precisa do LLM): destinatário
  ambíguo, usuário corrige o valor, injeção de prompt, ação não suportada,
  saída malformada do modelo.
- O harness de evals do TRAIL só checa o último turno (`src/trail/evals/cases.py:164`).
  Cenários multi-turno vão pro teste de control plane, não pro harness.

**Terminou quando:** `make matrix` (pytest direto sobre o control plane, sem LLM)
imprime a matriz e todas as células de invariante estão verdes com N ≥ 100 em
menos de um minuto. O comportamento do agente com LLM roda em `make eval`
(N ≈ 20) e é reportado como tabela separada — nunca rotulado "invariante".

**Fechado.** `make matrix` roda `tests/unit/test_invariants.py`: 5 invariantes ×
11 cenários (happy, confirmação repetida, execução concorrente, crash depois de
pagar, crash antes de pagar, timeout do banco, token emprestado, "sim" expirado,
ação mutada, destinatário ambíguo, recusa do banco) × N=100 trials semeados por
célula, em segundos, sem modelo. A injeção de crash abaixo do agente é o
`FaultyBank` (`tests/fakes.py`), que levanta uma exceção que o plane **não**
captura, antes ou depois do débito.

Três decisões que vieram de bugs encontrados durante a construção, e que são o
que faz o número significar alguma coisa:

- **Débito é contado, não inferido.** `len(bank.payments)` afirmaria a garantia
  do *banco* e passaria mesmo se o plane chamasse cinco vezes.
- **Célula que não se aplica imprime `·`.** Forçar toda invariante em todo
  cenário encheria a tabela de verde sem sentido.
- **Mas uma célula não pode ficar em branco em silêncio.** `MUST_APPLY` fixa
  quais invariantes cada cenário tem de alcançar de fato — uma rodada de mutação
  que desligou o sweep transformou um `100/100` em `·` e a suíte continuou verde.

A matriz foi mutation-tested: os checks foram rodados contra versões
deliberadamente quebradas do plane, e os que ainda passavam foram reescritos.

O golden set adversarial (acima do agente) ficou em `banking-v3`: 16 casos, 5
adversariais, 3 multi-turno — possíveis porque `Case.turn_checks` checa cada
turno, não só o último. Ele mede A2 e A11; não os fecha, e a tabela dele nunca é
rotulada "invariante".

---

## Marco 4 — Baseline em produção na AWS + operação · **NÃO COMEÇOU**

**Pergunta:** alguém consegue rodar isso às 3 da manhã?

**Estado:** nada de AWS existe — sem `infra-cdk/`, sem contrato
`/invocations` + `/ping`, sem imagem arm64, sem ADR de residência. O marco é
inteiro deploy e ainda não teve uma linha escrita.

O que já existe deste marco é a metade operacional, adiantada em S5 porque o
step-up out-of-band precisava dela: `docs/runbook.md` mais `trail intents`,
`trail step-up` e `trail reconcile`. Um humano resolve um `UNKNOWN` só com o
documento — **localmente**. O gate deste marco continua aberto, porque ele exige
um `UNKNOWN` forçado em produção e não há produção.

**O que fazer:**
- Deploy real na stack enterprise-AI da AWS (revisado 2026-09-01 — é o nicho da
  audiência): **AgentCore Runtime** (microVM, VPC mode → RDS Postgres),
  **AgentCore Identity** (Cognito JWT no authorizer, claims → `Context`),
  **AgentCore Observability** (ADOT → CloudWatch), IaC em CDK copiando
  constructs do template `awslabs/fullstack-solution-template-for-agentcore`.
  O app ganha o contrato do Runtime (`/invocations` + `/ping`, arm64) e mantém
  as rotas locais pro compose.
- Runbook: o que um humano faz com um intent em `UNKNOWN`. Caminho de
  reconciliação manual.
- ADR de residência de dados: Bedrock em `sa-east-1` tem poucos modelos. Se o
  PII tem que ficar no Brasil, a escolha entre modelo limitado, inferência
  cross-region ou provedor externo acontece **aqui**, não no marco 6.
  Achado da pesquisa: AgentCore Policy/Evaluations em sa-east-1 usam inferência
  cross-region **global** — o payload pode sair do Brasil. Entra no ADR.
- Acesso a modelo fica atrás da interface de provider do TRAIL. AWS-first é
  para infra; modelo é agnóstico desde o dia um.

**Terminou quando:** o sistema está no ar, com traces, e o runbook foi testado
forçando um `UNKNOWN` em produção.

---

## Marco 5 — Teste de estresse com voz · **EM ANDAMENTO**

**Pergunta:** o control plane é mesmo independente de canal?

**Métrica de sucesso é um número:**

```bash
git diff --stat <antes-da-voz> -- src/control_plane/
```

Se der ~0 linhas, a abstração aguentou e a história é "voz adicionou N linhas no
adaptador e 0 no caminho do dinheiro". Se não der ~0, o relatório é ainda melhor:
o que a voz revelou que faltava.

**O que fazer:**
- Um adaptador de voz (um provedor só, sem pipeline realtime próprio).
- Confiança do STT vira sinal de risco ("trezentos" vs "treze", "Renata" vs "Renato").
- Confirmação cross-channel: pedido por voz, confirmação por texto. A mudança
  de ownership que permite isso (step-up amarrado a customer + intent) é
  trabalho do marco 2 — atribuir lá, senão o diff da voz mede trabalho que
  não é da voz.

**Terminou quando:** um PIX por voz passa pelo mesmo `propose → confirm → execute`
e o diff no control plane está medido.

**Onde está.** O primeiro commit do marco entrou (`examples/banking/voice.py`,
`tests/unit/test_voice.py`) e o número já existe, contra a tag `before-voice`:

```text
src/control_plane/actions.py |  7 ++
src/control_plane/policy.py  | 14 ++
src/control_plane/plane.py   | 47 ++++++-----
3 arquivos, 63 inserções, 5 remoções
```

Lido com honestidade são **dois** números, e essa é a história:

- **21 linhas são voz de fato** — `Context.stt_confidence` e o sinal de risco
  `low_stt_confidence` que o lê. O campo tem de existir porque "não tenho
  certeza de ter ouvido isso" não é um fato que canal de texto nenhum saiba
  expressar, e é `None` em canal de texto em vez de 1.0: "certamente digitado" e
  "perfeitamente ouvido" são fatos diferentes. O sinal vive no risco, não como
  regra própria — incerteza sobre o que foi dito muda o cuidado com a ação, não
  escolhe a ação. **A abstração aguentou.**
- **As outras 42 são confirmação cross-channel**, que o plano de execução já
  atribui à decisão de ownership do marco 2, não à voz. É ali que a conta chega:
  um intent proposto por voz aceita confirmação (e `explain`) do mesmo cliente
  por outro canal, porque ler valor e destinatário por telefone e aceitar um
  "sim" falado é a confirmação mais fraca que este sistema conseguiria oferecer.

**Falta:** uma chamada real de STT para o diff não ser infalsificável (T25,
armadilha 6), e a decisão sobre o que o post reporta. O adaptador de hoje é uma
**simulação** e diz isso no próprio docstring: `TRANSCRIPTS` é uma tabela escrita
à mão das duas classes de confusão que o reconhecimento pt-BR faz com dinheiro e
nomes — colapso de magnitude ("trezentos" → "treze", erro de vinte vezes que o
cliente não pega numa leitura de volta) e confusão de destinatário ("Renata" →
"Renato", um fonema, duas pessoas). Uma tabela não produz taxa de erro; isso é o
benchmark T27, contra um reconhecedor de verdade.

---

## Marco 6 — Benchmarks e a decisão final · **NÃO COMEÇOU**

**Pergunta:** o que eu realmente colocaria em produção?

**Só 3 benchmarks.** O resto é ADR com raciocínio, não medição:

| Decisão | Como medir |
|---|---|
| Modelo para extrair a ação tipada | acurácia no golden set × p95 × custo |
| STT para pt-BR (valores e nomes) | taxa de erro em valores e nomes de contato |
| Bedrock vs API externa de modelo | latência, custo, residência |
| Control plane próprio vs AgentCore Policy | CREATE_PIX espelhado atrás de Gateway + Cedar em `LOG_ONLY`: cobertura das 5 invariantes, latência de autorização |

**Entrega final:** "A arquitetura que eu colocaria em produção — e por quê".
Diagrama final, metas de SLO, domínios de falha, fronteiras de confiança,
controles de segurança, índice de ADRs, o que ficou AWS e o que não ficou,
limitações e próximos passos.

**Terminou quando:** cada escolha no diagrama final aponta pra um ADR, e cada ADR
importante aponta pra um número.

---

## Posts

Post e marco são eixos diferentes: marco segue dependência de engenharia,
post segue narrativa. **Um post sai quando existe uma afirmação + uma evidência.**
Não quando um marco fecha.

### Esqueleto de todo post (sempre o mesmo)

```text
1. Pergunta      — a pergunta do marco, em uma linha
2. Afirmação     — a frase que o leitor vai repetir
3. Evidência     — o número, o diff, o teste
4. O que quebrou — o que não funcionou e o que isso ensinou
5. Decisão       — o ADR que saiu disso
```

Repetir a estrutura é o que sinaliza disciplina. O leitor aprende o formato
no post 1 e depois só lê conteúdo.

### Os 7 posts

| # | Título de trabalho | Afirmação | Evidência | Marco(s) |
|---|---|---|---|---|
| 0 ★ | Por que um control plane | O LLM não pode ser dono do dinheiro | V0 rodando: `manda 300 pra Renata` → propose → confirm → execute | nenhum (V0 já existe) |
| 1 | Onde o V0 errou | A regra do próprio projeto foi violada | a tool que aprovava + a trilha que vazava o `confirmation_id`, as duas correções e os testes que teriam pegado | 1 + início do 2 |
| 2 | Idempotência não é uma chave de dict | Crash no meio de um PIX não duplica dinheiro | Postgres + outbox + crash injection | fim do 2 + 3 |
| 3 ★ | A matriz | As 5 invariantes sobrevivem a falha e adversário | `make matrix` com N ≥ 100 por célula (o `make eval` com LLM, N ≈ 20, é tabela separada) | 3 |
| 4 | Rodar às 3h da manhã | Isso opera em produção | deploy + runbook testado + ADR `sa-east-1` | 4 |
| 5 | Voz custou N linhas | A abstração é independente de canal | `git diff --stat src/control_plane/` | 5 |
| 6 ★ | O que eu colocaria em produção | Recomendação defensável | 3 benchmarks + índice de ADRs | 6 |

★ = espinha dorsal. Os posts 0, 3 e 6 contam a história completa sozinhos:
tese → evidência → veredito. Os outros quatro são pontes. Se o tempo apertar,
são as pontes que caem, nunca a espinha.

O post 0 e o post 6 se espelham: o 0 abre com "o LLM não pode ser dono do
dinheiro" como hipótese; o 6 fecha com a mesma frase como veredito, agora
com os números do meio.

O post 1 admite erro. É o que mais transmite senioridade: mostra que o
critério existe independente do autor.

### Quando o número muda

- Marco sem achado surpreendente → vira parágrafo do post seguinte (série fica com 6).
- Marco com achado grande (ex.: voz quebrou a abstração) → ganha um segundo post (série fica com 8).
- O número flutua com a evidência, nunca com a vontade de publicar.

### Fallback de 5 posts

```text
0  tese + V0
1  onde o V0 errou + idempotência real   (marcos 1–2)
2  a matriz                              (marco 3)
3  AWS + runbook                         (marco 4)
4  voz + benchmarks + decisão final      (marcos 5–6)
```

Perde a voz como história própria — o diff vira uma linha do post final.

---

## Sequência resumida

```text
1. ameaças + metas      (doc)                                  FEITO   S1
2. endurecer V1         (Postgres, identidade, step-up real, regras BACEN)
                                                               FEITO   S1–S4
3. evidência de falha   (matriz invariante × cenário)          FEITO   S3–S5
4. AWS + runbook        (deploy + ADR de residência)           runbook feito, deploy não começou
5. voz                  (diff no control plane como métrica)   EM ANDAMENTO
6. benchmarks + final   (3 medições, 1 recomendação)           não começou
```

**Próximo passo quando voltar:** terminar o marco 5 (uma chamada real de STT,
para o diff não ser infalsificável) e então o marco 4 inteiro, que é o único
grande bloco intocado: `infra-cdk/`, contrato `/invocations` + `/ping`, imagem
arm64, deploy, e o `UNKNOWN` forçado em produção. Duas dívidas pequenas ficam no
caminho e valem ser pagas antes do deploy: ligar o serviço ao `PgStore` (hoje só
o CLI o usa) e escopar os endpoints de thread por cliente (A22 do threat model).
O plano de execução está em `docs/execution-plan.md`; as decisões já tomadas,
com evidência, em `docs/adr/`.
