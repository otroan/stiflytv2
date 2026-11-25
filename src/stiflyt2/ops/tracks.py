"""GPX tracks walked on the routes: the feature `tracks` (step 3 of the plan; import migration 0029).

A track is a way somebody walked, as their phone or watch logged it: a file
(GPX) with a point every second or so, with the device's height and the time.
It is something to compare a route with, and to lay a stretch of one on. v1
kept the line alone (no heights, no times); its tracks are taken in as lines.

What came is kept as it came (`ops.track_raw`, and the file on the server's
disk), once, by its digest. What is shown and used is the track cleaned
(`ops.track_line`):

- a point that jumps away and back, faster than anyone walks, is taken out;
- where the device stood still for two minutes or more, the points it logged
  there are one stop (first and last, where it stood on the whole);
- the track is cut in two where the device was off (a gap: far, and long or
  fast), and the parts of the file are joined where one begins where the one
  before ended;
- what a person cut out by hand (a detour, the drive home) is left out.

Nothing is guessed in: no point is moved but those of a stop, nothing is
smoothed. The cleaned track says what it comes to: length, time walking
(without the stops), and the climb by the device's heights.

Only signed-in people with the feature see tracks; adding and changing them
needs the editor or operations role as well. Every change is an event and can
be taken back (`ops/undo.py`); a track taken away keeps what came, so that Undo
can bring it back.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import os
import struct
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2 import features
from stiflyt2.config import settings
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import areas as areas_of

features.feature("tracks", "see GPX tracks walked on the routes, and add them where one may change routes or signs",
                 "ask to be given the feature `tracks`")

MOST_BYTES = 30_000_000
MOST_POINTS = 400_000
CLEANING = "c1"         # how tracks are cleaned now: another cleans every track anew (`stiflyt2 tracks clean-again`)
JOIN_M = 50             # the parts of a file are one where the next begins this near where the one before ended
GAP_M = 300             # a track is cut in two where two points lie further apart than this,
GAP_S = 600             # with ten minutes or more between them, or faster than one walks, or no times to say
FAST = 6.0              # metres a second: faster than anyone walks (21.6 km/h)
SPIKE_M = 40            # with no times: a point that jumps out this far and comes back to near where it left
STILL_M = 12            # standing still: the points stay within this of where they began,
STILL_S = 120           # for two minutes or more
JITTER_M = 2.0          # with no times: a point this near the one before adds nothing
CLIMB_M = 4.0           # the device's heights: a rise or a fall counts once it comes to this
SHORT_M = 20            # a part shorter than this is left out
RIDE_S = 60             # faster than one walks for a minute or more,
RIDE_M = 300            # and this far: a ride (a car, a boat, a lift), and no walk
NEAR_ROUTE_M = 100      # a track is of the areas of the routes it comes this near
AREA_M = 2000           # and of the area of the route nearest its middle, this near
_SRID = 25833


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def _event(conn, author, track: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, 'track', %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), track, type_, Jsonb(payload)))


def _uuid(some) -> bool:
    try:
        uuid.UUID(str(some))
        return True
    except ValueError:
        return False


def path(sha: str) -> Path:
    """Where a track's file is kept, as it came."""
    return settings.tracks_dir / sha[:2] / f"{sha}.gpx"


def _keep(target: Path, data: bytes) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}")
    part.write_bytes(data)
    os.replace(part, target)


# --- reading a file ------------------------------------------------------------------------------------------------

def _when(text: str | None) -> float:
    if not text:
        return math.nan
    try:
        t = dt.datetime.fromisoformat(text.strip())
    except ValueError:
        return math.nan
    return (t if t.tzinfo else t.replace(tzinfo=dt.UTC)).timestamp()


def _filled(values: np.ndarray) -> tuple[np.ndarray, bool]:
    """The values with the few that are missing filled in between their neighbours; none, where most are missing."""
    known = ~np.isnan(values)
    if known.sum() < max(2, 0.9 * len(values)):
        return np.zeros(len(values)), False
    if known.all():
        return values, True
    at = np.arange(len(values))
    return np.interp(at, at[known], values[known]), True


def parse(data: bytes) -> tuple[list[np.ndarray], str | None, bool, bool]:
    """The parts of a GPX file (its track segments, or its routes), each an array of lon, lat, height and time
    (seconds since 1970) per point; the name it gives; and whether it has heights and times."""
    parts: list[list] = []
    name, stack, current = None, [], None
    try:
        for event, node in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = node.tag.rsplit("}", 1)[-1]
            if event == "start":
                stack.append(tag)
                if tag in ("trkseg", "rte"):
                    current = []
                continue
            stack.pop()
            if tag in ("trkpt", "rtept") and current is not None:
                try:
                    lat, lon = float(node.get("lat")), float(node.get("lon"))
                except (TypeError, ValueError):
                    node.clear()
                    continue
                ele = when = None
                for child in node:
                    ctag = child.tag.rsplit("}", 1)[-1]
                    if ctag == "ele":
                        ele = child.text
                    elif ctag == "time":
                        when = child.text
                try:
                    height = float(ele) if ele is not None else math.nan
                except ValueError:
                    height = math.nan
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    current.append((lon, lat, height, _when(when)))
                node.clear()
            elif tag in ("trkseg", "rte"):
                if current and len(current) >= 2:
                    parts.append(current)
                current = None
            elif tag == "name" and name is None and stack and stack[-1] in ("trk", "rte", "metadata") and node.text:
                name = node.text.strip()[:200] or None
            if tag in ("trkseg", "rte", "trk", "wpt"):
                node.clear()
    except ET.ParseError as e:
        raise Invalid("that is not a GPX file this can read") from e
    if not parts:
        raise Invalid("there is no track in the file: a GPX with a track (trk) or a route (rte) is wanted")
    arrays = [np.array(p, dtype=np.float64) for p in parts]
    if sum(len(a) for a in arrays) > MOST_POINTS:
        raise Invalid(f"a track can have at most {MOST_POINTS:,} points")
    every = np.concatenate(arrays)
    heights, has_height = _filled(every[:, 2])
    times, has_time = _filled(every[:, 3])
    at = 0
    for a in arrays:
        a[:, 2], a[:, 3] = heights[at:at + len(a)], times[at:at + len(a)]
        at += len(a)
    return arrays, name, has_height, has_time


