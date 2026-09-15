# ADR 0002 — One `Store`, two implementations, synchronous

**Status:** accepted · **Date:** 2026-09-07 · **Evidence:** `tests/unit/test_plane_store.py`, `tests/integration/test_pgstore.py` (8 tests against a real Postgres)

## Context

The first version kept intents in a `dict` and events in a list, inside `ControlPlane` itself. Swapping that for Postgres would have meant editing every method that touches state — and the invariant matrix under crashes proves nothing on top of a dict that dies with the process.

## Decision

One interface (`Store`) with exactly the questions the plane asks: `get`, `by_confirmation`, `unsettled`, `put`, `append`, `events_for`. Two implementations: `MemoryStore` and `PgStore`.

Three choices inside it:

1. **Ownership stays out of the store.** `get` and `by_confirmation` answer about ids, not about principals; the plane does the comparison. Two places deciding who may see what end up disagreeing — and that was exactly the hole in the first version's `explain`.
2. **Saving is explicit.** `put` after every mutation, even with `MemoryStore`, where the mutated object already is the stored object. An in-memory store that forgives a forgotten `put` teaches the caller a habit Postgres punishes on the first restart.
3. **Synchronous.** LangChain runs `def` tools in a threadpool, so a synchronous `ConnectionPool` is reachable from inside a tool without blocking the event loop. Converting the plane to async would be the same work spread across every caller.

`unsettled` went in before it had a caller, because it is the only query whose absence would change the interface later — and an interface that changes with two finished implementations changes twice.

## Consequences

The honesty gate: `git diff tests/unit/test_control_plane.py` came out **empty**. The 33 existing tests at the time passed on the new seam without a single character changed — if the seam had required editing them, the seam would have been wrong.

`pgstore.py` is its own module, with a coverage `omit` in the **same commit**, because every line of it needs a database and the 90% floor of the unit tier would have broken on the day the file was created. A gate that has to be argued about is a gate the next person lowers instead of meeting.

Writing `PgStore` found a defect in the schema committed an hour earlier: `Context` carries `assurance`, `step_up` raises it, and the table stored only customer/session/channel — an intent that had completed step-up would come back as `medium` after a restart, and policy would demand again an authentication the customer had already done. The whole `Context` became JSONB.

`db/schema.sql` has no migration tool, by inherited decision: `CREATE TABLE IF NOT EXISTS` does not add a column to an existing table, and `make clean` is the migration. That is acceptable while the data is disposable, and **stops being acceptable once the database is deployed to AWS**.
