"""Looking things up across the sources and what is yours: nothing here changes anything.

What a person sees at a glance on the map, as answers that can be asked for:

- `turrutebasen`: everything that carries a route number, segment by segment
- `here`: what lies around a point, in every source and in what is yours
- `mapped_way`: the way over mapped paths and roads between points
- `shared` and `same_line`: routes that run on the same line
- `checks`: the odd things of an area that are easy to see and hard to count

Written for an assistant working through the API, and for the map.
"""
from __future__ import annotations

import itertools
import math
import re

from psycopg.rows import dict_row

from stiflyt2 import seasons
from stiflyt2.domain import routes, worklist
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.seasons import WINTER_CODE

HERE_M = 200            # how far around a point is looked, unless said
HERE_MAX_M = 2000
HERE_MOST = 12          # of each kind of thing, the nearest
NAMED_WITHIN_M = 2000   # a segment is said to lie by the nearest name this near
WAY_MARGIN_M = 1500     # mapped lines this far to the side of the straight line between two points are tried,
WAY_MARGIN = 0.3        # or this share of the distance between them, where that is more
SAME = 0.9              # two routes sharing this much of the longer one are the same line
SHORT_M = 100           # a route this short is more likely a stray number than a route
BACK_M = 6              # a line this near itself, on the way out and on the way back, is the same line walked twice:
                        # the two legs of a hairpin lie further apart (15 m and more, by jot60 and jot73)
BACK_LEAST_M = 60       # for at least this far each way
BACK_STEP_M = 10
GAP_AT_JUNCTION_M = 1.0 # a stretch may stop this far from its junction (the tolerance segments were joined with)
BY_GLACIER_M = 300      # a glacier's name this near a stretch in terrain
POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"
FOLLOWS = {"ST": "path", "BV": "road", "TR": "tractor road", "SB": "forest road", "GS": "foot and cycle way",
           "FO": "pavement", "IT": "in terrain", "VV": "water"}
NOT_A_MAINTAINER = re.compile(r"\b(test|dummy|prøve)", re.IGNORECASE)
JOINED_M = 2            # legs whose ends lie this near each other are walked on from one to the next
_PADDED = re.compile(r"^([a-z]{3})0+([1-9][0-9]*[a-z]?)$")


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0]


def _waiting(conn, area: str | None = None, *, season: str = seasons.SUMMER) -> list[dict]:
    """Turrutebasen's candidates of the season asked for that are neither yours nor set aside by a person."""
    aside = routes.withdrawn_by_a_person(conn) | set(routes.taken_out_by_a_person(conn))
    candidates = seasons.CANDIDATES[seasons.known(season)].route
    found = _rows(conn, f"""
        SELECT r.id, r.code, r.label, r.maintainers, r.length_m, ST_X(c.p) AS lon, ST_Y(c.p) AS lat
        FROM source_index.{candidates} r, LATERAL (SELECT ST_Transform(ST_PointOnSurface(r.geom), 4326) AS p) c
        WHERE %(a)s::text IS NULL OR r.area_prefix = %(a)s ORDER BY length(r.code), r.label""", {"a": area})
    return [c for c in found if c["label"] not in aside and not routes.owner_of(conn, c["id"])]