# --- the line in the database, as EWKB ------------------------------------------------------------------------------

def _ewkb(parts: list[np.ndarray], srid: int) -> bytes:
    """A MultiLineString with heights and times (ZM), as PostGIS reads it."""
    out = [struct.pack("<BIII", 1, 5 | 0xE0000000, srid, len(parts))]
    for p in parts:
        out.append(struct.pack("<BII", 1, 2 | 0xC0000000, len(p)))
        out.append(np.ascontiguousarray(p[:, :4], dtype="<f8").tobytes())
    return b"".join(out)


def _parts_of(ewkb: bytes | None) -> list[np.ndarray]:
    """The parts of a MultiLineString ZM from PostGIS, each an array of x, y, z, m."""
    if not ewkb:
        return []
    buf = bytes(ewkb)
    end = "<" if buf[0] == 1 else ">"
    kind = struct.unpack_from(f"{end}I", buf, 1)[0]
    off = 5 + (4 if kind & 0x20000000 else 0)
    dims = 2 + bool(kind & 0x80000000) + bool(kind & 0x40000000)
    (n,) = struct.unpack_from(f"{end}I", buf, off)
    off += 4
    parts = []
    for _ in range(n):
        sub = struct.unpack_from(f"{end}I", buf, off + 1)[0]
        sdims = 2 + bool(sub & 0x80000000) + bool(sub & 0x40000000) if sub & 0xC0000000 else dims
        (count,) = struct.unpack_from(f"{end}I", buf, off + 5)
        off += 9
        a = np.frombuffer(buf, dtype=f"{end}f8", count=count * sdims, offset=off).reshape(count, sdims)
        off += count * sdims * 8
        if sdims < 4:
            a = np.hstack([a, np.zeros((count, 4 - sdims))])
        parts.append(a.astype(np.float64))
    return parts


# --- cleaning --------------------------------------------------------------------------------------------------------

def _climb(z: np.ndarray) -> tuple[float, float]:
    """Up and down by the heights of a part, a rise or fall counting once it comes to CLIMB_M."""
    up = down = 0.0
    if len(z) < 2:
        return up, down
    ref = z[0]
    for h in z[1:]:
        if h - ref >= CLIMB_M:
            up += h - ref
            ref = h
        elif ref - h >= CLIMB_M:
            down += ref - h
            ref = h
    return up, down


