"""Shared flow for sources delivered as a zipped file by the Geonorge download API.

A FileGDB in EPSG:25833 unless the source says otherwise (the road network is GML).
"""
from __future__ import annotations

from pathlib import Path

from stiflyt2.config import settings
from stiflyt2.importer import core, delivery, geonorge

PROJECTION = "25833"


def run(
    *,
    key: str,
    uuid: str,
    scope: str,
    transform: str,
    layers: list[tuple[str, str, bool]],
    specs: list[core.LayerSpec],
    area: geonorge.Area = geonorge.NATIONWIDE,
    prepare_sql: list[str] | None = None,
    derived: list[str] | None = None,
    load_options: list[str] | None = None,
    file: Path | None = None,
    fmt: str = "FGDB",
    projection: str = PROJECTION,
    pattern: str = "*.gdb",
) -> core.Result:
    """Fetch the current delivery, or take a local copy of one, and make a snapshot of it.

    layers: (layer in the file, staging table, has geometry).
    derived: staging tables that prepare_sql creates.
    load_options: extra ogr2ogr arguments for every layer.

    The source is asked for the file's size and date first; when the active
    snapshot was made from that same file, nothing is downloaded.
    """
    if file is None:
        name, url = geonorge.order(uuid, area, projection, fmt)
        same = core.unchanged_upstream(key, scope, transform, *geonorge.probe(url))
        if same:
            return core.Result(same, True, {}, {})
        fetched = geonorge.download(url, settings.data_dir / key / "incoming", name)
        kept = delivery.keep(key, fetched, move=True)
    else:
        kept = delivery.keep(key, delivery.local(file))
    existing = core.find_snapshot(key, scope, kept.sha256, transform)
    if existing:
        return core.reuse(key, scope, existing)

    try:
        gdbs = list(kept.extract().glob(pattern))
        if len(gdbs) != 1:
            raise core.ImportRejected(
                f"expected one {pattern} in {kept.artifact.name}, found {len(gdbs)}")
        for layer, table, spatial in layers:
            core.ogr_load(gdbs[0], layer, table, spatial=spatial, extra=load_options)
        return core.ingest(core.Delivery(
            source_key=key, scope=scope, checksum=kept.sha256, artifact_path=kept.artifact,
            transform_version=transform, layers=specs, prepare_sql=prepare_sql or [],
            source_updated_at=kept.source_updated_at,
            manifest=kept.manifest(format=fmt, source_crs=int(projection)),
        ))
    finally:
        core.drop_staging([table for _, table, _ in layers] + (derived or []))
        kept.discard_extracted()
