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
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.fakes import calls, says, scripted
from trail.config import get_settings
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


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


@contextmanager
def running_app(monkeypatch: pytest.MonkeyPatch, model: Any) -> Iterator[TestClient]:
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
    with TestClient(app_module.app) as client:
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
    model = scripted(says("pronto"))
    with running_app(monkeypatch, model) as client:
        thread = open_thread(client)
        client.post(f"/threads/{thread['thread_id']}/turns", json={"message": "oi"})

        delete_response = client.delete(f"/threads/{thread['thread_id']}")
        listed = client.get("/threads").json()["threads"]
        reopened = client.get(f"/threads/{thread['thread_id']}").json()

    assert delete_response.status_code == 204
    assert listed == []
    # "Delete" means off the sidebar, not out of the checkpointer.
    assert reopened["messages"] == [
        {"role": "user", "text": "oi"},
        {"role": "agent", "text": "pronto"},
    ]