def turrutebasen(conn, code: str) -> dict:
    """Everything in the loaded Turrutebasen that carries a route number: its candidates and its segments.

    Each segment with what Turrutebasen says of it, the name it lies by, and
    the routes of yours that run over it.
    """
    code = code.strip().lower()
    if re.match(WINTER_CODE, code):
        return _winter(conn, code)
    candidates = _rows(conn, """
        SELECT r.id, r.label, r.name, r.maintainers, round(r.length_m)::int AS length_m,
               r.segment_count AS segments, s.shape, s.pieces, s.ends
        FROM source_index.route r LEFT JOIN source_index.route_shape s ON s.route_id = r.id
        WHERE r.code = %(c)s ORDER BY r.label""", {"c": code})
    if not candidates:
        raise NotFound(f"nothing in the loaded Turrutebasen carries the number {code}")
    aside = routes.withdrawn_by_a_person(conn) | set(routes.taken_out_by_a_person(conn))
    for c in candidates:
        yours = routes.owner_of(conn, c["id"])
        c["yours"] = yours and {"id": yours, "code": conn.execute(
            "SELECT code FROM core.route WHERE id = %s", (yours,)).fetchone()[0]}
        c["set_aside"] = c["label"] in aside and not yours
    segments = _rows(conn, """
        SELECT t.id, r.label AS candidate, round(t.length_m)::int AS length_m, t.codes, t.marked,
               t.attrs->>'skilting' = 'JA' AS signed, t.attrs->>'rutefolger' AS rutefolger, t.maintainers,
               t.updated, ST_X(m.ll) AS lon, ST_Y(m.ll) AS lat, n.name AS by_name, n.type AS by_type,
               round(n.metres)::int AS by_m,
               (SELECT coalesce(json_agg(DISTINCT jsonb_build_object('id', y.id, 'code', y.code)), '[]')
                FROM core.source_reference ad JOIN core.edge e ON e.id = ad.subject_id AND e.status = 'active'
                JOIN core.route_leg l ON l.edge_id = e.id
                JOIN core.route y ON y.id = l.route_id AND y.status = 'active'
                WHERE ad.subject_type = 'edge' AND ad.source_key = 'turrutebasen'
                  AND ad.external_id = t.id) AS yours
        FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
        JOIN source_index.trail_segment t ON t.id = rs.segment_id,
             LATERAL (SELECT ST_ClosestPoint(t.geom, ST_Centroid(t.geom)) AS p) c,
             LATERAL (SELECT ST_Transform(c.p, 4326) AS ll) m
        LEFT JOIN LATERAL (SELECT p.name, p.type, ST_Distance(p.geom, c.p) AS metres
                           FROM source_index.place p WHERE p.alternative IS NOT TRUE
                           ORDER BY p.geom <-> c.p LIMIT 1) n ON n.metres <= %(near)s
        WHERE r.code = %(c)s ORDER BY r.label, ST_XMin(t.geom)""", {"c": code, "near": NAMED_WITHIN_M})
    for s in segments:
        s["follows"] = FOLLOWS.get(s["rutefolger"])
    return {"code": code, "candidates": candidates, "segments": segments,
            "length_m": sum(s["length_m"] for s in segments)}


def _winter(conn, code: str) -> dict:
    """A winter route of DNT's in Turrutebasen (among its ski trails): its candidates and its segments, with when
    each was drawn. The line is where it was staked when it was drawn: it is staked anew each winter."""
    candidates = _rows(conn, """
        SELECT r.id, r.label, r.name, r.maintainers, round(r.length_m)::int AS length_m, r.segment_count AS segments,
               extract(year FROM r.captured_from)::int AS drawn_from, extract(year FROM r.captured_to)::int AS drawn_to
        FROM source_index.winter_route r WHERE r.code = %(c)s ORDER BY r.label""", {"c": code})
    if not candidates:
        raise NotFound(f"nothing in the loaded Turrutebasen carries the number {code}, among the winter routes either")
    for c in candidates:
        yours = routes.owner_of(conn, c["id"])
        c["yours"] = yours and {"id": yours, "code": conn.execute(
            "SELECT code FROM core.route WHERE id = %s", (yours,)).fetchone()[0]}
    segments = _rows(conn, """
        SELECT t.id, r.label AS candidate, round(t.length_m)::int AS length_m, t.codes, t.marking, t.preparation,
               t.maintainers, extract(year FROM t.captured)::int AS drawn, t.updated,
               ST_X(ST_Transform(ST_ClosestPoint(t.geom, ST_Centroid(t.geom)), 4326)) AS lon,
               ST_Y(ST_Transform(ST_ClosestPoint(t.geom, ST_Centroid(t.geom)), 4326)) AS lat
        FROM source_index.winter_route r JOIN source_index.winter_route_segment rs ON rs.route_id = r.id
        JOIN source_index.winter_segment t ON t.id = rs.segment_id
        WHERE r.code = %(c)s ORDER BY r.label, ST_XMin(t.geom)""", {"c": code})
    return {"code": code, "winter": True, "marking": {"SM": "staked for the season", "JA": "marked"},
            "candidates": candidates, "segments": segments, "length_m": sum(s["length_m"] for s in segments)}