def clean(raw: list[np.ndarray], has_time: bool, has_height: bool, cuts: list[dict] | None = None) -> dict:
    """The track cleaned: its parts (each an array of x, y, z, t in metres and seconds) and what they come to, from
    the parts as they came (x, y in metres), and the ranges of points as they came cut out by hand."""
    sizes = [len(p) for p in raw]
    pts = np.concatenate(raw) if raw else np.zeros((0, 4))
    seg = np.repeat(np.arange(len(raw)), sizes)
    n = len(pts)
    keep = np.ones(n, dtype=bool)
    told = {"jumps": 0, "doubles": 0, "backwards": 0, "still": 0, "stops": [], "gaps": 0, "short": 0, "cut": 0,
            "rides": 0, "ride_m": 0.0}
    cut_after = np.zeros(n, dtype=bool)       # a part ends at this point: what follows was cut out, by hand or as a ride
    for c in cuts or []:
        a, b = max(0, int(c["from"])), min(n - 1, int(c["to"]))
        if a <= b:
            told["cut"] += int(keep[a:b + 1].sum())
            keep[a:b + 1] = False
            if a > 0:
                cut_after[a - 1] = True
    xy = pts[:, :2].copy()
    stop_step = np.zeros(n, dtype=bool)       # the step to this point is a stop: no walking
    start = 0
    for size in sizes:
        idx = np.arange(start, start + size)
        start += size
        k = idx[keep[idx]]
        if len(k) < 2:
            continue
        # The same point twice, and time going backwards.
        same = np.r_[False, np.hypot(*(xy[k[1:]] - xy[k[:-1]]).T) < 0.01]
        if has_time:
            same &= np.r_[False, pts[k[1:], 3] == pts[k[:-1], 3]]
        told["doubles"] += int(same.sum())
        keep[k[same]] = False
        k = k[~same]
        if has_time and len(k) > 1:
            latest, back = -math.inf, []
            for i in k:
                if pts[i, 3] < latest:
                    back.append(i)
                else:
                    latest = pts[i, 3]
            told["backwards"] += len(back)
            keep[back] = False
            k = idx[keep[idx]]
        # Jumps: a point out and back again, faster than one walks (with no times: far out, and back to near where
        # it left). Twice over, for jumps of two points.
        for _ in range(3):
            if len(k) < 3:
                break
            p = xy[k]
            d1 = np.hypot(*(p[1:-1] - p[:-2]).T)
            d2 = np.hypot(*(p[2:] - p[1:-1]).T)
            d0 = np.hypot(*(p[2:] - p[:-2]).T)
            back = (d0 < 0.35 * np.minimum(d1, d2))
            if has_time:
                # Too fast to get to and from, where going straight past it is not: out and back. A ride is fast
                # throughout, and is no jump.
                t = pts[k, 3]
                v1 = d1 / np.maximum(t[1:-1] - t[:-2], 1.0)
                v2 = d2 / np.maximum(t[2:] - t[1:-1], 1.0)
                v0 = d0 / np.maximum(t[2:] - t[:-2], 1.0)
                jump = (v1 > FAST) & (v2 > FAST) & (v0 <= FAST) & (d1 > 15) & (d2 > 15)
                jump |= back & (d1 > SPIKE_M) & (d2 > SPIKE_M) & (v1 > FAST / 2) & (v2 > FAST / 2)
            else:
                jump = back & (d1 > SPIKE_M) & (d2 > SPIKE_M)
            if not jump.any():
                break
            gone = k[1:-1][jump]
            told["jumps"] += len(gone)
            keep[gone] = False
            k = idx[keep[idx]]
        # A ride: faster than one walks for a minute and some hundred metres or more. Left out, as a cut is.
        if has_time and len(k) > 2:
            steps = np.hypot(*(xy[k[1:]] - xy[k[:-1]]).T)
            fast = steps / np.maximum(np.diff(pts[k, 3]), 1.0) > FAST
            edges = np.flatnonzero(np.diff(np.r_[0, fast.astype(np.int8), 0]))
            for a, b in zip(edges[::2], edges[1::2], strict=True):        # steps a .. b-1, from k[a] to k[b]
                if pts[k[b], 3] - pts[k[a], 3] >= RIDE_S and steps[a:b].sum() >= RIDE_M:
                    keep[k[a + 1:b + 1]] = False
                    cut_after[k[a]] = True
                    told["rides"] += 1
                    told["ride_m"] += float(steps[a:b].sum())
            k = idx[keep[idx]]
        # Standing still: what the device logged there is one stop, first and last where it stood on the whole.
        if has_time and len(k) > 2:
            i, m = 0, len(k)
            while i < m - 1:
                j = i + 1
                while j < m and math.hypot(xy[k[j], 0] - xy[k[i], 0], xy[k[j], 1] - xy[k[i], 1]) <= STILL_M:
                    j += 1
                first, last = k[i], k[j - 1]
                if j - 1 > i + 1 and pts[last, 3] - pts[first, 3] >= STILL_S:
                    inner = k[i + 1:j - 1]
                    centre = xy[k[i:j]].mean(axis=0)
                    xy[first] = xy[last] = centre
                    keep[inner] = False
                    told["still"] += len(inner)
                    stop_step[last] = True
                    told["stops"].append({"x": float(centre[0]), "y": float(centre[1]), "at": float(pts[first, 3]),
                                          "s": float(pts[last, 3] - pts[first, 3])})
                    i = j
                else:
                    i += 1
        elif not has_time and len(k) > 1:
            kept_at, drop = k[0], []
            for i in k[1:]:
                if math.hypot(xy[i, 0] - xy[kept_at, 0], xy[i, 1] - xy[kept_at, 1]) < JITTER_M:
                    drop.append(i)
                else:
                    kept_at = i
            told["doubles"] += len(drop)
            keep[drop] = False
    # Stops one after the other are one.
    stops = []
    for s in sorted(told["stops"], key=lambda s: s["at"]):
        if stops and s["at"] - (stops[-1]["at"] + stops[-1]["s"]) <= 5 and math.hypot(s["x"] - stops[-1]["x"], s["y"] - stops[-1]["y"]) <= 3 * STILL_M:
            stops[-1]["s"] = s["at"] + s["s"] - stops[-1]["at"]
        else:
            stops.append(dict(s))
    told["stops"] = stops
    # The parts: where the file has them (joined where one begins where the one before ended), where something was
    # cut out by hand, and where the device was off.
    order = np.flatnonzero(keep)
    parts, current = [], []
    for i in order:
        if current:
            j = current[-1]
            d = math.hypot(xy[i, 0] - xy[j, 0], xy[i, 1] - xy[j, 1])
            dt_ = pts[i, 3] - pts[j, 3] if has_time else 0.0
            if cut_after[j:i].any() or (seg[i] != seg[j] and d > JOIN_M) or (
                    d > GAP_M and (not has_time or dt_ >= GAP_S or d / max(dt_, 1.0) > FAST)):
                if not cut_after[j:i].any() and seg[i] == seg[j]:
                    told["gaps"] += 1
                parts.append(current)
                current = []
        current.append(i)
    if current:
        parts.append(current)
    out, length, walking, up, down, low, high = [], 0.0, 0.0, 0.0, 0.0, math.inf, -math.inf
    for part in parts:
        p = np.array(part)
        steps = np.hypot(*(xy[p[1:]] - xy[p[:-1]]).T)
        if len(p) < 2 or steps.sum() < SHORT_M:
            told["short"] += len(p)
            continue
        line = np.column_stack([xy[p], pts[p, 2], pts[p, 3], p])
        out.append(line)
        length += float(steps.sum())
        if has_time:
            dts = np.diff(pts[p, 3])
            slow = (steps / np.maximum(dts, 1.0) < 0.2) & (dts > 30)
            walking += float(dts[~stop_step[p[1:]] & ~slow].sum())
        if has_height:
            u, d_ = _climb(pts[p, 2])
            up, down = up + u, down + d_
            low, high = min(low, float(pts[p, 2].min())), max(high, float(pts[p, 2].max()))
    times = [line[:, 3] for line in out]
    first = float(min(t[0] for t in times)) if has_time and out else None
    last_ = float(max(t[-1] for t in times)) if has_time and out else None
    return {"parts": out, "length_m": length, "points": int(sum(len(p) for p in out)),
            "started_at": first, "ended_at": last_, "total_s": (last_ - first) if first is not None else None,
            "moving_s": walking if has_time and out else None,
            "up_m": up if has_height and out else None, "down_m": down if has_height and out else None,
            "low_m": low if has_height and out else None, "high_m": high if has_height and out else None,
            "cleaned": {k: v for k, v in told.items()}}


