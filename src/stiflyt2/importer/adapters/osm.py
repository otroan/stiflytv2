"""OpenStreetMap ways with a highway tag (paths, tracks, roads), per named bounding box.

OpenStreetMap is under the Open Database License (ODbL 1.0). It may be shown
and compared with freely, with attribution "© OpenStreetMap contributors". A
database that takes geometry over from it becomes a derived database under the
same licence, which Turrutebasen (CC BY 4.0) cannot take in. The source is
therefore registered with export policy `unresolved`, and OSM is not among the
lines that snapping follows. That is a decision for the project owner.

Taken from Geofabrik's extract of Norway: one file (PBF, 1.4 GB) made anew from
OpenStreetMap every night, which is fetched when a box is due and cut per box
here with GDAL. Until 2026-11-23 the ways were asked of Overpass, tile by tile;
that service is meant for people at a browser, and was too often at capacity.
The file is kept while it is the one Geofabrik serves (known by the checksum
beside it), so that every box of a night is cut from one download, and the one
before is removed when a newer one comes.

Scope is a named bounding box in EPSG:25833, as for the mapped paths: a way that
crosses it comes whole. Coordinates are kept to 7 decimals, as OpenStreetMap
stores them. Every tag of a way is kept. A way tagged area=yes is an area (a
square, a parking place), not a line, and is not taken.

A way is identified by its OSM id. No editor names, changeset or version
numbers are read: who mapped a way is not ours to keep. A way that has been
edited shows up as updated because its tags or its line differ.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core

KEY = "osm"
URL = "https://download.geofabrik.de/europe/norway/norway-latest.osm.pbf"
TRANSFORM = "osm-2"
CONFIG = Path(__file__).with_name("osmconf.ini")

SPECS = [core.LayerSpec("way", "osm_way", "s.osm_id::text", "s.tags::jsonb", distinct=True)]

Bbox = tuple[int, int, int, int]


def _folder() -> Path:
    return settings.data_dir / KEY / "geofabrik"


def download(client: httpx.Client) -> tuple[Path, str]:
    """The extract Geofabrik serves now: the one kept, if it is that one, else fetched anew.

    Returns the file and its checksum; the file's time is when Geofabrik made it. A download that does
    not match the checksum it was served with (the file was made anew meanwhile) is fetched once more.
    """
    name = URL.rsplit("/", 1)[-1]
    try:
        said = client.get(f"{URL}.md5")
        said.raise_for_status()
        md5 = said.text.split()[0].lower()
        folder = _folder()
        kept = folder / f"{md5}-{name}"
        if kept.is_file():
            return kept, md5
        folder.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            part, digest = folder / f"{name}.part", hashlib.md5(usedforsecurity=False)    # Geofabrik's own checksum
            with client.stream("GET", URL) as r, part.open("wb") as out:
                r.raise_for_status()
                made = r.headers.get("last-modified")
                for chunk in r.iter_bytes(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() == md5:
                break
            md5 = client.get(f"{URL}.md5").text.split()[0].lower()      # made anew while it was fetched
        else:
            part.unlink(missing_ok=True)
            raise core.NotReached("Geofabrik's extract did not match its checksum twice over")
    except httpx.HTTPError as e:
        raise core.NotReached(f"Geofabrik did not answer ({type(e).__name__}: {str(e)[:120]})") from e
    for old in folder.glob(f"*-{name}"):
        old.unlink()                     # the one before: every box is cut from the newest
    kept = folder / f"{md5}-{name}"
    part.rename(kept)
    if made:
        at = parsedate_to_datetime(made).timestamp()
        os.utime(kept, (at, at))
    return kept, md5


def cut(extract: Path, bbox: Bbox, dest: Path) -> list[dict]:
    """The ways with a highway tag that cross a box (EPSG:25833): id, every tag, and points as (lon, lat)."""
    cmd = ["ogr2ogr", "-f", "GeoJSONSeq", str(dest), str(extract), "lines",
           "-where", "highway IS NOT NULL", "-select", "osm_id,all_tags",
           "-spat", *map(str, bbox), "-spat_srs", "EPSG:25833",
           "-lco", "COORDINATE_PRECISION=7", "--config", "OSM_CONFIG_FILE", str(CONFIG),
           "--config", "CPL_TMPDIR", str(dest.parent)]
    p = subprocess.run(cmd, capture_output=True, text=True, check=False,
                       env={**os.environ, "OSM_MAX_TMPFILE_SIZE": "1024"})
    if p.returncode != 0:
        raise core.ImportRejected(f"the extract could not be read: {p.stderr.strip()[-300:]}")
    out = []
    with dest.open() as f:
        for row in f:
            feature = json.loads(row.strip().lstrip("\x1e"))
            line = [(round(x, 7), round(y, 7)) for x, y, *_ in (feature["geometry"] or {}).get("coordinates") or []]
            if len(line) >= 2:
                props = feature["properties"]
                tags = props.get("all_tags") or {}      # an object, as GDAL writes a JSON field; text in older GDAL
                out.append({"id": int(props["osm_id"]), "tags": json.loads(tags) if isinstance(tags, str) else tags,
                            "line": line})
    return sorted(out, key=lambda w: w["id"])


def checksum(found: list[dict]) -> str:
    """Checksum of a box's ways, whenever and from whichever file they were cut."""
    return hashlib.sha256(json.dumps(found, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def run(name: str, bbox: Bbox) -> core.Result:
    """Every way with a highway tag in bbox, as scope `name`."""
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "OpenStreetMap: ways with a highway tag", "ODbL 1.0",
            "© OpenStreetMap contributors", "unresolved", "https://www.openstreetmap.org/copyright",
        )
    with httpx.Client(headers={"User-Agent": settings.user_agent}, follow_redirects=True,
                      timeout=httpx.Timeout(60.0, read=300.0)) as client:
        extract, md5 = download(client)
    made = datetime.fromtimestamp(extract.stat().st_mtime, UTC)
    work = settings.data_dir / KEY / "incoming" / name
    work.mkdir(parents=True, exist_ok=True)
    for old in work.iterdir():
        old.unlink()
    found = cut(extract, bbox, work / "cut.geojsons")
    if not found:
        raise core.ImportRejected(f"no ways found in {bbox}")

    digest = checksum(found)
    scope = f"bbox:{name}"
    existing = core.find_snapshot(KEY, scope, digest, TRANSFORM)
    if existing:
        return core.reuse(KEY, scope, existing)

    keep = settings.data_dir / KEY / digest[:16]
    keep.mkdir(parents=True, exist_ok=True)
    # What is loaded is what is kept: the ways as plain features, the id and every tag as one value,
    # so that no tag becomes a column.
    features = keep / "ways.geojson.gz"
    with gzip.open(features, "wt", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "properties": {"osm_id": w["id"], "tags": json.dumps(w["tags"], sort_keys=True, ensure_ascii=False)},
             "geometry": {"type": "LineString", "coordinates": w["line"]}} for w in found]}, f, ensure_ascii=False)
    try:
        core.ogr_load(f"/vsigzip/{features}", None, "osm_way")
        return core.ingest(core.Delivery(
            source_key=KEY, scope=scope, checksum=digest, artifact_path=keep,
            transform_version=TRANSFORM, layers=SPECS, source_updated_at=made,
            manifest={"url": URL, "md5": md5, "bbox": list(bbox), "ways": len(found),
                      "files": [features.name], "format": "Geofabrik OSM PBF, cut with GDAL", "source_crs": 4326},
        ))
    finally:
        core.drop_staging(["osm_way"])
