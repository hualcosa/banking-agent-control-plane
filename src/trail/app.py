"""The conversation service: three endpoints over one agent.

``POST /threads`` opens a thread and returns its greeting.
``POST /threads/{id}/turns/stream`` runs one turn and streams its frames.
``GET  /healthz`` says the process is up.

The streaming endpoint is the only interesting one, and what makes it work is
what it does *not* do: it holds no per-turn state, owns no vocabulary, and
renders whatever :func:`~trail.runtime.turns.run_turn` yields. Adding a stage,
a guardrail or a whole new example agent changes nothing in this file.

Two lifetimes are managed here and getting either wrong is subtle:

* **Persistence** is opened in the lifespan and held for the process.
  ``from_conn_string`` is an async context manager; entering it per request
  closes the pool when the request ends, which presents as an exhausted-pool
  error under load and as nothing at all in a smoke test.
* **Telemetry** is set up at *module scope*, below the ``app`` definition, not
  in the lifespan. ``FastAPIInstrumentor`` patches ``build_middleware_stack``,
  which Starlette calls on its way *into* the lifespan scope — a call from
  inside is already too late and yields no HTTP server spans at all.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from trail import costs
from trail.config import Settings, get_settings
from trail.identity import HEADER as IDENTITY_HEADER
from trail.identity import verify as verify_identity
from trail.runtime.agent import build_agent
from trail.runtime.checkpointers import open_persistence
from trail.runtime.events import SSE_HEADERS, error_json, sse
from trail.runtime.registry import load_spec
from trail.runtime.threads import (
    ThreadSummary,
    forget,
    list_threads,
    open_thread,
    owner_of,
    record_turn,
    visible_to,
)
from trail.runtime.turns import ERROR, TURN, run_turn
from trail.telemetry import configure_logging, setup_telemetry

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Wire models
# --------------------------------------------------------------------------


class StartThreadResponse(BaseModel):
    """A new thread, and the opening line the client should show."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    agent: str
    greeting: str
    #: Echoed so a client can render the dial without a second request, and so
    #: a screenshot of a demo carries the setting that produced it.
    guardrails: str


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)


class ThreadListResponse(BaseModel):
    """The conversation list, and whether it will still be here tomorrow.

    ``durable`` is not decoration. With ``TRAIL_CHECKPOINTER=memory`` this list
    is empty after every restart, and a client that cannot tell that apart from
    "you have had no conversations" renders a bug where there is a setting.
    """

    model_config = ConfigDict(extra="forbid")

    threads: list[dict] = Field(default_factory=list)
    durable: bool


