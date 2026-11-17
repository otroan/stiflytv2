"""Client for the Geonorge download API (nedlasting.geonorge.no/api).

Verified 2026-11-14: anonymous orders work for open datasets; the order
response lists files whose downloadUrl redirects to a static file carrying
Last-Modified and ETag.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from stiflyt2.config import settings

API = "https://nedlasting.geonorge.no/api"


@dataclass(frozen=True)
class Area:
    code: str
    name: str
    type: str


NATIONWIDE = Area("0000", "Hele landet", "landsdekkende")


@dataclass(frozen=True)
class Fetched:
    path: Path
    sha256: str
    size: int
    url: str
    source_updated_at: datetime | None


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, read=600.0),
    )


def order(uuid: str, area: Area, projection: str, fmt: str) -> tuple[str, str]:
    """Place an order and return (file name, download URL)."""
    body = {
        "email": "",
        "orderLines": [
            {
                "metadataUuid": uuid,
                "areas": [{"code": area.code, "name": area.name, "type": area.type}],
                "projections": [{"code": projection}],
                "formats": [{"name": fmt}],
            }
        ],
    }
    with _client() as c:
        r = c.post(f"{API}/order", json=body)
        r.raise_for_status()
        files = r.json().get("files", [])
    if len(files) != 1:
        raise RuntimeError(f"expected one file for {uuid} {area.code} {fmt}, got {len(files)}")
    f = files[0]
    if f.get("status") != "ReadyForDownload":
        raise RuntimeError(f"{f.get('name')} is not ready: {f.get('status')}")
    return f["name"], f["downloadUrl"]


def area(uuid: str, code: str) -> Area:
    """Look up one of the areas a dataset is offered for, e.g. a municipality number."""
    with _client() as c:
        r = c.get(f"{API}/codelists/area/{uuid}")
        r.raise_for_status()
    for a in r.json():
        if a.get("code") == code:
            return Area(a["code"], a["name"], a["type"])
    raise ValueError(f"dataset {uuid} is not offered for area {code}")


def probe(url: str) -> tuple[int | None, datetime | None]:
    """Size and modification time of the file behind a download URL, without fetching it."""
    with _client() as c, c.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
        r.raise_for_status()
        total = r.headers.get("content-range", "").rpartition("/")[2]
        if r.status_code != 206:
            total = r.headers.get("content-length", "")
        modified = r.headers.get("last-modified")
    return (int(total) if total.isdigit() else None,
            parsedate_to_datetime(modified) if modified else None)


def download(url: str, dest_dir: Path, name: str) -> Fetched:
    """Stream a file to dest_dir, verifying length. Returns its checksum.

    The file is written to a temporary name and renamed only when complete, so
    a truncated download can never be mistaken for a delivery.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / (name + ".part")
    h = hashlib.sha256()
    size = 0
    with _client() as c, c.stream("GET", url) as r:
        r.raise_for_status()
        expected = r.headers.get("content-length")
        modified = r.headers.get("last-modified")
        with tmp.open("wb") as out:
            for chunk in r.iter_bytes(1 << 20):
                out.write(chunk)
                h.update(chunk)
                size += len(chunk)
        final_url = str(r.url)
    if expected is not None and int(expected) != size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"truncated download of {name}: {size} of {expected} bytes")
    path = dest_dir / name
    tmp.replace(path)
    return Fetched(
        path=path,
        sha256=h.hexdigest(),
        size=size,
        url=final_url,
        source_updated_at=parsedate_to_datetime(modified) if modified else None,
    )
