"""How high the ground is anywhere in Norway, from Kartverket's national height model, kept here at 50 m.

The height service (`height.py`) answers about points, fifty at a time: right
for the routes, which are asked about once per line and kept. A walk planned
across the country is thousands of kilometres asked about anew each time, and
is read here instead: the national height model (the laser-scanned terrain,
Nasjonal høydemodell DTM), fetched once from Kartverket's coverage service
at 50 m, in tiles of 100 by 100 km, and read from them, between the four
nearest heights. A 50 m grid smooths the ground somewhat: what a walk climbs
comes out a little low where the ground is broken.

76 tiles cover the land (the same as Kartverket's DTM 50); fetched with
`stiflyt2 terrain fetch`, some 750 MB, under CC BY 4.0. It is a source, not
something decided: nothing here has a history.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

import httpx
import numpy as np

from stiflyt2.config import settings

WCS = "https://wcs.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833"
COVERAGE = "nhm_dtm_topo_25833"
CREDIT = "Heights: Kartverket's national height model, CC BY 4.0"
CELL_M = 50
TILE_M = 100_000
SIDE = TILE_M // CELL_M
# The tiles with land in them, by the first two digits of their lower left corner north, then east (m1 is -1).
TILES = [
    "6400", "6401", "64m1", "6500", "6501", "6502", "6503", "65m1", "6600", "6601", "6602", "6603", "66m1",
    "6700", "6701", "6702", "6703", "67m1", "6800", "6801", "6802", "6803", "68m1", "6900", "6901", "6902",
    "6903", "69m1", "7000", "7001", "7002", "7003", "7004", "7101", "7102", "7103", "7104", "7202", "7203",
    "7204", "7303", "7304", "7305", "7403", "7404", "7405", "7503", "7504", "7505", "7506", "7507", "7604",
    "7605", "7606", "7607", "7608", "7609", "7705", "7706", "7707", "7708", "7709", "7710", "7711", "7806",
    "7807", "7808", "7809", "7810", "7811", "7906", "7907", "7908", "7909", "7910", "7911"
]
KEPT = 16               # tiles kept read at once, 16 MB each
TRIES = 3


class Unavailable(Exception):
    """The coverage service did not give a tile."""


def folder() -> Path:
    return settings.data_dir / "terrain" / "nhm50"


def corner(code: str) -> tuple[int, int]:
    """The lower left corner of a tile (east, north)."""
    east = -1 if code[2:] == "m1" else int(code[2:])
    return east * TILE_M, int(code[:2]) * TILE_M


def code_of(east: float, north: float) -> str:
    e, n = int(east // TILE_M), int(north // TILE_M)
    return f"{n:02d}{'m1' if e == -1 else f'{e:02d}'}"


def ready() -> bool:
    """Whether the tiles have been fetched (all of them)."""
    return all((folder() / f"{code}.tif").is_file() for code in TILES)


def fetch(again: bool = False, said=lambda _: None, client: httpx.Client | None = None) -> dict:
    """Fetch the tiles that are not here yet (all of them, `again`). Can be stopped and run again."""
    where = folder()
    where.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=httpx.Timeout(30.0, read=300.0))
    got = 0
    try:
        for i, code in enumerate(TILES, 1):
            dest = where / f"{code}.tif"
            if dest.is_file() and not again:
                continue
            x, y = corner(code)
            params = {"service": "WCS", "request": "GetCoverage", "version": "1.0.0", "coverage": COVERAGE,
                      "crs": "EPSG:25833", "bbox": f"{x},{y},{x + TILE_M},{y + TILE_M}", "width": SIDE, "height": SIDE,
                      "format": "GeoTIFF"}
            why = "no answer"
            for attempt in range(TRIES):
                try:
                    r = client.get(WCS, params=params)
                    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/tiff"):
                        break
                    why = f"answered {r.status_code}: {r.text[:200]}"
                except httpx.HTTPError as e:
                    why = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
            else:
                raise Unavailable(f"Kartverket's height coverage did not give tile {code}: {why}")
            raw = where / f"{code}.raw.tif"
            raw.write_bytes(r.content)
            # Kept compressed: some 10 MB a tile in place of 16.
            part = where / f"{code}.part.tif"
            subprocess.run(["gdal_translate", "-q", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3", "-co", "TILED=YES",
                            str(raw), str(part)], check=True, capture_output=True)
            raw.unlink()
            part.rename(dest)
            got += 1
            said(f"{i}/{len(TILES)} {code}")
    finally:
        if own:
            client.close()
    return {"fetched": got, "tiles": len(TILES), "bytes": sum(p.stat().st_size for p in where.glob("*.tif"))}


_kept: OrderedDict[str, np.ndarray | None] = OrderedDict()
_lock = threading.Lock()


def _tile(code: str) -> np.ndarray | None:
    from PIL import Image

    with _lock:
        if code in _kept:
            _kept.move_to_end(code)
            return _kept[code]
    path = folder() / f"{code}.tif"
    grid = np.asarray(Image.open(path), dtype=np.float32) if path.is_file() else None
    with _lock:
        _kept[code] = grid
        while len(_kept) > KEPT:
            _kept.popitem(last=False)
    return grid


def heights_of(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The height of the ground at each point (east and north, EPSG:25833, as arrays), between the four nearest
    in the grid; NaN where there is no tile (the sea, abroad). Tile by tile, however many points."""
    x, y = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    out = np.full(len(x), np.nan)
    if not len(x):
        return out
    e, n = np.floor(x / TILE_M).astype(np.int64), np.floor(y / TILE_M).astype(np.int64)
    key = n * 1000 + e
    order = np.argsort(key, kind="stable")
    cuts = np.flatnonzero(np.diff(key[order])) + 1
    for part in np.split(order, cuts):
        ee, nn = int(e[part[0]]), int(n[part[0]])
        if not -1 <= ee <= 99 or not 0 <= nn <= 99:
            continue
        code = f"{nn:02d}{'m1' if ee == -1 else f'{ee:02d}'}"
        grid = _tile(code) if code in TILES else None
        if grid is None:
            continue
        x0, y0 = corner(code)
        col = np.clip((x[part] - x0) / CELL_M - 0.5, 0, SIDE - 1)
        row = np.clip((y0 + TILE_M - y[part]) / CELL_M - 0.5, 0, SIDE - 1)
        c0, r0 = np.floor(col).astype(int), np.floor(row).astype(int)
        c1, r1 = np.minimum(c0 + 1, SIDE - 1), np.minimum(r0 + 1, SIDE - 1)
        fc, fr = col - c0, row - r0
        out[part] = (grid[r0, c0] * (1 - fc) * (1 - fr) + grid[r0, c1] * fc * (1 - fr)
                     + grid[r1, c0] * (1 - fc) * fr + grid[r1, c1] * fc * fr)
    return out


def heights(points) -> list[float | None]:
    """The height of the ground at each point (east, north in EPSG:25833); None where there is no tile."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    return [None if np.isnan(v) else float(v) for v in heights_of(pts[:, 0], pts[:, 1])]


def extent() -> tuple[int, int, int, int]:
    """The box the tiles cover: west, south, east, north."""
    corners = [corner(code) for code in TILES]
    return (min(x for x, _ in corners), min(y for _, y in corners),
            max(x for x, _ in corners) + TILE_M, max(y for _, y in corners) + TILE_M)