def _key(cuts: list[dict]) -> str:
    return f"{CLEANING}|{json.dumps(sorted([int(c['from']), int(c['to'])] for c in cuts), separators=(',', ':'))}"


def _raw(conn, raw_id: str) -> tuple[list[np.ndarray], bool, bool]:
    got = conn.execute("SELECT ST_AsEWKB(geom), has_time, has_height FROM ops.track_raw WHERE id = %s", (raw_id,)).fetchone()
    return _parts_of(got[0]), got[1], got[2]


def _line(conn, track: str, raw_id: str, cuts: list[dict]) -> str:
    """The track cleaned, with what was cut out by hand left out: worked out once for each way it is cut."""
    key = _key(cuts)
    if conn.execute("SELECT 1 FROM ops.track_line WHERE track_id = %s AND key = %s", (track, key)).fetchone():
        return key
    raw, has_time, has_height = _raw(conn, raw_id)
    made = clean(raw, has_time, has_height, cuts)
    at = lambda s: dt.datetime.fromtimestamp(s, dt.UTC) if s is not None else None
    conn.execute("""
        INSERT INTO ops.track_line (track_id, key, geom, length_m, parts, points, started_at, ended_at, total_s, moving_s,
                                    up_m, down_m, low_m, high_m, cleaned)
        VALUES (%(t)s, %(key)s, ST_GeomFromEWKB(%(geom)s), %(length)s, %(parts)s, %(points)s, %(started)s, %(ended)s,
                %(total)s, %(moving)s, %(up)s, %(down)s, %(low)s, %(high)s, %(cleaned)s)""",
                 {"t": track, "key": key, "geom": _ewkb([p[:, :4] for p in made["parts"]], _SRID) if made["parts"] else None,
                  "length": made["length_m"], "parts": len(made["parts"]), "points": made["points"],
                  "started": at(made["started_at"]), "ended": at(made["ended_at"]), "total": made["total_s"],
                  "moving": made["moving_s"], "up": made["up_m"], "down": made["down_m"], "low": made["low_m"],
                  "high": made["high_m"], "cleaned": Jsonb(made["cleaned"])})
    return key


def _area_of(conn, track: str, key: str) -> str | None:
    """The area of the route in use nearest the middle of the track, when one is near."""
    found = conn.execute("""
        WITH m AS (SELECT ST_ClosestPoint(geom, ST_Centroid(geom)) AS p, geom FROM ops.track_line
                   WHERE track_id = %(t)s AND key = %(key)s AND geom IS NOT NULL)
        SELECT lower(left(r.code, 3)) FROM m, core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE ST_DWithin(v.geom, m.geom, %(near)s) ORDER BY ST_Distance(v.geom, m.p) LIMIT 1""",
        {"t": track, "key": key, "near": AREA_M}).fetchone()
    return found[0] if found else None


# --- adding ----------------------------------------------------------------------------------------------------------

def _made(conn, author, raw_id: str, name: str, area: str | None, origin: str) -> dict:
    track = str(uuid.uuid4())
    key = _line(conn, track, raw_id, [])
    conn.execute("""INSERT INTO ops.track (id, raw_id, name, area, origin, line_key, made_by, updated_by)
                    VALUES (%(id)s, %(raw)s, %(name)s, %(area)s, %(origin)s, %(key)s, %(who)s, %(who)s)""",
                 {"id": track, "raw": raw_id, "name": name[:200], "area": _area_of(conn, track, key) or area,
                  "origin": origin[:200], "key": key, "who": str(author)})
    _event(conn, author, track, "track.added", {"origin": origin[:200], "name": name[:200]})
    return one(conn, track)


def _again(conn, author, raw_id: str, name: str, area: str | None, origin: str) -> dict:
    kept = conn.execute("SELECT id::text FROM ops.track WHERE raw_id = %s ORDER BY made_at LIMIT 1", (raw_id,)).fetchone()
    if kept:
        return one(conn, kept[0]) | {"again": True}
    return _made(conn, author, raw_id, name, area, origin)


def add(conn, author, data: bytes, *, name: str | None = None, area: str | None = None, origin: str = "upload",
        filename: str | None = None) -> dict:
    """Keep a track from a GPX file. The same file added again is the track already kept (`again`)."""
    if not data:
        raise Invalid("there is no file")
    if len(data) > MOST_BYTES:
        raise Invalid(f"a track can be at most {MOST_BYTES // 1_000_000} MB")
    sha = hashlib.sha256(data).hexdigest()
    stem = (filename or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].strip() or None
    kept = conn.execute("SELECT id::text, named FROM ops.track_raw WHERE sha256 = %s", (sha,)).fetchone()
    if kept:
        return _again(conn, author, kept[0], (name or "").strip() or kept[1] or stem or "A track", area, origin)
    parts, named, has_height, has_time = parse(data)
    _keep(path(sha), data)
    raw_id = conn.execute("""
        INSERT INTO ops.track_raw (sha256, ext, bytes, points, has_time, has_height, named, geom)
        VALUES (%s, 'gpx', %s, %s, %s, %s, %s, ST_Transform(ST_GeomFromEWKB(%s), %s)) RETURNING id::text""",
        (sha, len(data), sum(len(p) for p in parts), has_time, has_height, named, _ewkb(parts, 4326), _SRID)).fetchone()[0]
    return _made(conn, author, raw_id, (name or "").strip() or named or stem or "A track", (area or "").lower() or None, origin)


