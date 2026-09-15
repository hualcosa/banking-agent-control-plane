# Runbook — resolving an intent stuck in `UNKNOWN`

For whoever got woken up at 3am because a PIX is hanging. This document is
self-contained: if you follow it from here to the end, the intent leaves
`UNKNOWN` and you can tell the customer whether the money moved or not.

Three commands, in this order:

```bash
trail intents                       # what is waiting on a person
trail reconcile pix_4c1e0a7b93d2    # ask the bank what happened
trail intents                       # confirm it left the list
```

None of them talks to the agent. They open the **same Postgres** the agent
writes to and build their own `ControlPlane` on top of it — so they work with the
agent down, which is exactly the scenario you were woken up for.

The CLI prints its labels and messages partly in Portuguese (the product's
customer-facing language). The sample output below is reproduced verbatim, with
English glosses where it matters.

---

## 1. What `UNKNOWN` means

`UNKNOWN` **is not an error**. It is the honest state for "the bank was called
and the answer never arrived": the money **may** have moved, and nobody in this
process knows.

The window is this one, in `src/control_plane/plane.py::ControlPlane._execute`:

```text
  write execution_request  ──►  bank.create_pix(...)  ──►  write the receipt
                            └──────── the window ────────┘
```

Two things fall into that window and **leave the same evidence**, so they get the
same answer:

| how it happens | what moves it to `UNKNOWN` |
|---|---|
| the bank does not answer (timeout) — in the mock, every amount ending in `,13` | `_execute` catches `BankTimeout` and transitions immediately |
| the process dies midway (deploy, OOM, kill) and the intent is stuck in `SUBMITTED` | the **restart sweep**, when the next process starts (`examples/banking/agent.py::sweep_on_boot`) |

The sweep is what turns a dead container into a recoverable event: it scans
`SUBMITTED`, moves each intent to `UNKNOWN` and **prints the ids in the startup
log**. If you got here because of a line like `restart sweep: N intent(s) left in
SUBMITTED moved to UNKNOWN`, this is exactly what it is about.

What **never** happens: a timeout automatically becoming `FAILED`. Treating a
timeout as a failure and retrying is how a customer pays twice.

## 2. What `reconcile` does — and what it does not

**Does:** asks the bank by the intent's **idempotency key** (which is the
`intent_id` itself) and writes what the bank answered to the ledger.

- the bank has the receipt → the intent becomes `COMPLETED`, with the receipt attached;
- the bank does not know the key → the intent becomes `FAILED`.

**Does not — none of these, under any circumstances:**

- **does not pay again.** There is no path from `reconcile` to the execution
  gateway; it only reads.
- **does not reverse** or cancel anything at the bank.
- **does not confirm** on anyone's behalf: consent belongs to the customer, in
  the conversation.
- **does not touch** an intent that is not in `UNKNOWN` — in that case it only
  reports the current state.
- **does not fix** the agent. If the agent keeps crashing, this resolves the
  pending payment, not the cause.

## 3. Before you start

1. `TRAIL_DATABASE_URL` must point to the **same** Postgres as the agent, and the
   agent must be writing there: `TRAIL_CONTROL_PLANE_STORE=postgres` (which is
   what `docker-compose.yml` sets for the services that handle traffic, and what
   the AWS Runtime sets). With `memory`, the agent keeps its intents inside its
   own process and these commands open a database nobody wrote to — `trail
   intents` lists nothing and nothing `trail reconcile` says about that payment
   can be trusted. Check this before trusting the output.
   From outside compose the host is `localhost`; `make intents` and
   `make reconcile INTENT=<intent_id>` set that up for you.
2. `TRAIL_CUSTOMER_ID` is the customer whose intents you see. The list is scoped
   per customer, even though the database is shared.
3. You need a `.env` (copy it from `.env.example`). These commands call no model,
   but they read the same configuration; if the key is missing they tell you which
   variable is missing instead of throwing a traceback.
4. The agent does **not** need to be running.

## 4. The procedure

### 4.1 List what is waiting on a person

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
intent            estado            valor        destinatário    criado (UTC)         próximo passo
pix_4c1e0a7b93d2  UNKNOWN           R$ 1.250,13  contact_renata  2026-09-07 03:14:11  trail reconcile pix_4c1e0a7b93d2
pix_77b1c0e4aa10  AWAITING_STEP_UP  R$ 800,00    contact_joao    2026-09-07 03:22:11  trail step-up pix_77b1c0e4aa10
  1 em UNKNOWN — o banco sabe o que aconteceu e este processo não; ver docs/runbook.md
