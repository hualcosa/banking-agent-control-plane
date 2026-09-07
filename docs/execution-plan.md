# Plano de execução — Agent Control Plane (blueprint de implementação)

## Contexto

`ROADMAP.md` (commit 8a63b95) fixa **o que** construir: 6 marcos, 7 posts. Este plano fixa **em que ordem e o que roda em paralelo** para terminar no menor prazo sem pular verificação. Dev solo + subagentes do Claude Code: "paralelo" = tarefas sem arquivos em comum e sem dependência, executáveis por subagentes sem conflito de merge. Cada sessão termina num gate verificável (um comando que passa), nunca em "acho que está pronto".

Dois achados da exploração mudaram o desenho original:
1. **`ControlPlane` fica síncrono.** LangChain roda tools `def` em threadpool (`langchain_core/tools/base.py:932`), então um `psycopg_pool.ConnectionPool` **síncrono** (dep já instalada, não usada) resolve — sem converter 33 testes pra async, sem tocar `turns.py`/`app.py` no caminho crítico.
2. **O harness de crash nasce sobre `MemoryStore`.** Com o storage atrás de uma interface, "crash" = descartar o objeto `ControlPlane` e reconstruir sobre o mesmo store. Só o sabor "processo morreu de verdade" precisa de Postgres. O harness roda em paralelo com o `PgStore` e é reapontado no fim (~1 sessão economizada).

## Decisões fechadas (com o usuário, 2026-08-31)

| Decisão | Escolha |
|---|---|
| Compute AWS (marco 4) | **AgentCore Runtime** (revisado 2026-09-01 — a audiência é o nicho enterprise-AI da AWS). MicroVM por sessão, `network_mode: VPC` → RDS Postgres (documentado, GA). Contrato: `POST /invocations` + `GET /ping` na 8080, **linux/arm64**, SSE preservado (chunk 10MB, stream até 60min). FastAPI é caminho de primeira classe: o app ganha `/invocations` embrulhando `run_turn` e mantém `/threads` pro compose local — thread_id vai no payload, session id no header (≥33 chars; UUID de 36 passa) |
| IaC | **CDK TypeScript**, copiando (sem forkar) do template `awslabs/fullstack-solution-template-for-agentcore` (Apache-2.0): `backend-construct.ts` (L2 `agentcore.Runtime` + Identity + custom resources), `cognito-construct.ts`, `frontend/src/lib/agentcore-client/` (client SSE com parser LangGraph pronto), `patterns/utils/auth.py`. VPC é nossa: 2+ subnets privadas em `sae1-az1..3`, NAT, endpoints ecr.dkr/ecr.api/logs + S3 gateway |
| Observabilidade deployada | **AgentCore Observability** (ADOT SDK, `opentelemetry-instrument` no CMD — como o Dockerfile do template; Transaction Search ligado, que Evaluations exige de todo jeito). Langfuse continua no compose local. ADR do marco 6 mantém os 3 candidatos com números: AgentCore · Langfuse Cloud (2 env vars) · LangSmith (OTLP + ~30 linhas de aliases). Risco conhecido: TracerProvider próprio do TRAIL (`telemetry.py:271`) × auto-instrumentação ADOT — orçar uma tarde |
| Identidade (marco 2 → 4) | Marco 2: **header assinado com HMAC** + mapa estático (local, offline, testável). Marco 4: **Cognito JWT** no `customJWTAuthorizer` do Runtime (com `customClaims` pro `customer_id`), header `Authorization` no `requestHeaderAllowlist`, decode em processo sem verificar assinatura (o Runtime já validou — padrão do template). O seam é o mesmo (`configurable` em `turns.py:98`); só troca o resolvedor |
| AgentCore Policy | **Não substitui o control plane — e isso é a tese confirmada.** Cedar sobre tools do Gateway, só allow/deny + `suppressOutput`: sem `REQUIRE_CONFIRMATION`, sem idempotência, sem máquina de estados durável, sem ledger; temporal é por sessão com id do caller (nova sessão zera contadores — a própria AWS flagra). Cobre ~1 das 5 camadas. Vira o ADR/benchmark âncora do marco 6: CREATE_PIX espelhado atrás de Gateway+Cedar em `LOG_ONLY` vs o plane — cobertura das invariantes + latência ($0.000025/req) |
| AgentCore Evaluations | Candidato pro marco 6 (não substitui a matriz, que é pytest abaixo do agente): lê traces OTel do LangGraph de qualquer runtime, GA em sa-east-1. ADR: TRAIL evals vs Evaluations |
| Memory / Gateway | **Não usar** (checkpointer LangGraph no Postgres já é dono do estado; Gateway só entra pro experimento de Policy no marco 6) |
| Branch strategy | **`main` única rodando local E AWS** — diferença em config (12-factor), nunca uma branch AWS de vida longa (a matriz rodaria numa branch e o deploy sairia de outra: os números parariam de descrever o que está no ar). A MESMA imagem responde `/ping` (contrato Runtime) e `make chat` (compose) — isso entra no gate do S6. Branches curtas por tarefa via PR normal. Commits da adaptação AgentCore tocam SÓ `src/trail/*` + `Dockerfile`, separados dos commits de domínio, para cherry-pick no TRAIL upstream (`~/Documents/TRAIL`) pós-marco 4 — sem sincronização contínua entre os repos durante o projeto |
| Step-up out-of-band | **CLI `trail step-up <intent_id>`** — processo separado → Postgres compartilhado (mesma estrutura de um callback de app). Rota HTTP só se a demo precisar |
| Outbox | Tabela `ledger_events` com o `execution_request` commitado **em transação própria antes** da chamada ao banco; sem worker de relay — a "entrega" é o sweep de restart + `trail reconcile` (dizer isso no post 2) |
| Ownership do step-up | Step-up amarra a `customer_id` + `intent_id` (cross-channel por natureza); `confirm` continua amarrado à sessão. Vira ADR — e pré-paga a confirmação cross-channel do marco 5 |
| Matriz N≥100 | **pytest sobre `ControlPlane` direto** (`make matrix`, segundos, CI). Golden set com LLM roda N≈20 e é reportado como tabela separada, nunca rotulado "invariante" |