def add_line(conn, author, wkt: str, *, name: str | None, area: str | None, origin: str) -> dict:
    """Keep a track that is a line alone, with no heights and no times (as v1 kept them), in EPSG:25833."""
    sha = hashlib.sha256(wkt.encode()).hexdigest()
    kept = conn.execute("SELECT id::text FROM ops.track_raw WHERE sha256 = %s", (sha,)).fetchone()
    if kept:
        return _again(conn, author, kept[0], (name or "").strip() or "A track", area, origin)
    try:
        with conn.transaction():
            raw_id = conn.execute("""
                INSERT INTO ops.track_raw (sha256, points, has_time, has_height, named, geom)
                SELECT %(sha)s, ST_NPoints(g), false, false, %(name)s, ST_Force4D(ST_Multi(g))
                FROM (SELECT ST_GeomFromText(%(wkt)s, %(srid)s) AS g) l RETURNING id::text""",
                {"sha": sha, "name": name, "wkt": wkt, "srid": _SRID}).fetchone()[0]
    except Exception as e:      # a line PostGIS cannot read
        raise Invalid(f"that is no line: {str(e).splitlines()[0][:200]}") from e
    return _made(conn, author, raw_id, (name or "").strip() or "A track", (area or "").lower() or None, origin)


# --- looking ---------------------------------------------------------------------------------------------------------

_SHOWN = """
    SELECT t.id::text, t.name, t.area, t.origin, t.made_at, t.made_by, t.updated_at, t.updated_by, t.cuts,
           r.has_time, r.has_height, r.points AS points_came, r.ext IS NOT NULL AS has_file,
           l.length_m, l.parts, l.points, l.started_at, l.ended_at, l.total_s, l.moving_s, l.up_m, l.down_m, l.low_m, l.high_m,
           l.cleaned, l.checked
    FROM ops.track t JOIN ops.track_raw r ON r.id = t.raw_id LEFT JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key"""


def _said(row: dict) -> dict:
    cleaned = row.pop("cleaned", None) or {}
    row["cleaned"] = {k: cleaned.get(k, 0) for k in ("jumps", "doubles", "backwards", "still", "gaps", "short", "cut", "rides", "ride_m")}
    row["cleaned"]["stops"] = len(cleaned.get("stops", []))
    row["cleaned"]["stopped_s"] = sum(s["s"] for s in cleaned.get("stops", []))
    row["cut_out"] = len(row.get("cuts") or [])
    row.pop("cuts", None)
    return row


def listing(conn, area: str | None = None, route: str | None = None) -> list[dict]:
    """The tracks of an area (or that come within 100 m of one of its routes), or those that come within 100 m of a
    route: the latest walked first."""
    where, params = [], {}
    near = """EXISTS (SELECT 1 FROM core.route_leg rl JOIN core.route r ON r.id = rl.route_id AND r.status = 'active'
                      JOIN core.edge e ON e.id = rl.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
                      WHERE {which} AND ST_DWithin(v.geom, l.geom, %(near)s))"""
    if area:
        where.append(f"(t.area = %(area)s OR {near.format(which='(lower(left(r.code, 3)) = %(area)s OR r.id = ANY(%(added)s::uuid[]))')})")
        params.update(area=area.lower(), added=areas_of.added_to(conn, area), near=NEAR_ROUTE_M)
    if route:
        if not _uuid(route):
            raise NotFound("no such route")
        where.append(near.format(which="r.id = %(route)s::uuid"))
        params.update(route=route, near=NEAR_ROUTE_M)
    sql = _SHOWN + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY coalesce(l.started_at, t.made_at) DESC"
    return [_said(r) for r in _rows(conn, sql, params)]


def lines(conn, area: str) -> dict:
    """The tracks of an area as lines to draw (simplified to a few metres): GeoJSON, longitude and latitude."""
    ids = [t["id"] for t in listing(conn, area)]
    found = _rows(conn, """
        SELECT t.id::text, t.name, l.started_at, l.length_m,
               ST_AsGeoJSON(ST_Transform(ST_Force2D(ST_Simplify(l.geom, 3)), 4326), 6)::json AS geometry
        FROM ops.track t JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key
        WHERE t.id = ANY(%(ids)s::uuid[]) AND l.geom IS NOT NULL""", {"ids": ids})
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": f.pop("geometry"), "properties": {**f, "started_at": f["started_at"].isoformat() if f["started_at"] else None}}
        for f in found]}


