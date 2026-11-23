"""Bring every imported source up to date.

Whatever has an active snapshot is checked again, for the same scope. A source,
municipality or box is added by importing it once by hand; from then on it is
refreshed. Every check is logged, so "nothing changed" can be told apart from
"the job did not run".
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core

SOURCES = ("turrutebasen", "stedsnavn", "kulturminner", "teig", "fkb_wfs", "vegnett", "osm")


@dataclass(frozen=True)
class Check:
    source_key: str
    scope: str
    outcome: str   # unchanged, imported, rejected, unreached or error
    snapshot_id: str | None
    detail: str


def again(source_key: str, scope: str, manifest: dict) -> core.Result:
    """Import the current delivery of one source and scope."""
    from stiflyt2.importer.adapters import (
        fkb_wfs,
        kulturminner,
        osm,
        stedsnavn,
        teig,
        turrutebasen,
        vegnett,
    )

    if source_key == "turrutebasen":
        return turrutebasen.run()
    if source_key == "stedsnavn":
        return stedsnavn.run()
    if source_key == "kulturminner":
        return kulturminner.run()
    if source_key == "teig":
        return teig.run(scope.removeprefix("kommune:"))
    if source_key == "vegnett":
        return vegnett.run(scope.removeprefix("kommune:"))
    if source_key == "osm":
        return osm.run(scope.removeprefix("bbox:"), tuple(manifest["bbox"]))
    if source_key == "fkb_wfs":
        return fkb_wfs.run(scope.removeprefix("bbox:"), tuple(manifest["bbox"]))
    raise KeyError(source_key)


def _store_dir(artifact: str) -> Path | None:
    """The delivery's own directory, <data_dir>/<source>/<checksum>, or None if it is not there."""
    path = Path(artifact).resolve()
    directory = path.parent if path.suffix else path  # a zip in the directory, or the directory
    return directory if directory.parent.parent == settings.data_dir.resolve() else None


def prune(keep: int, only: str | None = None) -> int:
    """Delete the kept original files of all but the newest `keep` snapshots per source and scope.

    The stored feature versions are not touched: every snapshot stays complete
    in the database. The active snapshot's files are always kept, and nothing
    outside the delivery directories under data_dir is ever removed.
    """
    if keep <= 0:
        return 0
    with psycopg.connect(settings.import_dsn) as conn:
        rows = conn.execute(
            """SELECT s.id, s.artifact_path,
                      row_number() OVER (PARTITION BY s.source_key, s.scope
                                         ORDER BY s.fetched_at DESC) <= %s
                      OR EXISTS (SELECT 1 FROM import_control.active_snapshot a
                                 WHERE a.snapshot_id = s.id)
               FROM source_raw.snapshot s
               WHERE (%s::text IS NULL OR s.source_key = %s)
                 AND NOT EXISTS (SELECT 1 FROM import_control.pruned_artifact p
                                 WHERE p.snapshot_id = s.id)""",
            (keep, only, only),
        ).fetchall()
        wanted = {_store_dir(artifact) for _, artifact, kept in rows if kept}
        pruned = 0
        for snapshot_id, artifact, kept in rows:
            directory = _store_dir(artifact)
            if kept or directory is None or directory in wanted:
                continue
            shutil.rmtree(directory, ignore_errors=True)
            conn.execute(
                "INSERT INTO import_control.pruned_artifact (snapshot_id, path) VALUES (%s, %s)",
                (snapshot_id, artifact),
            )
            pruned += 1
    return pruned


EVERY_DAYS = {"osm": 7}     # sources checked less often than nightly: the Overpass service is shared


def run(only: str | None = None) -> list[Check]:
    """Check every loaded source and scope, and import what has changed.

    A source that is checked less often than nightly is left alone until its
    time has come, unless it is asked for by name.
    """
    with psycopg.connect(settings.import_dsn) as conn:
        recent = {(k, s) for k, s in conn.execute(
            """SELECT source_key, scope FROM import_control.check_log
               WHERE outcome IN ('unchanged', 'imported')
                 AND started_at > now() - make_interval(hours => 24 * %s - 6)
                 AND source_key = %s""", (EVERY_DAYS["osm"], "osm"))} if only is None else set()
        active = conn.execute(
            """SELECT a.source_key, a.scope, s.manifest FROM import_control.active_snapshot a
               JOIN source_raw.snapshot s ON s.id = a.snapshot_id
               WHERE a.source_key = ANY(%s) AND (%s::text IS NULL OR a.source_key = %s)
               ORDER BY 1, 2""",
            (list(SOURCES), only, only),
        ).fetchall()
    checks, busy = [], {}
    for source_key, scope, manifest in active:
        if (source_key, scope) in recent:
            continue        # checked within the week
        started = datetime.now(UTC)
        try:
            if source_key in busy:
                # It did not answer for another scope tonight: not asked again until the next night.
                raise core.NotReached(f"not asked tonight: {busy[source_key]}")
            r = again(source_key, scope, manifest)
            check = Check(source_key, scope, "unchanged" if r.reused else "imported",
                          r.snapshot_id, "" if r.reused else json.dumps(r.diff))
        except core.NotReached as e:
            check = Check(source_key, scope, "unreached", None, str(e))
            busy.setdefault(source_key, f"it did not answer for {scope} ({e})")
        except core.ImportRejected as e:
            check = Check(source_key, scope, "rejected", None, str(e))
        except Exception as e:  # noqa: BLE001  one source failing must not stop the others
            check = Check(source_key, scope, "error", None, f"{type(e).__name__}: {e}")
        with psycopg.connect(settings.import_dsn) as conn:
            conn.execute(
                """INSERT INTO import_control.check_log
                       (source_key, scope, started_at, outcome, snapshot_id, detail)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (source_key, scope, started, check.outcome, check.snapshot_id,
                 check.detail[:2000]),
            )
        checks.append(check)
    return checks