## Fatos verificados que sustentam o plano (file:line)

**Erros/lacunas do V0** (matéria-prima dos posts 1–2):
- Violação da invariante: `examples/banking/tools.py:147 approve_step_up` (LLM repassa step-up). Prompt regra 4 em `examples/banking/agent.py`; teste `tests/unit/test_banking_agent.py:187` exercita a tool.
- IDOR no `explain`: `plane.py:288` sem `ctx`; `tools.py:171` sem `runtime` — e o ledger contém o `confirmation_id` (`plane.py:239`), token que `confirm` aceita. Entra no threat model E no post 1.
- Janela de crash: `plane.py:355` grava `execution_request` antes do banco, `:378` grava recibo depois. Intent preso em `SUBMITTED` não tem saída: `reconcile` (`plane.py:270`) só aceita `UNKNOWN`. **Reusar a aresta `("SUBMITTED","timeout")→UNKNOWN` que já existe (`state.py:66`)** no sweep de restart — sem aresta nova.
- Confirmação: id aleatório, sem TTL (`confirmed_at` nunca lido), sem digest do conteúdo. Duas células da matriz ("confirmação velha", "ação mutada") são **inexpressáveis** até esses campos existirem.
- Race no eval (viva HOJE, não só em N≥100): `PLANE` global (`tools.py:37`) + saldo mutável + `paid_before` mutável (`bank.py:131`, muda o sinal `new_recipient` entre runs) + runner `concurrency=4`. Segundo `make eval` no mesmo container pontua diferente do primeiro — invalida comparação com baseline.
- Política sem relógio: regra noturna entra entre a regra 1 e 2 de `policy.py:87-120`, com clock injetado (`evaluate(..., now=None)`) pra não contaminar 26 testes.