def here(conn, lon: float, lat: float, radius_m: float = HERE_M, *, season: str | None = None) -> dict:
    """What lies around a point, nearest first: names, mapped paths and roads, Turrutebasen, and what is yours.

    Each stretch says its season. Given a season, only the stretches of that season (and the connectors of places,
    which serve both), Turrutebasen's segments of that season, and at each junction its stretches of that season;
    else every stretch, and Turrutebasen's summer segments.
    """
    if not 0 < radius_m <= HERE_MAX_M:
        raise Invalid(f"look between 1 and {HERE_MAX_M} m around the point")
    if season is not None:
        season = seasons.known(season)
    at = {"lon": lon, "lat": lat, "r": radius_m, "most": HERE_MOST, "season": season}
    found = {"lon": lon, "lat": lat, "radius_m": radius_m}
    found["names"] = _rows(conn, f"""
        SELECT name, type, metres FROM (
            SELECT DISTINCT ON (p.place_id) p.name, p.type, round(ST_Distance(p.geom, {POINT}))::int AS metres
            FROM source_index.place p WHERE ST_DWithin(p.geom, {POINT}, %(r)s)
            ORDER BY p.place_id, p.alternative, p.name) n
        ORDER BY metres, name LIMIT %(most)s""", at)
    mapped = f"""SELECT 'path' AS source, id, kind, NULL AS name, round(length_m)::int AS length_m,
                        round(ST_Distance(geom, {POINT}))::int AS metres
                 FROM source_index.path WHERE ST_DWithin(geom, {POINT}, %(r)s)"""
    if _has(conn, "source_index.road"):
        mapped += f""" UNION ALL
                 SELECT 'road', id, kind, name, round(length_m)::int, round(ST_Distance(geom, {POINT}))::int
                 FROM source_index.road WHERE ST_DWithin(geom, {POINT}, %(r)s)"""
    found["mapped"] = _rows(conn, f"SELECT * FROM ({mapped}) m ORDER BY metres, id LIMIT %(most)s", at)
    # A winter segment is marked, or staked for the season (SM).
    marked = "t.marking IN ('JA', 'SM') AS marked" if season == seasons.WINTER else "t.marked"
    found["turrutebasen"] = _rows(conn, f"""
        SELECT t.id, t.codes, {marked}, t.attrs->>'rutefolger' AS rutefolger, t.maintainers,
               round(t.length_m)::int AS length_m, round(ST_Distance(t.geom, {POINT}))::int AS metres
        FROM source_index.{seasons.CANDIDATES[season or seasons.SUMMER].segment} t
        WHERE ST_DWithin(t.geom, {POINT}, %(r)s)
        ORDER BY metres, t.id LIMIT %(most)s""", at)
    of_season = "(%(season)s::text IS NULL OR e.season = %(season)s OR r.kind = 'connector')"
    found["stretches"] = _rows(conn, f"""
        SELECT e.id::text, e.season, r.kind, r.origin, r.marked, r.signed, round(ST_Length(r.geom))::int AS length_m,
               round(ST_Distance(r.geom, {POINT}))::int AS metres,
               (SELECT coalesce(json_agg(jsonb_build_object('id', y.id, 'code', y.code) ORDER BY y.code), '[]')
                FROM core.route_leg l JOIN core.route y ON y.id = l.route_id AND y.status = 'active'
                WHERE l.edge_id = e.id) AS routes
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.status = 'active' AND ST_DWithin(r.geom, {POINT}, %(r)s) AND {of_season}
        ORDER BY metres, e.id LIMIT %(most)s""", at)
    found["junctions"] = _rows(conn, f"""
        SELECT n.id::text, round(ST_Distance(n.geom, {POINT}))::int AS metres, p.name AS arrival_point_of,
               p.primary_node_id = n.id AS routes_meet_here,
               (SELECT count(*)::int FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE e.status = 'active' AND n.id IN (r.a_node, r.b_node) AND {of_season}) AS stretches
        FROM core.node n LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.status = 'active' AND ST_DWithin(n.geom, {POINT}, %(r)s)
        ORDER BY metres, n.id LIMIT %(most)s""", at)
    found["places"] = _rows(conn, f"""
        SELECT p.id::text, p.name, p.kind, p.reviewed, round(ST_Distance(p.geom, {POINT}))::int AS metres
        FROM core.place p WHERE p.status = 'confirmed' AND ST_DWithin(p.geom, {POINT}, %(r)s)
        ORDER BY metres, p.name LIMIT %(most)s""", at)
    for s in found["turrutebasen"]:
        s["follows"] = FOLLOWS.get(s["rutefolger"])
    return found


