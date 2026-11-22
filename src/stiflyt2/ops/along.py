"""Where on a route something is: positions along our own stretches, as issues and reports keep them (ADR-004).

A position is a stretch and how far along it, from 0 at its first end to 1 at
its last. A part of a route is from one position to another, walked as the
route walks. Nothing of a line is copied: what lies along a stretch is drawn
along its line as it is now, and found again on the part it lies on when the
stretch is divided (`follow`). Where it is in metres from a route's start is
worked out from the route as it is, when it is shown.
"""
from __future__ import annotations

import uuid

from psycopg.rows import dict_row

from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import anchor

ON_ROUTE_M = 50                         # a click this near a route's line is on it
MOST_PARTS = 20
_POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


class Route:
    """A route's stretches in walking order, to say where along it a position is, and back."""

    def __init__(self, conn, route: str) -> None:
        self.id = route
        self.legs = _rows(conn, """
            SELECT l.seq, l.edge_id::text AS edge, l.reversed, ST_Length(v.geom) AS m
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
            WHERE l.route_id = %(r)s ORDER BY l.seq""", {"r": route})
        start = 0.0
        for leg in self.legs:
            leg["start"] = start
            start += leg["m"]
        self.metres = start

    def index(self, edge: str, near_m: float | None = None) -> int | None:
        """The leg that walks a stretch: of several (there and back), the one nearest `near_m` along the route."""
        found = [i for i, leg in enumerate(self.legs) if leg["edge"] == edge]
        if not found:
            return None
        return min(found, key=lambda i: abs(self.legs[i]["start"] - near_m)) if near_m is not None else found[0]

    def metres_at(self, pos: dict, near_m: float | None = None) -> float | None:
        """How far from the route's start a position along a stretch is. None when the route does not walk it."""
        i = self.index(pos["edge"], near_m)
        if i is None:
            return None
        leg = self.legs[i]
        return leg["start"] + leg["m"] * ((1 - pos["at"]) if leg["reversed"] else pos["at"])

    def at_metres(self, metres: float) -> dict:
        """The position along a stretch that lies this far from the route's start."""
        if not self.legs:
            raise Invalid("the route has no stretches")
        if not 0 <= metres <= self.metres + 0.5:
            raise Invalid(f"the route is {self.metres / 1000:.1f} km long: say a place on it from 0 to there")
        leg = next((x for x in self.legs if x["start"] <= metres <= x["start"] + x["m"]), self.legs[-1])
        walked = min(max((metres - leg["start"]) / leg["m"], 0.0), 1.0) if leg["m"] else 0.0
        return {"edge": leg["edge"], "at": round(1 - walked if leg["reversed"] else walked, 6)}

    def walked(self, a: dict, b: dict) -> list[tuple[int, float, float]] | None:
        """The legs from one position to another, and how much of each, as walked: (seq, from, to). In the order
        the route walks them, whichever of the two was given first. None when the route does not walk both."""
        ma = self.metres_at(a)
        mb = self.metres_at(b, ma)
        if ma is None or mb is None:
            return None
        if mb < ma:
            ma, mb = mb, ma
        cut = []
        for leg in self.legs:
            lo, hi = leg["start"], leg["start"] + leg["m"]
            if hi <= ma or lo >= mb or not leg["m"]:
                continue
            cut.append((leg["seq"], (max(ma, lo) - lo) / leg["m"], (min(mb, hi) - lo) / leg["m"]))
        return cut


def on(conn, routes: list[str] | None, lon: float, lat: float) -> dict:
    """The stretch of these routes (None: of any route in use) nearest a clicked point, and where along it."""
    found = _rows(conn, f"""
        SELECT e.id::text AS edge, l.route_id::text AS route, ST_LineLocatePoint(v.geom, q.p) AS at,
               ST_Distance(v.geom, q.p) AS off
        FROM core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id,
             (SELECT {_POINT} AS p) q
        WHERE (%(routes)s::uuid[] IS NULL OR l.route_id = ANY(%(routes)s::uuid[]))
          AND ST_DWithin(v.geom, q.p, %(near)s)
        ORDER BY off, r.discontinued_since IS NOT NULL LIMIT 1""",
                  {"lon": lon, "lat": lat, "routes": routes, "near": ON_ROUTE_M})
    if not found:
        raise Invalid(f"that is not on {'the route' if routes else 'a route in use'}: click within {ON_ROUTE_M} m of its line")
    return {"edge": found[0]["edge"], "at": round(found[0]["at"], 6), "route": found[0]["route"]}


