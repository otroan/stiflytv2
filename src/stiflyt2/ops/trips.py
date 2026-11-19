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

Time is Naismith's rule as elsewhere here (`height.minutes`), for the way each
stretch is walked: up one way is down the other. Rests are not counted.
"""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass

from psycopg.rows import dict_row

from stiflyt2 import features, height
from stiflyt2.domain.places import Invalid, NotFound

features.feature("trips", "plan trips that cover routes", "ask to be given the feature `trips`")

HOURS = 7.0             # the walking of a day, with no rests, when nothing else is said
SLEEP = ("hut",)        # where a day may end
ENDS = ("hut", "parking")       # where a trip may begin and end
_DAY = 1e7              # what one more day costs, when the walk is cut into days: more than any day's evenness
_MOST_ODD = 400         # with more junctions than this to pair off, it is too much to ask for in one go


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

    def minutes(self, frm: str) -> float:
        return self.there if frm == self.a else self.back

    @property
    def weight(self) -> float:
        """What it costs to walk, when the way it is walked is not known yet."""
        return (self.there + self.back) / 2


def network(conn, over_ice: bool = False) -> tuple[dict[str, Stretch], dict[str, dict]]:
    """Every stretch that can be walked: those of routes in use, and the links between a place's arrival points.

    Not water. Not ice either, unless asked for. And the places at the junctions.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(f"""
            SELECT e.id::text AS id, r.a_node::text AS a, r.b_node::text AS b, {height._LEN} AS metres, p.at_m, p.z,
                   ST_AsGeoJSON(ST_Transform(r.geom, 4326), 5)::json->'coordinates' AS coords,
                   coalesce(u.codes, '{{}}') AS codes
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            LEFT JOIN height.profile p ON p.line = md5(ST_AsEWKB(r.geom))
            LEFT JOIN LATERAL (
                SELECT array_agg(t.code ORDER BY t.code) AS codes FROM core.route_leg l
                JOIN core.route t ON t.id = l.route_id
                WHERE l.edge_id = e.id AND t.status = 'active' AND t.discontinued_since IS NULL) u ON true
            WHERE e.status = 'active' AND r.a_node <> r.b_node AND r.kind <> 'water'
              AND (%(ice)s OR r.kind <> 'glacier') AND (u.codes IS NOT NULL OR r.kind = 'connector')""",
            {"ice": over_ice}).fetchall()
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
            tuple((c[0], c[1]) for c in r["coords"]))
    return stretches, places


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
        raise Invalid(f"no way over your routes leads on foot from {name} to {', '.join(left_out)}"
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
    end = None
    if start is not None and not back_to_start:
        rest = sorted(set(odd) ^ {start})
        if rest:
            end = max(rest, key=lambda n: (reach[start][0].get(n, 0.0), n))
            rest.remove(end)
        to_pair = rest
    elif start is None and odd and not back_to_start:
        begin, end = max(itertools.combinations(odd, 2), key=lambda p: (reach[p[0]][0].get(p[1], 0.0), p))
        # Of the two, the trip begins where one can sleep or park, if only one of them is such a place.
        if (places.get(end, {}).get("kind") in ENDS) and places.get(begin, {}).get("kind") not in ENDS:
            begin, end = end, begin
        start, to_pair = begin, [n for n in odd if n not in (begin, end)]
    else:
        to_pair = list(odd)
    for n in to_pair:
        reach.setdefault(n, _reach(ways, [n]))
    pairs = sorted((reach[p][0].get(q, float("inf")), p, q) for p, q in itertools.combinations(to_pair, 2))
    paired: set[str] = set()
    for far_apart, p, q in pairs:
        if p in paired or q in paired or far_apart == float("inf"):
            continue
        paired.update((p, q))
        for e in _way_back(reach[p][1], q):
            times[e] = times.get(e, 0) + 1

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

    seen: set[str] = set()
    days = []
    for i, j in itertools.pairwise(cuts):
        day = {"day": len(days) + 1, "from": _named(places, nodes[i]), "to": _named(places, nodes[j]),
               "metres": 0.0, "up_m": 0.0, "down_m": 0.0, "minutes": 0.0, "new_m": 0.0, "routes": set(), "coords": []}
        for e, frm, _ in steps[i:j]:
            s = stretches[e]
            forth = frm == s.a
            day["metres"] += s.metres
            day["minutes"] += s.minutes(frm)
            day["up_m"] += s.up if forth else s.down
            day["down_m"] += s.down if forth else s.up
            line = s.coords if forth else s.coords[::-1]
            day["coords"] += line if not day["coords"] else line[1:]
            if e in cover and e not in seen:
                seen.add(e)
                day["new_m"] += s.metres
                day["routes"] |= cover[e]
        day.update(metres=round(day["metres"]), up_m=round(day["up_m"]), down_m=round(day["down_m"]),
                   minutes=round(day["minutes"]), new_m=round(day["new_m"]), routes=sorted(day["routes"], key=_by_code),
                   long=day["minutes"] > limit + 1, no_hut=not beds[j], coords=[list(c) for c in day["coords"]])
        days.append(day)
    total = {k: sum(d[k] for d in days) for k in ("metres", "up_m", "down_m", "minutes", "new_m")}
    total["twice_m"] = total["metres"] - total["new_m"]
    return {"days": days, "total": total, "covered": sorted({c for e in seen for c in cover[e]}, key=_by_code),
            "left_out": left_out, "hours": hours, "tent": tent}


def _by_code(code: str) -> tuple:
    return (len(code.split("/")[0]), code)


def _named(places: dict[str, dict], node: str) -> dict:
    p = places.get(node)
    return {"place": p["name"], "kind": p["kind"]} if p else {"place": None, "kind": None}


def for_routes(conn, *, routes: list[str] | None = None, area: str | None = None, start: str | None = None,
               hours: float = HOURS, tent: bool = False, over_ice: bool = False, back_to_start: bool = False) -> dict:
    """A trip that covers some routes of yours, named by their codes, or every route in use of an area.

    `start` is a place, by its name or id. Routes that are discontinued are not
    walked. What of a route lies over water, or over ice unless that is asked
    for, is not walked either, and is said.
    """
    if not 3 <= hours <= 14:
        raise Invalid("a day is between 3 and 14 hours of walking")
    with conn.cursor(row_factory=dict_row) as cur:
        if routes:
            asked = [c.strip() for c in routes if c and c.strip()]
            found = cur.execute("""SELECT id::text, code FROM core.route WHERE status = 'active'
                                   AND discontinued_since IS NULL AND lower(code) = ANY(%s)""",
                                ([c.lower() for c in asked],)).fetchall()
            lacking = sorted({c.lower() for c in asked} - {f["code"].lower() for f in found})
            if lacking:
                raise NotFound(f"no route of yours in use is called {', '.join(lacking)}")
        elif area:
            found = cur.execute("""SELECT id::text, code FROM core.route WHERE status = 'active'
                                   AND discontinued_since IS NULL AND lower(left(code, 3)) = lower(%s)""", (area,)).fetchall()
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
    if start:
        begin = next((a["node"] for a in at if any(a["node"] in (s.a, s.b) for s in stretches.values())), None)
        if begin is None:
            raise Invalid(f"no route that can be walked comes to {at[0]['name']}")
    cover: dict[str, set] = {}
    not_on_foot: dict[str, str] = {}
    for leg in legs:
        if leg["edge"] in stretches:
            cover.setdefault(leg["edge"], set()).add(leg["code"])
        elif leg["kind"] in ("water", "glacier"):
            not_on_foot[leg["code"]] = "over water" if leg["kind"] == "water" and not_on_foot.get(leg["code"]) != "over ice" else "over ice"
    told = plan(stretches, places, cover, start=begin, hours=hours, tent=tent, back_to_start=back_to_start)
    told["asked"] = sorted((f["code"] for f in found), key=_by_code)
    told["not_on_foot"] = [{"code": c, "what": w} for c, w in sorted(not_on_foot.items(), key=lambda x: _by_code(x[0]))]
    return told
