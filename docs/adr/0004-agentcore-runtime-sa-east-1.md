# ADR 0004 — AgentCore Runtime in `sa-east-1`, on an in-region model

**Status:** accepted · **Date:** 2026-09-15 · **Evidence:** `tests/unit/test_pg_pool.py`, `tests/unit/test_trace_links.py`, `infra-cdk/bin/app.ts`, `infra-cdk/lib/agent-stack.ts`

## Context

The agent handles a Brazilian customer's banking data: balances, contacts, payment instructions. Two questions had to be answered with a real deployment rather than on paper:

1. **Where does the inference payload go?** Hosting the service in São Paulo means little if every prompt is routed to a model endpoint in another geography.
2. **What does the hosted runtime change about the code?** The service already ran under compose with Postgres, a signed identity header and Langfuse. AgentCore Runtime runs one microVM per session, exposes a fixed HTTP contract, and puts its own authorizer in front of the container.

The Bedrock catalogue in `sa-east-1` (listed on 2026-09-15) constrained the first question:

- Newer Claude models are only reachable there through `global.*` cross-region inference profiles, which route the request outside the region.
- `anthropic.claude-3-sonnet-20240229-v1:0` is a native on-demand model in the region, but it failed at `ConverseStream` with `ResourceNotFoundException` until the Anthropic first-time use-case form was submitted for the account.
- The `openai.gpt-5.6-*` IDs are listed in the region but return `AccessDeniedException` for this account.
- `openai.gpt-oss-120b-1:0` is a native on-demand model in the region and answered a Converse request with `stopReason=tool_use`.

## Decision

### Region and model

The CDK app pins `region: "sa-east-1"` instead of inheriting `AWS_REGION` from the shell. The default model is **`openai.gpt-oss-120b-1:0`**, an open-weight model invoked by its native in-region on-demand model ID — no inference profile — so the inference payload is processed in São Paulo. `-c model=` still overrides it at deploy time.

Measured on the deployed runtime: a smoke turn exited 0 in 1.26 s total, with 2,737 input and 81 output tokens. Price from the AWS Price List (`sa-east-1`, standard on-demand): USD 0.18 per 1M input tokens and USD 0.73 per 1M output tokens, recorded in `src/trail/costs.py`.

This choice is about residency and availability. It is **not** the result of a quality benchmark; that comparison has not been run.

### Runtime shape

- **One image for compose and AgentCore.** The same `Dockerfile` builds both; the AWS build is `linux/arm64` with the `aws` extra. The container listens on port 8080. `docker/entrypoint.sh` switches on `TRAIL_OTEL_MODE`: under `adot` it runs uvicorn inside `opentelemetry-instrument`, so the ADOT distro installs the global tracer provider before the app is imported.
- **`/ping` and `/invocations`.** The Runtime exposes only these two paths. `/ping` answers `Healthy` (never `HealthyBusy`: the service holds no background work that should keep a session alive). `/invocations` takes a JSON `op` — `start_thread`, `list_threads`, `get_thread`, `delete_thread`, `turn` — and each op calls the same helper the corresponding `/threads*` route calls under compose, so both deployments give the same answer.
- **Network.** VPC mode, in private subnets with egress through a NAT gateway, next to the RDS Postgres instance. ECR, CloudWatch Logs and S3 are reached through VPC endpoints.
- **Identity, checked twice.** The Runtime has a Cognito JWT authorizer, and `Authorization` is on its request-header allowlist so the token reaches the container. The process then verifies the token again (`TRAIL_IDENTITY_MODE=jwt`, `src/trail/jwt_identity.py`): RS256 signature against the pool's JWKS, `iss`, `exp`, `client_id`/`aud`, and `sub` becomes the customer id. AWS's own sample decodes the token without checking the signature, on the grounds that the authorizer already did. This service checks anyway, because the same image also runs outside the Runtime — compose, a laptop, a test — where nothing distinguishes a forwarded header from a forged one. The cost is one cached JWKS fetch per key rotation.
- **Secrets at boot.** The database password and the HMAC key that seals confirmed actions (ADR 0003) are read from Secrets Manager once at startup (`src/trail/dbsecret.py`) into `PGPASSWORD` and `TRAIL_CONFIRMATION_SECRET`. The database URL carries no password.
- **Session lifecycle.** Idle session timeout 120 s, maximum lifetime 2 h.

### Postgres pools check connections on checkout