**Reuso do TRAIL** (não construir):
- Schema: `db/schema.sql` (SQL puro, idempotente, aplicado por initdb + `evals/store.py:65 ensure_schema`). `intents`/`ledger_events` entram aí. **Armadilha:** initdb só roda em volume vazio — o control plane precisa aplicar o schema no startup como `store.py` já faz, senão "relation does not exist" em volume dev existente.
- Provider: `runtime/agent.py:85` hardcoded `openai:` → virar setting + `langchain-aws` (~15 linhas). Acesso a modelo não é mais gate (a página Model Access foi aposentada em 2025-09-29; modelos serverless habilitam sozinhos na 1ª invocação). O que resta verificar é o **catálogo** de sa-east-1: `aws bedrock list-inference-profiles --region sa-east-1` no dia 1 responde se tool-calling existe nativo ou só via perfil cross-region — dado do ADR de residência.
- Identidade: seam único em `turns.py:98` (`configurable`); zero auth em `app.py`.
- Evals: checks só no último turno (`runner.py:189-191`); extensão `turn_checks` ≈ 8 linhas. `Observation` não vê estado do plane → matriz vive em pytest.
- Coverage: `fail_under=90` sobre `trail`+`control_plane` no tier unit. `PgStore` (SQL só roda em integração) DERRUBA o gate → módulo próprio `control_plane/pgstore.py` + `omit` no coverage **no mesmo commit**.

## Correções no ROADMAP.md (aplicar na sessão 1, commit próprio)

1. Gate do marco 3: `make eval` → `make matrix` (pytest) + `make eval` (LLM, N≈20, tabela separada). O texto atual se contradiz (linha 100 vs 109).
2. Marco 2 ganha 3 itens: TTL de confirmação, digest da ação amarrado à confirmação, sweep de restart (`SUBMITTED→UNKNOWN`).
3. Marco 2 anota a ordem interna: Postgres → identidade → step-up CLI → regras BACEN (o step-up out-of-band só funciona com storage compartilhado).
4. Marco 1 ganha teto de custo mensal (USD) ao lado da meta de latência.
5. Marco 5: anotar que a mudança de ownership cross-channel pertence ao marco 2 (senão o diff da voz fica inflado).

## Tarefas (esforço: S<1h · M≈meia sessão · L≈sessão)

> **Estado das tarefas — nota acrescentada depois de S1–S5 e do primeiro commit
> de voz.** Este plano é documento histórico: o raciocínio abaixo é o de
> 2026-08-31 e fica como está, inclusive onde a execução discordou dele. A
> coluna **Estado** é a única coisa acrescentada, e diz apenas *feito* ou *não
> começou* — o que cada tarefa entregou está no `ROADMAP.md` (marco por marco,
> com o gate) e em `docs/threat-model.md` (linha por linha, com o teste).
>
> **Feito (S1–S5):** T1 · T2 · T3 · T4 · T5 · T6a · T6b · T6c · T7 · T8 · T9 ·
> T10 · T11 · T12 · T13 · T14 · T15 · T16 · T18 · T19 · T22 · T23 · T24.
> **Não começou:** T17 · T17b · T17c (o marco 4 inteiro: CDK, contrato do
> Runtime, UI deployada) · T20 · T21 · T25 · T26 · T27 · T28 · T29.
>
> Três notas onde o plano e a execução divergiram, e a divergência é informação:
>
> * **T19 saiu antes da hora, e por um bom motivo.** Runbook e `trail
>   intents`/`trail reconcile` são itens do marco 4, mas o step-up out-of-band do
>   T10 precisava dos mesmos comandos, então saíram juntos em S5. O marco 4
>   continua não começado: o runbook nunca foi executado contra produção.
> * **T6c está feito e ainda não está ligado.** O `PgStore` existe, é testado em
>   integração e é o que o CLI abre; o processo que serve o chat ainda constrói
>   `ControlPlane()` sobre `MemoryStore` (`examples/banking/tools.py:56`). A
>   durabilidade está provada no store, não no serviço.
> * **T13 rendeu uma armadilha que o plano não previu.** Uma célula da matriz
>   pode passar de `100/100` a "não se aplica" sem ficar vermelha — foi o que
>   aconteceu numa rodada de mutação que desligou o sweep. `MUST_APPLY` fixa
>   quais invariantes cada cenário tem de alcançar de fato.