def one(conn, track: str, drawn: bool = False) -> dict:
    """A track: what is said of it and what it comes to; `drawn`, also its lines to draw (cleaned, and as it came),
    its stops, and what was cut out of it by hand, where along it as it came."""
    found = _rows(conn, _SHOWN + " WHERE t.id = %(t)s::uuid", {"t": track}) if _uuid(track) else []
    if not found:
        raise NotFound("no such track")
    row = found[0]
    cuts, stops = row["cuts"], (row["cleaned"] or {}).get("stops", [])
    told = _said(dict(row))
    if not drawn:
        return told
    shapes = _rows(conn, """
        SELECT ST_AsGeoJSON(ST_Transform(ST_Force2D(ST_Simplify(l.geom, 1)), 4326), 6)::json AS line,
               ST_AsGeoJSON(ST_Transform(ST_Force2D(ST_Simplify(r.geom, 2)), 4326), 6)::json AS came
        FROM ops.track t JOIN ops.track_raw r ON r.id = t.raw_id LEFT JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key
        WHERE t.id = %(t)s::uuid""", {"t": track})[0]
    told["line"], told["came"] = shapes["line"], shapes["came"]
    if stops:
        at = conn.execute("""SELECT array_agg(ARRAY[round(ST_X(p)::numeric, 6), round(ST_Y(p)::numeric, 6)]::float8[] ORDER BY n)
                             FROM (SELECT n, ST_Transform(ST_SetSRID(ST_MakePoint(x, y), %s), 4326) AS p
                                   FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS u(x, y, n)) q""",
                          (_SRID, [s["x"] for s in stops], [s["y"] for s in stops])).fetchone()[0]
        told["stops"] = [{"lon": a[0], "lat": a[1], "at": dt.datetime.fromtimestamp(s["at"], dt.UTC).isoformat(), "s": s["s"]}
                         for s, a in zip(stops, at, strict=True)]
    else:
        told["stops"] = []
    along = _along(_raw(conn, row_raw(conn, track))[0]) if cuts else None
    told["cuts"] = [{"n": i, "from": c["from"], "to": c["to"], "from_m": round(float(along[min(c["from"], len(along) - 1)])),
                     "to_m": round(float(along[min(c["to"], len(along) - 1)]))} for i, c in enumerate(cuts)]
    return told


def row_raw(conn, track: str) -> str:
    return conn.execute("SELECT raw_id::text FROM ops.track WHERE id = %s::uuid", (track,)).fetchone()[0]


def _along(raw: list[np.ndarray]) -> np.ndarray:
    """How far along the track as it came each point is, in metres: the gaps between its parts not counted."""
    out, at = [], 0.0
    for p in raw:
        steps = np.r_[0.0, np.hypot(*np.diff(p[:, :2], axis=0).T)]
        out.append(at + np.cumsum(steps))
        at = out[-1][-1]
    return np.concatenate(out) if out else np.zeros(0)


# --- changing --------------------------------------------------------------------------------------------------------

def change(conn, author, track: str, said: dict) -> dict:
    """Say what a track is called, or which area it is of."""
    was = one(conn, track)
    sets = {}
    if (said.get("name") or "").strip() and said["name"].strip() != was["name"]:
        sets["name"] = said["name"].strip()[:200]
    if "area" in said and (said["area"] or None) != was["area"]:
        sets["area"] = (said["area"] or "").lower() or None
    if not sets:
        return was
    columns = ", ".join(f"{k} = %({k})s" for k in sets)
    conn.execute(f"UPDATE ops.track SET {columns}, updated_at = now(), updated_by = %(who)s WHERE id = %(t)s::uuid",
                 {**sets, "who": str(author), "t": track})
    _event(conn, author, track, "track.changed", {k: [was[k], v] for k, v in sets.items()})
    return one(conn, track)


def _nearest(conn, raw: list[np.ndarray], point: list[float]) -> int:
    x, y = conn.execute("SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s) AS p) q",
                        (point[0], point[1], _SRID)).fetchone()
    every = np.concatenate(raw)
    return int(np.argmin(np.hypot(every[:, 0] - x, every[:, 1] - y)))


def cut(conn, author, track: str, from_point: list[float], to_point: list[float]) -> dict:
    """Cut out of a track the part between two points on it (a detour, the drive home): what came stays, and the
    track is cleaned anew without it."""
    one(conn, track)
    raw_id = row_raw(conn, track)
    raw, _, _ = _raw(conn, raw_id)
    a, b = sorted((_nearest(conn, raw, from_point), _nearest(conn, raw, to_point)))
    if a == b:
        raise Invalid("the two points are the same point of the track: click where the part begins and where it ends")
    cuts = conn.execute("SELECT cuts FROM ops.track WHERE id = %s::uuid", (track,)).fetchone()[0]
    joined = sorted([*([c["from"], c["to"]] for c in cuts), [a, b]])
    merged: list[list[int]] = []
    for lo, hi in joined:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    new = [{"from": lo, "to": hi} for lo, hi in merged]
    key = _line(conn, track, raw_id, new)
    conn.execute("""UPDATE ops.track SET cuts = %s, line_key = %s, updated_at = now(), updated_by = %s WHERE id = %s::uuid""",
                 (Jsonb(new), key, str(author), track))
    along = _along(raw)
    _event(conn, author, track, "track.cut", {"from": a, "to": b, "metres": round(float(along[b] - along[a]))})
    return one(conn, track, drawn=True)


def put_back(conn, author, track: str, n: int) -> dict:
    """Put back what was cut out of a track by hand: the cut `n`, as listed."""
    one(conn, track)
    raw_id = row_raw(conn, track)
    cuts = conn.execute("SELECT cuts FROM ops.track WHERE id = %s::uuid", (track,)).fetchone()[0]
    if not 0 <= n < len(cuts):
        raise NotFound("no such cut")
    new = cuts[:n] + cuts[n + 1:]
    key = _line(conn, track, raw_id, new)
    conn.execute("""UPDATE ops.track SET cuts = %s, line_key = %s, updated_at = now(), updated_by = %s WHERE id = %s::uuid""",
                 (Jsonb(new), key, str(author), track))
    _event(conn, author, track, "track.put_back", cuts[n])
    return one(conn, track, drawn=True)