def mapped_way(conn, points: list[tuple[float, float]], route_id: str | None = None,
               reach_m: float = routes.MAPPED_REACH_M) -> dict:
    """The shortest way over mapped paths and roads through some points, without making anything.

    Says how long it is, how much of it is path and how much road, and, when
    a route of yours is named, how much of the way that route already follows.
    """
    if len(points) < 2:
        raise Invalid("give at least two points")
    xy = [tuple(conn.execute(
        f"SELECT ST_X(p), ST_Y(p) FROM (SELECT {POINT} AS p) x", {"lon": lon, "lat": lat}).fetchone())
        for lon, lat in points]
    # Asked first, and cheaply: the network between two points far apart takes seconds to put together.
    for n, (x, y) in enumerate(xy, start=1):
        near = conn.execute(f"""SELECT min(ST_Distance(m.geom, p.g)) FROM {routes.mapped_lines(conn)} m,
                                       (SELECT ST_SetSRID(ST_MakePoint(%s, %s), 25833) AS g) p
                                WHERE ST_DWithin(m.geom, p.g, %s)""", (x, y, reach_m)).fetchone()[0]
        if near is None:
            raise Invalid(f"no mapped path or road lies within {reach_m:.0f} m of point {n}")
    coords, by, legs = [], {"path": 0.0, "road": 0.0}, []
    for n, (a, b) in enumerate(itertools.pairwise(xy), start=1):
        paths = routes._Paths.near(conn, f"SRID=25833;LINESTRING({a[0]:.1f} {a[1]:.1f},{b[0]:.1f} {b[1]:.1f})",
                                   margin=max(WAY_MARGIN_M, WAY_MARGIN * math.dist(a, b)))
        found = paths.parts(paths.attach(a, reach_m), paths.attach(b, reach_m))
        if found is None:
            raise Invalid(f"the mapped paths and roads do not join between point {n} and point {n + 1}")
        metres = 0.0
        for part, (source, _) in found:
            along = routes._Paths.along(part)
            by["road" if source == "vegnett" else "path"] += along
            metres += along
            coords.extend(part if not coords else part[1:])
        legs.append({"from": n, "to": n + 1, "length_m": round(metres)})
    line = routes._line(conn, coords)
    way = {"length_m": line["length_m"], "on_paths_m": round(by["path"]), "on_roads_m": round(by["road"]),
           "legs": legs, "geometry": line["geometry"]}
    if route_id:
        along = conn.execute("""
            SELECT y.code, round(ST_Length(ST_Intersection(w.g, ST_Buffer(ST_Collect(r.geom), %(near)s))))::int
            FROM core.route y JOIN core.route_leg l ON l.route_id = y.id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 (SELECT ST_GeomFromText(%(wkt)s, 25833) AS g) w
            WHERE y.id = %(id)s AND y.status = 'active' GROUP BY y.code, w.g""",
            {"id": route_id, "wkt": line["wkt"], "near": routes.BESIDE_M}).fetchone()
        if along is None:
            raise NotFound("no such route")
        way["route"] = {"id": route_id, "code": along[0], "follows_m": along[1],
                        "share": round(along[1] / line["planar"], 2) if line["planar"] else 0.0}
    return way


_LEGS = """SELECT DISTINCT l.route_id, l.edge_id, ST_Length(r.geom) AS m
           FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
           JOIN core.edge_revision r ON r.id = e.current_revision_id
           WHERE l.route_id = ANY(%(ids)s::uuid[])"""


def shared(conn, a: str, b: str) -> dict:
    """What two routes of yours share: how far they run over the same stretches, and how far each alone."""
    known = dict(conn.execute(
        "SELECT id::text, code FROM core.route WHERE id = ANY(%s::uuid[]) AND status = 'active'", ([a, b],)).fetchall())
    if len(known) < 2:
        raise NotFound("no such route" if a != b else "give two different routes")
    one, other, both, stretches = conn.execute(f"""
        WITH legs AS ({_LEGS})
        SELECT coalesce(sum(m) FILTER (WHERE route_id = %(a)s), 0), coalesce(sum(m) FILTER (WHERE route_id = %(b)s), 0),
               coalesce((SELECT sum(x.m) FROM legs x JOIN legs y USING (edge_id)
                         WHERE x.route_id = %(a)s AND y.route_id = %(b)s), 0),
               (SELECT count(*) FROM legs x JOIN legs y USING (edge_id)
                WHERE x.route_id = %(a)s AND y.route_id = %(b)s)
        FROM legs""", {"ids": [a, b], "a": a, "b": b}).fetchone()
    return {"a": {"id": a, "code": known[a], "length_m": round(one), "alone_m": round(one - both)},
            "b": {"id": b, "code": known[b], "length_m": round(other), "alone_m": round(other - both)},
            "shared_m": round(both), "shared_stretches": stretches,
            "same_line": bool(max(one, other)) and both >= SAME * max(one, other)}