def position(conn, route: Route, said: dict, end: str = "") -> dict:
    """A position on a route, given as a point clicked (`lon`, `lat`) or as metres from its start (`m`)."""
    point, metres = said.get(f"{end}point" if end else "point"), said.get(f"{end}m" if end else "m")
    if point is not None:
        pos = on(conn, [route.id], point[0], point[1])
        return {"edge": pos["edge"], "at": pos["at"]}
    if metres is not None:
        return route.at_metres(float(metres))
    raise Invalid("say where on the route: a point on it, or how far from its start")


def edges(along: list) -> list[str]:
    return list(dict.fromkeys(p["edge"] for x in along for p in ([x] if "edge" in x else [x["from"], x["to"]])))


def follow(conn, positions: list[dict]) -> list[dict | None]:
    """Each position along a stretch, along what stands for the stretch now: itself, as its line is now; or where
    the same spot lies on the part it was divided into, or on the stretch it was joined into. None: gone."""
    edges = anchor.stands_for(conn, "edge", {p["edge"] for p in positions})
    told = []
    for p in positions:
        now = edges.get(p["edge"], [])
        if now == [p["edge"]] or not now:
            told.append(p if now else None)
            continue
        found = conn.execute("""
            WITH was AS (SELECT ST_LineInterpolatePoint(v.geom, %(at)s) AS p FROM core.edge e
                         JOIN core.edge_revision v ON v.id = e.current_revision_id WHERE e.id = %(e)s)
            SELECT e.id::text, ST_LineLocatePoint(v.geom, was.p) FROM core.edge e
            JOIN core.edge_revision v ON v.id = e.current_revision_id, was
            WHERE e.id = ANY(%(now)s::uuid[]) ORDER BY ST_Distance(v.geom, was.p) LIMIT 1""",
            {"at": p["at"], "e": p["edge"], "now": now}).fetchone()
        told.append({"edge": found[0], "at": round(found[1], 6)} if found else None)
    return told


def drawn(conn, route: Route, along: list) -> tuple[list, list, list]:
    """The parts as lines along the route as it is now, where each begins and ends in metres from its start, and the
    stretches they cover."""
    lines, said, covered = [], [], []
    by_seq = {leg["seq"]: leg["edge"] for leg in route.legs}
    for x in along:
        cut = route.walked(x["from"], x["to"])
        if not cut:
            continue
        covered += [by_seq[c[0]] for c in cut]
        ma = route.metres_at(x["from"])
        mb = route.metres_at(x["to"], ma)
        said.append({"from_m": round(min(ma, mb)), "to_m": round(max(ma, mb)), "metres": round(abs(mb - ma))})
        for (coords,) in conn.execute("""
                SELECT ST_AsGeoJSON(ST_Transform((ST_Dump(ST_LineMerge(ST_Collect(piece ORDER BY seq)))).geom, 4326), 6)::json -> 'coordinates'
                FROM (SELECT c.seq, ST_LineSubstring(CASE WHEN l.reversed THEN ST_Reverse(v.geom) ELSE v.geom END, c.lo, c.hi) AS piece
                      FROM unnest(%(seq)s::int[], %(lo)s::float8[], %(hi)s::float8[]) c(seq, lo, hi)
                      JOIN core.route_leg l ON l.route_id = %(r)s AND l.seq = c.seq
                      JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
                      WHERE c.hi > c.lo) p""",
                {"seq": [c[0] for c in cut], "lo": [c[1] for c in cut], "hi": [c[2] for c in cut], "r": route.id}):
            lines.append(coords)
    return lines, said, list(dict.fromkeys(covered))


def in_use(conn, route: str) -> dict:
    """A route of ours in use, by its id: its id and code."""
    try:
        uuid.UUID(str(route))
    except ValueError:
        found = []
    else:
        found = _rows(conn, "SELECT id::text, code FROM core.route WHERE id = %(r)s::uuid AND status = 'active'", {"r": route})
    if not found:
        raise NotFound("no route of ours in use has that id")
    return found[0]


def parts_of(conn, route: Route, said: list[dict] | None) -> list[dict]:
    """Parts of a route as kept: each from where it begins to where it ends, given as points clicked on its line
    (`from_point`, `to_point`) or in metres from its start (`from_m`, `to_m`)."""
    if not said:
        raise Invalid("say which part of the route: where it begins and where it ends")
    if len(said) > MOST_PARTS:
        raise Invalid(f"at most {MOST_PARTS} parts")
    kept = []
    for part in said:
        a, b = position(conn, route, part, "from_"), position(conn, route, part, "to_")
        cut = route.walked(a, b)
        if not cut or sum(hi - lo for _, lo, hi in cut) <= 0:
            raise Invalid("a part must have a length: where it begins and where it ends are the same")
        kept.append({"from": a, "to": b})
    return kept