def take_away(conn, author, track: str) -> dict:
    """Take a track away. What came stays, so that Undo can bring it back."""
    was = one(conn, track)
    conn.execute("DELETE FROM ops.track WHERE id = %s::uuid", (track,))
    _event(conn, author, track, "track.taken_away", {"name": was["name"]})
    return {"taken_away": track}


def clean_again(conn, every: bool = False) -> dict:
    """Clean the tracks whose cleaned line is not here (after a copy is put back, or the cleaning has changed); with
    `every`, all of them."""
    done = 0
    for track, raw_id, cuts, key in conn.execute(
            "SELECT t.id::text, t.raw_id::text, t.cuts, t.line_key FROM ops.track t").fetchall():
        want = _key(cuts)
        if every:
            conn.execute("DELETE FROM ops.track_line WHERE track_id = %s AND key = %s", (track, want))
        if every or want != key or not conn.execute(
                "SELECT 1 FROM ops.track_line WHERE track_id = %s AND key = %s", (track, key)).fetchone():
            conn.execute("SELECT set_config('stiflyt2.undoing', 'on', true)")      # no change of anybody's
            conn.execute("UPDATE ops.track SET line_key = %s WHERE id = %s", (_line(conn, track, raw_id, cuts), track))
            conn.execute("SELECT set_config('stiflyt2.undoing', 'off', true)")
            done += 1
    return {"cleaned": done}


# --- a stretch laid on a track --------------------------------------------------------------------------------------

REACH_M = 60            # an end of a stretch this near a track is taken to lie on it: a device is some metres off


def _passes(p: np.ndarray, at: tuple[float, float], reach: float) -> list[tuple[float, float]]:
    """Each time a part of a track passes a point within reach: how near, and where along the part (the point before,
    and how far on to the next, as a fraction)."""
    d = np.hypot(p[:, 0] - at[0], p[:, 1] - at[1])
    near = d <= reach
    out = []
    edges = np.flatnonzero(np.diff(np.r_[0, near.astype(np.int8), 0]))
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        i = a + int(np.argmin(d[a:b]))
        best = (float(d[i]), float(i))
        for j in (i - 1, i):
            if 0 <= j < len(p) - 1:
                u, seg = p[j, :2], p[j + 1, :2] - p[j, :2]
                span = float(seg @ seg)
                t = 0.0 if span == 0 else min(1.0, max(0.0, float((np.asarray(at) - u) @ seg) / span))
                q = u + t * seg
                gap = math.hypot(q[0] - at[0], q[1] - at[1])
                if gap < best[0]:
                    best = (gap, j + t)
        out.append(best)
    return out


def _at(p: np.ndarray, s: float) -> np.ndarray:
    i = min(int(s), len(p) - 2)
    return p[i, :2] + (s - i) * (p[i + 1, :2] - p[i, :2])


def piece(conn, track: str, a: tuple[float, float], b: tuple[float, float], near_m: float) -> dict | None:
    """The piece of a track from where it passes one point to where it passes the other (EPSG:25833), unbroken. Where
    it passes either more than once (out and back), the piece nearest `near_m` long. None where it passes not both."""
    got = conn.execute("""SELECT ST_AsEWKB(l.geom) FROM ops.track t JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key
                          WHERE t.id = %s::uuid""", (track,)).fetchone() if _uuid(track) else None
    if not got:
        raise NotFound("no such track")
    best = None
    for p in _parts_of(got[0]):
        along = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p[:, :2], axis=0).T))]
        def metres(s: float, along: np.ndarray = along) -> float:
            i = min(int(s), len(along) - 2)
            return float(along[i] + (s - i) * (along[i + 1] - along[i]))

        for da, sa in _passes(p, a, REACH_M):
            for db, sb in _passes(p, b, REACH_M):
                length = abs(metres(sb) - metres(sa))
                if length < 1:
                    continue
                score = abs(length - near_m)
                if best is None or score < best[0]:
                    best = (score, p, sa, sb, da, db, length)
    if best is None:
        return None
    _, p, sa, sb, da, db, length = best
    lo, hi = sorted((sa, sb))
    inner = p[math.floor(lo) + 1:math.ceil(hi), :2]
    line = np.vstack([_at(p, lo), inner, _at(p, hi)])
    if sa > sb:
        line = line[::-1]
    return {"coords": [tuple(a), *map(tuple, line.tolist()), tuple(b)], "ends_m": [round(da), round(db)], "on_track_m": length}


# --- against the height model and Naismith's rule ------------------------------------------------------------------

def _resampled(p: np.ndarray, step: float) -> np.ndarray:
    """A part every `step` metres along it (and its last point): x, y, the device's height, time, metres along."""
    along = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p[:, :2], axis=0).T))]
    at = np.r_[np.arange(0.0, along[-1], step), along[-1]]
    return np.column_stack([np.interp(at, along, p[:, k]) for k in range(4)] + [at])


