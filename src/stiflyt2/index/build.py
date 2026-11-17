"""Build source_index from the active snapshots.

A group is rebuilt only when the active snapshot of its source has changed (or
its SQL has). The new tables are created in source_index_build and moved into
source_index in the same transaction, so a reader sees either the old index
or the new one, and a failed build leaves the old one in place.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import psycopg

from stiflyt2.config import settings
from stiflyt2.index.tables import BUILD, GROUPS, Group

INDEX = "source_index"

_STATE = f"""CREATE TABLE IF NOT EXISTS {INDEX}.index_state (
    source     text PRIMARY KEY,
    signature  text NOT NULL,
    built_at   timestamptz NOT NULL DEFAULT now(),
    seconds    real NOT NULL,
    rows       jsonb NOT NULL
)"""


@dataclass(frozen=True)
class Built:
    source: str
    rows: dict
    seconds: float


def signature(conn, group: Group) -> str | None:
    """What a group is built from: its SQL version and the active snapshot of each scope.

    None when a source it needs has not been imported.
    """
    sources = list(group.depends or (group.source,))
    active = conn.execute(
        """SELECT source_key, scope, snapshot_id FROM import_control.active_snapshot
           WHERE source_key = ANY(%s) ORDER BY 1, 2""",
        (sources,),
    ).fetchall()
    if {source for source, _, _ in active} != set(sources):
        return None
    # A group derived from other groups' tables is rebuilt when their SQL changes too.
    versions = [group.version, *(g.version for g in GROUPS if g.source in group.depends)]
    return ("v" + ".".join(map(str, versions)) + " "
            + " ".join(f"{source}/{scope}={snap}" for source, scope, snap in active))


def _prepare(conn) -> None:
    conn.execute(_STATE)
    conn.execute(f"GRANT SELECT ON {INDEX}.index_state TO stiflyt2_api")


def build(*, force: bool = False, only: str | None = None) -> list[Built]:
    """Bring source_index up to date. Returns the groups that were rebuilt."""
    with psycopg.connect(settings.import_dsn) as conn:
        _prepare(conn)
    built = []
    for group in GROUPS:
        if only and only != group.source and only not in group.depends:
            continue
        with psycopg.connect(settings.import_dsn) as conn:
            wanted = signature(conn, group)
            current = conn.execute(
                f"SELECT signature FROM {INDEX}.index_state WHERE source = %s", (group.source,)
            ).fetchone()
            if wanted is None or (not force and current and current[0] == wanted):
                continue
            started = time.monotonic()
            for table in group.tables:
                conn.execute(f"DROP TABLE IF EXISTS {BUILD}.{table}")
            for sql in group.statements:
                conn.execute(sql)
            rows = {}
            for table in group.tables:
                conn.execute(f"ANALYZE {BUILD}.{table}")
                rows[table] = conn.execute(f"SELECT count(*) FROM {BUILD}.{table}").fetchone()[0]
                conn.execute(f"DROP TABLE IF EXISTS {INDEX}.{table}")
                conn.execute(f"ALTER TABLE {BUILD}.{table} SET SCHEMA {INDEX}")
                conn.execute(f"GRANT SELECT ON {INDEX}.{table} TO stiflyt2_api")
            seconds = time.monotonic() - started
            conn.execute(
                f"""INSERT INTO {INDEX}.index_state (source, signature, seconds, rows)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source) DO UPDATE SET signature = EXCLUDED.signature,
                        built_at = now(), seconds = EXCLUDED.seconds, rows = EXCLUDED.rows""",
                (group.source, wanted, seconds, json.dumps(rows)),
            )
        built.append(Built(group.source, rows, seconds))
    return built


def status() -> list[tuple]:
    """Per source: when its tables were built, their sizes, and whether they are current."""
    out = []
    with psycopg.connect(settings.import_dsn) as conn:
        _prepare(conn)
        for group in GROUPS:
            wanted = signature(conn, group)
            row = conn.execute(
                f"""SELECT signature, built_at::timestamp(0), seconds, rows
                    FROM {INDEX}.index_state WHERE source = %s""",
                (group.source,),
            ).fetchone()
            if wanted is None:
                state = "not imported"
            elif row is None:
                state = "not built"
            else:
                state = "current" if row[0] == wanted else "STALE"
            out.append((group.source, state, *(row[1:] if row else (None, None, {}))))
    return out
