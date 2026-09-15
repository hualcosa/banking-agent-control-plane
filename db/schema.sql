-- TRAIL — database bootstrap.
--
-- Applied once by the Postgres image from `docker-entrypoint-initdb.d`, on an
-- empty volume only. There is no migration tool by design: the schema is small,
-- the stack is disposable, and `make clean` is the migration.
--
-- The table list is deliberately empty, and that is the interesting part.
--
-- Conversation state — threads, checkpoints, cross-thread memory — is owned by
-- LangGraph's Postgres checkpointer and store, which create and migrate their
-- own tables from `setup()` at startup (see `trail.runtime.checkpointers`).
-- Declaring them here would mean maintaining a second, hand-written copy of a
-- schema the library already versions, and being wrong about it on the first
-- upgrade.
--
-- Per-call tokens, cost and latency live in Langfuse, arriving as typed
-- generations off the OTel span the trace middleware stamps. A local table
-- duplicating them would be a second source of truth for the same numbers, and
-- the one this repository can least afford to have disagree with itself.
--
-- What does live here is the eval harness. Run records and findings are
-- TRAIL's own data with no upstream owner, which is the test for whether a
-- table belongs in this file at all.
--
-- Every statement below is idempotent, because this file is applied twice: by
-- the Postgres image on an empty volume, and by `trail.evals.store` on the
-- first run against a volume that predates the harness. One definition, two
-- callers — the alternative is a second copy of the DDL in Python, which is
-- the duplication the paragraphs above refuse for LangGraph's tables.

-- Fail loudly rather than silently if the database is not what we think it is.
DO $$
BEGIN
    IF current_database() IS NULL THEN
        RAISE EXCEPTION 'no database';
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- The eval harness
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS eval_runs (
    id                 BIGSERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,

    -- Pins the measurement. A baseline scored against a different set of cases
    -- is not a baseline, and the harness refuses that comparison rather than
    -- printing a delta between two different experiments.
    golden_set_version TEXT        NOT NULL,

    -- What produced the numbers. Without these a run is a score with no
    -- subject: the same golden set over two models is two findings, not one
    -- trend, and `guardrails` is the dial most likely to explain a jump.
    agent              TEXT        NOT NULL,
    model              TEXT        NOT NULL,
    guardrails         TEXT        NOT NULL,

    -- Empty means the agent graded itself. Recorded rather than inferred,
    -- because a later reader comparing two runs needs to know which of them
    -- had an independent grader.
    judge_model        TEXT        NOT NULL DEFAULT '',

    -- FAILED is the zero-tolerance verdict: a fabricated fact, or a gate that
    -- refused a legitimate question. The baseline lookup filters on it, which
    -- is the boundary rule made mechanical — a failed run cannot become the
    -- thing a later run is judged "no regression" against.
    status             TEXT        NOT NULL
                       CHECK (status IN ('COMPLETED', 'FAILED')),

    -- The whole scorecard, each metric with its value, denominator and the bar
    -- it was measured against. JSONB and not a column per metric: the metric
    -- list belongs to the example's golden set, so a new example must not need
    -- a migration to be measurable.
    metrics            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    baseline_id        BIGINT      REFERENCES eval_runs (id) ON DELETE SET NULL
);

-- The baseline lookup, which is the only query with a hot path: most recent
-- COMPLETED run of this golden set.
CREATE INDEX IF NOT EXISTS eval_runs_baseline_idx
    ON eval_runs (golden_set_version, status, started_at DESC);