| # | Tarefa | Pré-req | Esf | Arquivos | Verificação | Estado |
|---|---|---|---|---|---|---|
| T1 | `docs/threat-model.md`: adversário × invariante × teste (incluir IDOR do explain, replay de confirmação, injeção) | — | M | novo | toda linha cita um id de teste | **feito** |
| T2 | CI: `pytest -m unit --cov` em PR | — | S | `.github/workflows/ci.yml` | um PR vermelho | **feito** |
| T3 | `explain(ctx, intent_id)` + ownership | — | S | `plane.py`, `tools.py`, testes | `OTHER_SESSION` recebe `[]` | **feito** |
| T4 | Apagar `approve_step_up` + regra 4 do prompt + teste | — | S | `tools.py`, `agent.py`, `test_banking_agent.py`, `golden.py` | nenhuma tool aprova nada | **feito** |
| T5 | Regras BACEN (noturna 20h–06h + limite/tx), clock injetado | — | M | `policy.py`, `tests/unit/test_policy.py` novo | testes com relógio fixo | **feito** |
| T6a | `db/schema.sql`: `intents`, `ledger_events` | — | S | `db/schema.sql` | aplica 2× limpo | **feito** |
| T6b | `Store` protocol + `MemoryStore`; `ControlPlane(store=…)` | — | M | `control_plane/store.py` novo, `state.py`, `plane.py` | 21 testes existentes passam **sem edição** | **feito** |
| T6c | `PgStore` (pool síncrono, aplica schema no startup) | T6a,T6b | M | `control_plane/pgstore.py` novo, `pyproject.toml` (omit) | `make test` ≥90% E `make test-integration` | **feito** |
| T7 | Sweep de restart `SUBMITTED→UNKNOWN` (aresta existente) | T6b | M | `plane.py`, lifespan | kill no meio do PIX → restart → sweep → reconcile → 1 débito | **feito** |
| T8 | TTL de confirmação + digest HMAC da ação | T6b | M | `state.py`, `plane.py` | expirada→CANCELLED; digest divergente→DENY | **feito** |
| T9 | Identidade HMAC: header → `app.py` → `configurable` → `context_for` | — | M | `app.py`, `turns.py`, `tools.py`, `cli.py`, testes | header forjado→401; 2 clientes isolados | **feito** |
| T10 | `trail step-up <intent_id>` no CLI | T6c,T4 | M | `cli.py`, runbook | step-up completa sem tocar o agente | **feito** |
| T11 | `Case.turn_checks` + loop no runner | — | S | `evals/cases.py`, `evals/runner.py` | suite atual verde + 1 caso multi-turno | **feito** |
| T12 | Seam de crash: `FaultyBank(MockBank)` com `crash_at` | T6b | S | `tests/fakes.py` | levanta entre débito e recibo | **feito** |
| T13 | Matriz: 5 invariantes × ~6 cenários × N≥100, seeded | T12,T7,T8 | L | `tests/unit/test_invariants.py` novo | `pytest -m matrix` verde, <60s | **feito** |
| T14 | Renderer + `make matrix` | T13 | S | `Makefile` | tabela impressa, exit≠0 em célula vermelha | **feito** |
| T15 | Golden set adversarial (+5 casos) | T11,T4,T9 | M | `golden.py` | ambíguo, correção, injeção, ação não suportada, saída malformada | **feito** |
| T16 | Matar a race do eval (banco por customer / plane por thread) | T9 | S | `bank.py` ou `tools.py` | 3 `make eval` seguidos, mesmo resultado | **feito** |
| T17 | IaC (CDK TS): VPC (subnets privadas em sae1-az1..3, NAT, endpoints), RDS, ECR, Cognito, AgentCore Runtime (VPC mode + JWT authorizer + header allowlist) — copiando constructs do template FAST | — | L | `infra-cdk/` novo | `cdk synth` limpo | não começou |
| T17b | Contrato do Runtime: `/invocations` (embrulha `run_turn`, SSE) + `/ping` no app; build arm64; `opentelemetry-instrument` no CMD | — | M | `app.py`, `Dockerfile` | container local responde ao contrato via curl | não começou |
| T17c | UI deployada fala com o Runtime: adotar `agentcore-client` (parser LangGraph pronto) + login Cognito | T17 | M | `ui/` | chat streaming em prod via JWT | não começou |
| T18 | Provider seam `TRAIL_LLM_PROVIDER` + `langchain-aws` | — | S | `agent.py:85`, `config.py` | `bedrock_converse:` monta sem rede | **feito** |
| T19 | Runbook + `trail intents`/`trail reconcile` | T6c | M | `cli.py`, `docs/runbook.md` | humano resolve `UNKNOWN` só com o doc | **feito** |
| T20 | ADR residência sa-east-1 | T18 | S | `docs/adr/` | cita latência medida | não começou |
| T21 | Deploy + `UNKNOWN` forçado em prod (PIX `,13`) | T17,T19,T7 | L | — | runbook executado em prod | não começou |
| T22 | `git tag before-voice` | T21 | S | — | baseline do diff | **feito** |
| T23 | Voz simulada: `stt_confidence` no `Context` + sinal de risco + tabela de transcrições sintéticas ("trezentos"→"treze", "Renata"→"Renato") | T22 | M | `actions.py`, `policy.py`, `voice.py` novo | confiança baixa → step-up/more-info; colapso de magnitude nunca executa | **feito** |
| T24 | Confirmação cross-channel (já paga pela decisão de ownership do T10) | T8,T9 | S | `plane.py:451` | voz propõe → texto confirma → 1 débito | **feito** |
| T25 | `git diff --stat before-voice -- src/control_plane/` + 1 chamada STT real pra provar o adaptador | T23,T24 | S | — | o número (~6 linhas esperadas) | não começou |
| T26 | Benchmark extração (3 modelos × golden set congelado) | T15 congelado | M | `Makefile` loop sobre `TRAIL_MODEL` | mesma `golden_set_version` | não começou |
| T27 | Benchmark STT pt-BR (valores e nomes) | T23 | M | `bench/` | WER em valores/nomes | não começou |
| T28 | Bedrock vs API externa + ADR observabilidade (Langfuse/LangSmith/AgentCore) | T18,T26 | M | `docs/adr/` | tabela latência/custo/residência | não começou |
| T29 | Post final + diagrama + índice de ADRs | tudo | L | `docs/` | cada seta → ADR → número | não começou |

