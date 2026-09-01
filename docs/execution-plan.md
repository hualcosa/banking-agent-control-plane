# Plano de execução — Agent Control Plane (blueprint de implementação)

## Contexto

`ROADMAP.md` (commit 8a63b95) fixa **o que** construir: 6 marcos, 7 posts. Este plano fixa **em que ordem e o que roda em paralelo** para terminar no menor prazo sem pular verificação. Dev solo + subagentes do Claude Code: "paralelo" = tarefas sem arquivos em comum e sem dependência, executáveis por subagentes sem conflito de merge. Cada sessão termina num gate verificável (um comando que passa), nunca em "acho que está pronto".

Dois achados da exploração mudaram o desenho original:
1. **`ControlPlane` fica síncrono.** LangChain roda tools `def` em threadpool (`langchain_core/tools/base.py:932`), então um `psycopg_pool.ConnectionPool` **síncrono** (dep já instalada, não usada) resolve — sem converter 33 testes pra async, sem tocar `turns.py`/`app.py` no caminho crítico.
2. **O harness de crash nasce sobre `MemoryStore`.** Com o storage atrás de uma interface, "crash" = descartar o objeto `ControlPlane` e reconstruir sobre o mesmo store. Só o sabor "processo morreu de verdade" precisa de Postgres. O harness roda em paralelo com o `PgStore` e é reapontado no fim (~1 sessão economizada).

## Decisões fechadas (com o usuário, 2026-08-31)

| Decisão | Escolha |
|---|---|
| Compute AWS (marco 4) | **ECS Fargate + ALB** (SSE funciona, pool quente, RDS na VPC) |
| Observabilidade deployada | **Langfuse Cloud** agora (2 env vars, zero código). ADR de observabilidade no marco 6 com 3 candidatos: Langfuse Cloud · LangSmith (OTLP + ~30 linhas de aliases `gen_ai.*` em `telemetry.py`) · AgentCore Observability (ADOT SDK + SigV4, ~1-2 dias, GA em sa-east-1 desde mai/2026). Nunca usar o tracing "nativo" do LangSmith — ignora os spans custom do TRAIL |
| Identidade (marco 2) | **Header assinado com HMAC** + mapa estático de clientes; seam idêntico a JWT depois |
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
- Provider: `runtime/agent.py:85` hardcoded `openai:` → virar setting + `langchain-aws` (~15 linhas). O risco não é o código: é a fila de aprovação de acesso a modelos Bedrock em sa-east-1 → **pedir acesso no dia 1**.
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

