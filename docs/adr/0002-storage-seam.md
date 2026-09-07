# ADR 0002 — Um `Store`, duas implementações, síncrono

**Status:** aceito · **Data:** 2026-09-07 · **Evidência:** `tests/unit/test_plane_store.py`, `tests/integration/test_pgstore.py` (8 testes contra Postgres real)

## Contexto

O V0 guardava intents num `dict` e eventos numa lista, dentro do próprio `ControlPlane`. Trocar por Postgres significaria editar todo método que toca estado — e o marco 3 (matriz de invariantes sob crash) não prova nada em cima de um dict que morre junto com o processo.

## Decisão

Uma interface (`Store`) com exatamente as perguntas que o plane faz: `get`, `by_confirmation`, `unsettled`, `put`, `append`, `events_for`. Duas implementações: `MemoryStore` e `PgStore`.

Três escolhas dentro dela:

1. **Ownership fica fora do store.** `get` e `by_confirmation` respondem sobre ids, não sobre principais; quem compara é o plane. Dois lugares decidindo quem pode ver o quê acabam discordando — e foi exatamente esse o furo do `explain` no V0.
2. **Salvar é explícito.** `put` depois de toda mutação, mesmo com `MemoryStore`, onde o objeto mutado já é o objeto guardado. Um store em memória que perdoa um `put` esquecido ensina ao chamador um hábito que o Postgres pune no primeiro restart.
3. **Síncrono.** LangChain roda tools `def` em threadpool, então um `ConnectionPool` síncrono é alcançável de dentro de uma tool sem bloquear o loop. Converter o plane para async seria o mesmo trabalho espalhado por todos os chamadores.

`unsettled` entrou antes de ter chamador, porque é a única consulta cuja ausência mudaria a interface depois — e interface que muda com duas implementações prontas muda duas vezes.

## Consequências

O gate de honestidade: `git diff tests/unit/test_control_plane.py` ficou **vazio**. Os 33 testes antigos passaram no seam novo sem um caractere alterado — se o seam exigisse editá-los, o seam estaria errado.

`pgstore.py` é módulo próprio com `omit` de cobertura no **mesmo commit**, porque toda linha dele precisa de banco e o piso de 90% do tier unit quebraria no dia em que o arquivo nasceu. Um gate que precisa ser discutido é um gate que a próxima pessoa baixa em vez de cumprir.

Escrever o `PgStore` achou um defeito no schema commitado uma hora antes: `Context` carrega `assurance`, `step_up` eleva, e a tabela guardava só cliente/sessão/canal — um intent com step-up feito voltaria como `medium` depois de um restart e a política exigiria de novo uma autenticação que o cliente já tinha feito. O `Context` inteiro virou JSONB.

`db/schema.sql` não tem ferramenta de migração, por decisão herdada: `CREATE TABLE IF NOT EXISTS` não adiciona coluna a tabela existente, e `make clean` é a migração. Isso é aceitável enquanto o dado é descartável e **deixa de ser no marco 4**.
