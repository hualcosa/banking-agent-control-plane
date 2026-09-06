"""Assembling one agent: a model, some tools, two gates, and a rail.

This module is the whole "framework" and it is deliberately short. Everything
underneath it — the tool-calling loop, the graph, the checkpointer protocol,
the streaming channels — is LangChain's, because writing those by hand is the
undifferentiated heavy lifting this repository exists to remove rather than to
demonstrate.

What TRAIL adds is the seam. An example supplies an :class:`AgentSpec`: a
system prompt, its tools, and the two checks it wants at its edges. The runtime
decides which of those checks are *mounted*, where state lives, and how every
step reports itself. An example never imports LangChain, and the runtime never
knows what the example is about.

Middleware order matters and is not arbitrary. ``TraceMiddleware`` is first,
which makes it the outermost layer: wrap-style hooks run outside-in and
node-style hooks unwind in reverse, so the rail opens before any gate and
closes after all of them. A blocked output guard has therefore already reported
itself by the time the rail's closing frame is emitted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent

from trail import costs
from trail.config import Settings
from trail.runtime.checkpointers import Persistence
from trail.runtime.middleware.guards import GuardSpec, build_guards
from trail.runtime.middleware.trace import TraceMiddleware


@dataclass(frozen=True)
class AgentSpec:
    """What an example agent is, in terms that mention no framework.

    An example module exposes one of these and nothing else. That is the
    contract: no LangChain import, no middleware, no knowledge of how the
    stream is shaped — which is what makes an example portable to whatever this
    runtime is built on next.
    """

    name: str
    system_prompt: str
    tools: Sequence[Any] = field(default_factory=tuple)
    guards: GuardSpec = field(default_factory=GuardSpec)
    #: Shown by the client as the opening line. Not a model call: an agent that
    #: burns a turn to say hello is charging for a greeting.
    greeting: str = ""


def build_model(settings: Settings) -> Any:
    """The chat model for ``settings``, as ``TRAIL_LLM_PROVIDER`` names it.

    ``init_chat_model`` reaches any provider LangChain has an integration for,
    and the provider is a setting rather than a literal so that reaching one is
    configuration and not a rewrite. The default stays ``openai``, which is a
    portability choice rather than a vendor one: OpenAI, Fireworks, Together,
    DeepInfra and DeepSeek all speak that dialect, so moving between them is
    ``TRAIL_LLM_BASE_URL`` plus ``TRAIL_MODEL``. ``bedrock_converse`` is the
    other one this repo installs (``uv sync --extra bedrock``); it authenticates
    from the ambient AWS chain, so the key and base URL below would be arguments
    ``ChatBedrockConverse`` has no field for and are sent only for ``openai``.
    """
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {"max_tokens": settings.max_tokens}
    if settings.llm_provider == "openai":
        kwargs["api_key"] = settings.llm_api_key.get_secret_value()
        # Passed explicitly, and the default of "none" is load-bearing. Left
        # unset, the integration sends a reasoning effort of its own, and a
        # reasoning model on /v1/chat/completions refuses function tools while
        # one is set:
        #
        #   Function tools with reasoning_effort are not supported […]
        #   To use function tools, use /v1/responses or set reasoning_effort to 'none'.
        #
        # Every tool call in the agent fails with a 400 that names a parameter
        # this repository never chose to send. Raise TRAIL_EFFORT above "none"
        # only against a Responses-API endpoint.
        kwargs["reasoning_effort"] = settings.effort
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
    return init_chat_model(f"{settings.llm_provider}:{settings.model}", **kwargs)


def build_agent(
    spec: AgentSpec,
    settings: Settings,
    *,
    model: Any | None = None,
    persistence: Persistence | None = None,
    thread_id: str | None = None,
    prices: dict[str, costs.ModelPrice] | None = None,
) -> Any:
    """Compile ``spec`` into a runnable agent.

    ``model`` is injectable and that is not a convenience. The unit suite
    passes a fake chat model and exercises the entire loop — gates, tool calls,
    rail frames, jump-to-end — with no network, no key and no Docker. A runtime
    that could only build its model from configuration would push every one of
    those assertions into the integration tier, where they run rarely and fail
    slowly.
    """
    guards = build_guards(settings.guardrails, spec.guards)
    middleware = [
        TraceMiddleware(
            mode=settings.guardrails,
            thread_id=thread_id,
            prompt_version=settings.prompt_version,
            prices=prices,
        ),
        *guards,
    ]
    return create_agent(
        model=model if model is not None else build_model(settings),
        tools=list(spec.tools),
        system_prompt=spec.system_prompt,
        middleware=middleware,
        checkpointer=persistence.checkpointer if persistence else None,
        store=persistence.store if persistence else None,
        name=spec.name,
    )