| # | Tarefa | Pré-req | Esf | Arquivos | Verificação |
|---|---|---|---|---|---|
| T1 | `docs/threat-model.md`: adversário × invariante × teste (incluir IDOR do explain, replay de confirmação, injeção) | — | M | novo | toda linha cita um id de teste |
| T2 | CI: `pytest -m unit --cov` em PR | — | S | `.github/workflows/ci.yml` | um PR vermelho |
| T3 | `explain(ctx, intent_id)` + ownership | — | S | `plane.py`, `tools.py`, testes | `OTHER_SESSION` recebe `[]` |
| T4 | Apagar `approve_step_up` + regra 4 do prompt + teste | — | S | `tools.py`, `agent.py`, `test_banking_agent.py`, `golden.py` | nenhuma tool aprova nada |
| T5 | Regras BACEN (noturna 20h–06h + limite/tx), clock injetado | — | M | `policy.py`, `tests/unit/test_policy.py` novo | testes com relógio fixo |
| T6a | `db/schema.sql`: `intents`, `ledger_events` | — | S | `db/schema.sql` | aplica 2× limpo |
| T6b | `Store` protocol + `MemoryStore`; `ControlPlane(store=…)` | — | M | `control_plane/store.py` novo, `state.py`, `plane.py` | 21 testes existentes passam **sem edição** |
| T6c | `PgStore` (pool síncrono, aplica schema no startup) | T6a,T6b | M | `control_plane/pgstore.py` novo, `pyproject.toml` (omit) | `make test` ≥90% E `make test-integration` |
| T7 | Sweep de restart `SUBMITTED→UNKNOWN` (aresta existente) | T6b | M | `plane.py`, lifespan | kill no meio do PIX → restart → sweep → reconcile → 1 débito |
| T8 | TTL de confirmação + digest HMAC da ação | T6b | M | `state.py`, `plane.py` | expirada→CANCELLED; digest divergente→DENY |
| T9 | Identidade HMAC: header → `app.py` → `configurable` → `context_for` | — | M | `app.py`, `turns.py`, `tools.py`, `cli.py`, testes | header forjado→401; 2 clientes isolados |
| T10 | `trail step-up <intent_id>` no CLI | T6c,T4 | M | `cli.py`, runbook | step-up completa sem tocar o agente |
| T11 | `Case.turn_checks` + loop no runner | — | S | `evals/cases.py`, `evals/runner.py` | suite atual verde + 1 caso multi-turno |
| T12 | Seam de crash: `FaultyBank(MockBank)` com `crash_at` | T6b | S | `tests/fakes.py` | levanta entre débito e recibo |
| T13 | Matriz: 5 invariantes × ~6 cenários × N≥100, seeded | T12,T7,T8 | L | `tests/unit/test_invariants.py` novo | `pytest -m matrix` verde, <60s |
| T14 | Renderer + `make matrix` | T13 | S | `Makefile` | tabela impressa, exit≠0 em célula vermelha |
| T15 | Golden set adversarial (+5 casos) | T11,T4,T9 | M | `golden.py` | ambíguo, correção, injeção, ação não suportada, saída malformada |
| T16 | Matar a race do eval (banco por customer / plane por thread) | T9 | S | `bank.py` ou `tools.py` | 3 `make eval` seguidos, mesmo resultado |
| T17 | IaC: VPC, RDS, ECR, secrets, ECS Fargate + ALB (só 4 serviços — Langfuse Cloud deleta os outros 6) | — | L | `infra/` novo | `terraform plan` limpo |
| T18 | Provider seam `TRAIL_LLM_PROVIDER` + `langchain-aws` | — | S | `agent.py:85`, `config.py` | `bedrock_converse:` monta sem rede |
| T19 | Runbook + `trail intents`/`trail reconcile` | T6c | M | `cli.py`, `docs/runbook.md` | humano resolve `UNKNOWN` só com o doc |
| T20 | ADR residência sa-east-1 | T18 | S | `docs/adr/` | cita latência medida |
| T21 | Deploy + `UNKNOWN` forçado em prod (PIX `,13`) | T17,T19,T7 | L | — | runbook executado em prod |
| T22 | `git tag before-voice` | T21 | S | — | baseline do diff |
| T23 | Voz simulada: `stt_confidence` no `Context` + sinal de risco + tabela de transcrições sintéticas ("trezentos"→"treze", "Renata"→"Renato") | T22 | M | `actions.py`, `policy.py`, `voice.py` novo | confiança baixa → step-up/more-info; colapso de magnitude nunca executa |
| T24 | Confirmação cross-channel (já paga pela decisão de ownership do T10) | T8,T9 | S | `plane.py:451` | voz propõe → texto confirma → 1 débito |
| T25 | `git diff --stat before-voice -- src/control_plane/` + 1 chamada STT real pra provar o adaptador | T23,T24 | S | — | o número (~6 linhas esperadas) |
| T26 | Benchmark extração (3 modelos × golden set congelado) | T15 congelado | M | `Makefile` loop sobre `TRAIL_MODEL` | mesma `golden_set_version` |
| T27 | Benchmark STT pt-BR (valores e nomes) | T23 | M | `bench/` | WER em valores/nomes |
| T28 | Bedrock vs API externa + ADR observabilidade (Langfuse/LangSmith/AgentCore) | T18,T26 | M | `docs/adr/` | tabela latência/custo/residência |
| T29 | Post final + diagrama + índice de ADRs | tudo | L | `docs/` | cada seta → ADR → número |

## Caminho crítico (confirmado pelos dois planejadores)

