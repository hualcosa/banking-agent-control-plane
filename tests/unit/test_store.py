"""Where a run goes after it is scored — proved without a database.

`store.py` talks to Postgres through exactly the surface `psycopg.AsyncConnection`
exposes: ``execute().fetchone()`` and a ``cursor()`` used as an async context
manager for ``executemany``. :class:`FakeConnection` below is that surface and
nothing more, recording every ``(sql, params)`` pair a real driver would have
sent over the wire so a test can assert on the statement family and the exact
values bound — not just that the call happened.

The integration test (`tests/integration/test_eval_run.py::test_a_run_survives_the_round_trip_through_postgres`)
proves the real driver accepts what this module sends. This file proves what
gets sent, offline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from trail.evals import store
from trail.evals.cases import Finding
from trail.evals.metrics import Metric, RunReport

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# A fake psycopg connection — records every call, scripts every fetchone
# --------------------------------------------------------------------------


class FakeCursor:
    """Enough of ``AsyncCursor`` for `store.py`: ``fetchone``, ``executemany``,
    and the async-context-manager protocol `connection.cursor()` is used under."""

    def __init__(self, calls: list[tuple], fetchone_rows: list) -> None:
        self._calls = calls
        self._fetchone_rows = fetchone_rows

    async def fetchone(self):
        return self._fetchone_rows.pop(0) if self._fetchone_rows else None

    async def executemany(self, sql: str, params_seq) -> None:
        self._calls.append(("executemany", sql, list(params_seq)))

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeConnection:
    """Enough of ``AsyncConnection`` for `store.py`. Every ``execute`` and
    ``executemany`` lands in ``.calls`` as ``(kind, sql, params)``; ``fetchone``
    pops from a scripted queue, in call order."""

    def __init__(self, fetchone_rows: list | None = None) -> None:
        self.calls: list[tuple] = []
        self._fetchone_rows = list(fetchone_rows or [])

    async def execute(self, sql: str, params=None) -> FakeCursor:
        self.calls.append(("execute", sql, params))
        return FakeCursor(self.calls, self._fetchone_rows)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.calls, self._fetchone_rows)


# --------------------------------------------------------------------------
# find_schema — env override, checkout walk-up, cwd fallback, not found
# --------------------------------------------------------------------------
#
# All four cases have to fake ``__file__`` itself: the real `store.py` lives
# inside this very checkout, so the checkout-walk-up branch would otherwise
# always succeed first and the other three branches would be unreachable.


def test_find_schema_prefers_the_env_override(tmp_path, monkeypatch) -> None:
    root = tmp_path / "override_root"
    (root / "db").mkdir(parents=True)
    schema = root / "db" / "schema.sql"
    schema.write_text("-- override")
    monkeypatch.setenv("TRAIL_DOCS_ROOT", str(root))
    # Neither the file-walk nor cwd could find anything here — a pass proves
    # the override was actually checked first, not just checked at all.
    monkeypatch.setattr(store, "__file__", str(tmp_path / "nowhere" / "store.py"))
    monkeypatch.chdir(tmp_path)
    assert store.find_schema() == schema


def test_find_schema_walks_up_from_file_to_the_checkout_root(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("TRAIL_DOCS_ROOT", raising=False)
    checkout = tmp_path / "checkout"
    (checkout / "db").mkdir(parents=True)
    (checkout / "README.md").write_text("marker")
    schema = checkout / "db" / "schema.sql"
    schema.write_text("-- checkout")
    monkeypatch.setattr(
        store, "__file__", str(checkout / "src" / "trail" / "evals" / "store.py")
    )
    # cwd is a sibling with nothing in it, so a hit proves it came from the walk.
    monkeypatch.chdir(tmp_path)
    assert store.find_schema() == schema


def test_find_schema_falls_back_to_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TRAIL_DOCS_ROOT", raising=False)
    monkeypatch.setattr(store, "__file__", str(tmp_path / "nowhere" / "store.py"))
    cwd_root = tmp_path / "cwd_root"
    (cwd_root / "db").mkdir(parents=True)
    schema = cwd_root / "db" / "schema.sql"
    schema.write_text("-- cwd")
    monkeypatch.chdir(cwd_root)
    assert store.find_schema() == schema


def test_find_schema_returns_none_when_nowhere_has_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TRAIL_DOCS_ROOT", raising=False)
    monkeypatch.setattr(store, "__file__", str(tmp_path / "nowhere" / "store.py"))
    monkeypatch.chdir(tmp_path)
    assert store.find_schema() is None


# --------------------------------------------------------------------------
# ensure_schema — reads the file and executes it; warns and skips otherwise
# --------------------------------------------------------------------------


async def test_ensure_schema_reads_the_file_and_executes_its_text(
    tmp_path, monkeypatch
) -> None:
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE eval_runs ();")
    monkeypatch.setattr(store, "find_schema", lambda: schema)
    conn = FakeConnection()
    await store.ensure_schema(conn)
    assert conn.calls == [("execute", "CREATE TABLE eval_runs ();", None)]


async def test_ensure_schema_warns_and_skips_when_the_file_is_missing(
    monkeypatch, caplog
) -> None:
    """Storage failure never costs a run — a missing schema file logs and
    moves on rather than raising, per the module's own docstring."""
    monkeypatch.setattr(store, "find_schema", lambda: None)
    conn = FakeConnection()
    with caplog.at_level(logging.WARNING):
        await store.ensure_schema(conn)
    assert conn.calls == []
    assert "schema.sql not found" in caplog.text


