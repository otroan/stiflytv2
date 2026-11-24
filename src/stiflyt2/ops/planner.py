"""The route planner: on foot from anywhere in Norway to anywhere, along DNT's routes where they lead.

A feature of its own (ADR-005). A person clicks where to begin and where to
end, and places to pass on the way, and gets a way on foot between them over
the network in `walk` (`ops/walknet.py`): every path and road OpenStreetMap
has, the marked routes of Turrutebasen, and our own. It reads and keeps
nothing.

Each stretch costs its length, times what it is: a path is walked as it is,
a road counts more the bigger it is, a ferry little but with a wait. How
much DNT's marked routes are preferred is the person's to say (`prefer`):
at 0 the shortest way is taken, at 1 a route of DNT's counts half its length
and so is followed a good way round. Glaciers, alpine paths and via ferratas
are left out unless asked for, and so are ferries if they are not wanted.

The search is pgRouting's A*, over the stretches in a box round each leg; a
box too narrow to find a way in is widened. Where a point is clicked off the
network, the way begins at the nearest stretch, and says how far off it was.

Where nothing is mapped, a leg may be drawn straight over the ground, as the
person says: a sketch, which knows nothing of the terrain but its heights,
and is timed as half as slow again as a path. A leg no way on foot is found
for is drawn so too, and says so; a leg much longer than the crow flies says
that, so that the person may draw it straight instead.

Heights, the time it takes and the days it is cut into are worked out
afterwards (`heights`), from the height model kept here (`terrain`), every
50 m, also across the country.
"""
from __future__ import annotations

import bisect
import itertools
import json
import math

import psycopg
from psycopg.rows import dict_row

from stiflyt2 import features, height, terrain
from stiflyt2.domain.places import Invalid, NotFound

features.feature("planner", "plan a walk from anywhere to anywhere in Norway",
                 "ask to be given the feature `planner`")

# What a metre of each kind of way counts, before DNT's routes are preferred. Paths are what is walked;
# the bigger a road, the less one wants to walk it.
KIND = {"path": 1.0, "footway": 1.0, "track": 1.0, "bridleway": 1.0, "steps": 1.1, "trail": 1.0, "link": 1.0,
        "pedestrian": 1.1, "cycleway": 1.2, "via_ferrata": 1.5, "living_street": 1.2, "residential": 1.3,
        "service": 1.3, "unclassified": 1.3, "road": 1.3, "tertiary": 1.6, "tertiary_link": 1.6,
        "secondary": 2.0, "secondary_link": 2.0, "primary": 2.5, "primary_link": 2.5,
        "trunk": 3.5, "trunk_link": 3.5}
FERRY = 3.0                 # a metre on a ferry counts three walked: it is taken to cross a fjord, not to travel
FERRY_WAIT_M = 5000         # and getting on one as much as this far walked
FERRY_MOST_M = 30_000       # a longer crossing is a voyage (Hurtigruten, to Denmark), not a way to walk on
DNT_LESS = 0.5              # at the most a route of DNT's counts this much less than its length
MARKED_LESS = 0.3           # and another marked route this much
PREFER = 0.6                # how much DNT's routes are preferred when nothing is said
ALPINE = ("demanding_alpine_hiking", "difficult_alpine_hiking")
SNAP_M = 10_000             # a point clicked further than this from any way has none near it: the leg is drawn straight
OFF_M = 5                   # nearer than this to a way is on it
MOST_POINTS = 100           # a walk the line of which has been dragged here and there has many
FERRY_KMH = 20              # how fast a ferry goes, for the time a plan takes
START, END = 9_000_000_000_000_001, 9_000_000_000_000_002       # the two ends of a leg, as vertices of their own
CLASSES = {                 # what a stretch is, as it is told and drawn
    "dnt": "a route of DNT's", "marked": "a marked route", "path": "a path", "road": "a road",
    "ferry": "a ferry", "off": "no way mapped", "field": "across country"}
PATHS = ("path", "footway", "track", "bridleway", "steps", "via_ferrata", "trail")
CREDITS = {"osm": "Paths and roads: © OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)",
           "turrutebasen": "Marked routes: Turrutebasen, Kartverket, CC BY 4.0",
           "height": "Heights: Kartverket's height model",
           "terrain": "Across country: Kartverket's national height model, CC BY 4.0, and OpenStreetMap's water, "
                      "rivers, bogs and glaciers"}