class Message(BaseModel):
    """One turn of a conversation, as a reader sees it.

    Only ``user`` and ``agent``. Tool results and the empty assistant turns
    that carry tool calls are machinery, and the machinery already has the
    pipeline rail — putting it in the transcript twice would be showing the
    framework rather than the conversation.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "agent"]
    text: str


class ThreadResponse(BaseModel):
    """A conversation, reopened."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    messages: list[Message] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve the example, open persistence, and compile the agent once.

    Compiling once is deliberate. ``create_agent`` builds a graph, and building
    one per request would pay that cost on every turn to produce an identical
    object — while also making a thread's continuity depend on a graph that no
    longer exists by the time the next turn arrives.
    """
    settings = get_settings()
    spec = load_spec(settings.agent)
    prices = costs.load_prices(settings.model_prices)

    async with open_persistence(settings.checkpointer, settings.database_url) as store:
        app.state.settings = settings
        app.state.spec = spec
        app.state.prices = prices
        app.state.persistence = store
        app.state.agent = build_agent(spec, settings, persistence=store, prices=prices)
        logger.info(
            "agent ready: name=%s model=%s guardrails=%s checkpointer=%s durable=%s",
            spec.name,
            settings.model,
            settings.guardrails,
            store.kind,
            store.durable,
        )
        if not settings.identity_secret.get_secret_value():
            # Said once, loudly, at startup: with no secret nothing can present
            # a verifiable identity, so every thread endpoint answers 401 and
            # the service looks broken rather than unauthenticated. Better a
            # line in the log than an afternoon spent on the client.
            logger.warning(
                "TRAIL_IDENTITY_SECRET is empty: no request can authenticate, "
                "every thread endpoint will answer 401"
            )

        # Whatever the example has to do before serving anything. For the
        # banking agent that is the restart sweep: an intent left in SUBMITTED
        # means a previous process called the bank and died before the answer
        # arrived, it has no way out on its own, and `reconcile` only accepts
        # UNKNOWN. Running it here — once, at boot, with no principal — is what
        # makes "the container restarted mid-payment" recoverable rather than a
        # stranded customer. What it *is* belongs to the example; that it runs
        # belongs here.
        for line in spec.on_startup() if spec.on_startup else ():
            logger.warning("%s: %s", spec.name, line)

        yield


app = FastAPI(
    title="TRAIL — traced agent runtime",
    version="1.0.0",
    summary="An LLM+tools agent with switchable guardrails and an observable pipeline.",
    description=(
        "One turn is a sequence of frames: a stage per guardrail, model call "
        "and tool call, each with its own latency, then the answer and a trace "
        "link. A guardrail that is switched off still reports itself, marked "
        "skipped, because an absence that renders as nothing is "
        "indistinguishable from a success."
    ),
    lifespan=lifespan,
)

# Module scope, with the app, and both halves matter — see this module's
# docstring. Get it wrong and the service silently emits no HTTP server spans,
# which makes a cross-service trace unreadable in exactly the service the
# observability argument is about.
configure_logging()
setup_telemetry(get_settings().service_name, app)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


#: One message for every way identity can fail: absent header, malformed
#: header, wrong secret, signature for a customer that does not exist. A 401
#: that says *which* is an oracle — it tells an attacker whether a customer id
#: is real and how close a forged signature came — so this endpoint answers the
#: same way to all of them, and says nothing a caller did not already know.
_UNAUTHENTICATED = "identidade ausente ou inválida"


def customer_id(request: Request) -> str:
    """The customer this request acts for, resolved from the channel.

    This function is the *whole* identity resolution in the service. Everything
    downstream — ``run_turn``, the ``configurable`` dict, ``context_for``, the
    control plane's principal checks — takes the customer as a value and never
    asks where it came from, which is what makes swapping the scheme a change
    here and nowhere else.

    Milestone 4 does exactly that swap: the Cognito JWT authorizer in front of
    AgentCore Runtime forwards the token, and this body becomes "verify the
    signature against the JWKS, read the ``sub`` claim". The resolver changes;
    the plumbing behind it does not. Keep it that way — a customer id that
    enters through any other door (a request body, a tool argument, a value the
    model repeats) is an identity the model can choose, which is the entire
    thing this seam exists to prevent.
    """
    resolved = verify_identity(
        request.headers.get(IDENTITY_HEADER),
        _settings(request).identity_secret.get_secret_value(),
    )
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHENTICATED
        )
    return resolved


#: One message for a thread this customer cannot have. A thread that belongs to
#: someone else and a thread that never existed answer with the same 404 and
#: the same string: 403 would confirm the id is real, which is the only thing
#: an attacker holding a guessed thread id is missing. The control plane makes
#: the same choice for a ledger trail — see ``ControlPlane.explain``.
_NO_SUCH_THREAD = "conversa não encontrada"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_THREAD)


async def _refuse_someone_elses_thread(store, thread_id: str, customer: str) -> None:
    """Refuse a turn on a thread that already belongs to somebody else.

    Weaker than :func:`~trail.runtime.threads.visible_to`, and deliberately: a
    turn on an id the index has never seen is how a thread is resumed after a
    restart and how a client that skipped ``POST /threads`` starts one, so an
    unknown id is *claimed* rather than refused. What cannot happen is a turn
    on a thread someone else owns — that would run the agent over their
    checkpoint and stream their conversation back as context.

    The answer is the same 404 as reading it, for the same reason.
    """
    owner = await owner_of(store, thread_id)
    if owner is not None and owner != customer:
        raise _not_found()


def _store(request: Request):
    """The cross-thread store, or ``None`` when the runtime has no persistence."""
    persistence = getattr(request.app.state, "persistence", None)
    return getattr(persistence, "store", None)


def _readable_messages(messages: list) -> list[Message]:
    """A stored conversation as a transcript, dropping the machinery.

    ``type`` is one of ``human``, ``ai`` or ``tool``; an ``ai`` message with no
    content is a turn that only asked for a tool. Verified against a live
    thread rather than assumed.
    """
    out: list[Message] = []
    for message in messages:
        kind = getattr(message, "type", "")
        content = getattr(message, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if kind == "human":
            out.append(Message(role="user", text=content))
        elif kind == "ai":
            out.append(Message(role="agent", text=content))
    return out


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.post(
    "/threads",
    response_model=StartThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_thread(
    request: Request, customer: str = Depends(customer_id)
) -> StartThreadResponse:
    """Open a thread.

    No model call happens here. The greeting is the example's own string, and
    spending a turn's tokens to produce a hello would put a cost on the
    scoreboard that bought nothing.
    """
    settings = _settings(request)
    spec = request.app.state.spec
    thread_id = str(uuid4())
    # Indexed on creation, not on first use. A thread that was opened and never
    # answered is worth seeing as exactly that — indexing only what succeeded
    # would hide the case where every turn is failing.
    # The customer is written with the record: this is where the thread gets an
    # owner, and every other endpoint reads it back to decide what it may say.
    await open_thread(_store(request), thread_id, customer)
    return StartThreadResponse(
        thread_id=thread_id,
        agent=spec.name,
        greeting=spec.greeting,
        guardrails=settings.guardrails,
    )


@app.post("/threads/{thread_id}/turns/stream", response_class=StreamingResponse)
async def stream_turn(
    thread_id: str,
    body: TurnRequest,
    request: Request,
    customer: str = Depends(customer_id),
):
    """Run one turn, streaming its frames as Server-Sent Events.

    This always answers 200, even when the turn fails. By the time anything can
    go wrong the response body has begun, and a body that has begun cannot
    become a 500 — so failures arrive as an ``error`` frame carrying the status
    the buffered endpoint would have returned.
    """
    settings = _settings(request)
    agent = request.app.state.agent

    store = _store(request)
    await _refuse_someone_elses_thread(store, thread_id, customer)

    async def frames() -> AsyncIterator[str]:
        async for name, payload in run_turn(
            agent,
            thread_id=thread_id,
            message=body.message,
            settings=settings,
            customer_id=customer,
        ):
            yield sse(name, error_json(payload) if name == ERROR else payload)
        # After the frames, so a client is never waiting on a write it cannot
        # see. A failed turn is still recorded: the sidebar should show the
        # conversation you were having when it broke.
        await record_turn(store, thread_id, body.message, customer)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers=SSE_HEADERS
    )


@app.post("/threads/{thread_id}/turns")
async def submit_turn(
    thread_id: str,
    body: TurnRequest,
    request: Request,
    customer: str = Depends(customer_id),
) -> dict:
    """Run one turn and return only the answer.

    Drains the same generator the streaming endpoint renders. One
    implementation behind two endpoints is what stops the streamed answer and
    the buffered answer from being able to disagree.
    """
    settings = _settings(request)
    agent = request.app.state.agent
    store = _store(request)
    await _refuse_someone_elses_thread(store, thread_id, customer)
    await record_turn(store, thread_id, body.message, customer)
    answer: dict | None = None

    async for name, payload in run_turn(
        agent,
        thread_id=thread_id,
        message=body.message,
        settings=settings,
        customer_id=customer,
    ):
        if name == TURN:
            answer = payload
        elif name == ERROR:
            # Re-raised rather than rendered: this endpoint's body has not
            # begun, so it can still answer with a real status code.
            raise (
                payload
                if isinstance(payload, HTTPException)
                else HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=str(payload)
                )
            )

    if answer is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the turn produced no answer",
        )
    return answer


@app.get("/threads", response_model=ThreadListResponse)
async def get_threads(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    customer: str = Depends(customer_id),
) -> ThreadListResponse:
    """This customer's conversations, most recently used first.

    Scoped, not merely authenticated. The list is the cheapest disclosure in
    the service — one request and you have every conversation id in the
    deployment — so the customer resolved from the header is a filter here and
    not just a gate.
    """
    persistence = getattr(request.app.state, "persistence", None)
    summaries: list[ThreadSummary] = await list_threads(
        _store(request), customer_id=customer, limit=limit, offset=offset
    )
    return ThreadListResponse(
        threads=[summary.as_json() for summary in summaries],
        durable=bool(persistence and persistence.durable),
    )


@app.get("/threads/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: str, request: Request, customer: str = Depends(customer_id)
) -> ThreadResponse:
    """Reopen one of this customer's conversations.

    Two stores answer two different questions, and both are asked. The index
    says whether this thread is *this customer's*; the checkpointer says what
    was said in it. A thread the index does not have for this customer — theirs
    and never opened, or somebody else's — is a 404 with the same body either
    way, so the endpoint cannot be walked to find out which thread ids exist.

    A thread that is this customer's but has no checkpoint still answers 200
    with an empty transcript. It is a real thread; it is one nobody has spoken
    to yet, and that is not the same answer as "no such thread".
    """
    if not await visible_to(_store(request), thread_id, customer):
        raise _not_found()
    agent = request.app.state.agent
    state = await agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = (state.values or {}).get("messages", []) if state else []
    return ThreadResponse(thread_id=thread_id, messages=_readable_messages(messages))


@app.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    thread_id: str, request: Request, customer: str = Depends(customer_id)
) -> None:
    """Drop one of this customer's conversations from the list.

    Someone else's thread is a 404 and so is an id that never existed — the
    same pair of answers ``GET`` gives, because a delete that answered 204 for
    a thread it did not touch would be a lie, and one that answered 403 would
    confirm the id. The cost is that deleting twice is a 404 the second time;
    an idempotent 204 here would be an existence oracle, and that is the more
    expensive of the two.

    The checkpoint stays. "Delete" here means what it means to someone tidying
    a sidebar, and this endpoint does not reach into storage it does not own to
    do something irreversible.
    """
    store = _store(request)
    if not await visible_to(store, thread_id, customer):
        raise _not_found()
    await forget(store, thread_id)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
