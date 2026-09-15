"""The FastAPI service, offline: lifespan, streaming, buffered turns, threads.

``trail.app`` wires the runtime pieces tested elsewhere (the loop in
``test_agent_loop.py``, the index in ``test_threads.py``) into HTTP. What is
worth asserting here is specific to the wiring: that the lifespan compiles a
real agent, that both turn endpoints answer the same way one generator
produces, that a failed turn still answers 200 while streaming and a real
status code while buffered, and that the thread endpoints expose the index
and the checkpointer correctly.

``build_agent`` is the injection seam. The lifespan calls it with no ``model``,
which would build a real ``ChatOpenAI`` and try to reach a network this tier
never has. Patching the module-level name — the same trick ``test_agent_loop``
plays directly on ``build_agent`` — makes the *whole app* run against a
scripted model instead, with no other line of ``app.py`` any different from
production.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from starlette.testclient import TestClient

from control_plane import Context, ControlPlane, MockBank
from examples.banking import tools as banking_tools
from tests.fakes import calls, says, scripted
from trail.config import get_settings
from trail.identity import HEADER as IDENTITY_HEADER
from trail.identity import sign, verify
from trail.runtime.agent import build_agent as real_build_agent

# ``trail.app`` runs OpenTelemetry setup at *module* scope — see its
# docstring on why it cannot wait for the lifespan. That executes exactly once
# per process, on whichever test imports the module first, which may be during
# collection, before the hermetic-env autouse fixture in ``tests/conftest.py``
# has run even once. Pinning the two settings that call depends on here, ahead
# of the import, is what keeps that one-time setup offline regardless of
# import order.
os.environ.setdefault("TRAIL_LLM_API_KEY", "unit-tests-never-call-the-api")
os.environ["TRAIL_OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
get_settings.cache_clear()

from trail import app as app_module  # noqa: E402

pytestmark = pytest.mark.unit

#: The nighttime PIX rule reads the clock, so the plane under test gets a
#: fixed one — same midday as ``test_banking_agent``.
MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)

#: The service verifies every request against this; the tests sign with it.
#: A secret the tests hold is the point — an identity nobody can forge is only
#: interesting if the suite can produce a *valid* one to contrast with.
SECRET = "unit-test-identity-secret"
CUSTOMER = "cust_123"
OTHER_CUSTOMER = "cust_999"


def header(customer: str = CUSTOMER, secret: str = SECRET) -> dict[str, str]:
    return {IDENTITY_HEADER: sign(customer, secret)}


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


@contextmanager
def running_app(
    monkeypatch: pytest.MonkeyPatch, model: Any, customer: str = CUSTOMER
) -> Iterator[TestClient]:
    """A ``TestClient`` whose lifespan compiles the real agent against ``model``.

    ``TestClient.__enter__`` runs ``trail.app.lifespan`` for real: it opens an
    in-memory checkpointer and store and calls ``build_agent`` exactly as
    production does. Patching that one name is the only substitution — the
    tool node, the guard middleware and the trace middleware are the genuine
    ones, so a passing test here is a claim about the actual wiring.
    """

    def fake_build_agent(spec, settings, *, persistence=None, prices=None, **_):
        return real_build_agent(
            spec, settings, model=model, persistence=persistence, prices=prices
        )

    monkeypatch.setattr(app_module, "build_agent", fake_build_agent)
    monkeypatch.setenv("TRAIL_IDENTITY_SECRET", SECRET)
    get_settings.cache_clear()
    # Every request this client makes carries a valid identity, so the tests
    # below read as they did before identity existed. The ones that care about
    # identity drop or replace the header explicitly.
    with TestClient(app_module.app, headers=header(customer)) as client:
        yield client


def read_sse(response: Any) -> list[tuple[str, Any]]:
    """Every ``(event, data)`` frame in a completed SSE response body."""
    frames: list[tuple[str, Any]] = []
    event = None
    for line in response.text.splitlines():
        if not line:
            event = None
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            frames.append((event, json.loads(line[5:].strip())))
    return frames


def open_thread(client: TestClient) -> dict[str, Any]:
    response = client.post("/threads")
    assert response.status_code == 201
    return response.json()


# --------------------------------------------------------------------------
# healthz
# --------------------------------------------------------------------------


def test_healthz_says_the_process_is_up() -> None:
    """No lifespan needed: the route touches no app state."""
    with TestClient(app_module.app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# starting a thread
# --------------------------------------------------------------------------


def test_starting_a_thread_costs_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The greeting is the example's own string, not a paid-for turn.

    An empty script means the model raises ``StopIteration`` on its first
    call. Opening a thread still answers 201, which is the proof no call was
    made.
    """
    with running_app(monkeypatch, scripted()) as client:
        thread = open_thread(client)

    assert thread["agent"] == "banking"
    assert thread["greeting"]
    assert thread["guardrails"] == "both"
    # A real uuid4, not an accidental echo of some fixed string.
    import uuid

    uuid.UUID(thread["thread_id"])


