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

## O que já existe (V0, commit f946c70)

O `src/control_plane/` já tem: ações tipadas, máquina de estados, política,
idempotência, ledger e reconciliação. Ou seja: os marcos 1–3 do plano original
já estão feitos em versão simplificada. O que falta está marcado no código:

```bash
grep -rn "ponytail:" src examples
```

Cada linha dessa lista é um atalho consciente do V0. Essa lista **é** o backlog do V1.

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

## Marco 1 — Modelo de ameaças, invariantes e metas

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

---

## Marco 2 — Endurecer o V1

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

---

## Marco 3 — Evidência de falha

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

---

## Marco 4 — Baseline em produção na AWS + operação

**Pergunta:** alguém consegue rodar isso às 3 da manhã?

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

## Marco 5 — Teste de estresse com voz (com prazo fechado)

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

---

## Marco 6 — Benchmarks e a decisão final

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
1. ameaças + metas      (doc)
2. endurecer V1         (Postgres, identidade, step-up real, regras BACEN)
3. evidência de falha   (matriz invariante × cenário)
4. AWS + runbook        (deploy + ADR de residência)
5. voz                  (diff no control plane como métrica)
6. benchmarks + final   (3 medições, 1 recomendação)
```

Próximo passo quando voltar: fechar o marco 1 — `docs/threat-model.md`, CI,
regras BACEN — e então o seam de storage (marco 2). O plano de execução está em
`docs/execution-plan.md`.
