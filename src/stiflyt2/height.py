"""How high the ground is along the routes: asked of Kartverket's height service, and kept.

A stretch is a line on the map and has no heights. Kartverket answers how
high the ground is at a point, and what kind of ground it is (snow and ice,
bog, lake), from its terrain model. Here a line is asked about at its own
corners and in between, no further apart than `STEP_M`, and the answer is
kept by the checksum of the line (`height.profile`): asked once, shared by
every route that runs there, and asked again only when the line is moved.

What a route climbs is worked out from that each time it is asked for, over
the stretches of the route in the order they are walked. Water is not
walked, and has no part in it.

This is about a source, not about what a person has decided: it is no part
of the overlay, and nothing here is undone or has a history.
"""
from __future__ import annotations

import itertools
import json
import math
import time

import httpx
from psycopg.rows import dict_row

from stiflyt2 import seasons
from stiflyt2.config import settings

PUNKT = "https://ws.geonorge.no/hoydedata/v1/punkt"
SRID = 25833
STEP_M = 10         # no further than this between two points asked about
AT_ONCE = 50        # as many points as the service takes in one question
TRIES = 4
ICE = "SnøIsbre"    # the service's name for ground under snow and ice all year
# A rise counts once it is this much. The terrain model is exact to the decimetre, a line on the map
# is not: across a slope it wanders some metres up and down the hillside, and every such step would
# be counted as climbing.
COUNTS_M = 2.0
# Naismith's rule, as v1 had it: three miles an hour, and an hour more for every 2,000 feet climbed.
KM_PER_HOUR = 4.828
MIN_PER_M_UP = 0.1
# Going down steeply takes time too. Langmuir's addition to the rule: ten minutes for every 300 m lost
# on ground steeper than 12 degrees. The same ground is what the marking handbook means by "very steep
# terrain", where what is lost counts half of what is climbed towards the grading.
STEEP = math.tan(math.radians(12))      # 21 %
STEEP_OVER_M = 50                       # the slope is taken over this far: point to point it is the line wandering
MIN_PER_M_STEEP_DOWN = 10 / 300
STEEP_DOWN_COUNTS = 0.5
# What length and climb alone make of a route, by the marking handbook (Merkehåndboka, as DNT gives it):
# the first grade whose limits it stays under, walked one way. Black has no limits.
GRADES = (("G", 5_000, 300), ("B", 10_000, 600), ("R", 20_000, 1_000))
_LEN = "ST_Length(ST_Transform(r.geom, 4326)::geography)"


class Unavailable(Exception):
    """The height service did not answer, or answered something else than what was asked."""


def client(timeout_s: float = 30.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=httpx.Timeout(timeout_s))


def ask(http: httpx.Client, points: list, tries: int = TRIES) -> list[tuple[float | None, str | None, str | None]]:
    """The height, the kind of ground and where the answer is from, for each point (east, north), in order."""
    found: list[tuple[float | None, str | None, str | None]] = []
    for i in range(0, len(points), AT_ONCE):
        some = points[i:i + AT_ONCE]
        params = {"koordsys": SRID, "punkter": json.dumps([[x, y] for x, y in some], separators=(",", ":")),
                  "geojson": "false"}
        why = "no answer"
        for attempt in range(tries):
            try:
                answer = http.get(PUNKT, params=params)
                if answer.status_code == 200:
                    said = answer.json().get("punkter") or []
                    break
                why = f"answered {answer.status_code}"
            except (httpx.HTTPError, ValueError) as e:
                why = f"{type(e).__name__}: {e}"
            if attempt + 1 < tries:
                time.sleep(0.5 * (attempt + 1))
        else:
            raise Unavailable(f"Kartverket's height service: {why}")
        if len(said) != len(some) or any(abs(p["x"] - x) > 0.05 or abs(p["y"] - y) > 0.05
                                         for p, (x, y) in zip(said, some, strict=True)):
            raise Unavailable("Kartverket's height service answered about other points than it was asked about")
        found.extend((p.get("z"), p.get("terreng"), p.get("datakilde")) for p in said)
    return found