-- Findings are normalised out of the run rather than nested in its JSON so the
-- failure taxonomy is queryable: `select kind, count(*) ... group by kind` is
-- the question this table exists to answer, and it is not a question you ask
-- of a JSON blob.
CREATE TABLE IF NOT EXISTS eval_findings (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    case_id    TEXT   NOT NULL,
    turn       INT    NOT NULL DEFAULT 0,

    -- OMISSION | FABRICATION | WRONG_PATH | ERROR. Unconstrained on purpose:
    -- the taxonomy is the harness's vocabulary and a new kind should not need
    -- a schema change to be recorded, only to be reported.
    kind       TEXT   NOT NULL,

    -- `check_name`, not `check`: CHECK is a reserved word, and a column that
    -- needs quoting in every query is a column that will eventually be typed
    -- without them.
    check_name TEXT   NOT NULL,

    -- 'check' or 'judge'. A substring test and a model's opinion are not the
    -- same evidence, and a reader must be able to weigh them differently
    -- without going back to read the case.
    source     TEXT   NOT NULL DEFAULT 'check',

    expected   TEXT   NOT NULL DEFAULT '',
    actual     TEXT   NOT NULL DEFAULT '',
    detail     TEXT   NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS eval_findings_run_idx  ON eval_findings (run_id);
CREATE INDEX IF NOT EXISTS eval_findings_kind_idx ON eval_findings (kind);


-- ---------------------------------------------------------------------------
-- The control plane
-- ---------------------------------------------------------------------------
--
-- Two tables, and the split between them is the whole design: `intents` is
-- mutable current state, `ledger_events` is append-only history. Nothing ever
-- updates or deletes a ledger row, which is what makes `explain` an audit
-- answer rather than a report generated from whatever the state happens to be
-- now.
--
-- These are TRAIL's own data with no upstream owner — the same test that let
-- the eval tables in above. LangGraph owns conversation state; this owns money.

CREATE TABLE IF NOT EXISTS intents (
    -- The intent id IS the idempotency key sent to the bank. One intent, one
    -- key, forever: a text primary key rather than a serial, because the value
    -- is meaningful to a system outside this database and must not be
    -- reassigned by it.
    id              TEXT        PRIMARY KEY,

    -- The principal that created it. Both halves are needed: a confirmation is
    -- honoured only from the same customer AND the same session, so a lookup
    -- that filters on one of them is a lookup that authorises too much.
    customer_id     TEXT        NOT NULL,
    session_id      TEXT        NOT NULL,

    -- The whole Context, not just its indexed halves. `assurance` lives here,
    -- and `step_up` raises it: a schema that stored only customer/session/
    -- channel would silently return an authenticated intent as medium-assurance
    -- after a restart, and the policy would then demand a step-up the customer
    -- already completed. The two columns above are duplicated out of this
    -- object because they are what ownership queries filter on; nothing else is.
    context         JSONB       NOT NULL,

    -- The canonical action, as the plane built it — never as the model typed
    -- it. JSONB because the action union grows (a scheduled PIX, a card block)
    -- without a migration, and because no query here filters on its innards.
    action          JSONB       NOT NULL,

    -- The state machine's current node. Unconstrained by CHECK on purpose: the
    -- transition table in `state.py` is the authority, and a second copy of it
    -- here would be a copy that drifts. The database stores where the machine
    -- is; it does not adjudicate where it may go.
    state           TEXT        NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The token the customer confirms, and when. Nullable until the intent
    -- reaches AWAITING_CONFIRMATION. `confirmed_at` exists so a confirmation
    -- can expire — a column written and never read is exactly how "old
    -- confirmation" became unexpressable in V0.
    confirmation_id TEXT,

    -- When the token was issued, and what the action hashed to at that moment.
    -- These are the two columns whose absence made "old confirmation" and
    -- "mutated action" unexpressable: not scenarios the system got wrong, but
    -- scenarios it could not be wrong about, because it kept nothing to check.
    confirmation_issued_at TIMESTAMPTZ,
    action_digest   TEXT,

    confirmed_at    TIMESTAMPTZ,
    confirmed_by    TEXT,

    -- What the bank answered. NULL until SUBMITTED resolves — and NULL while
    -- UNKNOWN, which is the state this column cannot describe and `reconcile`
    -- exists to settle.
    receipt         JSONB,

    -- Why a terminal state was reached, when it was not success.
    reason          TEXT        NOT NULL DEFAULT ''
);

-- The restart sweep asks exactly one question: which intents did this
-- process leave mid-flight? Partial index, because SUBMITTED is a vanishing
-- fraction of the table and the sweep runs on every boot.
CREATE INDEX IF NOT EXISTS intents_unsettled_idx
    ON intents (state)
    WHERE state IN ('SUBMITTED', 'AWAITING_CONFIRMATION', 'AWAITING_STEP_UP');

-- Ownership lookups: "this customer's intents", never "this id, whoever owns it".
CREATE INDEX IF NOT EXISTS intents_principal_idx
    ON intents (customer_id, session_id);

-- A confirmation_id must resolve to at most one intent, and must not be
-- guessable into someone else's. Unique rather than merely indexed: two
-- intents sharing a token is a bug that authorises a payment.
CREATE UNIQUE INDEX IF NOT EXISTS intents_confirmation_idx
    ON intents (confirmation_id)
    WHERE confirmation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ledger_events (
    -- Sequence, not timestamp, is the order. Two events in the same
    -- transaction can share a clock reading; `explain` still has to render
    -- them in the order they happened.
    seq        BIGSERIAL   PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Deliberately NOT a foreign key to `intents`. A read (`read_…`) writes
    -- events and never creates an intent, and the `execution_request` row is
    -- committed in its own transaction before the bank is called — a
    -- constraint here would make the outbox write depend on the row it is
    -- meant to outlive.
    intent_id  TEXT        NOT NULL,

    kind       TEXT        NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- `explain` is the only read: one intent's events, in order.
CREATE INDEX IF NOT EXISTS ledger_events_intent_idx
    ON ledger_events (intent_id, seq);

-- The reconciliation sweep asks for execution_requests with no matching
-- backend_response — a kind-filtered scan, so the kind is indexed.
CREATE INDEX IF NOT EXISTS ledger_events_kind_idx
    ON ledger_events (kind);


-- ---------------------------------------------------------------------------
-- The bank's book of payments
-- ---------------------------------------------------------------------------
--
-- what lets the runbook be executed for real. Balances and `paid_before` stay
-- in memory, per conversation: they are policy inputs, not money moved.
CREATE TABLE IF NOT EXISTS bank_payments (
    idempotency_key TEXT        PRIMARY KEY,
    receipt         JSONB       NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