# --------------------------------------------------------------------------
# streaming turns
# --------------------------------------------------------------------------


def test_a_streamed_turn_reports_stages_then_an_answer_then_a_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted(
        calls("search_docs", query="what is TRAIL"),
        says("O TRAIL é um scaffold local."),
    )
    with running_app(monkeypatch, model) as client:
        thread = open_thread(client)
        response = client.post(
            f"/threads/{thread['thread_id']}/turns/stream",
            json={"message": "o que é o TRAIL?"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = read_sse(response)
    names = [name for name, _ in frames]

    assert "stage" in names
    assert names.count("turn") == 1
    assert names[-1] == "trace"  # always last, even on success

    answer = next(data for name, data in frames if name == "turn")
    assert answer["text"] == "O TRAIL é um scaffold local."
    assert answer["thread_id"] == thread["thread_id"]


def test_a_failed_streamed_turn_answers_200_with_an_error_frame_and_still_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the body has begun cannot become a real status code.

    ``run_turn`` re-raises inside the generator; the endpoint renders it as an
    ``error`` frame instead. ``record_turn`` runs unconditionally after the
    frames, so the sidebar still shows the conversation that broke — asserted
    here by listing threads afterward.
    """
    with running_app(monkeypatch, scripted()) as client:  # empty script -> raises
        thread = open_thread(client)
        response = client.post(
            f"/threads/{thread['thread_id']}/turns/stream",
            json={"message": "oi"},
        )
        assert response.status_code == 200
        frames = read_sse(response)
        names = [name for name, _ in frames]
        assert "error" in names
        assert names[-1] == "trace"
        error = next(data for name, data in frames if name == "error")
        assert error == {"status": 500, "detail": "internal error"}

        threads = client.get("/threads").json()["threads"]

    assert [t["thread_id"] for t in threads] == [thread["thread_id"]]
    assert threads[0]["turns"] == 1


def test_an_empty_message_is_rejected_before_touching_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic's ``min_length=1`` is the whole guard; no turn is ever run."""
    with running_app(monkeypatch, scripted()) as client:  # would explode if called
        thread = open_thread(client)
        response = client.post(
            f"/threads/{thread['thread_id']}/turns/stream", json={"message": ""}
        )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# the buffered endpoint
# --------------------------------------------------------------------------


def test_the_buffered_endpoint_answers_with_the_same_text_streaming_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted(says("pronto"))
    with running_app(monkeypatch, model) as client:
        thread = open_thread(client)
        response = client.post(
            f"/threads/{thread['thread_id']}/turns", json={"message": "oi"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "pronto"
    assert body["thread_id"] == thread["thread_id"]


def test_the_buffered_endpoint_reraises_a_failed_turn_as_a_real_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body has not begun here, so a failure can still be a status code.

    ``submit_turn`` records the turn *before* draining the generator, unlike
    the streaming endpoint — so a turn that fails this way is recorded even
    though the client never saw a 200.
    """
    with running_app(monkeypatch, scripted()) as client:  # empty script -> raises
        thread = open_thread(client)
        response = client.post(
            f"/threads/{thread['thread_id']}/turns", json={"message": "oi"}
        )
        assert response.status_code == 502

        threads = client.get("/threads").json()["threads"]

    assert threads[0]["turns"] == 1


# --------------------------------------------------------------------------
# listing threads
# --------------------------------------------------------------------------


def test_the_thread_list_hides_unanswered_threads_and_reports_durability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted(says("a"))
    with running_app(monkeypatch, model) as client:
        open_thread(client)  # opened, never spoken to: must not appear
        spoken = open_thread(client)
        client.post(f"/threads/{spoken['thread_id']}/turns", json={"message": "oi"})

        body = client.get("/threads").json()

    assert [t["thread_id"] for t in body["threads"]] == [spoken["thread_id"]]
    # TRAIL_CHECKPOINTER is unset in the hermetic env, so this run is "memory".
    assert body["durable"] is False


def test_thread_list_pagination_reads_limit_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = scripted(says("a"), says("b"), says("c"))
    with running_app(monkeypatch, model) as client:
        ids = []
        for message in ("primeira", "segunda", "terceira"):
            thread = open_thread(client)
            client.post(
                f"/threads/{thread['thread_id']}/turns", json={"message": message}
            )
            ids.append(thread["thread_id"])

        full = client.get("/threads").json()["threads"]
        page = client.get("/threads?limit=1&offset=1").json()["threads"]

    # Most recently used first: the third turn taken is first in the list.
    assert [t["thread_id"] for t in full] == list(reversed(ids))
    assert [t["thread_id"] for t in page] == [full[1]["thread_id"]]


# --------------------------------------------------------------------------
# reopening a thread
# --------------------------------------------------------------------------


def test_get_thread_drops_tool_machinery_and_keeps_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_readable_messages``: human and non-empty ai in, tool calls out.

    The scripted turn takes a tool round, which drives a real ``tool`` message
    and an ``ai`` message whose only content is the tool call (empty text).
    Both must be invisible in the transcript a client renders.
    """
    model = scripted(
        calls("search_docs", query="what is TRAIL"),
        says("O TRAIL é um scaffold local."),
    )
    with running_app(monkeypatch, model) as client:
        thread = open_thread(client)
        client.post(
            f"/threads/{thread['thread_id']}/turns/stream",
            json={"message": "o que é o TRAIL?"},
        )
        body = client.get(f"/threads/{thread['thread_id']}").json()

    assert body["thread_id"] == thread["thread_id"]
    assert body["messages"] == [
        {"role": "user", "text": "o que é o TRAIL?"},
        {"role": "agent", "text": "O TRAIL é um scaffold local."},
    ]


def test_get_thread_on_an_unanswered_thread_is_an_empty_transcript_not_a_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread nobody has spoken to is real; it just has nothing said yet."""
    with running_app(monkeypatch, scripted()) as client:
        thread = open_thread(client)
        response = client.get(f"/threads/{thread['thread_id']}")

    assert response.status_code == 200
    assert response.json()["messages"] == []


# --------------------------------------------------------------------------
# deleting a thread
# --------------------------------------------------------------------------


def test_deleting_a_thread_drops_it_from_the_list_but_keeps_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting is still off the sidebar, not out of the checkpointer.

    What changed with ownership is where the transcript can be reached from.
    The index is what says a thread is yours, so dropping the record drops the
    only evidence that would let the service hand it back: a deleted thread is
    a 404, indistinguishable from an id that never existed, which is the point.

    The checkpoint is untouched, and speaking to the same id proves it — the
    conversation resumes with its history rather than starting empty.
    """
    model = scripted(says("pronto"), says("de novo"))
    with running_app(monkeypatch, model) as client:
        thread = open_thread(client)
        client.post(f"/threads/{thread['thread_id']}/turns", json={"message": "oi"})

        delete_response = client.delete(f"/threads/{thread['thread_id']}")
        listed = client.get("/threads").json()["threads"]
        gone = client.get(f"/threads/{thread['thread_id']}")

        client.post(f"/threads/{thread['thread_id']}/turns", json={"message": "voltei"})
        reopened = client.get(f"/threads/{thread['thread_id']}").json()

    assert delete_response.status_code == 204
    assert listed == []
    assert gone.status_code == 404
    assert reopened["messages"] == [
        {"role": "user", "text": "oi"},
        {"role": "agent", "text": "pronto"},
        {"role": "user", "text": "voltei"},
        {"role": "agent", "text": "de novo"},
    ]


# --------------------------------------------------------------------------
# scoping: a thread belongs to the customer whose turn created it
# --------------------------------------------------------------------------


def test_a_customer_never_sees_another_customers_thread_in_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list is a disclosure, and B's list must not count A's conversations.

    Both customers speak in the same process against the same index, which is
    what makes this a test of the endpoint rather than of two empty stores: A's
    thread is genuinely there, and B's list is empty anyway.
    """
    model = scripted(says("para A"), says("para B"))
    with running_app(monkeypatch, model) as client:
        mine = open_thread(client)
        client.post(f"/threads/{mine['thread_id']}/turns", json={"message": "oi"})

        theirs = client.post("/threads", headers=header(OTHER_CUSTOMER)).json()
        client.post(
            f"/threads/{theirs['thread_id']}/turns",
            json={"message": "olá"},
            headers=header(OTHER_CUSTOMER),
        )

        mine_list = client.get("/threads").json()["threads"]
        theirs_list = client.get("/threads", headers=header(OTHER_CUSTOMER)).json()

    assert [t["thread_id"] for t in mine_list] == [mine["thread_id"]]
    # Not "A's thread is absent" — B's list is exactly B's, and its length
    # says nothing about how many conversations the deployment holds.
    assert [t["thread_id"] for t in theirs_list["threads"]] == [theirs["thread_id"]]


def test_a_borrowed_thread_id_and_an_invented_one_are_the_same_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heart of it: 404, not 403, and byte-identical to a nonexistent id.

    A 403 answers the one question the attacker cannot otherwise answer — is
    this thread id real? — so a thread that belongs to someone else must look
    exactly like a thread that does not exist. Status *and* body are compared,
    because a distinguishing detail string is the same oracle in a different
    field. This is ``ControlPlane.explain``'s rule, applied to a transcript.
    """
    model = scripted(says("segredo do A"))
    with running_app(monkeypatch, model) as client:
        mine = open_thread(client)
        client.post(f"/threads/{mine['thread_id']}/turns", json={"message": "oi"})

        borrowed = client.get(
            f"/threads/{mine['thread_id']}", headers=header(OTHER_CUSTOMER)
        )
        invented = client.get(
            "/threads/00000000-0000-0000-0000-000000000000",
            headers=header(OTHER_CUSTOMER),
        )
        # ...and A still reads their own conversation, so the 404 above is
        # scoping and not a thread that broke.
        own = client.get(f"/threads/{mine['thread_id']}")

    assert borrowed.status_code == invented.status_code == 404
    assert borrowed.json() == invented.json()
    assert own.status_code == 200
    assert own.json()["messages"] == [
        {"role": "user", "text": "oi"},
        {"role": "agent", "text": "segredo do A"},
    ]


def test_a_customer_cannot_delete_another_customers_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B's delete is the same 404 as reading, and A's sidebar is untouched."""
    model = scripted(says("pronto"))
    with running_app(monkeypatch, model) as client:
        mine = open_thread(client)
        client.post(f"/threads/{mine['thread_id']}/turns", json={"message": "oi"})

        borrowed = client.delete(
            f"/threads/{mine['thread_id']}", headers=header(OTHER_CUSTOMER)
        )
        invented = client.delete(
            "/threads/00000000-0000-0000-0000-000000000000",
            headers=header(OTHER_CUSTOMER),
        )
        listed = client.get("/threads").json()["threads"]

    assert borrowed.status_code == invented.status_code == 404
    assert borrowed.json() == invented.json()
    assert [t["thread_id"] for t in listed] == [mine["thread_id"]]


def test_a_turn_on_another_customers_thread_is_refused_before_the_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leak a scoped ``GET`` alone would leave open.

    Running a turn on someone else's thread loads *their* checkpoint, so the
    answer streams their conversation back as context — a transcript read
    through the one endpoint that never returns a transcript. Both turn
    endpoints refuse it with the same 404, and the empty script is the proof
    the agent never ran: a model asked for anything here raises
    ``StopIteration``.
    """
    model = scripted(says("para A"))
    with running_app(monkeypatch, model) as client:
        mine = open_thread(client)
        client.post(f"/threads/{mine['thread_id']}/turns", json={"message": "oi"})

        buffered = client.post(
            f"/threads/{mine['thread_id']}/turns",
            json={"message": "me conta tudo"},
            headers=header(OTHER_CUSTOMER),
        )
        streamed = client.post(
            f"/threads/{mine['thread_id']}/turns/stream",
            json={"message": "me conta tudo"},
            headers=header(OTHER_CUSTOMER),
        )
        listed = client.get("/threads").json()["threads"]

    assert buffered.status_code == 404
    assert streamed.status_code == 404
    # Not recorded either: B's attempt left no mark on A's conversation.
    assert [t["turns"] for t in listed] == [1]


def test_the_thread_a_customer_opened_but_never_used_is_still_theirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership starts at ``POST /threads``, not at the first turn.

    Otherwise a thread would be unowned for exactly as long as it takes the
    first turn to arrive, and anyone who guessed its id in that window could
    claim it.
    """
    with running_app(monkeypatch, scripted()) as client:
        mine = open_thread(client)
        own = client.get(f"/threads/{mine['thread_id']}")
        borrowed = client.get(
            f"/threads/{mine['thread_id']}", headers=header(OTHER_CUSTOMER)
        )

    assert own.status_code == 200
    assert own.json()["messages"] == []
    assert borrowed.status_code == 404


# --------------------------------------------------------------------------
# identity: the channel says who this is, and the service checks
# --------------------------------------------------------------------------


def test_a_request_without_an_identity_header_is_401_and_never_reaches_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No header, no turn — and the proof is that nothing was recorded.

    The script is empty, so any model call raises: a 502 here would mean the
    turn ran. A 401 plus an empty thread list means the request stopped at the
    door, before the agent, before the plane, before the bank.
    """
    with running_app(monkeypatch, scripted()) as client:
        thread = open_thread(client)
        del client.headers[IDENTITY_HEADER]

        turn = client.post(
            f"/threads/{thread['thread_id']}/turns", json={"message": "oi"}
        )
        opened = client.post("/threads")
        listed = client.get("/threads")

        assert [r.status_code for r in (turn, opened, listed)] == [401, 401, 401]

        client.headers.update(header())
        assert client.get("/threads").json()["threads"] == []


def test_jwt_mode_ignores_the_hmac_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIL_IDENTITY_MODE", "jwt")
    monkeypatch.setenv("TRAIL_JWT_ISSUER", "https://issuer.example/pool")
    monkeypatch.setenv("TRAIL_JWT_CLIENT_ID", "client-abc")
    with running_app(monkeypatch, scripted(says("ok"))) as client:
        response = client.post("/threads")  # carries a valid HMAC header
    assert response.status_code == 401
    assert response.json() == {"detail": "identidade ausente ou inválida"}


def test_jwt_mode_reads_sub_from_a_verified_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.test_jwt_identity import CLIENT, ISSUER, resolver, token

    monkeypatch.setenv("TRAIL_IDENTITY_MODE", "jwt")
    monkeypatch.setenv("TRAIL_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("TRAIL_JWT_CLIENT_ID", CLIENT)
    monkeypatch.setattr(app_module, "jwks_key_resolver", lambda issuer: resolver())
    with running_app(monkeypatch, scripted(says("ok"))) as client:
        client.headers.pop(IDENTITY_HEADER)
        response = client.post(
            "/threads", headers={"Authorization": f"Bearer {token()}"}
        )
    assert response.status_code == 201


def test_a_forged_identity_header_is_401_and_the_body_says_nothing_about_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four ways to forge one, one indistinguishable answer.

    The bodies are compared to each other rather than to a literal: what
    matters is not the wording but that a caller cannot tell "that customer
    does not exist" from "your signature was close" from "you sent no header
    at all". A 401 that discriminates is an oracle for both customer ids and
    signatures.
    """
    valid_mac = sign(CUSTOMER, SECRET).split(":")[1]
    forgeries = [
        "",  # empty
        CUSTOMER,  # a claim with no signature at all
        sign(CUSTOMER, "the-wrong-secret"),  # signed, wrong key
        f"{OTHER_CUSTOMER}:{valid_mac}",  # someone else's id on a real MAC
    ]

    with running_app(monkeypatch, scripted()) as client:  # empty script -> raises
        thread = open_thread(client)
        answers = []
        for forged in forgeries:
            client.headers[IDENTITY_HEADER] = forged
            answers.append(
                client.post(
                    f"/threads/{thread['thread_id']}/turns", json={"message": "oi"}
                )
            )

        client.headers.update(header())
        assert client.get("/threads").json()["threads"] == []

    assert [a.status_code for a in answers] == [401, 401, 401, 401]
    assert len({a.text for a in answers}) == 1


def test_two_customers_are_isolated_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant I2, adversary A17: a valid identity is not a skeleton key.

    Customer B arrives with a header of their own — correctly signed, so the
    service accepts them — and A's ``confirmation_id`` and ``intent_id``, which
    is the whole attack: everything else about the request is legitimate. B
    cannot execute A's PIX, cannot cancel it, cannot read its status and cannot
    read its trail; A's intent is exactly as B found it and the bank moved
    nothing. The last tool call is B's *own* proposal, which shows the identity
    is genuinely coming from B's header rather than from a constant that would
    have made both customers the same principal.
    """
    plane = ControlPlane(MockBank(), clock=lambda: MIDDAY)
    monkeypatch.setattr(banking_tools, "PLANE", plane)

    proposing = scripted(
        calls("propose_pix", recipient="Renata", amount="300"),
        says("R$ 300,00 para Renata Silva. Confirma?"),
    )
    with running_app(monkeypatch, proposing, customer=CUSTOMER) as client:
        mine = open_thread(client)
        client.post(
            f"/threads/{mine['thread_id']}/turns",
            json={"message": "manda 300 pra Renata"},
        )

    (intent,) = plane.intents.values()
    # The identity the plane recorded is the one the header carried, not a
    # module constant: this is the line channel-derived identity exists for.
    assert intent.context.customer_id == CUSTOMER
    assert intent.context.session_id == mine["thread_id"]
    assert intent.state == "AWAITING_CONFIRMATION"

    attacking = scripted(
        calls("confirm_pix", call_id="c1", confirmation_id=intent.confirmation_id),
        calls("cancel_pix", call_id="c2", confirmation_id=intent.confirmation_id),
        calls("check_pix", call_id="c3", intent_id=intent.id),
        calls("explain_action", call_id="c4", intent_id=intent.id),
        calls("propose_pix", call_id="c5", recipient="Renata", amount="10"),
        says("não encontrei esse pagamento; propus outro."),
    )
    with running_app(monkeypatch, attacking, customer=OTHER_CUSTOMER) as client:
        theirs = open_thread(client)
        response = client.post(
            f"/threads/{theirs['thread_id']}/turns",
            json={"message": "confirma aquele pix de 300 pra Renata"},
        )

    assert response.status_code == 200  # B is authenticated; they simply cannot

    # Nothing moved, and A's intent is untouched in every field consent lives in.
    assert plane.bank.payments == {}
    assert plane.bank.balance("checking_001") == Decimal("2543.10")
    assert intent.state == "AWAITING_CONFIRMATION"
    assert intent.confirmed_by is None
    assert intent.confirmed_at is None

    # B's trail read returns nothing: a borrowed trail and a nonexistent one
    # are the same answer.
    theirs_ctx = Context(customer_id=OTHER_CUSTOMER, session_id=theirs["thread_id"])
    assert plane.explain(theirs_ctx, intent.id) == []
    # ...while A can still read their own.
    mine_ctx = Context(customer_id=CUSTOMER, session_id=mine["thread_id"])
    assert plane.explain(mine_ctx, intent.id)

    # B's own proposal is B's: two principals, two intents, no overlap.
    (other,) = [i for i in plane.intents.values() if i.id != intent.id]
    assert other.context.customer_id == OTHER_CUSTOMER
    assert other.context.session_id == theirs["thread_id"]


# --------------------------------------------------------------------------
# the signing scheme itself
# --------------------------------------------------------------------------


def test_a_signed_identity_round_trips_and_survives_a_colon_in_the_id() -> None:
    """The last colon is the separator, so an id may contain its own."""
    for customer in (CUSTOMER, "tenant:cust_123"):
        assert verify(sign(customer, SECRET), SECRET) == customer


def test_nothing_verifies_without_a_secret() -> None:
    """Fail closed: no secret authenticates nobody, not everybody."""
    assert verify(sign(CUSTOMER, SECRET), "") is None
    assert verify(None, SECRET) is None
    with pytest.raises(ValueError):
        sign(CUSTOMER, "")


# --------------------------------------------------------------------------
# AgentCore Runtime contract
# --------------------------------------------------------------------------

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def test_ping_says_healthy_in_the_runtime_vocabulary() -> None:
    with TestClient(app_module.app) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_invocations_start_thread_is_the_same_as_post_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_app(monkeypatch, scripted(says("oi"))) as client:
        response = client.post("/invocations", json={"op": "start_thread"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"thread_id", "agent", "greeting", "guardrails"}


def test_invocations_turn_streams_the_same_frames_as_the_thread_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_app(
        monkeypatch, scripted(says("resposta"), says("resposta"))
    ) as client:
        a = client.post("/threads").json()["thread_id"]
        via_route = read_sse(
            client.post(f"/threads/{a}/turns/stream", json={"message": "oi"})
        )
        b = client.post("/invocations", json={"op": "start_thread"}).json()["thread_id"]
        via_invocations = read_sse(
            client.post(
                "/invocations", json={"op": "turn", "thread_id": b, "message": "oi"}
            )
        )
    route_names = [name for name, _ in via_route]
    invocation_names = [name for name, _ in via_invocations]
    assert invocation_names == route_names
    turn = next(d for n, d in via_invocations if n == "turn")
    assert turn["text"] == "resposta"


def test_invocations_turn_falls_back_to_the_session_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_app(monkeypatch, scripted(says("ok"))) as client:
        thread = client.post("/threads").json()["thread_id"]
        response = client.post(
            "/invocations",
            json={"op": "turn", "message": "oi"},
            headers={SESSION_HEADER: thread},
        )
    assert response.status_code == 200
    turn = next(d for n, d in read_sse(response) if n == "turn")
    assert turn["thread_id"] == thread


def test_invocations_rejects_an_unknown_op(monkeypatch: pytest.MonkeyPatch) -> None:
    with running_app(monkeypatch, scripted(says("ok"))) as client:
        response = client.post("/invocations", json={"op": "pay_everyone"})
    assert response.status_code == 400
    assert "op" in response.json()["detail"]


def test_invocations_requires_identity_like_every_other_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with running_app(monkeypatch, scripted(says("ok"))) as client:
        client.headers.pop(IDENTITY_HEADER)
        response = client.post("/invocations", json={"op": "start_thread"})
    assert response.status_code == 401
