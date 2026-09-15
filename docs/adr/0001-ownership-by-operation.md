# ADR 0001 — Ownership: what belongs to the session and what belongs to the customer

**Status:** accepted · **Date:** 2026-09-07 (revised 2026-09-13) · **Evidence:** `tests/unit/test_control_plane.py`, `tests/unit/test_invariants.py` (`borrowed_token` cell)

## Context

The first version had a single rule: every operation on an intent required the **same `customer_id` AND the same `session_id`** (`_same_principal`). That is the defence against replaying a confirmation across conversations (adversary A4 in the threat model), and it worked as long as everything happened in one channel.

Two features broke that premise at the same time:

- **Out-of-band step-up** (`trail step-up`): the strong approval comes from the bank's app — a different process and, **by construction**, a different session. Under the old rule, 100% of real callbacks would have been refused. This failure mode was anticipated before the feature was built.
- **The operator runbook**: the operator resolving an `UNKNOWN` payment at 3 a.m. is not in the customer's conversation thread.

## Decision

Ownership stops being one rule and becomes **three**, chosen by what the operation does:

| Operation | Scope | Why |
|---|---|---|
| `confirm`, `cancel` | customer **+ session** | Consent is given inside a conversation. A "yes" is not transferable to another one. |
| `step_up`, `reconcile` | customer **+ intent** | Raising assurance and asking the bank what it did are safe to do from outside the conversation; neither moves money by itself. |
| `explain` | whoever opened the trail | The trail contains the `confirmation_id`, which `confirm` accepts. |

`confirm` and `explain` are session-scoped in every channel — no cross-channel exception.

## Consequences

What does **not** change: the customer boundary. A different `customer_id` is refused on every operation, in every channel, and each operation has a test for it.

What changes: a compromised session belonging to the same customer can now trigger `step_up` and `reconcile` on intents it did not create. The gain is that out-of-band step-up exists at all; the loss is real, and it is recorded here instead of discovered later. Current mitigation: neither operation moves money, and both are written to the ledger together with the session that called them.

## Alternatives rejected

- **Relax everything to customer + intent.** Reopens A4: a leaked `confirmation_id` becomes valid in any conversation. Consent is precisely the thing that must not be borrowable.
- **Keep `confirm` session-scoped and let step-up die.** That was the first version's state, and it is exactly the anticipated failure: the out-of-band channel would never work.
- **An explicit handoff token** (the customer requests a channel transfer). Safer and more expensive; if step-up ever sees real volume, this is where the design evolves.