## Caminho crítico (confirmado pelos dois planejadores)

```text
T6b Store+MemoryStore → T6c PgStore → T7 sweep → T8 TTL/digest → T13 matriz → T14 make matrix
                          ↘ T12 crash seam roda em paralelo com T6c
```
~5 sessões até a matriz; ~9–10 até o fim (o contrato AgentCore + CDK adicionam ~1 sessão ao marco 4). Tudo de marcos 1, 4 (CI/IaC/provider/T17b) e o loop de benchmark ficam FORA do caminho.

## Cadeias seriais (mesmos arquivos — nunca 2 subagentes ao mesmo tempo)

- **`plane.py`** (gargalo): T3 → T6b → T7 → T8 → T24
- **`tools.py`**: T3+T4 (mesma sessão) → T9 → T23
- **`golden.py`**: T4 → T15 → congelar antes de T26
- **`Makefile`**: um dono por sessão (T14, T26)

## Sessões (cada uma fecha num gate)

**Dia 1, antes de tudo (5 min):** mapear o catálogo de sa-east-1 (`aws bedrock list-foundation-models --region sa-east-1` + `list-inference-profiles`) — quais modelos com tool-calling existem nativos vs só via perfil cross-region (dado do ADR de residência; não há mais aprovação de acesso a modelo, a página Model Access foi aposentada em 2025-09-29); rodar `make up && make eval && make eval` pra confirmar a race com os próprios olhos.

**S1 — "onde o V0 errou"** · você: T3+T4 + medir p95/custo (1 `make eval`, `concurrency=1`) + correções no ROADMAP.md · subagentes em paralelo: T1, T2, T5, T11, T18, T17-esqueleto · merge: CI primeiro, depois o seu, depois o resto · **Gate: `make lint && make test` ≥90%; nenhuma tool aprova/executa nada.** → material do post 1 completo.