HUT_M = 400                 # a hut this near the way is passed
STEP_M = 50                 # heights are asked for this often along the way
MOST_HEIGHTS = 4000         # and no more often than this many points for the whole way
DAY_OVER = 60               # a day may run this many minutes over to end at a hut
SLOW_S = 60                 # a search may take this long: across the country it is a big one
ROUGH = 1.5                 # with no path, the flat takes this much longer than on a path
ROUNDABOUT, ROUNDABOUT_M = 2.5, 5000    # a leg this many times as long as the crow flies, and this much more, is said
PROFILE_POINTS = 1500       # the profile is drawn with no more points than this
GREAT = 1000                # a piece of the network with as many junctions as this is joined to more than itself
GREAT_SNAP_M = 30_000       # and is looked for this far off, straight over the ground (the plateaus of Finnmark)
HIGH_LESS = 0.4             # at the most, a metre up in the mountains (or down in the valleys) counts this much less
FIELD = 2.0                 # a metre across country counts as two on a path
BOG = 1.4                   # and over a bog as more again
FIELD_CLIMB = 4.0           # a metre climbed or gone down across country, as many metres walked
FIELD_NEAR_M = 250          # across country, a point is put on a path this near rather than on the lattice


class Options:
    """What the person said about how to go."""

    def __init__(self, prefer: float = PREFER, ferries: bool = True, ice: bool = False, alpine: bool = False,
                 mountains: float = 0.0, across: bool = False):
        if not 0 <= prefer <= 1:
            raise Invalid("how much DNT's routes are preferred is from 0 to 1")
        if not -1 <= mountains <= 1:
            raise Invalid("how much the mountains (1) or the valleys (-1) are preferred is from -1 to 1")
        self.prefer, self.ferries, self.ice, self.alpine = float(prefer), ferries, ice, alpine
        self.mountains, self.across = float(mountains), across

    def height(self) -> str:
        """What a metre counts more or less for how high it lies, as SQL: up in the mountains less when they are
        preferred, down in the valleys less when they are."""
        if not self.mountains:
            return "1"
        return f"(CASE WHEN high IS NULL THEN 1 ELSE 1 - {HIGH_LESS * self.mountains} * (2 * high - 1) END)"

    def field(self) -> str:
        """What a stretch across country counts, as SQL: twice a path, more over bog and glacier, and what it
        climbs and goes down."""
        return f"""((length_m * {FIELD} * (CASE WHEN wet THEN {BOG} ELSE 1 END) * (CASE WHEN ice THEN 1.5 ELSE 1 END)
                     + (up_m + down_m) * {FIELD_CLIMB}) * {self.height()})"""

    def allowed(self) -> str:
        """Which stretches may be walked, as SQL."""
        out = []
        if not self.ice:
            out.append("NOT ice")
        if not self.alpine:
            out.append(f"kind <> 'via_ferrata' AND coalesce(sac, '') NOT IN {ALPINE!r}")
        if not self.ferries:
            out.append("kind <> 'ferry'")
        else:
            out.append(f"(kind <> 'ferry' OR length_m <= {FERRY_MOST_M})")
        return " AND ".join(out) or "true"

    def cost(self) -> str:
        """What a stretch counts, as SQL: its length times what it is."""
        kinds = " ".join(f"WHEN '{k}' THEN {v}" for k, v in KIND.items())
        dnt, marked = 1 - DNT_LESS * self.prefer, 1 - MARKED_LESS * self.prefer
        return f"""(CASE WHEN kind = 'ferry' THEN length_m * {FERRY} + {FERRY_WAIT_M} ELSE length_m
                     * (CASE kind {kinds} ELSE 1.3 END)
                     * (CASE WHEN dnt THEN {dnt} WHEN trail THEN {marked} ELSE 1 END)
                     * (CASE sac WHEN 'alpine_hiking' THEN 1.5 WHEN 'demanding_mountain_hiking' THEN 1.15
                                 WHEN 'demanding_alpine_hiking' THEN 2.5 WHEN 'difficult_alpine_hiking' THEN 3 ELSE 1 END)
                     * (CASE WHEN visibility IN ('bad', 'horrible', 'no') THEN 1.4 ELSE 1 END)
                     * (CASE WHEN private THEN 1.3 ELSE 1 END) * (CASE WHEN tunnel THEN 3 ELSE 1 END)
                     * (CASE WHEN discouraged THEN 2 ELSE 1 END) * (CASE WHEN ice THEN 1.5 ELSE 1 END)
                     * {self.height()} END)"""

    def least(self) -> float:
        """The least a metre can count: what A*'s guess of the way left is scaled by, a little less for the map's
        own metres, which are a little longer than those on the ground away from the middle of the zone."""
        least = min(1 - DNT_LESS * self.prefer, *KIND.values())
        if self.ferries:
            least = min(least, FERRY)
        return least * (1 - HIGH_LESS * abs(self.mountains)) * 0.95


def _class(e: dict) -> str:
    if e["kind"] == "ferry":
        return "ferry"
    if e["kind"] == "field":
        return "field"
    if e["dnt"]:
        return "dnt"
    if e["trail"]:
        return "marked"
    return "path" if e["kind"] in PATHS or e["kind"] == "link" else "road"