```text
T6b Store+MemoryStore → T6c PgStore → T7 sweep → T8 TTL/digest → T13 matriz → T14 make matrix
                          ↘ T12 crash seam roda em paralelo com T6c
```
~5 sessões até a matriz; ~8–9 até o fim. Tudo de marcos 1, 4 (CI/IaC/provider) e o loop de benchmark ficam FORA do caminho.

## Cadeias seriais (mesmos arquivos — nunca 2 subagentes ao mesmo tempo)

- **`plane.py`** (gargalo): T3 → T6b → T7 → T8 → T24
- **`tools.py`**: T3+T4 (mesma sessão) → T9 → T23
- **`golden.py`**: T4 → T15 → congelar antes de T26
- **`Makefile`**: um dono por sessão (T14, T26)

## Sessões (cada uma fecha num gate)

**Dia 1, antes de tudo (humano, 15 min):** pedir acesso a modelos Bedrock em sa-east-1 (fila anda enquanto você trabalha); criar conta Langfuse Cloud; rodar `make up && make eval && make eval` pra confirmar a race com os próprios olhos.

**S1 — "onde o V0 errou"** · você: T3+T4 + medir p95/custo (1 `make eval`, `concurrency=1`) + correções no ROADMAP.md · subagentes em paralelo: T1, T2, T5, T11, T18, T17-esqueleto · merge: CI primeiro, depois o seu, depois o resto · **Gate: `make lint && make test` ≥90%; nenhuma tool aprova/executa nada.** → material do post 1 completo.

**S2 — seam de storage** · você: T6b (regra de honestidade: `git diff tests/unit/test_control_plane.py` só tem adições) · subagentes: T6a, T26-esqueleto · **Gate: `make test` verde com testes antigos intactos.**

**S3 — Postgres** · você: T6c · subagentes: T12 (sobre MemoryStore), T9 · **Gate: `make test` ≥90% E `make test-integration`.** (Armadilha do coverage: `omit` no mesmo commit.)

**S4 — recuperação** · você: T7+T8 · subagentes: T19, T15 · **Gate: teste de integração que mata o processo entre `plane.py:355` e `:378`, reinicia, varre, reconcilia e afirma `len(bank.payments)==1`.** → post 2.

**S5 — a matriz** · você: T13+T14+T16, T10 · subagentes: T20, T17-final · **Gate: `make matrix` verde, N≥100, <60s.** → post 3 (espinha dorsal, meio do projeto).

**S6 — produção** · T21 · **Gate: `UNKNOWN` real resolvido em prod só com o runbook.** → post 4.

**S7 — voz** · T22–T25 · **Gate: o número do `git diff --stat`.** → post 5.

**S8 — benchmarks + final** · T26–T29 · **Gate: script que falha se algum ADR não aponta pra um número.** → post 6.

## Armadilhas conhecidas (não redescobrir)

1. Coverage ≥90% quebra com `PgStore` → módulo próprio + `omit` no mesmo commit (`pyproject.toml:156-160`).
2. initdb só roda em volume vazio → `PgStore` aplica `db/schema.sql` no startup (padrão de `evals/store.py:65`).
3. `git diff tests antigos` no T6b: se o seam de storage exige editar teste antigo, o seam está errado.
4. Adicionar casos ao golden set muda `golden_set_version` e invalida baselines → congelar `golden.py` ANTES de rodar o benchmark T26.
5. Step-up out-of-band com `_same_principal` atual rejeita 100% dos callbacks (exige session igual) → afrouxar para customer+intent É a tarefa, não um bug do caminho.
6. Diff da voz ~0 sem STT real é infalsificável → 1 chamada STT real no T25, e dizer no post que WER de campo é o benchmark T27, não a simulação.

## Verificação end-to-end do plano inteiro

O projeto está pronto quando os quatro comandos passam em sequência num checkout limpo:
`make test` (≥90%, offline) → `make matrix` (invariantes, N≥100) → `make eval` (comportamento do agente, determinístico após T16) → `make smoke AGENT_BASE_URL=<prod>` (UNKNOWN forçado + runbook).

## Primeiro ato da implementação

Copiar este plano para `docs/execution-plan.md` no repo (o usuário vai usá-lo como blueprint), aplicar as 5 correções no `ROADMAP.md`, commitar os dois juntos.
