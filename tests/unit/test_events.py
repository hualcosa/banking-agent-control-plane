"""The wire contract: frames, their rendering, and the persistence registry.

These are the assertions a client depends on. A frame that loses a field or
changes an event name breaks a browser that this suite never runs, so the
shape is pinned here rather than discovered there.
"""

from __future__ import annotations

import json

import pytest

from trail.runtime.checkpointers import KINDS, open_persistence
from trail.runtime.events import StageEvent, emit, sse, stage_from_chunk

pytestmark = pytest.mark.unit


def test_a_frame_renders_as_one_sse_event() -> None:
    rendered = sse("stage", {"name": "model", "ns": 12})
    assert rendered.startswith("event: stage\ndata: ")
    assert rendered.endswith("\n\n")
    assert json.loads(rendered.split("data: ", 1)[1]) == {"name": "model", "ns": 12}


def test_accents_survive_the_wire_unescaped() -> None:
    """An SSE stream is UTF-8 by specification.

    Escaping "pendência" into ``\\u00ea`` spends bytes to make the wire harder
    to read, and every label this runtime emits is Portuguese.
    """
    assert "saída" in sse("stage", {"label": "saída"})


def test_a_payload_is_always_a_single_data_line() -> None:
    """A newline inside a string would otherwise split the frame in two."""
    rendered = sse("turn", {"text": "linha um\nlinha dois"})
    assert rendered.count("data:") == 1
    assert len(rendered.strip().splitlines()) == 2


def test_a_stage_event_rejects_an_unknown_status() -> None:
    """``blocked`` and ``skip`` are the vocabulary; a typo must not pass through.

    A frame with a status the client does not recognise renders as nothing,
    which is the one outcome this whole design exists to prevent.
    """
    with pytest.raises(ValueError, match="status"):
        StageEvent(name="x", kind="model", label="x", status="finished")  # type: ignore[arg-type]


def test_a_stage_event_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        StageEvent(name="x", kind="database", label="x", status="done")  # type: ignore[arg-type]


def test_a_stage_event_accepts_any_name_and_label() -> None:
    """The closed sets are ``kind`` and ``status``; the words are open.

    That split is what lets the rail render an agent nobody has written yet.
    """
    event = StageEvent(
        name="tool:whatever_someone_writes",
        kind="tool",
        label="qualquer",
        status="done",
    )
    assert event.name == "tool:whatever_someone_writes"


def test_a_frame_survives_a_round_trip_through_the_custom_channel() -> None:
    event = StageEvent(
        name="model", kind="model", label="modelo", status="done", ns=7_400
    )
    chunk = {"trail_stage": event.model_dump(mode="json")}
    assert stage_from_chunk(chunk) == event


def test_another_writer_s_custom_data_is_ignored() -> None:
    """The custom channel carries whatever any node wrote to it.

    A tool that knows nothing about this module may stream its own progress;
    the envelope key is what stops that from being parsed as a stage frame.
    """
    assert stage_from_chunk({"progress": "50%"}) is None
    assert stage_from_chunk("a string") is None


def test_emitting_outside_a_graph_is_a_no_op_not_an_error() -> None:
    """A check is testable on its own precisely because this does not raise."""
    emit(StageEvent(name="x", kind="io", label="x", status="done"))


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


async def test_memory_persistence_provides_both_slots() -> None:
    async with open_persistence("memory", "") as store:
        assert store.checkpointer is not None
        assert store.store is not None


async def test_memory_persistence_reports_itself_as_not_durable() -> None:
    """The honest label. A thread here dies with the process."""
    async with open_persistence("memory", "") as store:
        assert not store.durable


async def test_an_unknown_kind_names_the_valid_set() -> None:
    with pytest.raises(ValueError, match="memory, postgres"):
        async with open_persistence("redis", ""):
            pass


def test_postgres_is_offered() -> None:
    """The durable option must exist in the registry even though the unit tier
    cannot exercise it — a registry with one entry is not a slot."""
    assert "postgres" in KINDS