def _snap(conn, lon: float, lat: float, o: Options, great: bool = False) -> dict:
    """The stretch nearest to a point that may be walked, and where on it the point is; with `great`, the nearest
    in a great piece of the network, not one of the small pieces that join nothing."""
    with conn.cursor(row_factory=dict_row) as cur:
        found = cur.execute(f"""
            SELECT e.id, e.source, e.target, e.length_m, {o.cost()} / greatest(e.length_m, 0.01) AS per_m,
                   ST_LineLocatePoint(e.geom, p) AS f, ST_Distance(e.geom, p) AS off_m,
                   ST_X(ST_ClosestPoint(e.geom, p)) AS x, ST_Y(ST_ClosestPoint(e.geom, p)) AS y,
                   ST_AsGeoJSON(ST_Transform(ST_LineSubstring(e.geom, 0, ST_LineLocatePoint(e.geom, p)), 4326), 6)::json
                       -> 'coordinates' AS before,
                   ST_AsGeoJSON(ST_Transform(ST_LineSubstring(e.geom, ST_LineLocatePoint(e.geom, p), 1), 4326), 6)::json
                       -> 'coordinates' AS after,
                   ST_AsGeoJSON(ST_Transform(ST_ClosestPoint(e.geom, p), 4326), 6)::json -> 'coordinates' AS at,
                   e.kind, e.dnt, e.trail, e.ice, e.name, e.origin, e.piece, coalesce(e.piece_nodes, 0) AS piece_nodes
            FROM walk.edge e, (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS p) q
            WHERE {o.allowed()} AND e.kind <> 'ferry' {f"AND e.piece_nodes >= {GREAT}" if great else ""}
            ORDER BY e.geom <-> p LIMIT 1""", (lon, lat)).fetchone()
    if o.across and (found is None or found["off_m"] > FIELD_NEAR_M):
        field = _snap_field(conn, lon, lat, o)       # across country: onto the lattice, unless a path is near
        if field is not None and (found is None or field["off_m"] < found["off_m"]):
            found = field
    reach = GREAT_SNAP_M if great else SNAP_M
    if found is None or found["off_m"] > reach:
        raise NotFound(f"no path or road {'that leads anywhere ' if great else ''}within {reach / 1000:.0f} km "
                       f"of {lat:.5f} N {lon:.5f} E")
    for part in ("before", "after"):         # at an end of the stretch, a part of it is a point
        if not isinstance(found[part][0], list):
            found[part] = [found[part], found[part]]
    return found