def lacking(conn, *, route_id: str | None = None, area: str | None = None, again: bool = False,
            season: str = seasons.SUMMER) -> list[dict]:
    """The lines in use that have no heights yet: of one route, of the routes of one area (of the season asked
    for), or of everything.

    Each with the points to ask about. With `again`, every line, also those that have them.
    """
    season = seasons.known(season)
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT DISTINCT ON (k.line) k.line,
                   ST_AsGeoJSON(ST_Segmentize(r.geom, %(step)s), 2)::json->'coordinates' AS points,
                   ST_Length(r.geom) AS flat_m, {_LEN} AS ground_m
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 LATERAL (SELECT md5(ST_AsEWKB(r.geom)) AS line) k
            WHERE e.status = 'active' AND r.kind <> 'water'
              AND (%(route)s::uuid IS NULL
                   OR e.id IN (SELECT edge_id FROM core.route_leg WHERE route_id = %(route)s::uuid))
              AND (%(area)s::text IS NULL
                   OR e.id IN (SELECT l.edge_id FROM core.route_leg l
                               JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                                    AND t.season = %(season)s
                               WHERE lower(t.code) LIKE lower(%(area)s) || '%%'))
              AND (%(again)s OR NOT EXISTS (SELECT 1 FROM height.profile p WHERE p.line = k.line))
            ORDER BY k.line""",
            {"step": STEP_M, "route": route_id, "area": area, "again": again, "season": season}).fetchall()


def along(points: list, flat_m: float, ground_m: float) -> list[float]:
    """Metres to each point from the first, as walked on the ground: the map's own metres are a little off."""
    scale = ground_m / flat_m if flat_m else 1.0
    at, so_far = [0.0], 0.0
    for (ax, ay), (bx, by) in itertools.pairwise(points):
        so_far += math.hypot(bx - ax, by - ay) * scale
        at.append(round(so_far, 2))
    return at


def fetch(http: httpx.Client, line: dict, tries: int = TRIES) -> dict:
    """Ask about one line. Nothing is written."""
    answers = ask(http, line["points"], tries)
    return {"line": line["line"], "points": line["points"],
            "at_m": along(line["points"], line["flat_m"], line["ground_m"]),
            "z": [z for z, _, _ in answers], "ground": [g for _, g, _ in answers],
            "source": next((s for _, _, s in answers if s), None)}


def keep(conn, got: dict) -> None:
    """Keep what was answered about a line, in place of what was kept before."""
    conn.execute("""
        INSERT INTO height.profile (line, step_m, sampled, at_m, z, ground, source)
        VALUES (%(line)s, %(step)s, ST_SetSRID(ST_GeomFromGeoJSON(%(sampled)s), %(srid)s),
                %(at_m)s::real[], %(z)s::real[], %(ground)s::text[], %(source)s)
        ON CONFLICT (line) DO UPDATE SET step_m = EXCLUDED.step_m, sampled = EXCLUDED.sampled,
            at_m = EXCLUDED.at_m, z = EXCLUDED.z, ground = EXCLUDED.ground, source = EXCLUDED.source,
            fetched_at = now()""",
        {"line": got["line"], "step": STEP_M, "srid": SRID,
         "sampled": json.dumps({"type": "LineString", "coordinates": got["points"]}),
         "at_m": got["at_m"], "z": got["z"], "ground": got["ground"], "source": got["source"]})


def fill(lines: list[dict], store, http: httpx.Client | None = None, points: int | None = None,
         tries: int = TRIES, each=None) -> dict:
    """Ask about lines one at a time, and hand each answer to `store` as soon as it is there.

    With `points`, it stops before asking about more points than that; what is
    left waits. If the service stops answering, what has been stored stays.
    No database connection is held while the service is asked.
    """
    done, asked, own, stopped = 0, 0, http is None, None
    http = http or client()
    try:
        for line in lines:
            if points is not None and asked and asked + len(line["points"]) > points:
                break
            try:
                got = fetch(http, line, tries)
            except Unavailable as e:
                stopped = str(e)
                break
            store(got)
            done, asked = done + 1, asked + len(line["points"])
            if each:
                each(done, len(lines))
    finally:
        if own:
            http.close()
    return {"lines": done, "points": asked, "left": len(lines) - done, "stopped": stopped}