**S2 — seam de storage** · você: T6b (regra de honestidade: `git diff tests/unit/test_control_plane.py` só tem adições) · subagentes: T6a, T26-esqueleto · **Gate: `make test` verde com testes antigos intactos.**

**S3 — Postgres** · você: T6c · subagentes: T12 (sobre MemoryStore), T9 · **Gate: `make test` ≥90% E `make test-integration`.** (Armadilha do coverage: `omit` no mesmo commit.)

**S4 — recuperação** · você: T7+T8 · subagentes: T19, T15 · **Gate: teste de integração que mata o processo entre `plane.py:355` e `:378`, reinicia, varre, reconcilia e afirma `len(bank.payments)==1`.** → post 2.

**S5 — a matriz** · você: T13+T14+T16, T10 · subagentes: T20, T17-final · **Gate: `make matrix` verde, N≥100, <60s.** → post 3 (espinha dorsal, meio do projeto).

**S6 — contrato + IaC** · você: T17b (contrato `/invocations`+`/ping`, arm64) · subagentes: T17 (CDK), T17c (UI/agentcore-client) · **Gate: container local responde ao contrato do Runtime; `cdk synth` limpo.**

**S7 — produção** · T21 (deploy AgentCore Runtime + RDS via VPC mode) · **Gate: `UNKNOWN` real resolvido em prod só com o runbook.** → post 4.

**S8 — voz** · T22–T25 · **Gate: o número do `git diff --stat`.** → post 5.

**S9 — benchmarks + final** · T26–T29 + experimento Policy em shadow (`LOG_ONLY`) · **Gate: script que falha se algum ADR não aponta pra um número.** → post 6 (a série pode virar 8 posts se o shadow de Policy render achado próprio).

## Armadilhas conhecidas (não redescobrir)

1. Coverage ≥90% quebra com `PgStore` → módulo próprio + `omit` no mesmo commit (`pyproject.toml:156-160`).
2. initdb só roda em volume vazio → `PgStore` aplica `db/schema.sql` no startup (padrão de `evals/store.py:65`).
3. `git diff tests antigos` no T6b: se o seam de storage exige editar teste antigo, o seam está errado.
4. Adicionar casos ao golden set muda `golden_set_version` e invalida baselines → congelar `golden.py` ANTES de rodar o benchmark T26.
5. Step-up out-of-band com `_same_principal` atual rejeita 100% dos callbacks (exige session igual) → afrouxar para customer+intent É a tarefa, não um bug do caminho.
6. Diff da voz ~0 sem STT real é infalsificável → 1 chamada STT real no T25, e dizer no post que WER de campo é o benchmark T27, não a simulação.
7. **Residência (CRIS global):** Policy e Evaluations a partir de sa-east-1 usam inferência cross-region GLOBAL — payload pode ser processado fora do Brasil, e CloudWatch não diz onde. Vai pro ADR de residência (T20) como achado, não como surpresa.
8. **VPC mode do Runtime:** subnet pública NÃO dá internet (NAT obrigatório pra alcançar Bedrock); sem o S3 gateway endpoint o re-pull da imagem passa pelo NAT e custa; bug aberto no template (#49): stream fica pendurado em VPC mode durante o save de Memory — não usamos Memory, mas testar o streaming em VPC antes de gravar demo.
9. **Sessões do Runtime:** 2 chamadas concorrentes no MESMO session id → HTTP 409 (serializar por thread); sessão é efêmera (idle 15min / máx 8h) — irrelevante pra nós porque o estado vive no Postgres, mas o eval runner precisa de session ids distintos por caso (já tem: um thread por caso).

## Verificação end-to-end do plano inteiro

O projeto está pronto quando os quatro comandos passam em sequência num checkout limpo:
`make test` (≥90%, offline) → `make matrix` (invariantes, N≥100) → `make eval` (comportamento do agente, determinístico após T16) → `make smoke AGENT_BASE_URL=<prod>` (UNKNOWN forçado + runbook).

## Primeiro ato da implementação

Copiar este plano para `docs/execution-plan.md` no repo (o usuário vai usá-lo como blueprint), aplicar as 5 correções no `ROADMAP.md`, commitar os dois juntos.
