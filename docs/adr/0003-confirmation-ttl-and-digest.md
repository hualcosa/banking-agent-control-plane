# ADR 0003 — A confirmation expires, and the action cannot change underneath it

**Status:** accepted · **Date:** 2026-09-07 · **Evidence:** `tests/unit/test_recovery.py`, `tests/unit/test_invariants.py` (`expired_yes` and `mutated_action` cells)

## Context

The first version issued a random `confirmation_id` and accepted it forever, against any version of the action. `Intent.confirmed_at` was written and never read.

The effect was not a bug: it was the **absence of a fact**. "Stale confirmation" and "mutated action" were not scenarios the system got wrong — they were scenarios it could not even get wrong, because it stored nothing to compare against. Two cells of the invariant matrix were, literally, inexpressible.

## Decision

Two fields and two checks in `confirm`:

- **5-minute TTL** (`confirmation_issued_at`). A "yes" is consent to move money **now**, with the customer still in the conversation. Once expired, the intent is **cancelled** — it does not sit waiting on a token that will not get younger.
- **Keyed action digest** (`action_digest`): an HMAC over the action's canonical JSON. If the digest diverges, `DENY`, and nothing executes.

The digest is **keyed, not a plain hash**, and that is the part that matters: a plain hash tells you the action changed, but whoever can rewrite the action can also recompute the hash to match. The key is what makes the two writes require two separate capabilities.

The key comes from the environment (`TRAIL_CONFIRMATION_SECRET`), not from a random per-process value — a random key would invalidate every pending confirmation on each restart, which is an outage dressed up as a security control.

## Consequences

A short TTL has a usability cost: a customer who leaves to check their balance and comes back six minutes later has to propose again. Accepted, and adjustable in one place (`state.CONFIRMATION_TTL`).

Verifying the matrix showed the real value of these two checks in a way the unit tests did not: remove the TTL check and one cell turns red; remove the digest and another does. And the first attempt at detecting expiry **looked for the `confirmation_expired` event that the plane itself writes** — so deleting the check also deleted its evidence. The matrix would have derived "green" from a broken system. Today the invariant computes `confirmed_at - issued_at` on its own.