# --------------------------------------------------------------------------
# latest_baseline — row -> Baseline, no row -> None, params carry both filters
# --------------------------------------------------------------------------


async def test_latest_baseline_builds_a_baseline_from_the_row() -> None:
    conn = FakeConnection(fetchone_rows=[(7, "v1", {"case_pass_rate": {"value": 1.0}})])
    baseline = await store.latest_baseline(conn, "v1")
    assert baseline == store.Baseline(
        id=7, golden_set_version="v1", metrics={"case_pass_rate": {"value": 1.0}}
    )


async def test_latest_baseline_filters_on_version_and_completed_status() -> None:
    conn = FakeConnection(fetchone_rows=[(1, "v1", {})])
    await store.latest_baseline(conn, "v1")
    kind, sql, params = conn.calls[0]
    assert kind == "execute"
    assert "FROM eval_runs" in sql
    assert "status = 'COMPLETED'" in sql
    # The single bound parameter is the golden-set version, not the status —
    # COMPLETED is baked into the statement, never bound as data.
    assert params == ("v1",)


async def test_latest_baseline_is_none_without_a_matching_row() -> None:
    conn = FakeConnection(fetchone_rows=[])
    assert await store.latest_baseline(conn, "no-such-set") is None


async def test_latest_baseline_defaults_a_null_metrics_column_to_empty_dict() -> None:
    conn = FakeConnection(fetchone_rows=[(3, "v1", None)])
    baseline = await store.latest_baseline(conn, "v1")
    assert baseline.metrics == {}


# --------------------------------------------------------------------------
# save_run — one run row, one findings row per finding, Jsonb payloads
# --------------------------------------------------------------------------


def report(*findings: Finding) -> RunReport:
    return RunReport(
        golden_set_version="v1",
        status="COMPLETED",
        metrics=[Metric("case_pass_rate", 1.0, "rate", 2, 2)],
        findings=list(findings),
        baseline_id=9,
    )


def finding(case_id: str) -> Finding:
    return Finding(
        case_id=case_id,
        turn=0,
        kind="OMISSION",
        check="contains",
        source="check",
        expected="essencial",
        actual="algo",
        detail="faltou",
    )


async def test_save_run_inserts_the_run_row_with_every_field_bound() -> None:
    started = datetime(2026, 8, 27, tzinfo=UTC)
    r = report()
    conn = FakeConnection(fetchone_rows=[(42,)])
    run_id = await store.save_run(
        conn,
        r,
        agent="trail_guide",
        model="gpt-x",
        guardrails="both",
        judge_model="judge-y",
        started_at=started,
    )
    assert run_id == 42
    kind, sql, params = conn.calls[0]
    assert kind == "execute"
    assert "INSERT INTO eval_runs" in sql
    assert "RETURNING id" in sql
    (
        p_started,
        p_version,
        p_agent,
        p_model,
        p_guardrails,
        p_judge_model,
        p_status,
        p_metrics,
        p_baseline_id,
    ) = params
    assert (p_started, p_version, p_agent, p_model, p_guardrails, p_judge_model) == (
        started,
        "v1",
        "trail_guide",
        "gpt-x",
        "both",
        "judge-y",
    )
    assert p_status == "COMPLETED"
    assert isinstance(p_metrics, Jsonb)
    assert p_metrics.obj == r.metrics_json()
    assert p_baseline_id == 9


async def test_save_run_inserts_one_findings_row_per_finding_linked_to_the_run_id() -> (
    None
):
    r = report(finding("case-a"), finding("case-b"))
    conn = FakeConnection(fetchone_rows=[(42,)])
    run_id = await store.save_run(
        conn,
        r,
        agent="trail_guide",
        model="gpt-x",
        guardrails="both",
        judge_model="",
        started_at=datetime.now(UTC),
    )
    kind, sql, rows = conn.calls[1]
    assert kind == "executemany"
    assert "INSERT INTO eval_findings" in sql
    assert rows == [
        (
            run_id,
            f.case_id,
            f.turn,
            f.kind,
            f.check,
            f.source,
            f.expected,
            f.actual,
            f.detail,
        )
        for f in r.findings
    ]
    assert [row[0] for row in rows] == [run_id, run_id]


async def test_save_run_skips_the_findings_insert_when_there_are_none() -> None:
    """A run with a clean scorecard must not send an empty ``executemany``."""
    conn = FakeConnection(fetchone_rows=[(1,)])
    await store.save_run(
        conn,
        report(),
        agent="a",
        model="m",
        guardrails="both",
        judge_model="",
        started_at=datetime.now(UTC),
    )
    assert len(conn.calls) == 1
    assert conn.calls[0][0] == "execute"


# --------------------------------------------------------------------------
# connect — autocommit, schema applied, connection handed back
# --------------------------------------------------------------------------


async def test_connect_opens_autocommit_and_applies_the_schema(monkeypatch) -> None:
    fake_conn = FakeConnection()
    calls: list[tuple] = []

    async def fake_connect(conninfo, *, autocommit=False, **kwargs):
        calls.append((conninfo, autocommit, kwargs))
        return fake_conn

    monkeypatch.setattr(AsyncConnection, "connect", fake_connect)
    # ensure_schema itself is covered above; here only the wiring matters.
    monkeypatch.setattr(store, "find_schema", lambda: None)

    database_url = "postgresql://trail@db/trail"
    result = await store.connect(database_url)

    assert result is fake_conn
    assert calls == [(database_url, True, {})]