```

(Columns: intent, state, amount, recipient, created (UTC), next step. The footer
reads "1 in UNKNOWN — the bank knows what happened and this process does not".)

The **próximo passo** (next step) column already contains the ready-to-run
command. The DSN password is never printed; the host is — check that it is the
database you think it is before acting.

If there is nothing:

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  nada aguardando ação
```

("nothing awaiting action")

### 4.2 Reconcile — the bank paid

```console
$ trail reconcile pix_4c1e0a7b93d2
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  perguntando ao banco pela chave de idempotência; nenhum pagamento novo é feito por este comando
  banco consultado: PgBank com 1 pagamento(s) conhecido(s)
  COMPLETED pix_4c1e0a7b93d2
  confirmed with the bank: the payment was made
  R$ 1.250,13 → Renata Silva  ·  estado COMPLETED
  recibo pay_31f0c9a2 · E7C4A1B0D9E3 · COMPLETED
```

(The first two indented lines read "asking the bank by idempotency key; no new
payment is made by this command" and "bank queried: PgBank with 1 known
payment(s)". `recibo` is the receipt: payment id · `end_to_end_id` · status.)

**What to tell the customer:** the PIX went through; the proof is the
`end_to_end_id` above. Do not repeat the payment.

### 4.3 Reconcile — the bank has no record

```console
$ trail reconcile pix_4c1e0a7b93d2
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  perguntando ao banco pela chave de idempotência; nenhum pagamento novo é feito por este comando
  banco consultado: PgBank com 0 pagamento(s) conhecido(s)
  FAILED pix_4c1e0a7b93d2
  not executed
  R$ 1.250,13 → Renata Silva  ·  estado FAILED
```

⚠️ **Read the counter before believing the `FAILED`.** See section 6: with
`0 pagamento(s) conhecido(s)` (0 known payments), `FAILED` means "this book has
no record", which is only the same sentence as "the money did not move" when the
book being queried is the one the agent actually wrote to.

With a bank that really answers, `FAILED` is the correct resolution: nothing was
debited, and the customer can request the PIX again through the conversation (a
new request is a new intent — nothing is "retried" from here).

### 4.4 Running it twice is safe

```console
$ trail reconcile pix_4c1e0a7b93d2
  ...
  COMPLETED pix_4c1e0a7b93d2
  R$ 1.250,13 → Renata Silva  ·  estado COMPLETED
  recibo pay_31f0c9a2 · E7C4A1B0D9E3 · COMPLETED
```

An intent that is already settled only reports its own state. Nothing is
re-executed.

### 4.5 Confirm the queue is empty

```console
$ trail intents
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  nada aguardando ação
```

## 5. How to read the output

| first word | means | what to do |
|---|---|---|
| `COMPLETED` | the bank has the receipt: the money moved | tell the customer; nothing to repeat |
| `FAILED` | the bank does not know the key (read the payment counter, section 6) | the customer can request it again through the conversation |
| `DENY` · `unknown reference for this session: <id>` | the id does not exist **or** does not belong to this `TRAIL_CUSTOMER_ID` | check the id and the configured customer |
| `DENY` with any other text | the intent is not in `UNKNOWN` | re-run `trail intents`: it has already been resolved |

Exit codes: `0` when reconciliation happened (both `COMPLETED` and `FAILED`), `1`
when the command was refused, `1` with a message on `stderr` when the
configuration or the database did not even allow an attempt.

## 6. The book of payments — read this before trusting a `FAILED`

**The bank is still a mock** — contacts, balances and the `,13` tripwire are the
same as always. What is real is **where the book of what was paid lives**: the
`bank_payments` table in Postgres (`src/control_plane/pgbank.py::PgBank`), shared
between the agent and these commands.

`trail reconcile` always opens a `PgBank` over `TRAIL_DATABASE_URL`, and the
agent writes to that same table when `TRAIL_CONTROL_PLANE_STORE=postgres`. That
is why it prints `banco consultado: PgBank com N pagamento(s) conhecido(s)`
(bank queried: PgBank with N known payment(s)); the count is the number of rows
in `bank_payments`.

The operational rule stays short:

> **With `0 pagamento(s) conhecido(s)`, a `FAILED` is not proof that the money
> did not move.** It is proof that the book being queried has never seen any
> payment — which, with the right DSN and the agent on the postgres store, does
> mean the bank has no receipt. But a **wrong DSN** (an empty database, an
> instance that is not the agent's), or an agent running on the `memory` store,
> also produces `0`. Check the DSN line in the output before believing it.

**Ownership on the out-of-band path.** `step_up` and `reconcile` resolve the
intent by `customer_id` + `intent_id`
(`src/control_plane/plane.py::ControlPlane._owned_by_customer`), and that is what
lets them be run from another process — an operator is not in the customer's
conversation thread. Two consequences that matter for your shift:

- **wrong customer, right id, same answer:** an `intent_id` belonging to another
  `TRAIL_CUSTOMER_ID` answers `unknown reference for this session`. Check the
  first line of the output before suspecting the id.
- **consent did not loosen:** `confirm` still requires the customer **and** the
  session. No command in this runbook confirms a payment.

## 7. The other out-of-band command: `trail step-up`

It is not part of `UNKNOWN` recovery; it is here because it shows up in the same
list. It simulates the **mobile-app callback**: when policy required strong
authentication (`AWAITING_STEP_UP`), the assurance arrives through a channel that
is not the conversation.

```console
$ trail step-up pix_77b1c0e4aa10
cliente cust_123  banco postgresql://trail:***@localhost:5432/trail
  REQUIRE_CONFIRMATION pix_77b1c0e4aa10
  present this exact amount and recipient and request explicit confirmation
  R$ 800,00 → João Pereira  ·  estado AWAITING_CONFIRMATION
  a autenticação foi registrada; a confirmação continua sendo do cliente, na conversa onde o PIX foi proposto
```

(Last line: "authentication was recorded; confirmation still belongs to the
customer, in the conversation where the PIX was proposed".)

Step-up grants **assurance**, never **consent**: policy is re-evaluated and the
customer still has to confirm in the conversation where the PIX was proposed.
There is no path from here that moves money.

## 8. When to escalate

- `trail intents` lists the same intent in `SUBMITTED` after the agent has come
  back up: the sweep did not run — check the agent's boot log.
- The customer reports **two** debits for a single request: stop, reconcile
  nothing else, and take the `intent_id` to whoever owns the ledger; the
  idempotency key is the `intent_id`, and a double debit contradicts invariant I1
  (never move money twice for the same request).
- `trail intents` shows a DSN that is not the one for the environment you intend
  to operate on: stop and fix `TRAIL_DATABASE_URL` before any writing command.
- `trail reconcile` answers `unknown reference` for an id the UI showed: check
  that `TRAIL_CUSTOMER_ID` is the `sub` of the user who had the conversation, not
  some other user.

## 9. In production (AWS)

In production Postgres (RDS) lives inside the VPC; the operator reaches it through
an SSM port-forwarding tunnel via the bastion host. The `customer_id` is the
Cognito `sub`, not `cust_123`. You need `secretsmanager:GetSecretValue` on the RDS
secret and `ssm:StartSession` on the bastion.

The values in angle brackets are CloudFormation outputs printed by `cdk deploy`
(see `infra-cdk/`): `BastionInstanceId` from the `BankingAgentOps` stack,
`DbEndpoint` and `DbSecretArn` from `BankingAgentData`, and `UserPoolId` from
`BankingAgentIdentity`. The Makefile's region defaults to `sa-east-1`
(`AWS_REGION`).

```bash
# 1. tunnel to RDS (terminal 1, stays open): forwards localhost:15432 to <DbEndpoint>:5432
make ops-tunnel BASTION=<BastionInstanceId> DB_HOST=<DbEndpoint>
```

```bash
# 2. operator environment (terminal 2)
export TRAIL_DATABASE_URL=postgresql://trail@localhost:15432/trail
export TRAIL_DATABASE_SECRET_ARN=<DbSecretArn>     # PGPASSWORD is loaded from here
export TRAIL_CONTROL_PLANE_STORE=postgres
export TRAIL_CUSTOMER_ID=$(aws cognito-idp admin-get-user --region sa-east-1 \
  --user-pool-id <UserPoolId> --username <username> \
  --query "UserAttributes[?Name=='sub'].Value" --output text)
```

```bash
# 3. the usual three commands
# On the operator's machine, boto3 comes from the `aws` extra (it is already in the Runtime image):
uv run --extra aws trail intents
uv run --extra aws trail reconcile pix_xxxxxxxxxxxx
uv run --extra aws trail intents
```
