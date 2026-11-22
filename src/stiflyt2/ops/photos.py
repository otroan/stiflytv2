"""Photos from the routes: the feature `photos` (step 3 of the plan; import migration 0019).

A photo is kept once, by the SHA-256 digest of its file: the same photo
added twice is the one already there. Its file is kept as it came, on the
server's disk (`settings.photos_dir`), with a copy 1600 px long to show and a
square thumbnail, both turned upright and without the camera's data (who
took it, with what, where). Only signed-in people with the feature see them.

Where it was taken is the camera's position when the file has one, else
where a person places it; one with neither waits to be placed. A photo keeps
its own position, as the camera had it: it is not hung on the stretches, and
a route's photos are those taken near it. It may show an issue (`entry_id`).

Every change is an event and can be taken back (`ops/undo.py`); a photo taken
away keeps its files, so that Undo can bring it back.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import uuid
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2 import features
from stiflyt2.config import settings
from stiflyt2.domain.places import Invalid, NotFound

pillow_heif.register_heif_opener()

features.feature("photos", "see photos from the routes, and add them where one may change routes or signs",
                 "ask to be given the feature `photos`")

MOST_BYTES = 40_000_000
SHOWN_PX = 1600
THUMB_PX = 240
NEAR_ROUTE_M = 75           # a route's photos are those taken this near its line
AREA_M = 2000               # a photo is of the area of the route nearest it, this near
SIZES = ("thumb", "1600", "original")
# MPO is a JPEG with more pictures in it (depth, HDR), as phones write them: it is shown as the JPEG it begins with.
_EXT = {"JPEG": "jpg", "MPO": "jpg", "PNG": "png", "HEIF": "heic", "WEBP": "webp"}
_TYPE = {"jpg": "image/jpeg", "png": "image/png", "heic": "image/heic", "webp": "image/webp"}
_POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def _event(conn, author, photo: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, 'photo', %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), photo, type_, Jsonb(payload)))


def path(sha: str, size: str, ext: str = "jpg") -> Path:
    """Where a photo's file is kept: as it came, or the copy to show, or the thumbnail."""
    return settings.photos_dir / size / sha[:2] / f"{sha}.{ext if size == 'original' else 'jpg'}"


def _keep(target: Path, data: bytes) -> None:
    """Write a file once, whole: to a name of its own first, then into place."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
    part.write_bytes(data)
    os.replace(part, target)


def _degrees(value, ref) -> float | None:
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError):
        return None
    sign = -1 if str(ref or "").upper() in ("S", "W") else 1
    return sign * (d + m / 60 + s / 3600)


def _camera(img: Image.Image) -> dict:
    """What the camera wrote into the file: where, when, which way it looked."""
    exif = img.getexif()
    gps, more = exif.get_ifd(0x8825), exif.get_ifd(0x8769)
    lat, lon = _degrees(gps.get(2), gps.get(1)), _degrees(gps.get(4), gps.get(3))
    if lat is not None and lon is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon)):
        lat = lon = None
    taken = None
    for said in (more.get(36867), exif.get(306)):
        try:
            taken = dt.datetime.strptime(str(said).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
            break
        except (TypeError, ValueError):
            continue
    facing = gps.get(17)
    try:
        facing = float(facing) % 360 if facing is not None else None
    except (TypeError, ValueError):
        facing = None
    return {"lat": lat, "lon": lon, "taken_at": taken, "facing": facing}


def _copies(img: Image.Image) -> tuple[bytes, bytes, int, int]:
    """The copy to show and the thumbnail: upright, as JPEG, with none of the camera's data."""
    upright = ImageOps.exif_transpose(img).convert("RGB")
    shown = upright.copy()
    shown.thumbnail((SHOWN_PX, SHOWN_PX))
    thumb = ImageOps.fit(upright, (THUMB_PX, THUMB_PX))
    out = []
    for each, quality in ((shown, 82), (thumb, 78)):
        buf = io.BytesIO()
        each.save(buf, "JPEG", quality=quality, optimize=True)
        out.append(buf.getvalue())
    return out[0], out[1], upright.width, upright.height