def _snap_field(conn, lon: float, lat: float, o: Options) -> dict | None:
    """The stretch of the lattice across country nearest to a point."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(f"""
            SELECT e.id, e.source, e.target, e.length_m, {o.field()} / greatest(e.length_m, 0.01) AS per_m,
                   ST_LineLocatePoint(e.geom, p) AS f, ST_Distance(e.geom, p) AS off_m,
                   ST_X(ST_ClosestPoint(e.geom, p)) AS x, ST_Y(ST_ClosestPoint(e.geom, p)) AS y,
                   ST_AsGeoJSON(ST_Transform(ST_LineSubstring(e.geom, 0, ST_LineLocatePoint(e.geom, p)), 4326), 6)::json
                       -> 'coordinates' AS before,
                   ST_AsGeoJSON(ST_Transform(ST_LineSubstring(e.geom, ST_LineLocatePoint(e.geom, p), 1), 4326), 6)::json
                       -> 'coordinates' AS after,
                   ST_AsGeoJSON(ST_Transform(ST_ClosestPoint(e.geom, p), 4326), 6)::json -> 'coordinates' AS at,
                   'field' AS kind, false AS dnt, false AS trail, e.ice, NULL AS name, 'field' AS origin,
                   NULL::bigint AS piece, {GREAT} AS piece_nodes
            FROM walk.field e, (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS p) q
            WHERE e.kind = 'field' {"" if o.ice else "AND NOT e.ice"}
            ORDER BY e.geom <-> p LIMIT 1""", (lon, lat)).fetchone()


def _box(conn, a: dict, b: dict, margin: float) -> str:
    x0, x1 = sorted((a["x"], b["x"]))
    y0, y1 = sorted((a["y"], b["y"]))
    return f"ST_MakeEnvelope({x0 - margin}, {y0 - margin}, {x1 + margin}, {y1 + margin}, 25833)"


def _ends(a: dict, b: dict) -> str:
    """The two ends of a leg joined to the network: from each to both ends of the stretch it lies on."""
    rows = [(START, a["source"], a["f"] * a["length_m"] * a["per_m"], a["x"], a["y"]),
            (START, a["target"], (1 - a["f"]) * a["length_m"] * a["per_m"], a["x"], a["y"]),
            (END, b["source"], b["f"] * b["length_m"] * b["per_m"], b["x"], b["y"]),
            (END, b["target"], (1 - b["f"]) * b["length_m"] * b["per_m"], b["x"], b["y"])]
    if a["id"] == b["id"]:
        rows.append((START, END, abs(a["f"] - b["f"]) * a["length_m"] * a["per_m"], a["x"], a["y"]))
    values = ", ".join(f"({START + 10 + i}::bigint, {s}::bigint, {t}::bigint, {c}::float8, {x}::float8, {y}::float8, "
                       f"{x}::float8, {y}::float8)" for i, (s, t, c, x, y) in enumerate(rows))
    return f"SELECT * FROM (VALUES {values}) v(id, source, target, cost, x1, y1, x2, y2)"


def _leg(conn, a: dict, b: dict, o: Options) -> list[dict]:
    """The way from one snapped point to the next: the stretches walked, in order, each the way it is walked."""
    far = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    for margin in (max(5000, 0.3 * far), max(25_000, 1.0 * far)):
        box = _box(conn, a, b, margin)
        edges = (f"SELECT id, source, target, {o.cost()} AS cost, x1, y1, x2, y2 FROM walk.edge "
                 f"WHERE geom && {box} AND {o.allowed()} UNION ALL {_ends(a, b)}")
        if o.across:
            edges += (f" UNION ALL SELECT id, source, target, {o.field()} AS cost, x1, y1, x2, y2 FROM walk.field "
                      f"WHERE geom && {box}" + ("" if o.ice else " AND NOT ice"))
        with conn.cursor(row_factory=dict_row) as cur:
            walked = cur.execute("""
                SELECT r.path_seq, r.node, r.edge, e.kind, e.name, e.trail, e.dnt, e.ice, e.origin, e.sac, e.length_m,
                       ST_AsGeoJSON(ST_Transform(CASE WHEN r.node = e.source THEN e.geom ELSE ST_Reverse(e.geom) END,
                                                 4326), 6)::json -> 'coordinates' AS coords
                FROM pgr_aStar(%s, %s::bigint, %s::bigint, directed => false, heuristic => 4, factor => %s) r
                LEFT JOIN (SELECT id, source, kind, name, trail, dnt, ice, origin, sac, length_m, geom FROM walk.edge
                           UNION ALL
                           SELECT id, source, 'field', NULL, false, false, ice, 'field', NULL, length_m, geom
                           FROM walk.field) e ON e.id = r.edge
                WHERE r.edge <> -1 ORDER BY r.path_seq""", (edges, START, END, o.least())).fetchall()
        if walked:
            break
    else:
        raise NotFound("no way on foot was found between two of the points: a fjord or a border in between, "
                       "or the way leads over a glacier or along alpine paths that are left out")
    out = []
    for i, w in enumerate(walked):
        if w["kind"] is not None:
            out.append(w)
            continue
        # A step to or from an end of the leg, along part of the stretch the end lies on.
        if i == 0 and len(walked) == 1:          # both ends on the same stretch
            whole = a["before"] + a["after"][1:]
            line = _between(whole, a, b)
            out.append(_part(a, line))
        elif i == 0:
            to_source = walked[1]["node"] == a["source"] if len(walked) > 1 else w["node"] == a["source"]
            out.append(_part(a, a["before"][::-1] if to_source else a["after"]))
        else:
            from_source = w["node"] == b["source"]
            out.append(_part(b, b["before"] if from_source else b["after"][::-1]))
    return out


def _between(whole: list, a: dict, b: dict) -> list:
    """The part of a stretch between two points on it, from the first to the second."""
    if b["f"] >= a["f"]:        # the corners after a, up to b
        return [a["at"], *b["before"][len(a["before"]) - 1:-1], b["at"]]
    # back towards the stretch's start: the corners after b and before a, the other way
    return [a["at"], *b["after"][1:len(b["after"]) - len(a["after"]) + 1][::-1], b["at"]]


def _part(end: dict, coords: list) -> dict:
    return {"kind": end["kind"], "name": end["name"], "trail": end["trail"], "dnt": end["dnt"], "ice": end["ice"],
            "origin": end["origin"], "sac": None, "coords": coords,
            "length_m": _metres(coords)}


def _metres(coords: list) -> float:
    """How long a line of longitudes and latitudes is, near enough."""
    out = 0.0
    for (x0, y0), (x1, y1) in itertools.pairwise(coords):
        dx = math.radians(x1 - x0) * math.cos(math.radians((y0 + y1) / 2))
        out += 6_371_000 * math.hypot(dx, math.radians(y1 - y0))
    return out


def plan(conn, points: list, o: Options | None = None, straight: list[bool] | None = None,
         snap: list[bool] | None = None, kept: list[dict | None] | None = None) -> dict:
    """A way on foot through the points, in order: the line, told in steps, and what it adds up to.

    A leg between two points goes along paths and roads, or straight over the ground where `straight` says so
    (one for each leg): a sketch, where nothing is mapped. A leg no way on foot is found for is drawn straight
    too, and says why (`legs`). A point `snap` says of (one for each point; the line dragged to another path)
    is where the way passes on the nearest stretch, not the spot itself: it is not walked out to and back,
    and where it was moved to is in `snapped`.

    A leg `kept` gives (one for each leg, or None) is not looked for again but taken as it was found before:
    its line, its steps, the junctions along it and what it is from, as this gave them. So a change to one part
    of a way (a point moved, the line dragged) finds that part anew and leaves the rest as it was. Where the
    stretches along the way meet, by index into the line, is in `joints`.
    """
    o = o or Options()
    if not 2 <= len(points) <= MOST_POINTS:
        raise Invalid(f"a way goes through 2 to {MOST_POINTS} points")
    straight = list(straight) if straight is not None else [False] * (len(points) - 1)
    if len(straight) != len(points) - 1:
        raise Invalid("say for each leg between two points, and only for them, whether it is drawn straight")
    snap = list(snap) if snap is not None else [False] * len(points)
    if len(snap) != len(points):
        raise Invalid("say for each point, and only for them, whether it is moved onto the nearest stretch")
    kept = list(kept) if kept is not None else [None] * (len(points) - 1)
    if len(kept) != len(points) - 1:
        raise Invalid("say for each leg between two points, and only for them, whether it is kept as it was found")
    moved: dict[int, list] = {}
    joints: set[int] = set()
    if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM walk.built)").fetchone()[0]:
        raise NotFound("the planner's network has not been built yet (`stiflyt2 planner build`)")
    conn.execute(f"SET LOCAL statement_timeout = '{SLOW_S}s'")         # a long way is a big search
    snapped: dict[tuple[int, bool], dict | str] = {}
    coords: list = []
    steps: list[dict] = []
    off: dict[int, int] = {}
    origins: set[str] = set()
    walked = [0.0]

    def nearest(i: int, great: bool = False) -> dict | str:
        """The stretch nearest to a point, or why there is none."""
        if (i, great) not in snapped:
            try:
                snapped[i, great] = _snap(conn, *points[i], o, great)
            except NotFound as e:
                snapped[i, great] = str(e)
        return snapped[i, great]

    def ends(n: int) -> tuple:
        """Where a leg begins and ends on the network: on the nearest stretches, unless one of them is on a small
        piece the other is not on (a path that joins nothing), or none is near: then on the nearest stretch of a
        great piece, further off."""
        a, b = nearest(n), nearest(n + 1)
        if isinstance(a, dict) and isinstance(b, dict) and a["piece"] == b["piece"]:
            return a, b

        def great(end: dict | str, i: int) -> dict | str:
            if isinstance(end, dict) and end["piece_nodes"] >= GREAT:
                return end
            wider = nearest(i, True)        # none near, or only a small piece: further off, one that leads somewhere
            return wider if isinstance(wider, dict) else end
        return great(a, n), great(b, n + 1)

    def add(cls: str, name, ice: bool, line: list, metres: float) -> None:
        line = [c for k, c in enumerate(line) if k == 0 or c != line[k - 1]]      # a point where a stretch is cut is in both
        if len(line) < 2 or metres < 0.5:
            return
        walked[0] += metres
        start = len(coords) - 1 if coords else 0
        coords.extend(line[1:] if coords else line)
        joints.update((start, len(coords) - 1))
        last = steps[-1] if steps else None
        if last and last["class"] == cls and last["name"] == name and last["ice"] == ice and "via" not in last:
            last["metres"] += metres
            last["at"][1] = len(coords) - 1
        else:
            steps.append({"class": cls, "name": name, "ice": ice, "metres": metres, "at": [start, len(coords) - 1]})

    legs = []
    for n in range(len(points) - 1):
        p, q = [float(v) for v in points[n]], [float(v) for v in points[n + 1]]
        begun, before, crow = len(coords) - 1 if coords else 0, walked[0], _metres([p, q])
        why = way = None
        mine: set[str] = set()
        if kept[n]:
            k = kept[n]
            for st in k["steps"]:
                add(st["class"], st.get("name"), bool(st.get("ice")), k["coords"][st["at"][0]:st["at"][1] + 1], st["metres"])
            joints.update(begun + j for j in k.get("joints", []) if 0 <= j < len(k["coords"]))
            mine = set(k.get("origins", [])) & set(CREDITS)
            origins.update(mine)
            metres = walked[0] - before
            legs.append({"from": n, "to": n + 1, "straight": straight[n], "unfound": k.get("unfound"),
                         "metres": round(metres), "crow_m": round(crow), "at": [begun, len(coords) - 1],
                         "roundabout": bool(k.get("roundabout")), "origins": sorted(mine), "kept": True})
            if n + 2 < len(points) and steps:
                steps[-1]["via"] = n + 1
            continue
        if not straight[n]:
            a, b = ends(n)
            why = a if isinstance(a, str) else b if isinstance(b, str) else None
            if why is None:
                try:
                    way = _leg(conn, a, b, o)
                except NotFound as e:
                    why = str(e)
        if way is None:
            add("off", None, False, [p, q], crow)
        else:
            if snap[n]:
                moved[n] = a["at"]
            elif a["off_m"] > OFF_M:
                add("off", None, False, [p, a["at"]], a["off_m"])
                off[n] = round(a["off_m"])
            for w in way:
                mine.add({"turrutebasen": "turrutebasen", "ours": "turrutebasen", "field": "terrain"}.get(w["origin"], "osm"))
                add(_class(w), w["name"] if w["kind"] != "link" else None, bool(w["ice"]), w["coords"], w["length_m"])
            if snap[n + 1]:
                moved[n + 1] = b["at"]
            elif b["off_m"] > OFF_M:
                add("off", None, False, [b["at"], q], b["off_m"])
                off[n + 1] = round(b["off_m"])
        origins.update(mine)
        metres = walked[0] - before
        legs.append({"from": n, "to": n + 1, "straight": straight[n], "unfound": why, "metres": round(metres),
                     "crow_m": round(crow), "at": [begun, len(coords) - 1], "origins": sorted(mine), "kept": False,
                     "roundabout": way is not None and metres > ROUNDABOUT * crow and metres - crow > ROUNDABOUT_M})
        if n + 2 < len(points) and steps:
            steps[-1]["via"] = n + 1                # a point passed on the way
    _named(conn, steps, coords)
    by = {}
    for s in steps:
        s["metres"] = round(s["metres"])
        by[s["class"]] = by.get(s["class"], 0) + s["metres"]
        s["says"] = _says(s)
    on_foot = sum(m for c, m in by.items() if c != "ferry")
    return {"coords": coords, "steps": steps, "legs": legs,
            "total": {"metres": round(sum(by.values())), "walked_m": on_foot, "by": by,
                      "minutes": height.minutes(on_foot, 0), "ferry_minutes": round(by.get("ferry", 0) / FERRY_KMH * 0.06)},
            "off": [{"point": i, "off_m": m} for i, m in sorted(off.items())],
            "snapped": {str(i): at for i, at in sorted(moved.items())},
            "joints": sorted(j for j in joints if j < len(coords)),
            "credits": [CREDITS[k] for k in ("osm", "turrutebasen", "terrain") if k in origins],
            "options": {"prefer": o.prefer, "ferries": o.ferries, "ice": o.ice, "alpine": o.alpine,
                        "mountains": o.mountains, "across": o.across}}


# How long a search takes, as measured: a little for each leg, and then for every stretch in its box. A walk the
# length of Norway: 12 s along paths, 21 s across country too.
LEG_S = 0.2
EDGE_S = 3.2e-6
FIELD_S = 2.0e-6
HEIGHTS_S, HEIGHTS_S_PER_KM = 0.3, 0.0015
WALKED_PER_CROW = 1.4           # a way on foot is about this much longer than the crow flies


def _share(conn, table: str, box: str) -> float:
    """How much of a table lies in a box, as its statistics say; nothing where it has none (never analysed)."""
    try:
        with conn.transaction():
            return conn.execute(f"SELECT _postgis_selectivity('walk.{table}', 'geom', {box})").fetchone()[0] or 0.0
    except psycopg.Error:
        return 0.0


def estimate(conn, points: list, o: Options | None = None, straight: list[bool] | None = None,
             kept: list[bool] | None = None) -> dict:
    """About how long a plan would take, before it is asked for: from how many stretches lie in the box each leg is
    searched in, as the database's statistics have it. Takes a millisecond."""
    o = o or Options()
    if not 2 <= len(points) <= MOST_POINTS:
        raise Invalid(f"a way goes through 2 to {MOST_POINTS} points")
    straight = list(straight) if straight is not None else [False] * (len(points) - 1)
    tuples = dict(conn.execute("""SELECT relname, greatest(reltuples, 0) FROM pg_class
                                  WHERE relnamespace = 'walk'::regnamespace AND relname IN ('edge', 'field')""").fetchall())
    xy = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY u(x, y, i),
                         LATERAL ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 4326), 25833) p ORDER BY i""",
                      ([float(p[0]) for p in points], [float(p[1]) for p in points])).fetchall()
    legs, crow = [], 0.0
    for n, ((x1, y1), (x2, y2)) in enumerate(itertools.pairwise(xy)):
        far = math.hypot(x2 - x1, y2 - y1)
        crow += far
        if (n < len(straight) and straight[n]) or (kept and n < len(kept) and kept[n]):
            legs.append(0.0)                # drawn straight, or kept as it was found: nothing to look for
            continue
        m = max(5000, 0.3 * far)
        box = f"ST_MakeEnvelope({min(x1, x2) - m}, {min(y1, y2) - m}, {max(x1, x2) + m}, {max(y1, y2) + m}, 25833)"
        edges = _share(conn, "edge", box) if tuples.get("edge") else 0.0
        fields = _share(conn, "field", box) if o.across and tuples.get("field") else 0.0
        legs.append(LEG_S + edges * tuples.get("edge", 0) * EDGE_S + fields * tuples.get("field", 0) * FIELD_S)
    return {"seconds": round(sum(legs), 1), "legs": [round(s, 1) for s in legs],
            "heights_seconds": round(HEIGHTS_S + crow * WALKED_PER_CROW / 1000 * HEIGHTS_S_PER_KM, 1)}


def near(conn, lon: float, lat: float, o: Options | None = None) -> dict:
    """How far a point is from the nearest path or road one may walk, and where on it: for the line dragged
    there, to tell a path from open ground."""
    try:
        found = _snap(conn, lon, lat, o or Options())
    except NotFound:
        return {"off_m": None, "at": None}
    return {"off_m": round(found["off_m"], 1), "at": found["at"], "kind": found["kind"]}


def _named(conn, steps: list[dict], coords: list) -> None:
    """The marked routes a step goes along, by their codes: Turrutebasen's, and ours (in use) where they lie."""
    marked = [s for s in steps if s["class"] in ("dnt", "marked")]
    if not marked:
        return
    mids = [coords[(s["at"][0] + s["at"][1]) // 2] for s in marked]
    with conn.cursor() as cur:
        found = cur.execute("""
            SELECT i, (SELECT string_agg(DISTINCT c, ', ' ORDER BY c) FROM (
                           SELECT unnest(t.codes) AS c FROM source_index.trail_segment t WHERE ST_DWithin(t.geom, p, 25)
                           UNION
                           SELECT r.code FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
                           JOIN core.route_leg l ON l.edge_id = e.id
                           JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
                           WHERE e.status = 'active' AND ST_DWithin(v.geom, p, 25)) x)
            FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY u(x, y, i),
                 LATERAL ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 4326), 25833) p""",
            ([m[0] for m in mids], [m[1] for m in mids])).fetchall()
    for i, codes in found:
        if codes:
            marked[i - 1]["routes"] = codes


def _says(s: dict) -> str:
    what = {"dnt": "Along DNT's marked route", "marked": "Along a marked route", "path": "Path",
            "road": "Road", "ferry": "Ferry", "off": "Straight, with no path (a sketch)",
            "field": "Across country, with no path"}[s["class"]]
    if s.get("routes"):
        what += f" {s['routes']}"
    if s["name"] and (s["class"] in ("path", "road", "ferry") or not s.get("routes")):
        what += f", {s['name']}"
    if s["ice"]:
        what += ", over a glacier"
    return what


def heights(conn, parts: list[dict], hours: float = 7.0, http=None) -> dict:
    """How high the way goes, what it climbs, the time it takes, and days that end at huts.

    `parts` are the way's steps in order, each {coords, ferry, rough, ice}: what is on a ferry is not walked,
    what is rough has no path (and takes longer), what is on ice is said. The heights are read from the height
    model kept here (`terrain`), every 50 m; until it has been fetched, or with `http`, Kartverket's height
    service is asked, less often on a long way.
    """
    local = http is None and terrain.ready()
    walked_m = sum(_metres(p["coords"]) for p in parts if not p.get("ferry"))
    step = STEP_M if local else max(STEP_M, walked_m / MOST_HEIGHTS)
    samples: list[tuple[float, float]] = []       # (east, north) of each point asked about
    at: list[float] = []                          # metres walked to it
    rough: list[bool] = []                        # whether the way to it from the one before has no path
    ferry_at: list[tuple[float, float, float]] = []     # (metres walked before, metres on board, minutes)
    so_far = on_ice = 0.0
    with conn.cursor() as cur:
        for p in parts:
            coords = p["coords"]
            if len(coords) < 2:
                continue
            if p.get("ferry"):
                m = _metres(coords)
                ferry_at.append((so_far, m, m / FERRY_KMH * 0.06))
                continue
            got = cur.execute("""
                SELECT ST_AsGeoJSON(ST_LineInterpolatePoints(l, f), 1)::json -> 'coordinates', f,
                       ST_X(ST_StartPoint(l)), ST_Y(ST_StartPoint(l)), ST_Length(ST_Transform(l, 4326)::geography)
                FROM ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 25833) l,
                     LATERAL (SELECT least(1, %s / greatest(ST_Length(l), 0.01)) AS f) k""",
                (json.dumps({"type": "LineString", "coordinates": coords}), step)).fetchone()
            pts, f, x0, y0, ground_m = got
            pts = pts if isinstance(pts[0], list) else [pts]
            # The first point, and then one each f of the way: as far along as that, on the ground.
            samples.append((x0, y0))
            at.append(so_far)
            rough.append(False)
            for k, (x, y) in enumerate(pts, 1):
                samples.append((x, y))
                at.append(so_far + min(1.0, k * f) * ground_m)
                rough.append(bool(p.get("rough")))
            so_far += ground_m
            on_ice += ground_m if p.get("ice") else 0.0
    if local:
        z = terrain.heights(samples)
        credit = terrain.CREDIT
    else:
        own = http is None
        http = http or height.client()
        try:
            z = [a[0] for a in height.ask(http, samples)]
        finally:
            if own:
                http.close()
        credit = CREDITS["height"]
    known = [h for h in z if h is not None]
    up, down = height.climb(known) if known else (0.0, 0.0)
    flat_pts = list(zip(at, [h if h is not None else (known[0] if known else 0) for h in z], strict=True))
    _, steep_down = height.steep(flat_pts) if len(flat_pts) > 1 else (0.0, 0.0)
    # Minutes to each point, so that days can be cut: Naismith over each step, with what it climbs, and slower
    # where there is no path.
    minutes, flat = [0.0], 0.0
    for k in range(1, len(flat_pts)):
        (a0, z0), (a1, z1) = flat_pts[k - 1], flat_pts[k]
        going = (a1 - a0) / 1000 / height.KM_PER_HOUR * 60 * (ROUGH if rough[k] else 1.0)
        flat += going
        minutes.append(minutes[-1] + going + max(0.0, z1 - z0) * height.MIN_PER_M_UP)
    days = _days(conn, samples, at, minutes, hours)
    shown = max(1, math.ceil(len(at) / PROFILE_POINTS))       # the profile as it is drawn: no more points than that
    keep = list(range(0, len(at), shown)) + ([len(at) - 1] if at and (len(at) - 1) % shown else [])
    return {"profile": {"at_m": [round(at[i]) for i in keep], "z": [None if z[i] is None else round(z[i], 1) for i in keep]},
            "up_m": round(up), "down_m": round(down), "highest_m": round(max(known)) if known else None,
            "lowest_m": round(min(known)) if known else None, "on_ice_m": round(on_ice),
            "walked_m": round(at[-1]) if at else 0,
            "rough_m": round(sum(at[k] - at[k - 1] for k in range(1, len(at)) if rough[k])),
            "minutes": round(flat + up * height.MIN_PER_M_UP + steep_down * height.MIN_PER_M_STEEP_DOWN),
            "ferries": [{"after_m": round(a), "metres": round(m), "minutes": round(t)} for a, m, t in ferry_at],
            "days": days, "credits": [credit], "every_m": round(step)}


def _days(conn, samples: list, at: list, minutes: list, hours: float) -> list[dict]:
    """The way cut into days that end at a hut near it: as far as a day goes, or a little over to reach one."""
    if len(samples) < 2:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        huts = cur.execute("""
            WITH l AS (SELECT ST_MakeLine(ST_MakePoint(x, y) ORDER BY i) AS g
                       FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY u(x, y, i)),
                 near AS (SELECT h.*, ST_LineLocatePoint(ST_SetSRID(l.g, 25833), h.geom) AS f
                          FROM walk.hut h, l WHERE ST_DWithin(h.geom, ST_SetSRID(l.g, 25833), %s))
            SELECT name, kind, operator, dnt, f, ST_X(ST_Transform(geom, 4326)) AS lon, ST_Y(ST_Transform(geom, 4326)) AS lat
            FROM near ORDER BY f""", ([float(s[0]) for s in samples], [float(s[1]) for s in samples], HUT_M)).fetchall()
    total = at[-1]
    for h in huts:
        h["at_m"] = h["f"] * total
        h["minute"] = minutes[min(bisect.bisect_left(at, h["at_m"]), len(at) - 1)]
    days, begun, begun_m = [], 0.0, 0.0
    end = minutes[-1]
    while end - begun > hours * 60 + DAY_OVER:
        reach = [h for h in huts if begun + 30 < h["minute"] <= begun + hours * 60]
        over = [h for h in huts if begun + hours * 60 < h["minute"] <= begun + hours * 60 + DAY_OVER]
        hut = reach[-1] if reach else over[0] if over else None
        if hut:
            days.append({"to": hut["name"] or "a hut", "hut": _hut(hut), "at_m": round(hut["at_m"]),
                         "minutes": round(hut["minute"] - begun), "metres": round(hut["at_m"] - begun_m)})
            begun, begun_m = hut["minute"], hut["at_m"]
        else:
            i = min(bisect.bisect_left(minutes, begun + hours * 60), len(minutes) - 1)
            days.append({"to": None, "hut": None, "at_m": round(at[i]), "minutes": round(minutes[i] - begun),
                         "metres": round(at[i] - begun_m)})
            begun, begun_m = minutes[i], at[i]
    days.append({"to": None, "hut": None, "at_m": round(total), "minutes": round(end - begun),
                 "metres": round(total - begun_m), "last": True})
    for n, d in enumerate(days, 1):
        d["day"] = n
    return days


def _hut(h: dict) -> dict:
    return {"name": h["name"], "kind": h["kind"], "operator": h["operator"], "dnt": h["dnt"],
            "lon": round(h["lon"], 6), "lat": round(h["lat"], 6)}