def check(conn, track: str, http=None) -> dict:
    """A track cleaned against Kartverket's height model along the same way, every 10 m (as the routes are): the
    climb by the device and by the model, how far apart their heights are, and the time Naismith's rule gives
    (with Langmuir's addition for steep descents, as for the routes) against the time walking. Asks the height
    service; writes nothing."""
    from stiflyt2 import height

    t = one(conn, track)
    if not (t["has_time"] and t["has_height"]):
        raise Invalid("only a track with the device's times and heights can be checked against them")
    got = conn.execute("""SELECT l.key, ST_AsEWKB(l.geom) FROM ops.track t JOIN ops.track_line l ON l.track_id = t.id
                          AND l.key = t.line_key WHERE t.id = %s::uuid""", (track,)).fetchone()
    if not got or not got[1]:
        raise Invalid("nothing is left of the track to check")
    own = http is None
    http = http or height.client()
    try:
        up = down = dev_up = dev_down = steep_down = metres = 0.0
        diffs, ice = [], 0
        for p in _parts_of(got[1]):
            r = _resampled(p, height.STEP_M)
            said = height.ask(http, [(round(x, 2), round(y, 2)) for x, y in r[:, :2]])
            model = np.array([math.nan if z is None else z for z, _, _ in said])
            ice += sum(1 for _, ground, _ in said if ground == height.ICE)
            known = ~np.isnan(model)
            if known.sum() < 2:
                continue
            u, d = height.climb(model[known].tolist())
            du, dd = height.climb(r[known, 2].tolist())
            _, sd = height.steep(list(zip(r[known, 4].tolist(), model[known].tolist(), strict=True)))
            up, down, dev_up, dev_down, steep_down = up + u, down + d, dev_up + du, dev_down + dd, steep_down + sd
            metres += float(r[-1, 4])
            diffs.append(r[known, 2] - model[known])
    finally:
        if own:
            http.close()
    if not diffs:
        raise Invalid("the height model has no heights along this track")
    gap = np.concatenate(diffs)
    rule = height.minutes(metres, up, steep_down)
    walking = (t["moving_s"] or 0) / 60
    return {"key": got[0], "metres": round(metres), "model_up_m": round(up), "model_down_m": round(down),
            "steep_down_m": round(steep_down), "device_up_m": round(dev_up), "device_down_m": round(dev_down),
            "device_above_m": round(float(np.median(gap)), 1), "device_spread_m": round(float(np.std(gap)), 1),
            "naismith_min": rule, "walking_min": round(walking), "all_min": round((t["total_s"] or 0) / 60),
            "against_rule": round(walking / rule, 3) if rule else None, "ice_points": ice,
            "checked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}


def keep_check(conn, track: str, told: dict) -> None:
    """Keep what a check found, with the line it was made of: a line cut anew is checked anew."""
    conn.execute("UPDATE ops.track_line SET checked = %s WHERE track_id = %s::uuid AND key = %s",
                 (Jsonb({k: v for k, v in told.items() if k != "key"}), track, told["key"]))


def unchecked(conn) -> list[str]:
    """The tracks with times and heights whose cleaned line has not been checked."""
    return [r[0] for r in conn.execute("""
        SELECT t.id::text FROM ops.track t JOIN ops.track_raw r ON r.id = t.raw_id
        JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key
        WHERE r.has_time AND r.has_height AND l.geom IS NOT NULL AND l.checked IS NULL ORDER BY t.made_at""").fetchall()]


def compared(conn, area: str | None = None) -> dict:
    """What the checked tracks of an area (or all of them) come to together: the time walking against Naismith's
    rule, and the device's climb against the height model's, each over all of them and track by track."""
    rows = [t for t in (listing(conn, area) if area else listing(conn)) if t.get("checked")]
    if not rows:
        return {"tracks": 0}
    c = [t["checked"] for t in rows]
    rule, walking = sum(x["naismith_min"] for x in c), sum(x["walking_min"] for x in c)
    model, device = sum(x["model_up_m"] for x in c), sum(x["device_up_m"] for x in c)
    ratios = sorted(x["against_rule"] for x in c if x["against_rule"])
    return {"tracks": len(rows), "naismith_min": rule, "walking_min": walking,
            "against_rule": round(walking / rule, 3) if rule else None,
            "against_rule_least": ratios[0] if ratios else None, "against_rule_most": ratios[-1] if ratios else None,
            "model_up_m": model, "device_up_m": device, "device_against_model": round(device / model, 3) if model else None,
            "metres": sum(x["metres"] for x in c)}


# --- files -----------------------------------------------------------------------------------------------------------

def file(conn, track: str) -> tuple[Path, str]:
    """The file a track came as, and the name to give it."""
    t = one(conn, track)
    sha = conn.execute("SELECT r.sha256, r.ext FROM ops.track t JOIN ops.track_raw r ON r.id = t.raw_id WHERE t.id = %s::uuid",
                       (track,)).fetchone()
    if not sha[1]:
        raise NotFound("this track came as a line, with no file")
    where = path(sha[0])
    if not where.exists():
        raise NotFound("the track's file is not on this server")
    return where, f"{_safe(t['name'])}.gpx"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()[:80] or "track"


def gpx(conn, track: str) -> tuple[bytes, str]:
    """The track cleaned, as a GPX file: its parts as segments, with heights and times where it has them."""
    t = one(conn, track)
    got = conn.execute("""SELECT ST_AsEWKB(ST_Transform(l.geom, 4326)) FROM ops.track t
                          JOIN ops.track_line l ON l.track_id = t.id AND l.key = t.line_key WHERE t.id = %s::uuid""",
                       (track,)).fetchone()
    root = ET.Element("gpx", {"version": "1.1", "creator": "Stiflyt v2", "xmlns": "http://www.topografix.com/GPX/1/1"})
    trk = ET.SubElement(root, "trk")
    ET.SubElement(trk, "name").text = t["name"]
    for part in _parts_of(got[0] if got else None):
        seg = ET.SubElement(trk, "trkseg")
        for lon, lat, z, m in part:
            pt = ET.SubElement(seg, "trkpt", {"lat": f"{lat:.7f}", "lon": f"{lon:.7f}"})
            if t["has_height"]:
                ET.SubElement(pt, "ele").text = f"{z:.1f}"
            if t["has_time"]:
                ET.SubElement(pt, "time").text = dt.datetime.fromtimestamp(m, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), f"{_safe(t['name'])} (cleaned).gpx"