def same_line(conn, area: str, *, season: str = seasons.SUMMER) -> list[dict]:
    """Routes of an area, of the season asked for, that are one line under two numbers: yours in use, and
    candidates that wait.

    Two routes that share nearly all of the longer one. A short route along a
    part of a long one is not this: that is what routes do.
    """
    season = seasons.known(season)
    tables = seasons.CANDIDATES[season]
    mine = worklist.routes_of(conn, area, season=season)
    found = [{"of": "yours", "shared_m": round(m), "routes": [
                {"id": a, "code": ca, "length_m": round(ma)}, {"id": b, "code": cb, "length_m": round(mb)}]}
             for a, ca, ma, b, cb, mb, m in conn.execute(f"""
        WITH legs AS ({_LEGS}), total AS (SELECT route_id, sum(m) AS m FROM legs GROUP BY 1)
        SELECT x.route_id::text, rx.code, tx.m, y.route_id::text, ry.code, ty.m, sum(x.m)
        FROM legs x JOIN legs y ON y.edge_id = x.edge_id AND y.route_id <> x.route_id
        JOIN core.route rx ON rx.id = x.route_id JOIN core.route ry ON ry.id = y.route_id
        JOIN total tx ON tx.route_id = x.route_id JOIN total ty ON ty.route_id = y.route_id
        WHERE (length(rx.code), rx.code, rx.id) < (length(ry.code), ry.code, ry.id)
        GROUP BY 1, 2, 3, 4, 5, 6 HAVING sum(x.m) >= %(same)s * greatest(tx.m, ty.m)
        ORDER BY length(rx.code), rx.code, ry.code""", {"ids": mine, "same": SAME})]
    waiting = {c["id"]: c for c in _waiting(conn, area, season=season)}
    for a, la, ma, b, lb, mb, m in conn.execute(f"""
            WITH legs AS (SELECT rs.route_id, rs.segment_id, t.length_m AS m, r.label, r.length_m AS whole
                          FROM source_index.{tables.route} r JOIN source_index.{tables.route_segment} rs ON rs.route_id = r.id
                          JOIN source_index.{tables.segment} t ON t.id = rs.segment_id
                          WHERE r.area_prefix = %(a)s)
            SELECT x.route_id, x.label, x.whole, y.route_id, y.label, y.whole, sum(x.m)
            FROM legs x JOIN legs y ON y.segment_id = x.segment_id
                 AND (length(x.label), x.label) < (length(y.label), y.label)
            GROUP BY 1, 2, 3, 4, 5, 6 HAVING sum(x.m) >= %(same)s * greatest(x.whole, y.whole)
            ORDER BY length(x.label), x.label, y.label""", {"a": area.lower(), "same": SAME}):
        if a in waiting or b in waiting:    # both yours already: judged above, as they are now
            found.append({"of": "turrutebasen", "shared_m": round(m), "routes": [
                {"id": a, "code": la, "length_m": round(ma), "waits": a in waiting},
                {"id": b, "code": lb, "length_m": round(mb), "waits": b in waiting}]})
    return found


def _doubles_back(coords: list[tuple[float, float]]) -> list[tuple[tuple[float, float], int]]:
    """Where a line turns and runs back along itself: (the turning point, metres each way)."""
    points, left = [coords[0]], BACK_STEP_M     # a point every step along the line
    for p, q in itertools.pairwise(coords):
        span, done = math.dist(p, q), 0.0
        while span - done >= left:
            done += left
            points.append((p[0] + (q[0] - p[0]) * done / span, p[1] + (q[1] - p[1]) * done / span))
            left = BACK_STEP_M
        left -= span - done
    found, k = [], 1
    while k < len(points) - 1:
        t = 1
        while k - t >= 0 and k + t < len(points) and math.dist(points[k - t], points[k + t]) <= BACK_M:
            t += 1
        if (t - 1) * BACK_STEP_M >= BACK_LEAST_M:
            found.append((points[k], (t - 1) * BACK_STEP_M))
            k += t
        else:
            k += 1
    return found


