"""Paths and tractor roads from Kartverket's open WFS (derived from FKB-TraktorvegSti).

This is the open stand-in for the licensed FKB-TraktorvegSti product, under
CC BY 4.0 (confirmed by the project owner 2026-11-14; the service's own
description still says "Fees: Norge Digitalt").

Verified 2026-11-14: layer ms:traktorveg_sti returns geometry and typeveg only.
There is no persistent id, capture date or accuracy, so a feature is identified
by its geometry: a moved path shows up as one missing and one created, never
updated.

Scope is a named bounding box in EPSG:25833. It is fetched in tiles. The server
sends at most 10,000 features per response; a tile holding more is fetched as
four smaller tiles. If a response does not hold the number of features it
states, the whole delivery is refused.

Since 2026-11-20 the server may also send the first 9,999 features of a
dense tile and say that is all there is (numberMatched="9999"). A tile sent
that full is therefore always fetched as four smaller ones, whatever the
response says. Trusting it lost 6,900 paths round Geilo, Hol and Ål in
Skarvheimen for two nights, and paths in Rondane from the first import.

The layer schema is fetched and kept with the tiles, and loading uses only that
copy. Left to itself GDAL fetches the schema from the server while loading and,
if that fails, guesses column types from the data, giving different columns.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core

KEY = "fkb_wfs"
URL = "https://wms.geonorge.no/skwms1/wms.traktorveg_skogsbilveger"
LAYER = "ms:traktorveg_sti"
TRANSFORM = "fkb-wfs-1"
TILE_M = 25_000
MIN_TILE_M = 500
MOST = 9_999        # the most features the server sends in one response
SCHEMA_FILE = "schema.xsd"
HEAD = 4000  # the root element with its counts fits well inside this

# The root element states when the response was generated. Left in, unchanged
# data would get a new checksum, and a new snapshot, on every fetch.
_TIMESTAMP = re.compile(rb'\s+timeStamp="[^"]*"')

SPECS = [
    core.LayerSpec(
        "veglenke", "fkb_wfs",
        "md5(ST_AsEWKB(s.geom)::text || coalesce(s.typeveg, ''))",
        "jsonb_build_object('typeveg', s.typeveg, 'objtype', s.objtype, "
        "'vegkategori', nullif(s.vegkategori, ''), "
        "'klasselandbruksveg', nullif(s.klasselandbruksveg, ''), "
        "'kommunenummer', s.kommunenummer)",
        min_rows=1, distinct=True,
    )
]

Bbox = tuple[int, int, int, int]


def tiles(bbox: Bbox, size: int = TILE_M) -> list[Bbox]:
    x0, y0, x1, y1 = bbox
    return [
        (x, y, min(x + size, x1), min(y + size, y1))
        for x in range(x0, x1, size)
        for y in range(y0, y1, size)
    ]


def content_sha256(body: bytes) -> bytes:
    """Digest of a tile response, ignoring the generation time in its root element."""
    return hashlib.sha256(_TIMESTAMP.sub(b"", body[:HEAD], count=1) + body[HEAD:]).digest()


def _fetch_schema(client: httpx.Client, dest: Path) -> None:
    r = client.get(URL, params={
        "service": "WFS", "version": "2.0.0", "request": "DescribeFeatureType",
        "typeName": LAYER, "outputFormat": "application/gml+xml; version=3.2",
    })
    r.raise_for_status()
    if b'name="traktorveg_sti"' not in r.content:
        raise core.ImportRejected(f"no schema for {LAYER}: {r.content[:300]!r}")
    dest.write_bytes(r.content)


def checksum(schema: Path, files: list[Path]) -> str:
    """Checksum of a delivery: its schema and its tile responses, in order."""
    h = hashlib.sha256(hashlib.sha256(schema.read_bytes()).digest())
    for f in files:
        h.update(content_sha256(f.read_bytes()))
    return h.hexdigest()


def _quarters(bbox: Bbox) -> list[Bbox]:
    x0, y0, x1, y1 = bbox
    xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
    return [(x0, y0, xm, ym), (x0, ym, xm, y1), (xm, y0, x1, ym), (xm, ym, x1, y1)]


def _fetch_tile(client: httpx.Client, bbox: Bbox, dest: Path) -> int | None:
    """Save one tile's response and return its feature count.

    Returns None, saving nothing, when the server holds more features for the
    tile than it sends in one response.
    """
    r = client.get(URL, params={
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": LAYER,
        "srsName": "EPSG:25833",
        "bbox": ",".join(map(str, bbox)) + ",urn:ogc:def:crs:EPSG::25833",
    })
    r.raise_for_status()
    body = r.content
    head = body[:HEAD].decode("utf-8", "replace")
    if "ExceptionReport" in head:
        raise core.ImportRejected(f"WFS error for tile {bbox}: {head[:300]}")
    root = head[:head.find(">", head.find("<wfs:FeatureCollection")) + 1]
    matched = re.search(r'numberMatched="(\d+|unknown)"', root)
    returned = re.search(r'numberReturned="(\d+)"', root)
    if not matched or not returned:
        raise core.ImportRejected(f"tile {bbox}: response does not state feature counts")
    # Verified 2026-11-15: the server states the number for a tile it sends whole. It says
    # "unknown" for a tile holding nothing, and for a tile it sends only the first 10,000 of,
    # sometimes with a link to the next page and sometimes without.
    if matched.group(1) == "unknown":
        if int(returned.group(1)) or ' next="' in root:
            return None
    elif int(returned.group(1)) < int(matched.group(1)):
        return None
    # Seen from 2026-11-20 (and on one tile in Rondane from the start): a tile cut at 9,999 said to hold 9,999.
    if int(returned.group(1)) >= MOST:
        return None
    actual = body.count(b"<ms:traktorveg_sti ")
    if actual != int(returned.group(1)):
        raise core.ImportRejected(
            f"tile {bbox}: body holds {actual} features, header says {returned.group(1)}")
    dest.write_bytes(body)
    return actual


def run(name: str, bbox: Bbox) -> core.Result:
    """Fetch every path and tractor road intersecting bbox as scope `name`."""
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "Traktorveg og sti (åpen WFS, fra FKB)", "CC BY 4.0", "© Kartverket",
            "allowed",
            "https://kartkatalog.geonorge.no/metadata/f38e7e46-bbe1-49ea-b221-e034c81fce1d",
        )
    work = settings.data_dir / KEY / "incoming" / name
    work.mkdir(parents=True, exist_ok=True)
    for old in work.glob("*.gml"):
        old.unlink()
    schema = work / SCHEMA_FILE
    files, per_tile = [], {}
    with httpx.Client(headers={"User-Agent": settings.user_agent},
                      timeout=httpx.Timeout(60.0, read=600.0)) as client:
        _fetch_schema(client, schema)
        todo = tiles(bbox)
        while todo:
            t = todo.pop(0)
            f = work / ("_".join(map(str, t)) + ".gml")
            n = _fetch_tile(client, t, f)
            if n is None:
                # A dense tile: fetch it as four smaller ones instead.
                if min(t[2] - t[0], t[3] - t[1]) < 2 * MIN_TILE_M:
                    raise core.ImportRejected(
                        f"tile {t}: more features than the server sends in one response, "
                        f"even at {MIN_TILE_M} m")
                todo[:0] = _quarters(t)
                continue
            per_tile[",".join(map(str, t))] = n
            files.append(f)

    # Unchanged data gives the same checksum, so re-running is a no-op. The
    # files kept on disk are the responses exactly as received.
    digest = checksum(schema, files)
    scope = f"bbox:{name}"
    existing = core.find_snapshot(KEY, scope, digest, TRANSFORM)
    if existing:
        return core.reuse(KEY, scope, existing)

    keep = settings.data_dir / KEY / digest[:16]
    keep.mkdir(parents=True, exist_ok=True)
    for f in [schema, *files]:
        (keep / f.name).write_bytes(f.read_bytes())
    pinned = ["-oo", f"XSD={keep / SCHEMA_FILE}", "-oo", "DOWNLOAD_SCHEMA=NO"]
    try:
        first = True
        for f in files:
            if per_tile and f.stat().st_size and b"<ms:traktorveg_sti " in f.read_bytes():
                core.ogr_load(keep / f.name, "traktorveg_sti", "fkb_wfs",
                              extra=pinned + ([] if first else ["-append"]), overwrite=first)
                first = False
        if first:
            raise core.ImportRejected(f"no features found in {bbox}")
        return core.ingest(core.Delivery(
            source_key=KEY, scope=scope, checksum=digest, artifact_path=keep,
            transform_version=TRANSFORM, layers=SPECS,
            manifest={"url": URL, "layer": LAYER, "bbox": list(bbox), "tile_m": TILE_M,
                      "tiles": per_tile, "files": [f.name for f in files], "format": "GML",
                      "schema": SCHEMA_FILE, "source_crs": 25833},
        ))
    finally:
        core.drop_staging(["fkb_wfs"])
