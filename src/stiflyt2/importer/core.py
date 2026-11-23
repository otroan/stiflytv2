"""Snapshot loader: staging tables -> immutable feature versions -> diff -> activate.

An adapter fetches a file, loads its layers into import_staging with ogr2ogr and
describes them with LayerSpec. Everything after that is the same for every
source and lives here.

Rules enforced here:
- The same file for the same scope is one snapshot (idempotent re-import).
- A feature version is stored once and listed in every snapshot containing it.
- Nothing is activated unless every layer loaded and validated.
- A failed import records the failure and changes nothing else.
- "missing" is only ever derived from a complete snapshot.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from stiflyt2.config import settings

STAGING = "import_staging"


class ImportRejected(Exception):
    """The delivery failed validation. Nothing was activated."""


class NotReached(ImportRejected):
    """The source did not answer, or was too busy to: nothing is known to be wrong with what it holds, and it is
    asked again the next night. Nothing was activated."""


@dataclass(frozen=True)
class LayerSpec:
    layer: str            # layer key stored on feature_version
    table: str            # staging table name (in import_staging)
    id_sql: str           # SQL expression over alias s giving the external id (text)
    props_sql: str        # SQL expression over alias s giving jsonb properties
    geom_sql: str | None = "s.geom"
    min_rows: int = 1
    distinct: bool = False  # collapse exact duplicate rows (sources without row ids)
    # Some sources deliver one object as several rows: same id, same attributes,
    # one geometry part each. Merge them into one feature.
    collect: bool = False


@dataclass
class Delivery:
    source_key: str
    scope: str
    checksum: str
    artifact_path: Path
    transform_version: str
    layers: list[LayerSpec]
    source_updated_at: object = None
    manifest: dict = field(default_factory=dict)
    prepare_sql: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Result:
    snapshot_id: str
    reused: bool
    counts: dict
    diff: dict


def ogr_load(
    src: Path | str,
    layer: str | None,
    table: str,
    *,
    spatial: bool = True,
    overwrite: bool = True,
    extra: list[str] | None = None,
) -> None:
    """Load one layer into import_staging.<table>; geometry is reprojected to EPSG:25833."""
    cmd = [
        "ogr2ogr", "-f", "PostgreSQL", f"PG:{settings.import_dsn}", str(src),
        "-nln", f"{STAGING}.{table}", "--config", "PG_USE_COPY", "YES",
    ]
    if overwrite:
        cmd += ["-overwrite", "-lco", "FID=ogc_fid", "-lco", "UNLOGGED=ON"]
        if spatial:
            cmd += ["-lco", "GEOMETRY_NAME=geom", "-lco", "SPATIAL_INDEX=NONE"]
    if spatial:
        cmd += ["-nlt", "GEOMETRY", "-t_srs", "EPSG:25833"]
    cmd += extra or []
    if layer:
        cmd.append(layer)
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode != 0:
        raise ImportRejected(f"ogr2ogr failed for {layer or src}: {p.stderr.strip()[:500]}")


def drop_staging(tables: list[str]) -> None:
    with psycopg.connect(settings.import_dsn, autocommit=True) as conn:
        for t in tables:
            conn.execute(f'DROP TABLE IF EXISTS {STAGING}."{t}"')


def register_source(conn, key, name, licence, attribution, export_policy, homepage=None) -> None:
    conn.execute(
        """INSERT INTO source_raw.source (key, name, licence, attribution, export_policy, homepage)
           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING""",
        (key, name, licence, attribution, export_policy, homepage),
    )


def find_snapshot(source_key: str, scope: str, checksum: str, transform_version: str) -> str | None:
    """An existing complete snapshot for this exact file, if any."""
    with psycopg.connect(settings.import_dsn) as conn:
        row = conn.execute(
            """SELECT id FROM source_raw.snapshot
               WHERE source_key = %s AND scope = %s AND checksum = %s
                 AND transform_version = %s AND state = 'complete'""",
            (source_key, scope, checksum, transform_version),
        ).fetchone()
        return str(row[0]) if row else None


def unchanged_upstream(source_key: str, scope: str, transform_version: str,
                       size: int | None, modified) -> str | None:
    """The active snapshot, if it was made from the file the source is serving now."""
    if size is None or modified is None:
        return None
    with psycopg.connect(settings.import_dsn) as conn:
        row = conn.execute(
            """SELECT s.id FROM import_control.active_snapshot a
               JOIN source_raw.snapshot s ON s.id = a.snapshot_id
               WHERE a.source_key = %s AND a.scope = %s AND s.transform_version = %s
                 AND s.source_updated_at = %s AND (s.manifest->>'bytes')::bigint = %s""",
            (source_key, scope, transform_version, modified, size),
        ).fetchone()
        return str(row[0]) if row else None


def _normalise(conn, d: Delivery) -> dict:
    conn.execute(
        """CREATE TEMP TABLE norm (
               layer text, external_id text, properties jsonb, geom geometry,
               content_hash bytea) ON COMMIT DROP"""
    )
    counts = {}
    for sql in d.prepare_sql:
        conn.execute(sql)
    for spec in d.layers:
        geom = spec.geom_sql or "NULL::geometry"
        rows = f"""SELECT ({spec.id_sql})::text AS id, jsonb_strip_nulls({spec.props_sql}) AS p,
                          {geom} AS g
                   FROM {STAGING}."{spec.table}" s"""
        if spec.collect:
            differing = conn.execute(
                f"""SELECT id FROM ({rows}) r GROUP BY id HAVING count(DISTINCT p) > 1 LIMIT 3"""
            ).fetchall()
            if differing:
                raise ImportRejected(
                    f"layer {spec.layer}: rows sharing an id differ in attributes, "
                    f"e.g. {[i for (i,) in differing]}")
            # Parts are ordered by their bytes so the merged geometry, and with it
            # the content hash, does not depend on row order in the delivery.
            conn.execute(
                f"""INSERT INTO norm (layer, external_id, properties, geom)
                    SELECT %s, r.id, (array_agg(r.p))[1],
                           ST_Collect(part.geom ORDER BY ST_AsEWKB(part.geom))
                    FROM ({rows}) r LEFT JOIN LATERAL ST_Dump(r.g) part ON true
                    GROUP BY r.id""",
                (spec.layer,),
            )
        else:
            conn.execute(
                f"""INSERT INTO norm (layer, external_id, properties, geom)
                    SELECT {"DISTINCT" if spec.distinct else ""} %s, id, p, g FROM ({rows}) r""",
                (spec.layer,),
            )
        n = conn.execute("SELECT count(*) FROM norm WHERE layer = %s", (spec.layer,)).fetchone()[0]
        counts[spec.layer] = n
        if n < spec.min_rows:
            raise ImportRejected(f"layer {spec.layer}: {n} rows, expected at least {spec.min_rows}")
    bad = conn.execute("SELECT layer, count(*) FROM norm WHERE external_id IS NULL GROUP BY 1").fetchall()
    if bad:
        raise ImportRejected(f"features without an id: {dict(bad)}")
    dup = conn.execute(
        """SELECT layer, count(*) FROM (
               SELECT layer, external_id FROM norm GROUP BY 1, 2 HAVING count(*) > 1) q
           GROUP BY 1"""
    ).fetchall()
    if dup:
        raise ImportRejected(f"duplicate ids within the delivery: {dict(dup)}")
    srid = conn.execute(
        "SELECT DISTINCT ST_SRID(geom) FROM norm WHERE geom IS NOT NULL"
    ).fetchall()
    if srid and srid != [(25833,)]:
        raise ImportRejected(f"unexpected SRID(s) after load: {srid}")
    conn.execute(
        """UPDATE norm SET content_hash = sha256(
               convert_to(properties::text, 'UTF8') || COALESCE(ST_AsEWKB(geom), ''::bytea))"""
    )
    conn.execute("CREATE INDEX ON norm (layer, external_id, content_hash)")
    conn.execute("ANALYZE norm")
    return counts


def _previous(conn, source_key: str, scope: str) -> str | None:
    row = conn.execute(
        "SELECT snapshot_id FROM import_control.active_snapshot WHERE source_key = %s AND scope = %s",
        (source_key, scope),
    ).fetchone()
    return str(row[0]) if row else None


def _shrink_check(conn, d: Delivery, prev: str, counts: dict, max_shrink: float) -> None:
    old = conn.execute("SELECT counts FROM source_raw.snapshot WHERE id = %s", (prev,)).fetchone()[0]
    for layer, n in counts.items():
        before = (old or {}).get(layer)
        if before and n < before * (1 - max_shrink):
            raise ImportRejected(
                f"layer {layer} shrank from {before} to {n} rows "
                f"(more than {max_shrink:.0%}); refusing to treat this as deletions"
            )


def _churn_check(conn, d: Delivery, prev: str, max_churn: float) -> None:
    """Refuse a delivery in which most features of a layer changed at once.

    Real edits touch a small share of a dataset. When most of it changes, the
    usual cause is a changed export (format, precision, attribute names), and
    storing it would duplicate the whole layer.
    """
    rows = conn.execute(
        """SELECT fv.layer, count(*),
                  count(*) FILTER (WHERE n.external_id IS NOT NULL
                                     AND n.content_hash <> fv.content_hash)
           FROM source_raw.snapshot_member m
           JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
           LEFT JOIN norm n ON n.layer = fv.layer AND n.external_id = fv.external_id
           WHERE m.snapshot_id = %s GROUP BY 1""",
        (prev,),
    ).fetchall()
    for layer, before, changed in rows:
        if before >= 10 and changed > before * max_churn:
            raise ImportRejected(
                f"layer {layer}: {changed} of {before} features changed "
                f"(more than {max_churn:.0%}); set STIFLYT2_MAX_CHURN=1 to accept this delivery")


def _diff(conn, source_key: str, scope: str, prev: str | None, snap: str) -> dict:
    known = conn.execute(
        """SELECT counts FROM import_control.diff
           WHERE from_snapshot IS NOT DISTINCT FROM %s AND to_snapshot = %s""",
        (prev, snap),
    ).fetchone()
    if known:
        return known[0]
    diff_id = conn.execute(
        """INSERT INTO import_control.diff (source_key, scope, from_snapshot, to_snapshot, counts)
           VALUES (%s, %s, %s, %s, '{}') RETURNING id""",
        (source_key, scope, prev, snap),
    ).fetchone()[0]
    if prev is None:
        created = conn.execute(
            "SELECT count(*) FROM source_raw.snapshot_member WHERE snapshot_id = %s", (snap,)
        ).fetchone()[0]
        counts = {"created": created, "updated": 0, "missing": 0, "initial": True}
    else:
        conn.execute(
            """INSERT INTO import_control.diff_item
                   (diff_id, layer, external_id, kind, old_version, new_version)
               WITH old AS (
                   SELECT fv.layer, fv.external_id, fv.id FROM source_raw.snapshot_member m
                   JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                   WHERE m.snapshot_id = %(prev)s),
               new AS (
                   SELECT fv.layer, fv.external_id, fv.id FROM source_raw.snapshot_member m
                   JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                   WHERE m.snapshot_id = %(snap)s)
               SELECT %(diff)s, COALESCE(n.layer, o.layer), COALESCE(n.external_id, o.external_id),
                      CASE WHEN o.id IS NULL THEN 'created'
                           WHEN n.id IS NULL THEN 'missing' ELSE 'updated' END,
                      o.id, n.id
               FROM old o FULL JOIN new n USING (layer, external_id)
               WHERE o.id IS DISTINCT FROM n.id""",
            {"prev": prev, "snap": snap, "diff": diff_id},
        )
        rows = conn.execute(
            "SELECT kind, count(*) FROM import_control.diff_item WHERE diff_id = %s GROUP BY 1",
            (diff_id,),
        ).fetchall()
        counts = {"created": 0, "updated": 0, "missing": 0, **dict(rows)}
    conn.execute("UPDATE import_control.diff SET counts = %s WHERE id = %s",
                 (json.dumps(counts), diff_id))
    return counts


def _activate(conn, source_key: str, scope: str, snap: str, prev: str | None) -> None:
    conn.execute(
        """INSERT INTO import_control.active_snapshot (source_key, scope, snapshot_id)
           VALUES (%s, %s, %s)
           ON CONFLICT (source_key, scope)
           DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, activated_at = now()""",
        (source_key, scope, snap),
    )
    conn.execute(
        """INSERT INTO import_control.activation_log
               (source_key, scope, snapshot_id, previous_id) VALUES (%s, %s, %s, %s)""",
        (source_key, scope, snap, prev),
    )


def reuse(source_key: str, scope: str, snapshot_id: str) -> Result:
    """The delivery is already stored as this snapshot: make sure it is the active one.

    Normally it is, and nothing happens. If the source serves an earlier
    delivery again, its snapshot is activated again, with a diff.
    """
    with psycopg.connect(settings.import_dsn) as conn:
        prev = _previous(conn, source_key, scope)
        if prev == snapshot_id:
            return Result(snapshot_id, True, {}, {})
        counts = conn.execute(
            "SELECT counts FROM source_raw.snapshot WHERE id = %s", (snapshot_id,)
        ).fetchone()[0]
        diff = _diff(conn, source_key, scope, prev, snapshot_id)
        _activate(conn, source_key, scope, snapshot_id, prev)
    return Result(snapshot_id, False, counts, diff)


def ingest(d: Delivery, *, max_shrink: float = 0.2) -> Result:
    """Turn loaded staging tables into an immutable, activated snapshot."""
    existing = find_snapshot(d.source_key, d.scope, d.checksum, d.transform_version)
    if existing:
        return reuse(d.source_key, d.scope, existing)

    with psycopg.connect(settings.import_dsn) as conn:
        snap = str(conn.execute(
            """INSERT INTO source_raw.snapshot
                   (source_key, scope, checksum, crs, artifact_path, transform_version,
                    source_updated_at, manifest)
               VALUES (%s, %s, %s, 25833, %s, %s, %s, %s) RETURNING id""",
            (d.source_key, d.scope, d.checksum, str(d.artifact_path), d.transform_version,
             d.source_updated_at, json.dumps(d.manifest)),
        ).fetchone()[0])

    try:
        with psycopg.connect(settings.import_dsn) as conn:
            counts = _normalise(conn, d)
            prev = _previous(conn, d.source_key, d.scope)
            if prev:
                _shrink_check(conn, d, prev, counts, max_shrink)
                _churn_check(conn, d, prev, settings.max_churn)
            conn.execute(
                """INSERT INTO source_raw.feature_version
                       (source_key, layer, external_id, content_hash, properties, geom, first_seen)
                   SELECT %s, layer, external_id, content_hash, properties, geom, %s FROM norm
                   ON CONFLICT (source_key, layer, external_id, content_hash) DO NOTHING""",
                (d.source_key, snap),
            )
            conn.execute(
                """INSERT INTO source_raw.snapshot_member (snapshot_id, feature_version_id)
                   SELECT %s, fv.id FROM norm n JOIN source_raw.feature_version fv
                     ON fv.source_key = %s AND fv.layer = n.layer
                    AND fv.external_id = n.external_id AND fv.content_hash = n.content_hash""",
                (snap, d.source_key),
            )
            diff = _diff(conn, d.source_key, d.scope, prev, snap)
            conn.execute(
                """UPDATE source_raw.snapshot
                   SET state = 'complete', complete = true, counts = %s WHERE id = %s""",
                (json.dumps(counts), snap),
            )
            _activate(conn, d.source_key, d.scope, snap, prev)
    except Exception as e:
        with psycopg.connect(settings.import_dsn) as conn:
            conn.execute(
                "UPDATE source_raw.snapshot SET state = 'failed', error = %s WHERE id = %s",
                (str(e)[:2000], snap),
            )
        raise
    return Result(snap, False, counts, diff)