def climb(z: list[float], least: float = COUNTS_M) -> tuple[float, float]:
    """Metres gained and metres lost from the first height to the last.

    A rise or a fall counts from where it turns, and only once it is `least`
    or more: smaller steps are the line wandering on a slope. What is left
    at the end is counted as it is, so that gained less lost is always the
    last height less the first.
    """
    if len(z) < 2:
        return 0.0, 0.0
    up = down = 0.0
    turn = far = z[0]       # where it last turned, and the furthest it has come since
    going = 0
    for h in z[1:]:
        if going == 0:
            if abs(h - turn) >= least:
                going, far = (1 if h > turn else -1), h
        elif going == 1:
            if h >= far:
                far = h
            elif far - h >= least:
                up, turn, far, going = up + far - turn, far, h, -1
        elif h <= far:
            far = h
        elif h - far >= least:
            down, turn, far, going = down + turn - far, far, h, 1
    for a, b in ((turn, far), (far, z[-1])) if going else ((z[0], z[-1]),):
        up, down = up + max(b - a, 0.0), down + max(a - b, 0.0)
    return up, down


def steep(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Metres gained and metres lost on ground steeper than 12 degrees, along points of (metres walked, height)."""
    up = down = 0.0
    if not points:
        return up, down
    at, z = points[0]
    for here, h in points[1:]:
        if here - at < STEEP_OVER_M:
            continue
        if abs(h - z) / (here - at) > STEEP:
            up, down = up + max(h - z, 0.0), down + max(z - h, 0.0)
        at, z = here, h
    return up, down


def minutes(metres: float, up_m: float, steep_down_m: float = 0.0) -> int:
    """How long it takes to walk, by Naismith's rule with Langmuir's addition for going down steeply.

    For a fit walker on a path, with no rests.
    """
    return round(metres / 1000 / KM_PER_HOUR * 60 + up_m * MIN_PER_M_UP + steep_down_m * MIN_PER_M_STEEP_DOWN)


def at_least(metres: float, up_m: float, steep_down_m: float = 0.0) -> str:
    """The grading that length and climb alone give: G, B, R or S.

    What is lost on very steep ground counts half of what is climbed. The
    ground underfoot and how exposed it is are the rest of a grading, and
    are not known here. They can make a route harder than this, never easier.
    """
    counted = up_m + STEEP_DOWN_COUNTS * steep_down_m
    return next((grade for grade, length, climb in GRADES if metres < length and counted < climb), "S")


def one_way(metres: float, up_m: float, down_m: float, steep_down_m: float = 0.0) -> dict:
    return {"up_m": round(up_m), "down_m": round(down_m), "steep_down_m": round(steep_down_m),
            "minutes": minutes(metres, up_m, steep_down_m), "at_least": at_least(metres, up_m, steep_down_m)}


def harder(*grades: str | None) -> str | None:
    """The hardest of some gradings, or None when one of them is not known."""
    return None if None in grades or not grades else max(grades, key="GBRS".index)


def of_route(conn, route_id: str, samples: bool = False, ground: bool = False) -> dict:
    """What a route climbs, walked in the order of its stretches, as far as its heights are known.

    `up_m` and `down_m` are walked from `from_node` to `to_node`; the other
    way they change places. When the heights are known for all of it,
    `there` and `back` say for each way what is climbed, what is lost on
    steep ground, the minutes it takes by Naismith's rule, and the grading
    that length and climb alone give. With `samples`, also the profile itself: for each point the
    metres walked, the height, whether the ground is snow and ice, and
    where it is (longitude, latitude); a point with no height is where a
    part with no heights yet begins, or where the route is in pieces and
    the next piece begins. And the places passed. With `ground`,
    each point also says what the height service calls the ground there.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        legs = cur.execute(f"""
            SELECT l.reversed, {_LEN} AS metres, p.at_m, p.z, p.ground, p.source,
                   (CASE WHEN l.reversed THEN r.b_node ELSE r.a_node END)::text AS a,
                   (CASE WHEN l.reversed THEN r.a_node ELSE r.b_node END)::text AS b,
                   CASE WHEN %(samples)s THEN ST_AsGeoJSON(ST_Transform(p.sampled, 4326), 5)::json->'coordinates'
                   END AS lonlat
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
            JOIN core.edge_revision r ON r.id = e.current_revision_id
            LEFT JOIN height.profile p ON p.line = md5(ST_AsEWKB(r.geom))
            WHERE l.route_id = %(id)s AND r.kind <> 'water' ORDER BY l.seq""",
            {"id": route_id, "samples": samples}).fetchall()
    walked = known = ice = up = down = steep_up = steep_down = 0.0
    low = high = None
    profile: list[list] = []
    run: list[tuple[float, float]] = []           # one unbroken part: metres walked and height

    def close() -> None:
        nonlocal up, down, steep_up, steep_down
        if run:
            gained, lost = climb([z for _, z in run])
            steeply_up, steeply_down = steep(run)
            up, down, steep_up, steep_down = up + gained, down + lost, steep_up + steeply_up, steep_down + steeply_down
            run.clear()

    stopped = None                                # the junction the stretch before ended at
    for leg in legs:
        z = leg["z"] or []
        # A stretch that does not begin where the one before ended: the route is in pieces there, or goes on
        # by boat. Nothing is climbed from the one to the other, and the profile has a break.
        if stopped is not None and leg["a"] != stopped:
            close()
            if samples and profile and profile[-1][1] is not None:
                profile.append([round(walked), None, 0, None, None])
        stopped = leg["b"]
        if not any(h is not None for h in z):
            close()
            if samples and (not profile or profile[-1][1] is not None):
                profile.append([round(walked), None, 0, None, None])
            walked += leg["metres"]
            continue
        at, under, lonlat = leg["at_m"], leg["ground"], leg["lonlat"]
        order = range(len(z) - 1, -1, -1) if leg["reversed"] else range(len(z))
        length = at[-1]
        before = None
        for i in order:
            here = (length - at[i]) if leg["reversed"] else at[i]
            if before is not None:       # each point answers for half the way to its neighbours
                ice += (here - before[0]) * ((under[i] == ICE) + before[1]) / 2
            before = (here, under[i] == ICE)
            if z[i] is None:
                continue
            run.append((walked + here, z[i]))
            low, high = (z[i] if low is None else min(low, z[i])), (z[i] if high is None else max(high, z[i]))
            if samples:
                profile.append([round(walked + here), round(z[i], 1), int(under[i] == ICE), *lonlat[i],
                                *([under[i]] if ground else [])])
        walked, known = walked + leg["metres"], known + leg["metres"]
    close()
    found = {"on_foot_m": round(walked), "known_m": round(known),
             "up_m": round(up) if known else None, "down_m": round(down) if known else None,
             "low_m": None if low is None else round(low), "high_m": None if high is None else round(high),
             "ice_m": round(ice) if known else None,
             "from_node": legs[0]["a"] if legs else None, "to_node": legs[-1]["b"] if legs else None,
             "source": next((g["source"] for g in legs if g["source"]), None)}
    whole = bool(legs) and round(known) == round(walked)
    found["there"] = one_way(walked, up, down, steep_down) if whole else None
    found["back"] = one_way(walked, down, up, steep_up) if whole else None       # what is climbed steeply is lost steeply back
    if samples:
        found["samples"] = profile
        found["places"] = _passed(conn, legs)
    return found


def _passed(conn, legs: list[dict]) -> list[dict]:
    """The places a route passes, with how far along it each lies."""
    at, so_far = [], 0.0
    for leg in legs:
        at.append((leg["a"], so_far))
        so_far += leg["metres"]
    if legs:
        at.append((legs[-1]["b"], so_far))
    names = dict(conn.execute("""
        SELECT n.id::text, p.name FROM core.node n JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%s::uuid[])""", ([n for n, _ in at],)).fetchall())
    passed: list[dict] = []
    for node, metres in at:
        if node in names and not (passed and passed[-1]["name"] == names[node] and metres - passed[-1]["at_m"] < 200):
            passed.append({"at_m": round(metres), "name": names[node]})
    return passed


def state(conn) -> dict:
    """How much of what is walked has heights."""
    lines, without, metres, without_m = conn.execute(f"""
        SELECT count(*), count(*) FILTER (WHERE p.line IS NULL),
               coalesce(sum({_LEN}), 0), coalesce(sum({_LEN}) FILTER (WHERE p.line IS NULL), 0)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        LEFT JOIN height.profile p ON p.line = md5(ST_AsEWKB(r.geom))
        WHERE e.status = 'active' AND r.kind <> 'water'""").fetchone()
    return {"stretches": lines, "without": without, "metres": round(metres), "without_m": round(without_m)}