def checks(conn, area: str, *, season: str = seasons.SUMMER) -> dict:
    """The odd things of an area, among its routes of the season asked for: easy to see on a map, and until now
    only found by someone looking.

    Each is a sentence about one or two routes, with a spot on the map where
    there is one. None of them is certainly wrong: they are where to look.
    """
    area = area.lower()
    season = seasons.known(season)
    mine, found = worklist.routes_of(conn, area, season=season), []

    def tell(kind: str, text: str, about: list[dict], at: tuple | None = None) -> None:
        found.append({"kind": kind, "text": text, "routes": about,
                      "lon": at and round(at[0], 6), "lat": at and round(at[1], 6)})

    for pair in same_line(conn, area, season=season):
        a, b = pair["routes"]
        tell("same line", f"{a['code']} and {b['code']} are the same line for {pair['shared_m'] / 1000:.1f} km"
             + (" in Turrutebasen" if pair["of"] == "turrutebasen" else "")
             + f", of {a['length_m'] / 1000:.1f} and {b['length_m'] / 1000:.1f} km: one of them goes another way,"
               " or is a number too many",
             [{"id": r["id"], "code": r["code"], "yours": pair["of"] == "yours"} for r in (a, b)])

    lines = _rows(conn, """
        SELECT y.id::text, y.code, sum(ST_Length(r.geom)) AS metres,
               y.maintainers || coalesce((SELECT array_agg(DISTINCT m) FROM core.route_leg o, unnest(o.maintainers) m
                                          WHERE o.route_id = y.id), '{}') AS maintainers,
               json_agg(ST_AsGeoJSON(CASE WHEN l.reversed THEN ST_Reverse(r.geom) ELSE r.geom END, 1)::json
                        ->'coordinates' ORDER BY l.seq) AS legs
        FROM core.route y JOIN core.route_leg l ON l.route_id = y.id JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE y.id = ANY(%(ids)s::uuid[]) GROUP BY y.id ORDER BY length(y.code), y.code""", {"ids": mine})

    def lonlat(x: float, y: float) -> tuple:
        return conn.execute("SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s),"
                            " 25833), 4326) AS p) x", (x, y)).fetchone()

    codes = {r["code"].lower() for r in lines}
    waiting = _waiting(conn, area, season=season)
    for c in waiting:
        codes.add(c["code"])
    for r in lines:
        about = [{"id": r["id"], "code": r["code"], "yours": True}]
        parts = []      # the route as it is walked: its legs in order, one line as long as each starts where the last ended
        for leg in r["legs"]:
            if parts and math.dist(parts[-1][-1], leg[0]) <= JOINED_M:
                parts[-1].extend(leg[1:])
            else:
                parts.append(list(leg))
        if r["metres"] < SHORT_M:
            tell("short", f"{r['code']} is {r['metres']:.0f} m long: a route, or a number on a stray bit of line?",
                 about, lonlat(*parts[0][0][:2]))
        for part in parts:
            for at, metres in _doubles_back([tuple(c[:2]) for c in part]):
                tell("out and back", f"{r['code']} goes {metres} m out and the same way back", about, lonlat(*at))
        odd = sorted({m for m in r["maintainers"] if NOT_A_MAINTAINER.search(m)})
        if odd:
            tell("maintainer", f"{r['code']} is said to be maintained by {' and '.join(odd)}", about,
                 lonlat(*parts[0][0][:2]))
        twin = _PADDED.match(r["code"].lower())
        if twin and "".join(twin.groups()) in codes:
            tell("code", f"{r['code']} looks like a misspelling of {''.join(twin.groups())}, which is a route too",
                 about, lonlat(*parts[0][0][:2]))
    for c in waiting:
        about, at = [{"id": c["id"], "code": c["label"], "yours": False}], (c["lon"], c["lat"])
        if c["length_m"] < SHORT_M:
            tell("short", f"{c['label']} is {c['length_m']:.0f} m long in Turrutebasen: a route, or a number on a"
                          " stray bit of line?", about, at)
        odd = sorted(m for m in c["maintainers"] if NOT_A_MAINTAINER.search(m))
        if odd:
            tell("maintainer", f"{c['label']} is said to be maintained by {' and '.join(odd)} in Turrutebasen",
                 about, at)
        twin = _PADDED.match(c["code"])
        if twin and "".join(twin.groups()) in codes:
            tell("code", f"{c['label']} looks like a misspelling of {''.join(twin.groups())}, which is a route too",
                 about, at)

    # A stretch that Turrutebasen has in terrain or unmarked, by a glacier's name, and that is not held as glacier.
    # Turrutebasen's foot routes: of the summer's routes only.
    for row in _rows(conn, """
            SELECT DISTINCT ON (e.id) y.id::text, y.code, g.name, round(ST_Length(r.geom))::int AS metres,
                   ST_X(c.p) AS lon, ST_Y(c.p) AS lat
            FROM core.route_leg l JOIN core.route y ON y.id = l.route_id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.source_reference ad ON ad.subject_type = 'edge' AND ad.subject_id = e.id
                 AND ad.source_key = 'turrutebasen'
            JOIN source_index.trail_segment t ON t.id = ad.external_id
                 AND (NOT t.marked OR t.attrs->>'rutefolger' = 'IT')
            JOIN LATERAL (SELECT p.name FROM source_index.place p
                          WHERE p.type = 'isbre' AND ST_DWithin(p.geom, r.geom, %(near)s)
                          ORDER BY p.geom <-> r.geom LIMIT 1) g ON true,
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p) c
            WHERE l.route_id = ANY(%(ids)s::uuid[]) AND y.season = %(summer)s AND r.kind NOT IN ('glacier', 'water')
              AND ST_Length(r.geom) > %(near)s
            ORDER BY e.id, y.code""", {"ids": mine, "near": BY_GLACIER_M, "summer": seasons.SUMMER}):
        tell("glacier", f"{row['metres']} m of {row['code']} is unmarked or in terrain by {row['name']} and is not "
                        "said to be glacier", [{"id": row["id"], "code": row["code"], "yours": True}],
             (row["lon"], row["lat"]))
    # A stretch that stops short of the junction it hangs in: the line of the route has a hole there.
    for row in _rows(conn, """
            SELECT DISTINCT ON (e.id) y.id::text, y.code, round(d.m::numeric, 1)::float8 AS metres,
                   ST_X(c.p) AS lon, ST_Y(c.p) AS lat
            FROM core.route_leg l JOIN core.route y ON y.id = l.route_id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node,
                 LATERAL (SELECT greatest(ST_Distance(ST_StartPoint(r.geom), a.geom),
                                          ST_Distance(ST_EndPoint(r.geom), b.geom)) AS m,
                                 CASE WHEN ST_Distance(ST_StartPoint(r.geom), a.geom)
                                           >= ST_Distance(ST_EndPoint(r.geom), b.geom) THEN a.geom ELSE b.geom END AS g) d,
                 LATERAL (SELECT ST_Transform(d.g, 4326) AS p) c
            WHERE l.route_id = ANY(%(ids)s::uuid[]) AND d.m > %(gap)s
            ORDER BY e.id, y.code""", {"ids": mine, "gap": GAP_AT_JUNCTION_M}):
        tell("gap at a junction", f"A stretch of {row['code']} stops {row['metres']} m short of the junction it "
                                  "hangs in: the line has a hole there",
             [{"id": row["id"], "code": row["code"], "yours": True}], (row["lon"], row["lat"]))

    # Two places with one name, on the routes of the area: the same place made twice, or two to tell apart.
    for row in _rows(conn, """
            WITH here AS (
                SELECT DISTINCT p.id, p.name, p.kind, p.geom FROM core.route_leg l
                JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
                JOIN core.node n ON n.id IN (r.a_node, r.b_node)
                JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
                WHERE l.route_id = ANY(%(ids)s::uuid[]))
            SELECT DISTINCT ON (lower(h.name)) h.name, round(ST_Distance(h.geom, o.geom))::int AS metres,
                   ST_X(ST_Transform(h.geom, 4326)) AS lon, ST_Y(ST_Transform(h.geom, 4326)) AS lat
            FROM here h JOIN core.place o ON o.status = 'confirmed' AND lower(o.name) = lower(h.name) AND o.id <> h.id
            ORDER BY lower(h.name), h.id""", {"ids": mine}):
        tell("place name", f"Two places are called {row['name']}, {row['metres']} m apart: one place made twice, "
                           "or two to tell apart by name?", [], (row["lon"], row["lat"]))
    order = ("same line", "short", "code", "maintainer", "out and back", "glacier", "gap at a junction", "place name")
    found.sort(key=lambda f: order.index(f["kind"]))
    return {"area": area, "checks": found, "counts": {k: sum(f["kind"] == k for f in found) for k in order}}
