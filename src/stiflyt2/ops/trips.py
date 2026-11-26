"""Trips for work: on foot from hut to hut, covering the routes that are to be seen to.

A feature of its own (ADR-005). Somebody is to inspect a handful of routes,
or all of an area, and asks how to walk them: this proposes a trip that
covers every stretch of them with as little as possible walked twice, cut
into days that end at a hut. It reads the routes and keeps nothing.

How it is worked out, on the stretches of the routes in use as one network:

1. The stretches to cover are joined into one piece, along the shortest ways
   between the pieces (a route that cannot be reached on foot is left out, and
   said so).
2. A junction where an odd number of them meet is a place where the walk must
   begin, end, or walk something twice. They are paired off, nearest first,
   and the way between each pair is walked once more. Two are left: where the
   trip begins and where it ends.
3. Then every stretch can be walked in one go (an Euler path), and is.
4. The walk is cut into days at huts, as few as the hours of a day allow and
   as even as they come. A day may run up to an hour over to reach a hut;
   where none lies within that, the day ends at a junction and says so (a
   night in a tent, or a day to walk on from). With a tent, a day may end
   at any junction.

Between the routes, and from where a trip begins to them, it may also go
along whatever paths and roads are loaded: paths from FKB, roads from NVDB,
and paths, tracks and footways from OpenStreetMap. They are looked for only
round the routes of the trip and the ways between their ends, and a route is
preferred where both run. Their heights are not known, and they are timed as
flat ground. A plan is computed and kept nowhere: what of OpenStreetMap is
in it is a produced work under ODbL, and is credited (`credits`).

Each day comes with walking instructions: along which route, path or road to
where, and where a part is walked back the same way.

Time is Naismith's rule as elsewhere here (`height.minutes`), for the way each
stretch is walked: up one way is down the other. Rests are not counted.
"""
from __future__ import annotations

import heapq
import itertools
import math
from collections import Counter
from dataclasses import dataclass, replace

from psycopg.rows import dict_row

from stiflyt2 import features, height, seasons
from stiflyt2.domain import routes as owned
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import areas as areas_of

features.feature("trips", "plan trips that cover routes", "ask to be given the feature `trips`")

HOURS = 7.0             # the walking of a day, with no rests, when nothing else is said
SLEEP = ("hut",)        # where a day may end
ENDS = ("hut", "parking")       # where a trip may begin and end
_DAY = 1e7              # what one more day costs, when the walk is cut into days: more than any day's evenness
_MOST_ODD = 400         # with more junctions than this to pair off, it is too much to ask for in one go
OFF_ROUTE = 1.25        # a path or road off the routes counts this much more than a route as long: routes first
MAPPED_M = 1500         # how far round the routes, and round the ways between their ends, paths and roads are looked for
TIE_M = 40              # a junction or a place this near a mapped path or road is joined to it
BESIDE_M = 20           # a mapped path or road this near a route for most of its length is that route, mapped
TWIN = 3.0              # and counts this much more than the route as long: it is there to join, not to walk instead
SMALL_M = 15            # a step shorter than this has no instruction of its own: it goes with the next
SHORT_LEG_M = 100       # nor does a run of steps shorter than this, once the instructions are put together
JOIN_M = 10             # paths and roads from different sources join where an end lies this near another line
# What of OpenStreetMap a trip may walk: paths, tracks and footways (its roads are NVDB's), not via ferratas nor
# what it says is demanding or difficult alpine walking.
OSM_WALKED = """SELECT 'osm' AS source, id, geom FROM source_index.osm_way
                WHERE on_foot AND kind <> 'via_ferrata'
                  AND coalesce(sac_scale, '') NOT IN ('demanding_alpine_hiking', 'difficult_alpine_hiking')"""
CREDITS = {"fkb_wfs": "Paths: FKB Traktorveg og sti, Kartverket, CC BY 4.0",
           "vegnett": "Roads: NVDB Vegnett Pluss, Kartverket, CC BY 4.0",
           "osm": "Paths: © OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)"}
JOIN_FAR_M = 25_000     # route ends further apart than this are not looked for a way between
MOST_MAPPED_M = 150_000     # a trip over more route than this does not look for paths and roads between them


@dataclass(frozen=True)
class Stretch:
    id: str
    a: str
    b: str
    metres: float
    there: float            # minutes from a to b
    back: float             # minutes from b to a
    up: float               # metres climbed from a to b: what is lost the other way
    down: float
    codes: tuple            # the routes in use that run on it
    coords: tuple           # (lon, lat) from a to b
    kind: str = "route"     # route; link (between a place's arrival points); path, road (off the routes); hop (onto one)
    name: str | None = None     # of a road
    twin: bool = False      # a path or road that lies along a route: the route, mapped
    source: str | None = None   # where a path or road comes from: fkb_wfs, vegnett, osm

    def minutes(self, frm: str) -> float:
        return self.there if frm == self.a else self.back

    @property
    def weight(self) -> float:
        """What it costs to walk, when the way it is walked is not known yet. Off the routes it counts for more."""
        return (self.there + self.back) / 2 * (1.0 if self.kind in ("route", "link") else TWIN if self.twin else OFF_ROUTE)