def _area_at(conn, lon: float, lat: float) -> str | None:
    """The area of the route in use nearest a point, when one is near."""
    found = conn.execute(f"""
        SELECT lower(left(r.code, 3)) FROM core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id,
             (SELECT {_POINT} AS p) q
        WHERE ST_DWithin(v.geom, q.p, %(near)s) ORDER BY ST_Distance(v.geom, q.p) LIMIT 1""",
        {"lon": lon, "lat": lat, "near": AREA_M}).fetchone()
    return found[0] if found else None


def _uuid(some) -> bool:
    try:
        uuid.UUID(str(some))
        return True
    except ValueError:
        return False


def _entry(conn, entry: str | None) -> dict | None:
    if not entry:
        return None
    found = _rows(conn, "SELECT id::text, area FROM ops.entry WHERE id = %(e)s::uuid", {"e": entry}) if _uuid(entry) else []
    if not found:
        raise NotFound("no such issue")
    return found[0]


def _report(conn, report: str | None) -> dict | None:
    if not report:
        return None
    found = _rows(conn, "SELECT id::text, area FROM ops.report WHERE id = %(r)s::uuid", {"r": report}) if _uuid(report) else []
    if not found:
        raise NotFound("no such report")
    return found[0]


def add(conn, author, data: bytes, *, lon: float | None = None, lat: float | None = None, entry: str | None = None,
        caption: str | None = None, area: str | None = None, origin: str = "upload", report: str | None = None) -> dict:
    """Keep a photo. Where it was taken is the camera's position, else `lon`, `lat`, else it waits to be placed.

    The same file added again is the photo already kept, as it is (`again`).
    """
    if not data:
        raise Invalid("there is no file")
    if len(data) > MOST_BYTES:
        raise Invalid(f"a photo can be at most {MOST_BYTES // 1_000_000} MB")
    sha = hashlib.sha256(data).hexdigest()
    kept = conn.execute("SELECT id::text FROM ops.photo WHERE sha256 = %s", (sha,)).fetchone()
    if kept:
        return one(conn, kept[0]) | {"again": True}
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise Invalid("that is not a photo this can read: JPEG, PNG, HEIC or WebP") from e
    ext = _EXT.get(img.format or "")
    if not ext:
        raise Invalid(f"photos in {img.format} are not kept: JPEG, PNG, HEIC or WebP")
    camera = _camera(img)
    shown, thumb, width, height = _copies(img)
    linked = _entry(conn, entry)
    of_report = _report(conn, report)
    if camera["lon"] is not None:
        lon, lat, placed = camera["lon"], camera["lat"], "camera"
    elif lon is not None and lat is not None:
        placed = "hand"
    else:
        lon = lat = placed = None
    # Of the area where it was taken, when a route is near; else of the one it was added in.
    where = ((_area_at(conn, lon, lat) if lon is not None else None) or (area or "").lower() or (linked or {}).get("area")
             or (of_report or {}).get("area") or None)
    _keep(path(sha, "original", ext), data)
    _keep(path(sha, "1600"), shown)
    _keep(path(sha, "thumb"), thumb)
    made = conn.execute(f"""
        INSERT INTO ops.photo (sha256, ext, bytes, width, height, taken_at, geom, placed, facing, area, caption,
                               entry_id, report_id, origin, made_by, updated_by)
        VALUES (%(sha)s, %(ext)s, %(bytes)s, %(w)s, %(h)s, %(taken)s,
                CASE WHEN %(lon)s::float8 IS NULL THEN NULL ELSE {_POINT} END, %(placed)s, %(facing)s, %(area)s,
                %(caption)s, %(entry)s::uuid, %(report)s::uuid, %(origin)s, %(who)s, %(who)s)
        RETURNING id::text""",
        {"sha": sha, "ext": ext, "bytes": len(data), "w": width, "h": height, "taken": camera["taken_at"],
         "lon": lon, "lat": lat, "placed": placed, "facing": camera["facing"], "area": where,
         "caption": (caption or "").strip()[:500] or None, "entry": linked["id"] if linked else None,
         "report": of_report["id"] if of_report else None,
         "origin": origin[:200], "who": str(author)}).fetchone()[0]
    _event(conn, author, made, "photo.added", {"placed": placed, "entry": linked["id"] if linked else None, "origin": origin[:200]})
    return one(conn, made)


