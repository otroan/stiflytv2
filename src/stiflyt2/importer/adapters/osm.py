"""OpenStreetMap ways with a highway tag (paths, tracks, roads), per named bounding box.

OpenStreetMap is under the Open Database License (ODbL 1.0). It may be shown
and compared with freely, with attribution "© OpenStreetMap contributors". A
database that takes geometry over from it becomes a derived database under the
same licence, which Turrutebasen (CC BY 4.0) cannot take in. The source is
therefore registered with export policy `unresolved`, and OSM is not among the
lines that snapping follows. That is a decision for the project owner.

Fetched from the Overpass API, in tiles, one request at a time and with a
pause between them. Scope is a named bounding box in EPSG:25833, as for the
mapped paths. A way that crosses a tile border comes with every tile it
touches, whole each time; the copies are identical and are stored once.

A way is identified by its OSM id. No editor names, changeset or version
numbers are fetched: who mapped a way is not ours to keep. A way that has
been edited shows up as updated because its tags or its line differ.

The response states when the OSM data it was taken from were current. That is
left out of the checksum, so unchanged data give no new snapshot.
"""
from __future__ import annotations

import hashlib
import json
import time

import httpx
import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core

KEY = "osm"
URL = "https://overpass-api.de/api/interpreter"
TRANSFORM = "osm-1"
TILE_M = 25_000
PAUSE_S = 2.0          # between requests: the service is shared and free
# Seconds to wait before asking again when it is busy, each wait twice the one before, as Overpass asks: about
# 16 minutes in all, then it is left for the night (`core.NotReached`) and asked again the next.
RETRIES = (30, 60, 120, 240, 480)
QUERY = '[out:json][timeout:300];way["highway"]({s},{w},{n},{e});out tags geom qt;'

SPECS = [core.LayerSpec("way", "osm_way", "s.osm_id::text", "s.tags::jsonb", distinct=True)]

Bbox = tuple[int, int, int, int]


def tiles(bbox: Bbox, size: int = TILE_M) -> list[Bbox]:
    x0, y0, x1, y1 = bbox
    return [(x, y, min(x + size, x1), min(y + size, y1))
            for x in range(x0, x1, size) for y in range(y0, y1, size)]


def _latlon(conn, tile: Bbox) -> dict:
    """The tile's extent in degrees, as Overpass wants it: south, west, north, east."""
    s, w, n, e = conn.execute(
        """SELECT ST_YMin(g), ST_XMin(g), ST_YMax(g), ST_XMax(g)
           FROM (SELECT ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 25833), 4326) AS g) x""",
        tile).fetchone()
    return {"s": round(s, 6), "w": round(w, 6), "n": round(n, 6), "e": round(e, 6)}


def _fetch(client: httpx.Client, query: str) -> bytes:
    for wait in (*RETRIES, None):
        try:
            r = client.post(URL, data={"data": query})
        except httpx.TransportError as e:
            if wait is not None:
                time.sleep(wait)
                continue
            raise core.NotReached(f"Overpass did not answer ({type(e).__name__}) after {len(RETRIES) + 1} tries") from e
        if r.status_code in (429, 502, 503, 504) and wait is not None:
            time.sleep(wait)        # busy, down for a moment or out of time: ask again later
            continue
        if r.status_code in (429, 502, 503, 504):
            # Not a fault of ours or of the data: say so and leave what is loaded as it is.
            busy = ", at capacity" if b"at capacity" in r.content else ""
            raise core.NotReached(f"Overpass answers {r.status_code}{busy} after {len(RETRIES) + 1} tries over "
                                  f"{sum(RETRIES) // 60} minutes")
        if r.status_code != 200:
            raise core.ImportRejected(f"Overpass answers {r.status_code}")
        return r.content
    raise AssertionError("unreachable")


def ways(body: bytes) -> list[dict]:
    """The ways of a response that have a line: id, tags, and points as (lon, lat)."""
    found = json.loads(body)
    if "elements" not in found:
        raise core.ImportRejected(f"not an Overpass answer: {body[:200]!r}")
    if any("runtime error" in r for r in [found.get("remark", "")]):
        raise core.ImportRejected(f"Overpass gave up: {found['remark'][:200]}")
    out = []
    for el in found["elements"]:
        line = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
        if el.get("type") == "way" and len(line) >= 2:
            out.append({"id": el["id"], "tags": el.get("tags") or {}, "line": line})
    return sorted(out, key=lambda w: w["id"])


def checksum(per_tile: list[list[dict]]) -> str:
    """Checksum of a delivery: the ways of each tile, whatever the time the answer was made."""
    h = hashlib.sha256()
    for found in per_tile:
        h.update(hashlib.sha256(json.dumps(found, sort_keys=True, ensure_ascii=False).encode()).digest())
    return h.hexdigest()


def run(name: str, bbox: Bbox) -> core.Result:
    """Fetch every way with a highway tag in bbox as scope `name`."""
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "OpenStreetMap: ways with a highway tag", "ODbL 1.0",
            "© OpenStreetMap contributors", "unresolved", "https://www.openstreetmap.org/copyright",
        )
        boxes = [(t, _latlon(conn, t)) for t in tiles(bbox)]
    work = settings.data_dir / KEY / "incoming" / name
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*.json"):
        old.unlink()
    files, per_tile, counts = [], [], {}
    with httpx.Client(headers={"User-Agent": settings.user_agent},
                      timeout=httpx.Timeout(60.0, read=600.0)) as client:
        for i, (tile, box) in enumerate(boxes):
            if i:
                time.sleep(PAUSE_S)
            body = _fetch(client, QUERY.format(**box))
            found = ways(body)
            f = work / ("_".join(map(str, tile)) + ".json")
            f.write_bytes(body)
            files.append(f)
            per_tile.append(found)
            counts[",".join(map(str, tile))] = len(found)
    if not any(per_tile):
        raise core.ImportRejected(f"no ways found in {bbox}")

    digest = checksum(per_tile)
    scope = f"bbox:{name}"
    existing = core.find_snapshot(KEY, scope, digest, TRANSFORM)
    if existing:
        return core.reuse(KEY, scope, existing)

    keep = settings.data_dir / KEY / digest[:16]
    keep.mkdir(parents=True, exist_ok=True)
    for f in files:
        (keep / f.name).write_bytes(f.read_bytes())
    try:
        first = True
        for f, found in zip(files, per_tile, strict=True):
            if not found:
                continue
            # The answers are kept as received. For loading, each is rewritten as plain
            # features: the id, and every tag as one value, so that no tag becomes a column.
            features = keep / (f.stem + ".geojson")
            features.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "properties": {"osm_id": w["id"], "tags": json.dumps(w["tags"], sort_keys=True,
                                                                      ensure_ascii=False)},
                 "geometry": {"type": "LineString", "coordinates": w["line"]}} for w in found]},
                ensure_ascii=False))
            core.ogr_load(features, None, "osm_way", extra=[] if first else ["-append"],
                          overwrite=first)
            features.unlink()
            first = False
        return core.ingest(core.Delivery(
            source_key=KEY, scope=scope, checksum=digest, artifact_path=keep,
            transform_version=TRANSFORM, layers=SPECS,
            manifest={"url": URL, "query": QUERY, "bbox": list(bbox), "tile_m": TILE_M,
                      "tiles": counts, "files": [f.name for f in files], "format": "Overpass JSON",
                      "source_crs": 4326},
        ))
    finally:
        core.drop_staging(["osm_way"])