Every pool the process opens — the `AsyncConnectionPool` behind the LangGraph checkpointer, the LangGraph store's `PoolConfig`, and the synchronous `ConnectionPool` behind `PgStore` and `PgBank` — is built from `src/control_plane/pg_pool.py`, capped at `min_size=0` / `max_size=2`, and passes `check=<Pool>.check_connection`. On checkout the pool runs one round trip; a dead connection is discarded and replaced instead of being handed to the caller.

This came out of an incident during the first deployment. Startup succeeded (`/ping` 200, persistence and control plane on Postgres), but the first `/invocations` after an idle gap returned 500:

```text
psycopg.OperationalError: consuming input failed: ... connection abort
  at owner_of → AsyncPostgresStore.aget
```

Tried first, none of which fixed it:

- Capping every pool at `min_size=0` / `max_size=2` and turning on TCP keepalives.
- Moving RDS to a larger instance class for connection headroom, after many idle sessions — each with four pools at default size — had exhausted connection slots.
- Cutting the idle session timeout from 15 min to 120 s.
- Draining stale sessions: `StopRuntimeSession` returned 404 for sessions that were still answering `/ping` and serving `/invocations`.
- Pinning a session: the client's `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header did not guarantee routing to that microVM; invocations landed on whichever warm session the Runtime selected.

Connection counts on RDS at the time were well under the limit, and the failing sessions had been idle for roughly six to seven minutes. **Root cause:** an idle AgentCore session gets no CPU between invocations. Neither the kernel's keepalive probes nor psycopg_pool's maintenance tasks run, so pooled connections die unnoticed, and the next request checks one out. Keepalives could not help, because they need the process to be scheduled.

The fix was reproduced locally against compose Postgres: open persistence as production does, call `store.aget`, run `pg_terminate_backend` on every other backend, call `store.aget` again. Before the fix the second call raised `OperationalError: consuming input failed: server closed the connection unexpectedly`; after it, the call returned the stored value (the synchronous `ConnectionPool` variant also passed). After deploying the fix, the smoke turn above succeeded. `tests/unit/test_pg_pool.py::test_every_pool_checks_a_connection_before_handing_it_out` keeps the `check` from being dropped.

### Observability

Under `TRAIL_OTEL_MODE=adot`, spans go to CloudWatch GenAI Observability. One turn produced 22 spans: `AgentCore.Runtime.Invoke` → `POST /invocations` → `trail.turn` → `invoke_agent` → the model and tool spans. In `adot` mode the trace link the service returns points at the CloudWatch GenAI Observability console for `AWS_REGION` instead of Langfuse (`src/trail/telemetry.py::trace_url`, `tests/unit/test_trace_links.py::test_adot_mode_links_to_agentcore_observability`).

## Consequences

- **Payload residency holds only while the model ID stays native.** Switching `-c model=` to a `global.*` profile — the only way to reach newer Claude models from this region — silently gives up in-region inference. That is a deliberate decision to make, not a config tweak.
- **Customer banking data lands in CloudWatch.** The spans carry prompts and model outputs. That is useful for debugging and is also a copy of customer data in a second store, with its own retention and access policy. Whether to keep, redact or drop that content is an **open privacy decision**.
- **Trace APIs are not the way to read traces.** X-Ray indexing is at 1%, so the X-Ray trace APIs return little; the console reads the spans directly.
- **Every checkout costs a round trip.** Paid on every pool checkout instead of a 500 on the first turn after an idle gap.
- **Session hygiene cannot be relied on.** Stopping sessions and routing to a chosen session did not behave as expected, so correctness cannot depend on a session being fresh or on reaching a specific microVM; the process must survive being frozen at any point.
- **The hosted runtime is now a second code path to keep equal.** `/invocations` must keep delegating to the same helpers as the `/threads*` routes.

## Alternatives rejected

- **A `global.*` Claude inference profile.** Newer Claude models, but the request is routed outside `sa-east-1`, which defeats the point of deploying in São Paulo.
- **`anthropic.claude-3-sonnet` in-region.** Blocked at `ConverseStream` until the Anthropic use-case form was submitted for the account.
- **The GPT-5.6 family.** Listed in the region, not available to this account.
- **Trusting the Runtime's authorizer alone.** Leaves the container's identity story dependent on where it happens to run.
- **Keepalives, bigger RDS, shorter idle timeout, session draining** as the fix for the 500. Each was tried; none addressed a process that is not scheduled while idle.

## Not done / open

- A forced `UNKNOWN` payment drill (and its operator resolution) in the deployed environment.
- Deploying and verifying the web UI stack against the Runtime.
- A model benchmark: quality of the typed-action extraction across candidate models.
- Bedrock versus an external model API.
- This control plane versus AgentCore Policy.
- What to do with prompts and outputs in CloudWatch spans.