_SHOWN = """
    SELECT p.id::text, p.sha256, p.ext, p.bytes, p.width, p.height, p.taken_at, p.placed, p.facing, p.area, p.caption,
           p.entry_id::text AS entry, p.report_id::text AS report, p.origin, p.made_at, p.made_by, p.updated_at, p.updated_by,
           round(ST_X(ST_Transform(p.geom, 4326))::numeric, 6)::float8 AS lon,
           round(ST_Y(ST_Transform(p.geom, 4326))::numeric, 6)::float8 AS lat
    FROM ops.photo p"""


def listing(conn, area: str | None = None, route: str | None = None, entry: str | None = None,
            unplaced: bool = False, report: str | None = None) -> list[dict]:
    """The photos of an area, those taken near a route (within 75 m of its line), those of an issue, or those that
    wait to be placed: the newest taken first."""
    where, params = [], {}
    if area:
        where.append("p.area = %(area)s")
        params["area"] = area.lower()
    if route:
        if not _uuid(route):
            raise NotFound("no such route")
        where.append("""EXISTS (SELECT 1 FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                                JOIN core.edge_revision v ON v.id = e.current_revision_id
                                WHERE l.route_id = %(route)s::uuid AND ST_DWithin(v.geom, p.geom, %(near)s))""")
        params.update(route=route, near=NEAR_ROUTE_M)
    if entry:
        if not _uuid(entry):
            raise NotFound("no such issue")
        where.append("p.entry_id = %(entry)s::uuid")
        params["entry"] = entry
    if report:
        if not _uuid(report):
            raise NotFound("no such report")
        where.append("p.report_id = %(report)s::uuid")
        params["report"] = report
    if unplaced:
        where.append("p.geom IS NULL")
    sql = _SHOWN + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.taken_at DESC NULLS LAST, p.made_at DESC"
    return _rows(conn, sql, params)


def one(conn, photo: str) -> dict:
    found = _rows(conn, _SHOWN + " WHERE p.id = %(p)s::uuid", {"p": photo}) if _uuid(photo) else []
    if not found:
        raise NotFound("no such photo")
    return found[0]


def change(conn, author, photo: str, said: dict) -> dict:
    """Say something else of a photo: what it shows (`caption`), where it was taken (`lon`, `lat`: placed by hand),
    or which issue it shows (`entry`; None: none)."""
    was = one(conn, photo)
    sets, told = {}, {}
    if "caption" in said and (said["caption"] or "").strip() != (was["caption"] or ""):
        sets["caption"] = (said["caption"] or "").strip()[:500] or None
        told["caption"] = True
    if said.get("lon") is not None and said.get("lat") is not None:
        conn.execute(f"""UPDATE ops.photo SET geom = {_POINT}, placed = 'hand' WHERE id = %(p)s::uuid""",
                     {"lon": said["lon"], "lat": said["lat"], "p": photo})
        sets["area"] = _area_at(conn, said["lon"], said["lat"]) or was["area"]
        told["placed"] = True
    if "entry" in said and said["entry"] != was["entry"]:
        linked = _entry(conn, said["entry"])
        sets["entry_id"] = linked["id"] if linked else None
        told["entry"] = sets["entry_id"]
    if not told:
        return was
    columns = ", ".join(f"{k} = %({k})s" + ("::uuid" if k == "entry_id" else "") for k in sets)
    conn.execute(f"UPDATE ops.photo SET {columns + ', ' if columns else ''}updated_at = now(), updated_by = %(who)s "
                 "WHERE id = %(p)s::uuid", {**sets, "who": str(author), "p": photo})
    _event(conn, author, photo, "photo.changed", told)
    return one(conn, photo)


def take_away(conn, author, photo: str) -> dict:
    """Take a photo away. Its files stay, so that Undo can bring it back."""
    was = one(conn, photo)
    conn.execute("DELETE FROM ops.photo WHERE id = %s::uuid", (photo,))
    _event(conn, author, photo, "photo.taken_away", {"caption": was["caption"]})
    return {"taken_away": photo}


def file(conn, photo: str, size: str) -> tuple[Path, str]:
    """The file of a photo to send, and its type: the thumbnail, the copy to show, or the file as it came."""
    if size not in SIZES:
        raise NotFound(f"a photo comes as {', '.join(SIZES)}")
    p = one(conn, photo)
    where = path(p["sha256"], size, p["ext"])
    if not where.exists():
        raise NotFound("the photo's file is not on this server")
    return where, _TYPE[p["ext"]] if size == "original" else "image/jpeg"
