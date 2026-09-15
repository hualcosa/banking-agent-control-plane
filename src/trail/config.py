"""Runtime configuration, read from the environment with a ``TRAIL_`` prefix.

One :class:`Settings` object serves every process — agent, evals, and CLI. The
values that differ per container (``TRAIL_SERVICE_NAME``, ``TRAIL_AGENT_BASE_URL``)
are set in ``docker-compose.yml``; everything else has a working default.

The API key is a :class:`~pydantic.SecretStr` and is never logged or
serialised: ``repr`` and ``str`` render it as ``**********``, and
``model_dump(mode="json")`` emits the same mask. Read it exactly once, at the
call site, via ``settings.llm_api_key.get_secret_value()``.

The model is reached through LangChain's ``init_chat_model``, bound by default
to an OpenAI-compatible endpoint (``TRAIL_LLM_PROVIDER``). That default is a
deliberate portability choice rather than a vendor one: OpenAI, Fireworks AI,
Together AI, DeepInfra and DeepSeek all speak the same dialect, so moving
between them is ``TRAIL_LLM_BASE_URL`` plus ``TRAIL_MODEL`` and no code change.
Another provider entirely — Bedrock, say — is ``TRAIL_LLM_PROVIDER``.

Three fields describe the *shape* of the agent rather than its credentials —
``guardrails``, ``checkpointer`` and ``agent``. They are the dials this
scaffold exists to expose: which gates run, where conversation state lives, and
which example is mounted. Each is a registry key, so an unknown value fails at
startup with the valid set in the message rather than at the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed configuration. Every field maps to ``TRAIL_<FIELD>``."""

    model_config = SettingsConfigDict(
        env_prefix="TRAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model -------------------------------------------------------------
    llm_api_key: SecretStr

    #: Default is OpenAI's cheapest GPT-5.6 tier. Extraction is a reading task,
    #: not a reasoning one, so this starts at the bottom of the range rather
    #: than the top. Descending from a frontier model and measuring what breaks
    #: on the way down is what the eval harness is for, not a shortcut around
    #: it — though no such descent has been run and published from this
    #: repository yet, so this is a choice and not yet a finding.
    model: str = "gpt-5.6-luna"

    #: Which LangChain integration builds the model — the prefix in
    #: ``init_chat_model("<provider>:<model>")``. ``openai`` covers every
    #: OpenAI-compatible host (see ``llm_base_url``); ``bedrock_converse``
    #: reaches Amazon Bedrock and authenticates from the ambient AWS chain
    #: rather than ``llm_api_key``, so it needs ``uv sync --extra bedrock`` and
    #: a region, not a key. Any other string LangChain knows works too, as long
    #: as its integration package is installed.
    llm_provider: str = "openai"

    #: ``None`` uses OpenAI's endpoint. Point it at another Responses-API host
    #: (Fireworks, Together, DeepInfra, api.deepseek.com) to swap providers
    #: without touching code. Note that third-party hosts serving open weights
    #: may quantize: a metric that moves after such a swap has two candidate
    #: causes, so change one variable at a time.
    llm_base_url: str | None = None

    prompt_version: str = "2026-08-15.2"

    #: The model that grades the golden set's judge checks. Empty means the
    #: agent's own model, which is the default because demanding a second model
    #: to run the suite at all is how a suite stops being run — and the
    #: scorecard flags every such run as self-evaluation rather than letting
    #: the bias pass unstated. Set it to a different model and the flag goes
    #: away, which is the only thing that actually removes the bias.
    judge_model: str = ""

    #: ``none`` disables reasoning. Extraction copies what was said; it does not
    #: deliberate, and reasoning tokens here are latency and spend with nothing
    #: to show for them.
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "none"

    max_tokens: int = 2048

    #: Per-model rates as JSON, merged over the table in ``trail.costs``::
    #:
    #:     TRAIL_MODEL_PRICES='{"my-model": {"input": 0.5, "output": 1.5}}'
    #:
    #: A model with no rate reports a cost of ``None`` rather than zero, so
    #: this is how a deployment prices a model this repository has never heard
    #: of without waiting for a release.
    model_prices: str = ""

    # --- Infrastructure ----------------------------------------------------
    # libpq obtains the password from PGPASSWORD. Credentials do not belong in
    # a URL that can surface in logs, traces, process listings or exceptions.
    database_url: str = "postgresql://trail@postgres:5432/trail"

    #: ARN of the Secrets Manager secret RDS generated for the database user.
    #: Empty — compose, tests — leaves `PGPASSWORD` to the environment.
    database_secret_arn: str = ""
    #: ARN of the secret holding the confirmation key (plain text). Empty
    #: leaves `TRAIL_CONFIRMATION_SECRET` to the environment.
    confirmation_secret_arn: str = ""

    #: Where the control plane keeps intents and its ledger. ``memory`` is the
    #: default so that a unit test, a REPL or a `python -c` never opens a
    #: connection — and deliberately NOT derived from ``database_url`` being
    #: set, because the test tier sets that variable and would then need a
    #: database to run offline. The compose stack sets this to ``postgres``
    #: for the services that serve traffic, which is what makes `trail intents`
    #: and `trail reconcile` read the same state the agent writes.
    control_plane_store: Literal["memory", "postgres"] = "memory"

    #: Keys the digest that binds a confirmation to one exact action. Empty
    #: falls back to the development default inside the control plane, which is
    #: fine locally and is why the startup log says so when it happens: a
    #: deployment that ships the dev key has a confirmation anyone who reads
    #: this repository can forge.
    confirmation_secret: SecretStr = SecretStr("")
    #: Where the UI and CLI reach the agent. Port 8080 matches the AgentCore
    #: Runtime contract, and the compose agent container listens on it too.
    agent_base_url: str = "http://agent:8080"

    #: OTLP/HTTP+protobuf, full signal path. Langfuse does not accept OTLP over
    #: gRPC at all, and the HTTP exporter's ``endpoint=`` kwarg does NOT
    #: auto-append ``/v1/traces`` the way the generic ``OTEL_EXPORTER_OTLP_ENDPOINT``
    #: env var does when a collector reads it directly — the path has to be
    #: spelled out here.
    otel_exporter_otlp_endpoint: str = (
        "http://langfuse-web:3000/api/public/otel/v1/traces"
    )

    #: Extra OTLP headers, in the OTel standard `k=v,k=v` form. Langfuse
    #: authenticates ingestion with HTTP Basic over the project's API key pair,
    #: so this carries `Authorization: Basic <b64(pk:sk)>`. Keeping it a generic
    #: header string rather than a `langfuse_api_key` field is what preserves the
    #: claim in telemetry.py: the vendor lives in configuration, not in code.
    otel_exporter_otlp_headers: str = ""

    #: Where a **browser** reaches the Langfuse UI, which is not where this
    #: process reaches Langfuse's OTLP collector. The endpoint above names a
    #: compose service and is resolved inside the compose network; the link
    #: built from this one is clicked from a laptop, where
    #: ``http://langfuse-web:3000`` resolves to nothing. The two look
    #: interchangeable and are not, and the failure is a dead link in a demo
    #: rather than an error in a log. Deployed behind a real hostname, this
    #: becomes that hostname while the OTLP endpoint stays internal.
    langfuse_ui_base_url: str = "http://localhost:3000"

    #: Langfuse scopes every trace URL by project, so the deep link needs the
    #: project id as well as the host. This is knowable at config time only
    #: because the stack is provisioned headlessly with a fixed project id — see
    #: LANGFUSE_INIT_PROJECT_ID in docker-compose.yml. Change one and change both.
    langfuse_project_id: str = "trail"

    #: ``langfuse`` (compose) or ``adot`` (AgentCore Runtime). The entrypoint
    #: reads the same variable to decide who installs the tracer provider; here
    #: it decides which UI the trace link opens.
    otel_mode: str = "langfuse"

    #: Region of the CloudWatch console the ``adot`` trace link opens. Read
    #: unprefixed because AgentCore Runtime already sets ``AWS_REGION``.
    aws_region: str = Field(default="", validation_alias="AWS_REGION")

    service_name: str = "trail-agent"

    # --- Identity ----------------------------------------------------------

    #: Shared secret the channel signs the customer id with, and the service
    #: verifies (``trail.identity``). Empty — the default — authenticates
    #: **nobody**: every request to a thread endpoint is refused with 401.
    #: That is deliberate. A missing auth secret that opens the door is the
    #: failure mode this setting exists to prevent, so the safe default is the
    #: one that is loudly broken rather than quietly wide open.
    #:
    #: A shared HMAC secret is the local stand-in for the channel's real
    #: identity provider; the AWS deployment uses a Cognito JWT instead
    #: (``identity_mode = "jwt"``), verified at the same point in ``trail.app``.
    identity_secret: SecretStr = SecretStr("")

    #: Which resolver ``trail.app.customer_id`` runs. ``hmac`` is the signed
    #: header (compose, CLI, tests); ``jwt`` is the Cognito bearer token the
    #: AgentCore Runtime forwards in production. Same seam, different verifier.
    identity_mode: Literal["hmac", "jwt"] = "hmac"
    #: ``https://cognito-idp.<region>.amazonaws.com/<user pool id>``. Empty
    #: authenticates nobody in ``jwt`` mode — fail closed, like the secret.
    jwt_issuer: str = ""
    #: The Cognito app client the runtime's authorizer allows.
    jwt_client_id: str = ""

    #: Which customer the **CLI** presents when it signs that header. It is a
    #: client-side setting, not a server-side one: the service never reads it,
    #: and nothing about a request is trusted because this says so. It stands
    #: in for the login `trail chat` does not have yet.
    customer_id: str = "cust_123"

    #: Cognito access token for ``trail invoke`` against a deployed AgentCore
    #: Runtime. Empty leaves invoke to fail closed with a message naming the
    #: variable rather than opening an unauthenticated connection.
    bearer_token: SecretStr = SecretStr("")

    #: Default runtime ARN for ``trail invoke`` when ``--runtime-arn`` is
    #: omitted. Empty means the flag is required on the command line.
    runtime_arn: str = ""

    # --- The dials ----------------------------------------------------------

    #: Which gates run. This is the whole guardrail configuration: the runtime
    #: turns it into a middleware list, and a mode that omits a gate still
    #: emits that gate's stage frame with ``status="skip"``. A guardrail you
    #: cannot see is not a guardrail you can trust, so switching one off is
    #: visible on the pipeline rail rather than silent.
    guardrails: Literal["both", "input", "output", "none"] = "both"

    #: Where conversation state lives between turns. ``memory`` loses every
    #: thread when the process restarts and is the right default for tests and
    #: a first run; ``postgres`` is what makes a thread outlive the container
    #: that started it. Same agent code either way — this is the point of the
    #: checkpointer being a constructor argument.
    checkpointer: Literal["memory", "postgres"] = "memory"

    #: Which example agent to mount. Resolved against the registry in
    #: ``trail.runtime.registry``; the default is ``banking``, the control-plane
    #: demo. ``trail_guide`` still ships and answers questions about TRAIL.
    agent: str = "banking"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings`, constructed once.

    Cached because reading the environment and the ``.env`` file on every
    request is pointless work, and because a single instance means the whole
    process agrees on the prompt version stamped into traces and records.

    Tests that need different values should call ``get_settings.cache_clear()``
    after patching the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
