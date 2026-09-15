"""Where a run goes after it is scored, and where the baseline comes from.

Two tables, both defined in ``db/schema.sql`` and neither defined here. This
module *applies* that file rather than carrying its own copy of the DDL: the
Postgres image runs it on an empty volume, and this runs it again on a volume
that predates the harness. A second copy in Python would be two definitions of
one schema, disagreeing on the first change.

Storage failure never costs a run. If Postgres is unreachable the scorecard
still prints, with a line saying it was not recorded and no baseline was
compared — a harness that threw away results because it could not file them
would be the most annoying possible way to lose an afternoon.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from trail.evals.metrics import RunReport

logger = logging.getLogger(__name__)

_MARKERS = ("README.md", "docker-compose.yml")


def find_schema() -> Path | None:
    """``db/schema.sql``, from wherever the process started.

    Same three-step search `examples/trail_guide/tools.py` uses, and for the
    same reason: walking up from ``__file__`` is right in a checkout and wrong
    in the image, where the package is installed under ``site-packages``.
    """
    override = os.environ.get("TRAIL_DOCS_ROOT")
    roots = [Path(override)] if override else []
    roots += [
        parent
        for parent in Path(__file__).resolve().parents
        if any((parent / marker).exists() for marker in _MARKERS)
    ]
    roots.append(Path.cwd())
    for root in roots:
        schema = root / "db" / "schema.sql"
        if schema.exists():
            return schema
    return None


@dataclass(frozen=True)
class Baseline:
    """The run a new one is compared against."""

    id: int
    golden_set_version: str
    metrics: dict[str, Any]


async def ensure_schema(connection: AsyncConnection) -> None:
    """Apply ``db/schema.sql``. Idempotent; every statement is ``IF NOT EXISTS``."""
    schema = find_schema()
    if schema is None:
        logger.warning("db/schema.sql not found; assuming the tables already exist")
        return
    await connection.execute(schema.read_text())


async def latest_baseline(
    connection: AsyncConnection, golden_set_version: str
) -> Baseline | None:
    """The most recent ``COMPLETED`` run of the same golden set, or ``None``.

    Two filters and both are the rule made mechanical. ``status = 'COMPLETED'``
    is the zero-tolerance boundary: a run that fabricated a fact cannot become
    the thing a later run is judged "no regression" against. The version match
    is the other half — a delta against a different set of cases is a number
    with no meaning that looks exactly like one with meaning.
    """
    row = await (
        await connection.execute(
            """
            SELECT id, golden_set_version, metrics
              FROM eval_runs
             WHERE golden_set_version = %s AND status = 'COMPLETED'
          ORDER BY started_at DESC
             LIMIT 1
            """,
            (golden_set_version,),
        )
    ).fetchone()
    if row is None:
        return None
    return Baseline(id=row[0], golden_set_version=row[1], metrics=row[2] or {})


async def save_run(
    connection: AsyncConnection,
    report: RunReport,
    *,
    agent: str,
    model: str,
    guardrails: str,
    judge_model: str,
    started_at: datetime,
) -> int:
    """Write the run and its findings; return the run id.

    Findings go in one ``executemany`` rather than a loop of round trips: a
    failing run is exactly the one with hundreds of them, and it is also the
    one whose numbers someone is waiting on.
    """
    run_id = (
        await (
            await connection.execute(
                """
                INSERT INTO eval_runs (started_at, finished_at, golden_set_version,
                                       agent, model, guardrails, judge_model,
                                       status, metrics, baseline_id)
                     VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)
                  RETURNING id
                """,
                (
                    started_at,
                    report.golden_set_version,
                    agent,
                    model,
                    guardrails,
                    judge_model,
                    report.status,
                    Jsonb(report.metrics_json()),
                    report.baseline_id,
                ),
            )
        ).fetchone()
    )[0]

    if report.findings:
        async with connection.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO eval_findings (run_id, case_id, turn, kind, check_name,
                                           source, expected, actual, detail)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
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
                    for f in report.findings
                ],
            )
    return run_id


async def connect(database_url: str) -> AsyncConnection:
    """An autocommit connection with the schema applied."""
    connection = await AsyncConnection.connect(database_url, autocommit=True)
    await ensure_schema(connection)
    return connection