def network(conn, over_ice: bool = False) -> tuple[dict[str, Stretch], dict[str, dict]]:
    """Every stretch that can be walked: those of routes in use, and the links between a place's arrival points.

    Not water. Not ice either, unless asked for. And the places at the junctions. The summer's routes only:
    a winter route runs over frozen lakes and bogs, which are not walked.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(f"""
            SELECT e.id::text AS id, r.a_node::text AS a, r.b_node::text AS b, {height._LEN} AS metres, p.at_m, p.z, r.kind,
                   ST_AsGeoJSON(ST_Transform(r.geom, 4326), 5)::json->'coordinates' AS coords,
                   coalesce(u.codes, '{{}}') AS codes
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            LEFT JOIN height.profile p ON p.line = md5(ST_AsEWKB(r.geom))
            LEFT JOIN LATERAL (
                SELECT array_agg(t.code ORDER BY t.code) AS codes FROM core.route_leg l
                JOIN core.route t ON t.id = l.route_id
                WHERE l.edge_id = e.id AND t.status = 'active' AND t.season = %(summer)s
                  AND t.discontinued_since IS NULL) u ON true
            WHERE e.status = 'active' AND r.a_node <> r.b_node AND r.kind <> 'water'
              AND (%(ice)s OR r.kind <> 'glacier') AND (u.codes IS NOT NULL OR r.kind = 'connector')""",
            {"ice": over_ice, "summer": seasons.SUMMER}).fetchall()
        places = {p["node"]: p for p in cur.execute("""
            SELECT n.id::text AS node, p.id::text AS id, p.name, p.kind, n.id = p.primary_node_id AS first
            FROM core.node n JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
            WHERE n.status = 'active'""").fetchall()}
    stretches = {}
    for r in rows:
        points = [(at, z) for at, z in zip(r["at_m"] or [], r["z"] or [], strict=False) if z is not None]
        up, down = height.climb([z for _, z in points])
        steep_up, steep_down = height.steep(points)
        stretches[r["id"]] = Stretch(
            r["id"], r["a"], r["b"], r["metres"], height.minutes(r["metres"], up, steep_down),
            height.minutes(r["metres"], down, steep_up), up, down, tuple(r["codes"]),
            tuple((c[0], c[1]) for c in r["coords"]), "link" if r["kind"] == "connector" and not r["codes"] else "route")
    return stretches, places


def _flat(metres: float) -> float:
    return height.minutes(metres, 0.0)


def _joined(lines: list, join_m: float) -> list:
    """Lines (ref, coords) with where the ends of the others lie on them, within join_m: (ref, coords, cuts) as
    `routes._Paths` takes them. Worked out here on a grid, which is much quicker than in the database for the
    thousands of lines round a trip."""
    cell = 50.0
    grid: dict[tuple, list] = {}
    for i, (_, coords) in enumerate(lines):
        for k, (p, q) in enumerate(itertools.pairwise(coords)):
            for cx in range(int((min(p[0], q[0]) - join_m) // cell), int((max(p[0], q[0]) + join_m) // cell) + 1):
                for cy in range(int((min(p[1], q[1]) - join_m) // cell), int((max(p[1], q[1]) + join_m) // cell) + 1):
                    grid.setdefault((cx, cy), []).append((i, k))
    cuts: dict[int, dict] = {}
    for j, (_, coords) in enumerate(lines):
        for end in (coords[0], coords[-1]):
            for i, k in grid.get((int(end[0] // cell), int(end[1] // cell)), ()):
                if i == j:
                    continue
                p, q = lines[i][1][k], lines[i][1][k + 1]
                dx, dy = q[0] - p[0], q[1] - p[1]
                u = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((end[0] - p[0]) * dx + (end[1] - p[1]) * dy) / (dx * dx + dy * dy)))
                if math.dist(end, (p[0] + u * dx, p[1] + u * dy)) <= join_m:
                    cuts.setdefault(i, {}).setdefault(end, (j, k, u))
    out = []
    for i, (ref, coords) in enumerate(lines):
        total = owned._Paths.along(coords)
        said = []
        for end, (_, k, u) in cuts.get(i, {}).items():
            at = owned._Paths.along(coords[:k + 1]) + u * math.dist(coords[k], coords[k + 1])
            said.append((at / total if total else 0.0, end[0], end[1]))
        out.append((ref, coords, said))
    return out


def _beside(coords, routes, metres: float = BESIDE_M) -> bool:
    """Whether a line lies along the routes (a graph of their lines) for most of its length."""
    along = owned._Paths.along(coords)
    samples = [owned._between(coords, d, d)[0] for d in [*(i * 30.0 for i in range(int(along // 30) + 1)), along]]
    return sum(1 for pt in samples if routes.locate(pt, metres)) >= 0.5 * len(samples)


def _links(paths, ties, routes=None) -> dict[str, Stretch]:
    """Mapped paths and roads as stretches that can be walked, joined to the junctions that lie by them.

    `paths` is a graph of them (`routes._Paths`); `ties` are junctions and their (x, y), in the paths' own
    coordinates. A junction within TIE_M of a path or road is joined to its nearest point by a short hop, and
    the line is cut there. `routes`, a graph of the routes' own lines: a path or road that lies along them is
    the route itself, mapped. It is kept, as it may be what joins the rest to the route, but counts for much
    more than the route (TWIN), so that the route is walked where both run. What is made keeps the paths'
    coordinates: they are turned into lon, lat afterwards.
    """
    along = owned._Paths.along
    twins = {n for n, (_, coords, _, _) in enumerate(paths.pieces) if routes is not None and _beside(coords, routes)}
    on: dict[int, list] = {}
    made: dict[str, Stretch] = {}
    for node, xy in ties:
        spot = paths.locate(xy, TIE_M)
        if not spot:
            continue
        coords = spot["coords"]
        at = along(coords[:spot["i"] + 1]) + math.dist(coords[spot["i"]], spot["foot"])
        on.setdefault(spot["piece"], []).append((at, f"t:{node}", spot["foot"]))
        hop = math.dist(xy, spot["foot"])
        made[f"h:{node}"] = Stretch(f"h:{node}", node, f"t:{node}", hop, _flat(hop), _flat(hop), 0.0, 0.0, (),
                                    (tuple(xy), tuple(spot["foot"])), "hop")
    for n, (ref, coords, u, v) in enumerate(paths.pieces):
        total = along(coords)
        cuts = [(0.0, f"k:{u[0]}:{u[1]}", coords[0]), *sorted(on.get(n, [])), (total, f"k:{v[0]}:{v[1]}", coords[-1])]
        for k, ((lo, a, _), (hi, b, _)) in enumerate(itertools.pairwise(cuts)):
            if a == b:
                continue
            part = list(coords) if (lo, hi) == (0.0, total) else owned._between(coords, lo, hi)
            if len(part) < 2:
                part = [part[0], part[0]]
            metres = along(part)
            made[f"m{n}:{k}"] = Stretch(f"m{n}:{k}", a, b, metres, _flat(metres), _flat(metres), 0.0, 0.0, (),
                                        tuple(part), "road" if ref[0] == "vegnett" else "path", ref[1], n in twins, ref[0])
    return made


def _mapped(conn, stretches: dict[str, Stretch], places: dict[str, dict], cover: dict[str, set],
            begin: str | None) -> tuple[dict[str, Stretch], str | None]:
    """The paths and roads that a trip may take between its routes, and from where it begins: as stretches.

    Looked for round the routes to cover, round straight lines between the ends of them, and round a line from
    where the trip begins: within MAPPED_M of those. Not for more than MOST_MAPPED_M of route: then none, and why.
    """
    long = sum(stretches[e].metres for e in cover if e in stretches)
    if long > MOST_MAPPED_M:
        return {}, (f"Between the routes only along your routes: paths and roads are looked for in trips of up to "
                    f"{MOST_MAPPED_M // 1000} km of route, and this is {long / 1000:.0f} km.")
    meets: Counter = Counter()
    for e in cover:
        if e in stretches:
            meets[stretches[e].a] += 1
            meets[stretches[e].b] += 1
    ends = sorted(n for n, k in meets.items() if k % 2)
    with conn.cursor() as cur:
        at = {i: (x, y) for i, x, y in cur.execute(
            "SELECT id::text, ST_X(geom), ST_Y(geom) FROM core.node WHERE id = ANY(%s::uuid[])",
            ([*ends, *([begin] if begin else [])],)).fetchall()}
        between = [f"LINESTRING({at[p][0]:.1f} {at[p][1]:.1f},{at[q][0]:.1f} {at[q][1]:.1f})"
                   for p, q in itertools.combinations([n for n in ends if n in at], 2) if math.dist(at[p], at[q]) <= JOIN_FAR_M]
        geom = cur.execute("""
            WITH cover AS (SELECT ST_Collect(r.geom) AS g FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                           WHERE e.id = ANY(%(e)s::uuid[]))
            SELECT ST_AsEWKT(ST_Collect(ARRAY[c.g, ST_SetSRID(ST_GeomFromText(%(lines)s), 25833),
                                               (SELECT ST_ShortestLine(n.geom, c.g) FROM core.node n WHERE n.id = %(b)s::uuid)]))
            FROM cover c""", {"e": [e for e in cover if _uuid(e)], "b": begin,
                              "lines": f"MULTILINESTRING({','.join(x[10:] for x in between)})" if between else "MULTILINESTRING EMPTY"}).fetchone()[0]
        lines = owned.mapped_lines(conn)
        if conn.execute("SELECT to_regclass('source_index.osm_way') IS NOT NULL").fetchone()[0]:
            lines = f"({lines[1:-1]} UNION ALL {OSM_WALKED})"
        found = [((source, pid), [tuple(c[:2]) for c in g["coordinates"]]) for source, pid, g in cur.execute(
            f"""SELECT m.source, m.id, ST_AsGeoJSON(m.geom)::json FROM {lines} m
                WHERE ST_DWithin(m.geom, %s::geometry, %s) AND GeometryType(m.geom) = 'LINESTRING'""",
            (geom, MAPPED_M)).fetchall()]
        paths = owned._Paths(_joined(found, JOIN_M))
        if not paths.pieces:
            return {}, None
        nodes = sorted({x for s in stretches.values() for x in (s.a, s.b) if _uuid(x)} | set(places))
        ties = [(i, (x, y)) for i, x, y in cur.execute(
            """SELECT id::text, ST_X(geom), ST_Y(geom) FROM core.node WHERE status = 'active' AND id = ANY(%s::uuid[])
               AND ST_DWithin(geom, %s::geometry, %s)""", (nodes, geom, MAPPED_M + TIE_M)).fetchall()]
        routes = owned._Paths([(("route", e), [tuple(c[:2]) for c in g["coordinates"]], []) for e, g in cur.execute(
            """SELECT e.id::text, ST_AsGeoJSON(r.geom)::json FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
               WHERE e.id = ANY(%s::uuid[]) AND ST_DWithin(r.geom, %s::geometry, %s)""",
            ([e for e, s in stretches.items() if s.kind == "route" and _uuid(e)], geom, MAPPED_M)).fetchall()])
        made = _links(paths, ties, routes)
        # Into lon, lat, all at once; and the roads' names.
        points = [c for s in made.values() for c in s.coords]
        turned = cur.execute("""SELECT round(ST_X(p)::numeric, 6)::float8, round(ST_Y(p)::numeric, 6)::float8
                                FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS t(x, y, i),
                                     LATERAL ST_Transform(ST_SetSRID(ST_MakePoint(t.x, t.y), 25833), 4326) p ORDER BY t.i""",
                             ([float(c[0]) for c in points], [float(c[1]) for c in points])).fetchall()
        named = dict(cur.execute("SELECT id, name FROM source_index.road WHERE id = ANY(%s) AND name IS NOT NULL",
                                 ([s.name for s in made.values() if s.kind == "road"],)).fetchall())
    k, out = 0, {}
    for key, s in made.items():
        coords = tuple(turned[k:k + len(s.coords)])
        k += len(s.coords)
        out[key] = replace(s, coords=coords, name=named.get(s.name) if s.kind == "road" else None)
    return out, None


def _uuid(text: str) -> bool:
    return len(text) == 36 and text.count("-") == 4


def _ways(stretches: dict[str, Stretch]) -> dict[str, list]:
    ways: dict[str, list] = {}
    for s in stretches.values():
        ways.setdefault(s.a, []).append((s.b, s.id, s.weight))
        ways.setdefault(s.b, []).append((s.a, s.id, s.weight))
    return ways


def _reach(ways: dict[str, list], sources) -> tuple[dict[str, float], dict[str, tuple]]:
    """From some junctions at once: how far it is to every junction, and which way one came."""
    far = {n: 0.0 for n in sources}
    came: dict[str, tuple] = {}
    queue = [(0.0, n) for n in far]
    heapq.heapify(queue)
    while queue:
        d, here = heapq.heappop(queue)
        if d > far.get(here, float("inf")):
            continue
        for there, stretch, w in ways.get(here, ()):
            if d + w < far.get(there, float("inf")):
                far[there] = d + w
                came[there] = (here, stretch)
                heapq.heappush(queue, (d + w, there))
    return far, came


def _way_back(came: dict[str, tuple], node: str) -> list[str]:
    """The stretches from a junction back to where the search began."""
    way = []
    while node in came:
        node, stretch = came[node]
        way.append(stretch)
    return way


def plan(stretches: dict[str, Stretch], places: dict[str, dict], cover: dict[str, set], *, start: str | None = None,
         hours: float = HOURS, tent: bool = False, back_to_start: bool = False) -> dict:
    """A trip that walks every stretch in `cover` (stretch -> the routes it is wanted for), cut into days.

    `start` is a junction. With none, the trip begins and ends where the least is walked twice.
    """
    cover = {e: codes for e, codes in cover.items() if e in stretches}
    if not cover:
        raise Invalid("none of that can be walked: it is over water or ice, or not in use")
    ways = _ways(stretches)
    if start is not None and start not in ways:
        raise Invalid("no stretch that can be walked leads to where the trip is to begin")
    said_where = start is not None          # then it begins there, whatever kind of place that is

    # 1. The pieces the stretches to cover lie in, joined along the shortest ways between them.
    piece: dict[str, str] = {}

    def of(n: str) -> str:
        while piece.setdefault(n, n) != n:
            piece[n] = n = piece[piece[n]]
        return n

    for e in cover:
        piece[of(stretches[e].a)] = of(stretches[e].b)
    if start is not None:
        of(start)
    size: dict[str, float] = {}
    for e in cover:
        size[of(stretches[e].a)] = size.get(of(stretches[e].a), 0.0) + stretches[e].metres
    first = of(start) if start is not None else max(size, key=lambda p: (size[p], p))
    joined = {n for n in piece if of(n) == first}
    apart = {of(n) for n in piece} - {first}
    times = {e: 1 for e in cover}              # how many times each stretch is walked
    out_of_reach: set[str] = set()
    while apart:
        far, came = _reach(ways, joined)
        near = min((n for n in piece if of(n) in apart and n in far), key=lambda n: (far[n], n), default=None)
        if near is None:
            out_of_reach = {n for n in piece if of(n) in apart}
            break
        for e in _way_back(came, near):         # the way there is walked, once so far
            times[e] = times.get(e, 0) + 1
            joined.update((stretches[e].a, stretches[e].b))
        taken = of(near)
        joined.update(n for n in piece if of(n) == taken)
        apart.discard(taken)
    left_out = sorted({c for e, codes in cover.items() if stretches[e].a in out_of_reach for c in codes}, key=_by_code)
    for e in [e for e in times if stretches[e].a in out_of_reach and e in cover]:
        del times[e]
    if not any(e in times for e in cover):
        # Begun where none of it can be reached from: only the routes are walked, and none of them leads there.
        # Said so, with where it would begin if that were left to it.
        name = (places.get(start) or {}).get("name") or "where it was to begin"
        try:
            there = plan(stretches, places, cover, hours=hours, tent=tent)["days"][0]["from"]["place"]
        except Invalid:
            there = None
        raise Invalid(f"no way on foot leads from {name} to {', '.join(left_out)}"
                      + (f": begin at {there}, or leave where it begins empty" if there else ""))

    # 2. The junctions where an odd number of walks meet, paired off: nearest first.
    def odd_ones() -> list[str]:
        meets: dict[str, int] = {}
        for e, n in times.items():
            for node in (stretches[e].a, stretches[e].b):
                meets[node] = meets.get(node, 0) + n
        return sorted(n for n, k in meets.items() if k % 2)

    odd = odd_ones()
    if len(odd) > _MOST_ODD:
        raise Invalid("that is too much for one trip to be worked out: ask for fewer routes at a time")
    reach = {n: _reach(ways, [n]) for n in {*odd, *([start] if start is not None else [])}}
    # Where it begins and ends is chosen with the pairing, as what leaves the least to walk twice: a junction
    # left over ("*") is paired with nothing, at no cost, and is where the walk begins or ends.
    if back_to_start:
        to_pair, spare = list(odd), []
    elif start is not None:
        # The start is made odd if it is not, by pairing it too; the one left over is where it ends (the start
        # itself, if that is what is left: then the walk comes back to it).
        to_pair, spare = sorted(set(odd) ^ {start}), ["*end"]
    else:
        to_pair, spare = list(odd), ["*begin", "*end"] if odd else []

    def apart(p: str, q: str) -> float:
        if p.startswith("*") or q.startswith("*"):
            return float("inf") if p.startswith("*") and q.startswith("*") else 0.0
        return reach[p][0].get(q, float("inf"))

    ends = []
    for p, q in _paired([*to_pair, *spare], apart):
        if p.startswith("*") or q.startswith("*"):
            ends.append(q if p.startswith("*") else p)
            continue
        for e in _way_back(reach[p][1], q):
            times[e] = times.get(e, 0) + 1
    if start is None and len(ends) == 2:
        begin, _ = sorted(ends)
        # Of the two, the trip begins where one can sleep or park, if only one of them is such a place.
        if places.get(begin, {}).get("kind") not in ENDS:
            begin = next((n for n in ends if places.get(n, {}).get("kind") in ENDS), begin)
        start = begin

    # 3. Every stretch walked in one go: an Euler path, from where the trip begins.
    link: dict[str, list] = {}
    for e, n in sorted(times.items()):
        s = stretches[e]
        for _ in range(n):
            token = [e, False]          # one walk of the stretch, and whether it is done
            link.setdefault(s.a, []).append((s.b, token))
            link.setdefault(s.b, []).append((s.a, token))
    if start is None:
        start = min((n for n in link if places.get(n, {}).get("kind") in ENDS), default=min(link))
    if start not in link:       # where it begins lies off everything to cover: reached, and then the walk
        start = min(link)
    stack, walk = [(start, None)], []
    while stack:
        here = stack[-1][0]
        nxt = None
        while link.get(here):
            there, token = link[here].pop()
            if not token[1]:
                token[1] = True
                nxt = (there, token[0])
                break
        if nxt:
            stack.append(nxt)
        else:
            walk.append(stack.pop())
    walk.reverse()                      # junctions in the order they are come to, each with the stretch walked to it
    steps = [(by, walk[i - 1][0], here) for i, (here, by) in enumerate(walk) if by is not None]

    # To where one can sleep or park, at each end, when the walk does not begin and end at such a place.
    if not tent:
        ends_at = [n for n, p in places.items() if p["kind"] in ENDS and n in ways]
        for last in (False, True):
            tip = steps[-1][2] if last else steps[0][1]
            if places.get(tip, {}).get("kind") in ENDS or not ends_at or (said_where and not last):
                continue
            far, came = _reach(ways, [tip])
            near = min((n for n in ends_at if n in far), key=lambda n: (far[n], n), default=None)
            if near is None:
                continue
            way, here, extra = _way_back(came, near), near, []     # from the place back to the tip
            for e in way:
                there = stretches[e].a if stretches[e].b == here else stretches[e].b
                extra.append((e, here, there))
                here = there
            steps = steps + [(e, b, a) for e, a, b in reversed(extra)] if last else extra + steps

    # 4. Cut into days where one can sleep: as few as the hours allow, and as even as they come. A day may run
    # up to an hour over to reach a hut. Where none lies within that, the day ends at a junction all the same,
    # and says so: that night is in a tent, or the day is walked on from.
    limit = hours * 60
    clock = [0.0]
    for e, frm, _ in steps:
        clock.append(clock[-1] + stretches[e].minutes(frm))
    nodes = [steps[0][1], *(to for _, _, to in steps)]
    last = len(nodes) - 1
    beds = [i == last or tent or places.get(n, {}).get("kind") in SLEEP for i, n in enumerate(nodes)]
    best: list[tuple[float, int | None]] = [(0.0, None)] + [(float("inf"), None)] * last
    for j in range(1, last + 1):
        for i in range(j - 1, -1, -1):
            t = clock[j] - clock[i]
            if t > limit + 300 and best[j][1] is not None:
                break               # further back than this, no day begins
            if t <= 0 or best[i][0] == float("inf"):
                continue
            cost = best[i][0] + _DAY + t * t / 1e3 + _DAY * max(0.0, t - limit) / 30 + (0 if beds[j] else 2 * _DAY)
            if cost < best[j][0]:
                best[j] = (cost, i)
    cuts, j = [], last
    while j:
        cuts.append(j)
        j = best[j][1]
    cuts = [0, *reversed(cuts)]

    labels = _labels(steps, stretches, cover)
    routes_at: dict[str, dict] = {}
    for s in stretches.values():
        if s.kind == "route":
            for x in (s.a, s.b):
                at = routes_at.setdefault(x, {"codes": set(), "stretches": 0})
                at["codes"].update(s.codes)
                at["stretches"] += 1
    seen: set[str] = set()
    used: set[str] = set()          # where the paths and roads off the routes that are walked come from
    days = []
    for i, j in itertools.pairwise(cuts):
        day = {"day": len(days) + 1, "from": _named(places, nodes[i]), "to": _named(places, nodes[j]),
               "metres": 0.0, "up_m": 0.0, "down_m": 0.0, "minutes": 0.0, "new_m": 0.0, "routes": set(), "coords": []}
        legs: list[dict] = []
        for k, (e, frm, to) in enumerate(steps[i:j], start=i):
            s = stretches[e]
            forth = frm == s.a
            day["metres"] += s.metres
            day["minutes"] += s.minutes(frm)
            day["up_m"] += s.up if forth else s.down
            day["down_m"] += s.down if forth else s.up
            line = s.coords if forth else s.coords[::-1]
            first = max(len(day["coords"]) - 1, 0)
            day["coords"] += line if not day["coords"] else line[1:]
            if e in cover and e not in seen:
                seen.add(e)
                day["new_m"] += s.metres
                day["routes"] |= cover[e]
            if not legs or legs[-1]["label"] != labels[k]:
                legs.append({"label": labels[k], "metres": 0.0, "minutes": 0.0, "at": [first, first], "names": [], "kinds": set()})
            if s.kind in ("path", "road") and s.metres >= SMALL_M:
                legs[-1]["names"].append(_off_name(s))
                legs[-1]["kinds"].add(s.kind)
            if s.source:
                used.add(s.source)
            legs[-1]["metres"] += s.metres
            legs[-1]["minutes"] += s.minutes(frm)
            legs[-1]["at"][1] = len(day["coords"]) - 1
            legs[-1]["to"] = to
            legs[-1]["arrive"] = s
            legs[-1]["leave"] = stretches[steps[k + 1][0]] if k + 1 < len(steps) else None
        steps_said = []
        for leg in _tidy(legs):
            kind, what, again, back, _ = leg["label"]
            where = _where(leg["to"], what if kind == "route" else None, places, routes_at, leg["arrive"], leg["leave"])
            steps_said.append({"says": _say(kind, what, again, back, where, leg["names"]),
                               "kind": kind if kind == "route" else "road" if "road" in leg["kinds"] else "path", "again": again, "back": back, "metres": round(leg["metres"]),
                               "minutes": round(leg["minutes"]), "at": leg["at"]})
        day.update(metres=round(day["metres"]), up_m=round(day["up_m"]), down_m=round(day["down_m"]),
                   minutes=round(day["minutes"]), new_m=round(day["new_m"]), routes=sorted(day["routes"], key=_by_code),
                   long=day["minutes"] > limit + 1, no_hut=not beds[j], coords=[list(c) for c in day["coords"]],
                   steps=steps_said, back_m=sum(x["metres"] for x in steps_said if x["back"]),
                   off_route_m=sum(x["metres"] for x in steps_said if x["kind"] in ("path", "road")))
        days.append(day)
    total = {k: sum(d[k] for d in days) for k in ("metres", "up_m", "down_m", "minutes", "new_m", "back_m", "off_route_m")}
    total["twice_m"] = total["metres"] - total["new_m"]
    return {"days": days, "total": total, "covered": sorted({c for e in seen for c in cover[e]}, key=_by_code),
            "left_out": left_out, "hours": hours, "tent": tent, "credits": [CREDITS[k] for k in sorted(used) if k in CREDITS]}


def _paired(nodes: list[str], apart) -> list[tuple[str, str]]:
    """Junctions paired off so that the ways between the pairs are short in all: nearest first, then two pairs
    swapped wherever the other way of pairing their four is shorter, until no swap helps. Not always the
    shortest pairing there is, which takes more working out than a trip needs."""
    taken: set[str] = set()
    pairs = []
    for far, p, q in sorted((apart(p, q), p, q) for p, q in itertools.combinations(nodes, 2)):
        if p not in taken and q not in taken and far < float("inf"):
            taken.update((p, q))
            pairs.append((p, q))
    for _ in range(30):
        better = False
        for i, j in itertools.combinations(range(len(pairs)), 2):
            (a, b), (c, d) = pairs[i], pairs[j]
            now = apart(a, b) + apart(c, d)
            other, (x, y) = min((apart(a, c) + apart(b, d), ((a, c), (b, d))), (apart(a, d) + apart(b, c), ((a, d), (b, c))))
            if other < now - 1e-6:
                pairs[i], pairs[j], better = x, y, True
        if not better:
            break
    return pairs


def _labels(steps: list, stretches: dict[str, Stretch], cover: dict[str, set]) -> list[tuple]:
    """For each step of the walk, what it is walked as: (kind, route code, again, back the same way, turn).

    A route is followed by the code it was followed by, where the next stretch has it too. Paths and roads off
    the routes in a row are one instruction ("off"). A short hop onto one, a link between a place's arrival
    points, and any step shorter than SMALL_M, are part of what comes after.
    """
    done: dict[str, bool] = {}          # each stretch walked so far, and whether a to b the last time
    labels: list[tuple | None] = []
    code, turns, before = None, 0, None
    for e, frm, _ in steps:
        s = stretches[e]
        forth = frm == s.a
        again, back = e in done, e in done and done[e] != forth
        done[e] = forth
        if e == before:
            turns += 1                  # turned on the spot: back along what was just walked is a new instruction
        before = e
        if s.metres < SMALL_M or s.kind in ("hop", "link"):
            labels.append(None)
        elif s.kind == "route":
            wanted = sorted(cover.get(e, ()), key=_by_code) or sorted(s.codes, key=_by_code)
            code = code if code in s.codes else (wanted[0] if wanted else None)
            labels.append(("route", code, again, back, turns))
        else:
            code = None
            labels.append(("off", None, again, back, turns))
    for k in range(len(labels) - 1, -1, -1):        # what has no label of its own goes with what comes after it
        if labels[k] is None:
            labels[k] = labels[k + 1] if k + 1 < len(labels) else None
    for k, label in enumerate(labels):              # or, at the very end, with what came before
        if label is None:
            labels[k] = labels[k - 1] if k else ("route", None, False, False, 0)
    return labels


def _tidy(legs: list[dict]) -> list[dict]:
    """Instructions put together: one for less than SHORT_LEG_M goes with the next (the last with the one before),
    and two that then say the same thing are one."""
    def same(a: dict, b: dict) -> bool:
        return (a["label"][0], a["label"][1], a["label"][3]) == (b["label"][0], b["label"][1], b["label"][3])

    def into(a: dict, b: dict) -> None:         # b, which comes after a, is walked as part of a
        a["metres"] += b["metres"]
        a["minutes"] += b["minutes"]
        a["at"] = [a["at"][0], b["at"][1]]
        a["names"] = a["names"] + b["names"]
        a["kinds"] = a["kinds"] | b["kinds"]
        a.update(to=b["to"], arrive=b["arrive"], leave=b["leave"])

    out: list[dict] = []
    carry = None
    for k, leg in enumerate(legs):
        leg = dict(leg, at=list(leg["at"]))
        joined = carry is not None
        if carry is not None:
            first = carry
            into(first, leg)
            leg = dict(first, label=leg["label"])
            carry = None
        if leg["metres"] < SHORT_LEG_M and k < len(legs) - 1:
            carry = leg
            continue
        if out and ((joined and same(out[-1], leg)) or leg["metres"] < SHORT_LEG_M):
            into(out[-1], leg)
        else:
            out.append(leg)
    return out


def _where(node: str, code: str | None, places: dict[str, dict], routes_at: dict[str, set],
           arrive: Stretch | None = None, leave: Stretch | None = None) -> str:
    """What a spot is called in a walking instruction: its place, where it is on the routes, or what it is.

    A junction is named by the routes that come or go there: not those that run on through it with the walk,
    on the stretch it arrives by and the one it leaves by.
    """
    node = node.removeprefix("t:")      # the spot on a path or road a junction is joined to
    if node in places:
        return places[node]["name"]
    at = routes_at.get(node)
    if not at:
        return "a spot on the way"
    if code is None:                    # come to the routes from off them: which routes
        named = ", ".join(sorted(at["codes"], key=_by_code)[:3]) or "the routes"
        return f"the end of {named}" if at["stretches"] == 1 else named
    if at["stretches"] == 1:
        return f"the end of {code}"
    through = set(arrive.codes if arrive and arrive.kind == "route" else ()) & set(leave.codes if leave and leave.kind == "route" else ())
    here = at["codes"] - through - {code}
    if not here:
        here = at["codes"] - {code}
    if here:
        return f"the junction with {', '.join(sorted(here, key=_by_code)[:3])}"
    return f"a junction on {code}" if code else "a junction"


_STREETS = ("vegen", "veien", "vegjen", "gata", "gate", "stien", "vegen ", "veg", "vei")


def _say(kind: str, what: str | None, again: bool, back: bool, to: str, names: list[str] | None = None) -> str:
    if kind == "off":
        said = []
        for name in names or []:
            if name not in said:
                said.append(name)
        along = said[0] if len(said) == 1 else f"{', '.join(said[:-1])} and {said[-1]}" if said else "paths and roads"
        return f"{'Back the same way along' if back else 'Along'} {along}{' again' if again and not back else ''} to {to}"
    if back:
        return f"Back the same way{f' along {what}' if what else ''} to {to}"
    return f"Along {what or 'the route'}{' again' if again else ''} to {to}"


def _off_name(s: Stretch) -> str:
    """A path or road off the routes, as an instruction names it."""
    if s.kind == "path":
        return "an OpenStreetMap path" if s.source == "osm" else "a path"
    if not s.name:
        return "a road"
    return s.name if s.name.lower().endswith(_STREETS) else f"the road {s.name}"


def _by_code(code: str) -> tuple:
    return (len(code.split("/")[0]), code)


def _named(places: dict[str, dict], node: str) -> dict:
    p = places.get(node)
    return {"place": p["name"], "kind": p["kind"]} if p else {"place": None, "kind": None}


def for_routes(conn, *, routes: list[str] | None = None, area: str | None = None, start: str | None = None,
               hours: float = HOURS, tent: bool = False, over_ice: bool = False, back_to_start: bool = False,
               mapped: bool = True) -> dict:
    """A trip that covers some routes of yours, named by their codes, or every route in use of an area.

    `start` is a place, by its name or id. Routes that are discontinued are not
    walked. What of a route lies over water, or over ice unless that is asked
    for, is not walked either, and is said. With `mapped`, it may go along
    paths and roads between the routes, and from where it begins to them.

    The summer's routes only: a trip is walked, and a winter route is not.
    """
    if not 3 <= hours <= 14:
        raise Invalid("a day is between 3 and 14 hours of walking")
    with conn.cursor(row_factory=dict_row) as cur:
        if routes:
            asked = [c.strip() for c in routes if c and c.strip()]
            winter = sorted(c for c in asked if seasons.of_code(c) == seasons.WINTER)
            if winter:
                raise Invalid(f"{', '.join(winter)} {'is a winter route' if len(winter) == 1 else 'are winter routes'}: "
                              "a trip is planned over the summer's routes")
            found = cur.execute("""SELECT id::text, code FROM core.route WHERE status = 'active' AND season = %s
                                   AND discontinued_since IS NULL AND lower(code) = ANY(%s)""",
                                (seasons.SUMMER, [c.lower() for c in asked])).fetchall()
            lacking = sorted({c.lower() for c in asked} - {f["code"].lower() for f in found})
            if lacking:
                raise NotFound(f"no route of yours in use is called {', '.join(lacking)}")
        elif area:
            found = cur.execute("""SELECT id::text, code FROM core.route WHERE status = 'active' AND season = %s
                                   AND discontinued_since IS NULL
                                   AND (lower(left(code, 3)) = lower(%s) OR id = ANY(%s::uuid[]))""",
                                (seasons.SUMMER, area, areas_of.added_to(conn, area, season=seasons.SUMMER))).fetchall()
            if not found:
                raise NotFound(f"no route of yours in use begins with {area.lower()}")
        else:
            raise Invalid("say which routes, by their codes, or an area")
        legs = cur.execute("""SELECT l.edge_id::text AS edge, t.code, r.kind FROM core.route_leg l
                              JOIN core.route t ON t.id = l.route_id JOIN core.edge e ON e.id = l.edge_id
                              JOIN core.edge_revision r ON r.id = e.current_revision_id
                              WHERE l.route_id = ANY(%s::uuid[]) AND e.status = 'active'""",
                           ([f["id"] for f in found],)).fetchall()
        begin = None
        if start:
            at = cur.execute("""SELECT n.id::text AS node, p.name FROM core.place p JOIN core.node n ON n.place_id = p.id AND n.status = 'active'
                                WHERE p.status = 'confirmed' AND (p.id::text = %(s)s OR lower(p.name) = lower(%(s)s))
                                ORDER BY n.id = p.primary_node_id DESC""", {"s": start.strip()}).fetchall()
            if not at:
                raise NotFound(f"no place of yours is called {start}")
    stretches, places = network(conn, over_ice)
    cover: dict[str, set] = {}
    not_on_foot: dict[str, str] = {}
    for leg in legs:
        if leg["edge"] in stretches:
            cover.setdefault(leg["edge"], set()).add(leg["code"])
        elif leg["kind"] in ("water", "glacier"):
            not_on_foot[leg["code"]] = "over water" if leg["kind"] == "water" and not_on_foot.get(leg["code"]) != "over ice" else "over ice"
    notes = []
    if mapped:
        made, why = _mapped(conn, stretches, places, cover, at[0]["node"] if start else None)
        stretches.update(made)
        if why:
            notes.append(why)
        elif made:
            notes.append("Paths and roads off your routes are timed as flat ground: their heights are not known.")
    if start:
        begin = next((a["node"] for a in at if any(a["node"] in (s.a, s.b) for s in stretches.values())), None)
        if begin is None:
            raise Invalid(f"no route{', path or road' if mapped else ''} that can be walked comes to {at[0]['name']}")
    told = plan(stretches, places, cover, start=begin, hours=hours, tent=tent, back_to_start=back_to_start)
    told["notes"] = notes if any(d["off_route_m"] for d in told["days"]) or not mapped or any("only along" in n for n in notes) else []
    told["asked"] = sorted((f["code"] for f in found), key=_by_code)
    told["not_on_foot"] = [{"code": c, "what": w} for c, w in sorted(not_on_foot.items(), key=lambda x: _by_code(x[0]))]
    return told
