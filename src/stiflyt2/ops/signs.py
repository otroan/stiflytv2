"""Signs: where a post stands, what its signs say, and the orders taken out.

Everything a sign says is worked out from the routes as they are now, each
time it is asked for:

- **A post** stands where a route ends, where three ways or more meet, and
  at a hut or a parking place that a route passes. Junctions of one place
  within `SAME_M` of each other share a post. A place with junctions
  further apart has a post at each: one sign site, several posts. A person
  can also put a sign site at a spot on a route that is no junction.
- **A sign** points along one stretch out of the junction, to the next hut
  or parking place on a route that leaves that way, or to the end of that
  route if it ends first. Never past a hut, and never to a junction on the
  way (decided 2026-11-16). Routes that go the same way to the same
  place share a sign; two routes that reach it by different ways have a
  sign each, though they set out along one stretch. At an end point (a
  place where a route ends, never a junction on the way) with the next hut
  near (under `NEAR_KM`), people walk on: the post also points to where the
  signs at that hut point, as far as `ONWARD_KM` in all (decided 2026-11-17,
  from the post at Viva: Sprongdalshytta is 2.5 km off, and the post points
  on to Arentzbu and Sota sæter). A person can add a destination the routes
  do not give: a place nearer than the next hut, or further on. It points
  the shortest way there. And a post may carry a sign with the name of the
  place, where nothing else says where one is.
- **The distance** is the metres along the route times a factor for what is
  walked more than the line (1.05, as v1 measured it from GPX tracks), cut
  down to the half kilometre under 10 km and rounded to the whole from 10;
  under half a kilometre, to the hundred metres ("0.2 km").
  Where the way leads over a glacier or by boat it is the distance to the
  edge of the ice or to the boat, and the sign says so.
- **The arrow** is worked out from where the post stands. A sign points
  along its stretch; read by someone standing at the junction and looking
  at the post, that is to the left, to the right or straight ahead. Move
  the post to the other side of the path and the arrows turn.

On a post the signs hang as the sign manual has it: the place's name on
top, the signs that point right together and those that point left
together, and of signs that point the same way the one with the longest
distance on top.

Most signs hang where they are to hang already. An order is the signs a
person has marked to be made: the worn one here, the two there that say
something that is no longer so, now and then all the signs of a site that
is new. What an order held is kept as it was, and a sign that has been ordered is
waiting until a person says it is up.

What is kept (`ops`) is what a person has decided: that a site has no signs,
where a post stands, another arrow, text, distance or colour, a sign taken
out, a further post; and each order as it was taken out. A sign is found
again by the junction it is at, the stretch it points along and where it
points to, all three ids of the routes' own, which hold when the routes are
mended.

The routes are never changed from here.
"""
from __future__ import annotations

import heapq
import math
import uuid
from collections import Counter

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2.domain import identity
from stiflyt2.domain.places import Conflict, Invalid, NotFound

FACTOR = 1.05               # walked against the line, as v1 measured it
DESTINATIONS = ("hut", "parking")
ICE_EDGE = "glacier_edge"   # a place where a route steps onto the ice: somewhere a sign points to, and no end of a day
SAME_M = 30                 # junctions of one place this near each other share a post
ON_ROUTE_M = 50             # a sign site put at a spot is on the stretch of a route this near
OFF_JUNCTION_M = 15         # and at least this far from the stretch's ends, where the junction's own site is
PLACE_M = 150               # a destination clicked this near a place of ours is that place
NEARBY_M = 500              # a name in Stedsnavn this near a site with no name says where the site is
NEARBY_GROUPS = ["terreng", "ferskvann", "markslag", "sjø"]     # what lies in the terrain,
NEARBY_TYPES = ["seterStøl", "turisthytte", "hotell", "fjellgard", "bru", "klopp", "vad"]      # and what a walker steers by
NEAR_KM = 5                 # a hut nearer than this, as its sign says it, is one people walk on from
ONWARD_KM = 25              # and the post points on to where that hut's signs point, up to this far in all
NO_STRETCH = "00000000-0000-0000-0000-000000000000"     # a sign added by hand has no stretch of its own
POST_OUT_M = 4.0            # how far from the junction a post is assumed to stand
AHEAD_DEG = 25              # an arrow within this of straight away from the reader points straight ahead
BEARING_OVER_M = 20         # which way a stretch leaves is taken over this far
AT_THE_EDGE_M = 100         # a sign nearer the ice or the boat than this gives no distance
# A sign site has no state of its own: its signs hang, are to be made, or are waiting to be put up. The one
# thing said of a site is that there are to be no signs there. (`ops.sign_site.status` holds it: 'rejected'.)
NO_SIGNS = "rejected"
_HANGS = {"høyre": 0, "venstre": 1, "rett fram": 2}    # the order of the groups on a post
DIRECTIONS = ("venstre", "rett fram", "høyre")
COLOURS = ("trehvit", "grønn")
VIA = {"glacier": ("bre", "til brekanten"), "water": ("båt", "til båt")}
_LEN = "ST_Length(ST_Transform(r.geom, 4326)::geography)"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def shown_km(metres: float | None, factor: float = FACTOR) -> float | None:
    """The kilometres a sign says: under 10 cut down to the half, from 10 rounded to the whole.

    Under half a kilometre it is cut down to the hundred metres, and is never
    less than that: "0.2 km" (decided 2026-11-17; v1 said 0.0).
    """
    if metres is None:
        return None
    km = metres / 1000 * factor
    if km < 0.5:
        return max(0.1, math.floor(km * 10 + 1e-9) / 10)
    return math.floor(km * 2) / 2 if km < 10 else float(round(km))


def bearing(a: tuple, b: tuple) -> float:
    """The compass bearing from a to b, in degrees."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def arrow(junction: tuple, post: tuple, leaves: float) -> str:
    """Which way a sign on the post points, as read by someone at the junction looking at the post."""
    turn = (leaves - bearing(junction, post) + 180) % 360 - 180     # to the right is positive
    return "rett fram" if abs(turn) <= AHEAD_DEG else "høyre" if turn > 0 else "venstre"


def stands(junction: tuple, leaving: list[float]) -> tuple:
    """Where a post is assumed to stand: beside the junction, in the widest opening between the ways out."""
    ways = sorted(set(leaving))
    if not ways:
        towards = 0.0
    elif len(ways) == 1:
        towards = (ways[0] + 90) % 360
    else:
        gap, start = max(((b - a) % 360 or 360, a) for a, b in zip(ways, [*ways[1:], ways[0]], strict=True))
        towards = (start + gap / 2) % 360
    return (junction[0] + POST_OUT_M * math.sin(math.radians(towards)),
            junction[1] + POST_OUT_M * math.cos(math.radians(towards)))


class _Network:
    """Every route in use, as walked: its stretches from junction to junction, and what lies at the junctions."""

    def __init__(self, conn) -> None:
        legs = _rows(conn, f"""
            SELECT t.code, e.id::text AS edge, r.kind, r.a_node::text AS a, r.b_node::text AS b, {_LEN} AS metres
            FROM core.route t JOIN core.route_leg l ON l.route_id = t.id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE t.status = 'active' AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL
              AND r.a_node <> r.b_node""")
        self.of_route: dict[str, dict[str, list[dict]]] = {}      # route -> junction -> the stretches of it there
        self.at: dict[str, dict[str, dict]] = {}                  # junction -> stretch -> the stretch
        self.routes_at: dict[str, set[str]] = {}
        self.routes_on: dict[str, set[str]] = {}                  # stretch -> the routes that walk it
        for leg in legs:
            self.routes_on.setdefault(leg["edge"], set()).add(leg["code"])
            for here, there in ((leg["a"], leg["b"]), (leg["b"], leg["a"])):
                way = {"edge": leg["edge"], "to": there, "metres": leg["metres"], "kind": leg["kind"]}
                self.of_route.setdefault(leg["code"], {}).setdefault(here, []).append(way)
                self.at.setdefault(here, {})[leg["edge"]] = way
                self.routes_at.setdefault(here, set()).add(leg["code"])
        self.node = {n["id"]: n for n in _rows(conn, """
            SELECT n.id::text AS id, ST_X(n.geom) AS x, ST_Y(n.geom) AS y,
                   p.id::text AS place, p.name, p.kind
            FROM core.node n LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
            WHERE n.id = ANY(%(ids)s::uuid[])""", {"ids": list(self.at)})}
        # Sign sites at a spot on a stretch. Each is a place to stand on the way, with a way to either end.
        self.spots = _spots(conn)
        self.spot = {spot["id"]: spot for spot in self.spots}
        for spot in self.spots:
            self.node[spot["id"]] = {"id": spot["id"], "x": spot["x"], "y": spot["y"], "place": None, "name": None,
                                     "kind": None}

    def place(self, node: str) -> str | None:
        return self.node[node]["place"]

    def is_end(self, node: str) -> bool:
        return any(len(ways.get(node, ())) == 1 for ways in self.of_route.values())

    def has_post(self, node: str) -> bool:
        return (self.is_end(node) or len(self.at[node]) >= 3 or self.node[node]["kind"] in DESTINATIONS)

    def reach(self, route: str, start: str, first: dict) -> list[dict]:
        """Where a sign at `start` points along `first` on this route: the next hut or parking, or the route's end."""
        home, ways, found = self.place(start), self.of_route[route], []
        stack = [({**first, "from": start}, 0.0, None, frozenset())]
        while stack:
            way, so_far, via, walked = stack.pop()
            if way["kind"] in VIA and via is None:          # from here it is over ice or by boat: the sign says how far to there
                via = {"kind": way["kind"], "after_m": so_far, "from": way["from"]}
            here, so_far, walked = way["to"], so_far + way["metres"], walked | {way["edge"]}
            spot = self.node[here]
            onward = [w for w in ways.get(here, ()) if w["edge"] not in walked]
            arrived = spot["kind"] in DESTINATIONS and spot["place"] != home
            if here == start:
                continue                                    # round and back: no sign to where one stands
            if arrived or not onward:
                if spot["place"] == home and home is not None:
                    continue                                # the route ends inside the place the post is at
                found.append({"to_type": "place" if spot["place"] else "node", "to_id": spot["place"] or here,
                              "name": spot["name"], "kind": spot["kind"], "metres": so_far, "via": via, "way": walked})
                continue
            stack.extend(({**w, "from": here}, so_far, via, walked) for w in onward)
        return found

    def shortest(self, start: str, first: list[dict] | None, goals: set[str], spot: dict | None = None) -> list[dict] | None:
        """The shortest way over the routes from a junction to one of some junctions, or to a spot on a stretch.

        `first`: the ways out, where the start is itself a spot on a stretch.
        Given as the stretches walked, each with where it is walked from.
        """
        best, came = {start: 0.0}, {}
        heap = [(0.0, start)]
        while heap:
            so_far, here = heapq.heappop(heap)
            if so_far > best.get(here, math.inf):
                continue
            for way in (first if here == start and first is not None else self.at.get(here, {}).values()):
                there = so_far + way["metres"]
                if there < best.get(way["to"], math.inf):
                    best[way["to"]], came[way["to"]] = there, (here, way)
                    heapq.heappush(heap, (there, way["to"]))
        last = None
        if spot:        # the last of the way is along the spot's own stretch, from whichever end is nearer by
            ends = [(best[n] + m, n, m) for n, m in ((spot["a"], spot["to_a_m"]), (spot["b"], spot["to_b_m"])) if n in best]
            if not ends:
                return None
            _, end, metres = min(ends)
            last = {"edge": spot["edge"], "to": spot["id"], "metres": metres, "kind": spot["kind"], "from": end}
        else:
            reached = [n for n in goals if n in best and n != start]
            if not reached:
                return None
            end = min(reached, key=best.get)
        walked = [last] if last else []
        while end != start:
            before, way = came[end]
            walked.append({**way, "from": before})
            end = before
        return walked[::-1]

    def along(self, edges: list[str]) -> list[str]:
        """The routes a way follows, in order: each for as long as it lasts."""
        def lasts(code: str, i: int) -> int:
            j = i
            while j < len(edges) and code in self.routes_on.get(edges[j], ()):
                j += 1
            return j - i

        routes, i = [], 0
        while i < len(edges):
            on = sorted(self.routes_on.get(edges[i], ()), key=lambda c: (len(c), c))
            if not on:
                i += 1
                continue
            far = [lasts(code, i) for code in on]
            routes.append(on[far.index(max(far))])
            i += max(far)
        return routes


def _sites(conn, net: _Network, area: str | None) -> list[dict]:
    """The sign sites as the graph gives them, before what a person has decided: posts, and the signs on each."""
    with_post = [n for n in net.at if net.has_post(n)]
    # One post for the junctions of a place that lie close together; the best connected one is where it stands.
    posts: list[list[str]] = []
    for node in sorted(with_post, key=lambda n: (-len(net.at[n]), -len(net.routes_at[n]), n)):
        spot = net.node[node]
        near = next((p for p in posts if spot["place"] and net.place(p[0]) == spot["place"]
                     and math.dist((spot["x"], spot["y"]), (net.node[p[0]]["x"], net.node[p[0]]["y"])) <= SAME_M), None)
        if near:
            near.append(node)
        else:
            posts.append([node])
    # What the routes give each post, for every post there is: a post near a hut points on to where the hut's do.
    gives: list[dict] = []
    at_place: dict[str, list[int]] = {}
    for nodes in posts:
        signs: dict[tuple, dict] = {}
        for node in nodes:
            for route in sorted(net.routes_at[node]):
                for first in net.of_route[route][node]:
                    for dest in net.reach(route, node, first):
                        # One sign per destination and route: from the junction of the post it is nearest by.
                        seen = signs.get((dest["to_id"], route))
                        if seen is None or dest["metres"] < seen["metres"]:
                            signs[(dest["to_id"], route)] = {**dest, "node": node, "edge": first["edge"], "route": route}
        if net.place(nodes[0]):
            at_place.setdefault(net.place(nodes[0]), []).append(len(gives))
        gives.append(signs)
    sites: dict[tuple, dict] = {}
    for nodes, signs in zip(posts, gives, strict=True):
        main = net.node[nodes[0]]
        routes = sorted({c for n in nodes for c in net.routes_at[n]}, key=lambda c: (len(c), c))
        if area and not any(c.lower().startswith(area.lower()) for c in routes):
            continue
        key = ("place", main["place"]) if main["place"] else ("node", main["id"])
        site = sites.setdefault(key, {"at_type": key[0], "at_id": key[1], "place_name": main["name"],
                                      "kind": main["kind"], "routes": set(), "posts": [], "free": False})
        site["routes"].update(routes)
        on = _onward(signs, main["place"], gives, at_place) if _end_point(net, nodes) else {}
        site["posts"].append({"node": main["id"], "nodes": nodes, "at": (main["x"], main["y"]),
                              "signs": _shared({**signs, **on})})
    for spot in net.spots:      # a site a person has put on a route: signs both ways along its stretch
        routes = sorted(net.routes_on.get(spot["edge"], ()), key=lambda c: (len(c), c))
        if not spot["site"] or not routes or (area and not any(c.lower().startswith(area.lower()) for c in routes)):
            continue
        signs = {}
        for route in routes:
            for to, metres, leaves in ((spot["a"], spot["to_a_m"], spot["leaves_a"]), (spot["b"], spot["to_b_m"], spot["leaves_b"])):
                first = {"edge": spot["edge"], "to": to, "metres": metres, "kind": spot["kind"]}
                for dest in net.reach(route, spot["id"], first):
                    seen = signs.get((dest["to_id"], route))
                    if seen is None or dest["metres"] < seen["metres"]:
                        signs[(dest["to_id"], route)] = {**dest, "node": spot["id"], "edge": spot["edge"], "route": route,
                                                         "leaves": leaves}
        sites[("point", spot["id"])] = {
            "at_type": "point", "at_id": spot["id"], "place_name": None, "kind": None, "routes": set(routes), "free": True,
            "posts": [{"node": spot["id"], "nodes": [spot["id"]], "at": (spot["x"], spot["y"]),
                       "ways": [spot["leaves_a"], spot["leaves_b"]], "signs": _shared(signs)}]}
    return list(sites.values())


def _end_point(net: _Network, nodes: list[str]) -> bool:
    """Whether a post stands at an end point: a place where a route ends. A junction in the terrain is not one,
    though a route may end there; neither is a place that routes only pass, nor the edge of a glacier."""
    return (bool(net.place(nodes[0])) and net.node[nodes[0]]["kind"] != ICE_EDGE
            and any(net.is_end(n) for n in nodes))


def _onward(mine: dict, home: str | None, gives: list[dict], at_place: dict[str, list[int]]) -> dict:
    """The signs a post at an end point has because a hut is near: to where the signs at that hut point.

    Only at an end point, a place where a route ends: at a junction on the
    way no such thing is weighed, and the edge of a glacier has no part in it:
    not as where the post stands, not as the place that is near, not as
    where to point on to (decided 2026-11-17). From a post with a place
    under `NEAR_KM` off, people walk the next stage too. So the post points, the same way as to that place, on to each
    place its posts point to, where that is under `ONWARD_KM` in all, is not
    back the way one came, and is not somewhere the post points to already.
    The shortest way to each.
    """
    have = {s["to_id"] for s in mine.values()}
    onward: dict[tuple, dict] = {}
    for near in mine.values():
        if near["to_type"] != "place" or near.get("kind") == ICE_EDGE or near["metres"] * FACTOR >= NEAR_KM * 1000:
            continue
        for i in at_place.get(near["to_id"], ()):
            for on in gives[i].values():
                total = near["metres"] + on["metres"]
                if (on["to_type"] != "place" or on.get("kind") == ICE_EDGE or on["to_id"] in have
                        or on["to_id"] in (home, near["to_id"])
                        or near["way"] & on["way"] or total * FACTOR >= ONWARD_KM * 1000):
                    continue
                seen = onward.get((on["to_id"], "onward"))
                if seen is None or total < seen["metres"]:
                    via = near["via"] or (on["via"] and {**on["via"], "after_m": near["metres"] + on["via"]["after_m"]})
                    onward[(on["to_id"], "onward")] = {
                        **near, "to_type": on["to_type"], "to_id": on["to_id"], "name": on["name"], "metres": total,
                        "via": via, "way": near["way"] | on["way"], "then": [on["route"]], "onward": near["name"]}
    return onward


def _shared(signs: dict) -> list[dict]:
    """Routes that go the same way to the same place share a sign. Two that set out along one stretch and
    reach the place by different ways have a sign each, told apart by a route of each, unless the two signs
    would say the same."""
    merged: dict[tuple, dict] = {}
    for sign in signs.values():
        key = (sign["node"], sign["edge"], sign["to_type"], sign["to_id"], sign["way"])
        routes = [sign["route"], *sign.get("then", ())]
        if key in merged:
            merged[key]["routes"].extend(routes)
        else:
            merged[key] = {**sign, "routes": routes}
    # Two ways that come to the same on the sign (the same kilometres, over the same ice or water) are one
    # sign all the same: there is nothing to tell them apart by on the wood.
    says: dict[tuple, dict] = {}
    for key, sign in merged.items():
        via = sign["via"]
        same = (*key[:4], shown_km(via["after_m"] if via else sign["metres"]), via and (via["kind"], via["from"]))
        if same in says:
            says[same]["routes"].extend(sign["routes"])
        else:
            says[same] = sign
    for sign in says.values():
        sign["routes"] = list(dict.fromkeys(sign["routes"]))
    together = Counter(same[:4] for same in says)
    for same, sign in says.items():
        sign["apart"] = min(sign["routes"], key=lambda c: (len(c), c)) if together[same[:4]] > 1 else ""
    return sorted(says.values(), key=lambda s: (s["edge"], s["metres"]))


def _with_added(conn, net: _Network, sites: list[dict], added: list[dict]) -> None:
    """Hang on each post the destinations a person has added to it: each the shortest way there over the routes."""
    if not added:
        return
    names = dict(conn.execute("SELECT id::text, name FROM core.place WHERE status = 'confirmed' AND id = ANY(%s::uuid[])",
                              ([str(r["to_id"]) for r in added if r["to_type"] == "place"],)).fetchall())
    posts = {node: post for site in sites for post in site["posts"] for node in post["nodes"]}
    for row in added:
        node, to_id = str(row["node_id"]), str(row["to_id"])
        post = posts.get(node)
        if not post:
            continue            # no post there any more, or one of another area
        mine = net.spot.get(node)
        first = None if not mine else [
            {"edge": mine["edge"], "to": mine[end], "metres": mine[f"to_{end}_m"], "kind": mine["kind"]} for end in "ab"]
        goal = net.spot.get(to_id) if row["to_type"] == "point" else None
        goals = {n for n, v in net.node.items() if v["place"] == to_id} if row["to_type"] == "place" else set()
        way = net.shortest(node, first, goals, goal) if (goal or goals) else None
        sign = {"node": node, "key_edge": NO_STRETCH, "to_type": row["to_type"], "to_id": to_id, "apart": "",
                "name": names.get(to_id) if row["to_type"] == "place" else None, "added": True}
        if not way:
            post["signs"].append({**sign, "edge": None, "metres": None, "via": None, "routes": [], "leaves": None})
            continue
        over, so_far, via = [], 0.0, None
        for leg in way:
            if leg["kind"] in VIA and via is None:
                via = {"kind": leg["kind"], "after_m": so_far, "from": leg["from"]}
            so_far += leg["metres"]
            over.append(leg["edge"])
        sign.update(edge=over[0], metres=so_far, via=via, routes=net.along(over))
        if mine:
            sign["leaves"] = mine["leaves_a"] if way[0]["to"] == mine["a"] else mine["leaves_b"]
        post["signs"].append(sign)


def _spots(conn) -> list[dict]:
    """The spots on a route that people have put sign sites at: where on which stretch, and the ways from there.

    A spot keeps the stretch it was put on. When that stretch has been
    divided or replaced since, it is on the one of those that it lies nearest.
    """
    spots = []
    for point in _rows(conn, """
            SELECT sp.id::text AS id, sp.edge_id::text AS edge,
                   EXISTS (SELECT 1 FROM ops.sign_site s WHERE s.at_type = 'point' AND s.at_id = sp.id) AS site
            FROM ops.sign_point sp WHERE NOT sp.removed"""):
        try:
            now = identity.current(conn, "edge", point["edge"])["now"]
        except NotFound:
            continue
        found = _rows(conn, f"""
            SELECT e.id::text AS edge, r.kind, r.a_node::text AS a, r.b_node::text AS b,
                   ST_X(c.at) AS x, ST_Y(c.at) AS y, l.f * {_LEN} AS to_a_m, (1 - l.f) * {_LEN} AS to_b_m,
                   ST_X(c.back) AS ax, ST_Y(c.back) AS ay, ST_X(c.onward) AS bx, ST_Y(c.onward) AS by
            FROM ops.sign_point sp JOIN core.edge e ON e.id = ANY(%(now)s::uuid[]) AND e.status = 'active'
            JOIN core.edge_revision r ON r.id = e.current_revision_id,
            LATERAL (SELECT ST_LineLocatePoint(r.geom, sp.geom) AS f,
                            %(over)s / greatest(ST_Length(r.geom), 0.01) AS step) l,
            LATERAL (SELECT ST_LineInterpolatePoint(r.geom, l.f) AS at,
                            ST_LineInterpolatePoint(r.geom, greatest(0, l.f - l.step)) AS back,
                            ST_LineInterpolatePoint(r.geom, least(1, l.f + l.step)) AS onward) c
            WHERE sp.id = %(id)s ORDER BY ST_Distance(r.geom, sp.geom) LIMIT 1""",
            {"now": now, "id": point["id"], "over": BEARING_OVER_M})
        if found:
            f = found[0]
            spots.append({"id": point["id"], "site": point["site"], **{k: f[k] for k in ("edge", "kind", "a", "b", "x", "y", "to_a_m", "to_b_m")},
                          "leaves_a": bearing((f["x"], f["y"]), (f["ax"], f["ay"])),
                          "leaves_b": bearing((f["x"], f["y"]), (f["bx"], f["by"]))})
    return spots


def _leaving(conn, pairs: set[tuple]) -> dict[tuple, float]:
    """Which way each stretch leaves each junction: the bearing over its first metres from there."""
    if not pairs:
        return {}
    listed = sorted(pairs)
    found = _rows(conn, """
        SELECT v.node::text AS node, v.edge::text AS edge, ST_X(n.geom) AS x, ST_Y(n.geom) AS y,
               ST_X(p.at) AS px, ST_Y(p.at) AS py
        FROM unnest(%(nodes)s::uuid[], %(edges)s::uuid[]) AS v(node, edge)
        JOIN core.node n ON n.id = v.node JOIN core.edge e ON e.id = v.edge
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
        LATERAL (SELECT ST_LineInterpolatePoint(
                     CASE WHEN r.a_node = v.node THEN r.geom ELSE ST_Reverse(r.geom) END,
                     least(1.0, %(over)s / greatest(ST_Length(r.geom), 0.01))) AS at) p""",
        {"nodes": [n for n, _ in listed], "edges": [e for _, e in listed], "over": BEARING_OVER_M})
    return {(f["node"], f["edge"]): bearing((f["x"], f["y"]), (f["px"], f["py"])) for f in found}


def _positions(conn, points: list[tuple]) -> list[dict]:
    """Longitude and latitude of some points, and each as UTM in the zone it lies in, to ten metres."""
    if not points:
        return []
    found = _rows(conn, """
        SELECT ST_X(g.w) AS lon, ST_Y(g.w) AS lat, g.zone,
               ST_X(ST_Transform(g.p, 25800 + g.zone)) AS e, ST_Y(ST_Transform(g.p, 25800 + g.zone)) AS n
        FROM unnest(%(x)s::float8[], %(y)s::float8[]) WITH ORDINALITY AS v(x, y, i),
        LATERAL (SELECT ST_SetSRID(ST_MakePoint(v.x, v.y), 25833) AS p) q,
        LATERAL (SELECT q.p, ST_Transform(q.p, 4326) AS w,
                        (floor((ST_X(ST_Transform(q.p, 4326)) + 180) / 6) + 1)::int AS zone) g
        ORDER BY v.i""", {"x": [p[0] for p in points], "y": [p[1] for p in points]})
    for f in found:
        band = "CDEFGHJKLMNPQRSTUVWX"[min(19, int((f["lat"] + 80) // 8))]
        f["utm"] = f"UTM {f['zone']}{band} {round(f['e'] / 10) * 10} {round(f['n'] / 10) * 10}"
        f["lon"], f["lat"] = round(f["lon"], 6), round(f["lat"], 6)
    return found


def _glaciers(conn, nodes: set[str]) -> dict[str, str]:
    """What the glacier is called where a way steps onto the ice: from the place made at its edge."""
    return {r["id"]: r["name"].split(" brekant")[0] for r in _rows(conn, """
        SELECT n.id::text AS id, p.name FROM core.node n JOIN core.place p ON p.id = n.place_id
        WHERE p.status = 'confirmed' AND p.kind = 'glacier_edge' AND n.id = ANY(%(ids)s::uuid[])""",
        {"ids": list(nodes)})}


def _nearby(conn, points: list[tuple]) -> list[dict | None]:
    """For each point, the nearest name in Stedsnavn within `NEARBY_M`: what a site with no name lies by."""
    if not points:
        return []
    found = {r["i"]: r for r in _rows(conn, """
        SELECT v.i, n.name, n.type, round(n.m)::int AS metres
        FROM unnest(%(x)s::float8[], %(y)s::float8[]) WITH ORDINALITY AS v(x, y, i),
        LATERAL (SELECT p.name, p.type, ST_Distance(p.geom, g.at) AS m
                 FROM (SELECT ST_SetSRID(ST_MakePoint(v.x, v.y), 25833) AS at) g, source_index.place p
                 WHERE ST_DWithin(p.geom, g.at, %(near)s) AND NOT p.alternative AND p.name_status = 'hovednavn'
                   AND (p.main_group = ANY(%(groups)s) OR p.type = ANY(%(types)s))
                 ORDER BY ST_Distance(p.geom, g.at), p.name LIMIT 1) n""",
        {"x": [p[0] for p in points], "y": [p[1] for p in points], "near": NEARBY_M,
         "groups": NEARBY_GROUPS, "types": NEARBY_TYPES})}
    return [({"name": found[i]["name"], "type": found[i]["type"], "metres": found[i]["metres"]} if i in found else None)
            for i in range(1, len(points) + 1)]


def sign_key(node: str, edge: str, to_type: str, to_id: str, apart: str = "") -> str:
    """What a sign is found by: its junction, the stretch it points along, where it points to, and, where
    two signs share all three, a route of its own."""
    return f"{node}|{edge}|{to_type}:{to_id}" + (f"|{apart}" if apart else "")


def _key_parts(key: str) -> tuple[str, str, str, str, str]:
    try:
        node, edge, to, *apart = key.split("|")
        to_type, to_id = to.split(":")
        if to_type not in ("place", "node", "point") or len(apart) > 1:
            raise ValueError(key)
        return node, edge, to_type, to_id, apart[0] if apart else ""
    except ValueError:
        raise NotFound("no such sign") from None


def _said(conn) -> dict:
    """What people have decided, and what the latest order said of each sign."""
    ordered: dict[str, dict] = {}
    for order in _rows(conn, "SELECT id, at, by, rows FROM ops.sign_order ORDER BY id"):
        for row in order["rows"]:
            ordered[row["key"]] = {**row, "order": order["id"], "at": order["at"], "by": order["by"]}
    return {
        "sites": {(s["at_type"], str(s["at_id"])): s for s in _rows(conn, "SELECT * FROM ops.sign_site")},
        "posts": {(str(p["node_id"]), p["no"]): p for p in _rows(conn, """
            SELECT node_id, no, taken_out, name_sign, name_text, name_to_order, name_up_order,
                   ST_X(geom) AS x, ST_Y(geom) AS y
            FROM ops.sign_post""")},
        "signs": {sign_key(str(s["node_id"]), str(s["first_edge"]), s["to_type"], str(s["to_id"]), s["route"]): s
                  for s in _rows(conn, "SELECT * FROM ops.sign")},
        "ordered": ordered,
    }


def areas(conn) -> list[dict]:
    """The areas that have routes of ours in use: the three letters their codes start with."""
    return _rows(conn, """
        SELECT lower(left(code, 3)) AS area, count(*)::int AS routes FROM core.route
        WHERE status = 'active' AND discontinued_since IS NULL AND unpublished_why IS NULL AND code ~* '^[a-z]{3}'
        GROUP BY 1 ORDER BY 2 DESC, 1""")


def listing(conn, area: str | None = None) -> dict:
    """The sign sites of an area (or of everything): each with its posts and what the signs on them say.

    What a person has decided is laid over what the graph gives; each sign
    also says what it would say untouched, and whether it has changed since
    it was last ordered.
    """
    net = _Network(conn)
    sites, said = _sites(conn, net, area), _said(conn)
    _with_added(conn, net, sites, [s for s in said["signs"].values() if s["added"]])
    leaves = _leaving(conn, {(s["node"], s["edge"]) for site in sites for p in site["posts"] for s in p["signs"]
                             if "leaves" not in s and s["edge"]}
                      | {(p["node"], e) for site in sites for p in site["posts"] for e in net.at.get(p["node"], ())})
    ice = _glaciers(conn, {s["via"]["from"] for site in sites for p in site["posts"] for s in p["signs"]
                           if s["via"] and s["via"]["from"]})
    # A site with no name of its own says what it lies by, if something near has a name.
    nameless = [s for s in sites if not (said["sites"].get((s["at_type"], s["at_id"])) or {}).get("name") and not s["place_name"]]
    lies_by = {(s["at_type"], s["at_id"]): near
               for s, near in zip(nameless, _nearby(conn, [s["posts"][0]["at"] for s in nameless]), strict=True)}
    out = []
    for site in sites:
        kept = said["sites"].get((site["at_type"], site["at_id"])) or {}
        name = kept.get("name") or site["place_name"]
        posts = []
        for post in site["posts"]:
            ways = post.get("ways") or [leaves[(post["node"], e)] for e in net.at[post["node"]]
                                        if (post["node"], e) in leaves]
            # Further posts at the same junction are a person's; each sign hangs on the post it is said to, or the first.
            numbers = sorted({1} | {no for (node, no) in said["posts"] if node == post["node"]})
            by_no = {}
            for no in numbers:
                row = said["posts"].get((post["node"], no)) or {}
                at = (row["x"], row["y"]) if row.get("x") is not None else stands(post["at"], ways)
                if no > 1 and row.get("x") is None:
                    at = (at[0] + 2.0 * (no - 1), at[1])
                by_no[no] = {"key": f"{post['node']}:{no}", "node": post["node"], "no": no, "at": at,
                             "assumed": row.get("x") is None, "taken_out": bool(row.get("taken_out")), "signs": [],
                             # A sign with the place's name and no arrow, where a person has said the post has one.
                             "name_sign": {"key": f"{post['node']}:{no}|name", "text": row.get("name_text") or name,
                                           "to_order": bool(row.get("name_to_order")), "ordered": None,
                                           "_up": row.get("name_up_order")}
                             if row.get("name_sign") else None}
            for sign in post["signs"]:
                key = sign_key(sign["node"], sign.get("key_edge", sign["edge"]), sign["to_type"], sign["to_id"], sign["apart"])
                own = said["signs"].get(key) or {}
                on = by_no.get(own.get("post_no") or 1) or by_no[1]
                if sign["metres"] is None:      # added by hand, and no way leads there over the routes
                    on["signs"].append({
                        "key": key, "text": own.get("text") or sign["name"], "km": None, "direction": own.get("direction"),
                        "colour": own.get("colour") or COLOURS[0], "routes": [],
                        "generated": {"text": sign["name"], "direction": None, "km": None}, "by_hand": [],
                        "bearing": None, "metres": None, "added": True, "no_way": True, "onward": None,
                        "to_order": False, "why": None, "_up": None,
                        "to": {"type": sign["to_type"], "id": sign["to_id"], "name": sign["name"]},
                        "unnamed": not (own.get("text") or sign["name"]), "taken_out": bool(own.get("taken_out")),
                        "ordered": None})
                    continue
                via = sign["via"]
                metres = via["after_m"] if via else sign["metres"]
                generated, worked_km = sign["name"], shown_km(metres)
                if via:
                    word, till = VIA[via["kind"]]
                    generated = f"{sign['name'] or own.get('text') or '?'} via {ice.get(via['from']) or word}"
                    if metres < AT_THE_EDGE_M:
                        worked_km = None                        # at the edge already: a distance of nothing says nothing
                    else:
                        generated += f" ({till})"
                # A distance said by hand is what the sign says, rounded the same way.
                km = worked_km if own.get("distance_m") is None else shown_km(own["distance_m"], 1.0)
                node_at = (net.node[sign["node"]]["x"], net.node[sign["node"]]["y"])
                way = sign["leaves"] if "leaves" in sign else leaves.get((sign["node"], sign["edge"]))
                worked_out = arrow(node_at, on["at"], way) if way is not None else None
                on["signs"].append({
                    "key": key, "text": own.get("text") or generated, "km": km,
                    "direction": own.get("direction") or worked_out, "colour": own.get("colour") or COLOURS[0],
                    "routes": sorted(sign["routes"], key=lambda c: (len(c), c)),
                    "generated": {"text": generated, "direction": worked_out, "km": worked_km},
                    "by_hand": sorted(k for k in ("text", "direction", "colour", "distance_m", "post_no")
                                      if own.get(k) is not None and not (k == "text" and sign["to_type"] == "point")),
                    "bearing": None if way is None else round(way), "metres": round(metres),
                    "to": {"type": sign["to_type"], "id": sign["to_id"], "name": sign["name"]},
                    "added": bool(sign.get("added")), "no_way": False, "onward": sign.get("onward"),
                    "to_order": bool(own.get("to_order")), "why": own.get("why"), "_up": own.get("up_order"),
                    "unnamed": not (own.get("text") or sign["name"]), "taken_out": bool(own.get("taken_out")),
                    "ordered": None})
            posts.extend(by_no.values())
        for post in posts:          # two signs on a post that say the same are told apart by their routes
            texts = [s["text"] for s in post["signs"] if not s["taken_out"]]
            for sign in post["signs"]:
                if sign["text"] and texts.count(sign["text"]) > 1 and "text" not in sign["by_hand"]:
                    sign["text"] = f"{sign['text']} ({', '.join(sign['routes'])})"
            # What it said when it was last ordered, whether it says the same now, and whether it is up.
            for sign in post["signs"]:
                before, up = said["ordered"].get(sign["key"]), sign.pop("_up")
                if before:
                    sign["ordered"] = {"order": before["order"], "at": before["at"], "by": before["by"],
                                       "changed": any(before.get(k) != sign[k] for k in ("text", "km", "direction", "colour")),
                                       "up": up == before["order"]}
            if post["name_sign"]:
                before, up = said["ordered"].get(post["name_sign"]["key"]), post["name_sign"].pop("_up")
                if before:
                    post["name_sign"]["ordered"] = {"order": before["order"], "at": before["at"], "by": before["by"],
                                                    "changed": before.get("text") != post["name_sign"]["text"],
                                                    "up": up == before["order"]}
            # As they hang (the sign manual): those that point right together, those that point left together,
            # and of signs that point the same way the longest distance on top. What is taken out, last.
            post["signs"].sort(key=lambda g: (g["taken_out"], _HANGS.get(g["direction"], 3),
                                              g["bearing"] if g["bearing"] is not None else 999,
                                              -(g["km"] if g["km"] is not None else -1), g["text"] or ""))
        where = _positions(conn, [p["at"] for p in posts] + [site["posts"][0]["at"]])
        for post, at in zip(posts, where, strict=False):
            post.update(lon=at["lon"], lat=at["lat"], utm=at["utm"])
            del post["at"]
        centre = where[-1]
        foreign = sorted({c[:3].lower() for c in site["routes"]} - ({area.lower()} if area else set()))
        out.append({
            "key": f"{site['at_type']}:{site['at_id']}", "name": name, "named": bool(name), "kind": site["kind"],
            "free": site["free"], "near": lies_by.get((site["at_type"], site["at_id"])),
            "no_signs": kept.get("status") == NO_SIGNS, "code": kept.get("code"),
            "routes": sorted(site["routes"], key=lambda c: (len(c), c)), "other_areas": foreign if area else [],
            "lon": centre["lon"], "lat": centre["lat"],
            "back_text": kept.get("back_text") or "\n".join(filter(None, (name, centre["utm"]))),
            "back_text_by_hand": bool(kept.get("back_text")), "name_by_hand": bool(kept.get("name")),
            "send_to_name": kept.get("send_to_name"), "send_to_address": kept.get("send_to_address"),
            "posts": posts})
    out.sort(key=lambda s: (s["name"] is None, s["name"] or "", s["key"]))
    signs = [s for site in out for p in site["posts"] if not p["taken_out"] for s in p["signs"] if not s["taken_out"]]
    return {"area": area, "factor": FACTOR, "near_km": NEAR_KM, "onward_km": ONWARD_KM, "sites": out,
            "totals": {"sites": len(out), "no_signs": sum(s["no_signs"] for s in out),
                       "posts": sum(not p["taken_out"] for s in out for p in s["posts"]), "signs": len(signs),
                       "onward": sum(bool(s["onward"]) for s in signs),
                       "waiting": sum(bool(s["ordered"]) and not s["ordered"]["up"] for s in signs) + sum(
                           bool(p["name_sign"] and p["name_sign"]["ordered"] and not p["name_sign"]["ordered"]["up"])
                           and not p["taken_out"] for s in out for p in s["posts"]),
                       "to_order": sum(s["to_order"] for s in signs) + sum(
                           bool(p["name_sign"] and p["name_sign"]["to_order"]) and not p["taken_out"]
                           for s in out for p in s["posts"]),
                       "name_signs": sum(bool(p["name_sign"]) and not p["taken_out"] for s in out for p in s["posts"]),
                       "unnamed": sum(s["unnamed"] for s in signs),
                       "changed_since_ordered": sum(bool(s["ordered"] and s["ordered"]["changed"]) for s in signs)}}


# --- what a person decides ---------------------------------------------------------------------------------

def _event(conn, author, subject: str, subject_id: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), subject, subject_id, type_, Jsonb(payload)))


def _site_parts(key: str) -> tuple[str, str]:
    at_type, _, at_id = key.partition(":")
    if at_type not in ("place", "node", "point") or not at_id:
        raise NotFound("no such sign site")
    return at_type, at_id


def _is_spot(conn, some_id: str) -> bool:
    try:
        some_id = str(uuid.UUID(str(some_id)))
    except ValueError:
        return False
    return bool(conn.execute("SELECT 1 FROM ops.sign_point WHERE id = %s AND NOT removed", (some_id,)).fetchone())


def _current(conn, subject: str, subject_id: str) -> str:
    """The id a junction, place or stretch has now: itself, or what came instead of it."""
    if subject == "point" or (subject == "node" and _is_spot(conn, subject_id)):     # a spot is where it was put
        if not _is_spot(conn, subject_id):
            raise NotFound("no such sign site")
        return subject_id
    table = {"node": "node", "place": "place", "edge": "edge"}[subject]
    now = identity.current(conn, table, subject_id)
    if now["state"] not in ("current", "replaced"):
        raise NotFound(f"that {subject} is no longer there")
    return now["now"][0]


def _site_row(conn, at_type: str, at_id: str, area: str | None, author) -> dict:
    """What is kept about a site, made when there is nothing yet."""
    was = _rows(conn, "SELECT * FROM ops.sign_site WHERE at_type = %(t)s AND at_id = %(i)s FOR UPDATE",
                {"t": at_type, "i": at_id})
    if not was:
        if not area:
            raise Invalid("say which area the site is in")
        conn.execute("INSERT INTO ops.sign_site (at_type, at_id, area, updated_by) VALUES (%s, %s, %s, %s)",
                     (at_type, at_id, area.lower(), str(author)))
        was = _rows(conn, "SELECT * FROM ops.sign_site WHERE at_type = %(t)s AND at_id = %(i)s",
                    {"t": at_type, "i": at_id})
    return was[0]


def set_site(conn, key: str, author, *, area: str | None = None, no_signs: bool | None = None, name: str | None = None,
             back_text: str | None = None, send_to_name: str | None = None, send_to_address: str | None = None) -> None:
    """Say that a site is to have no signs (or is to have them after all), or say its name, the text on the back
    of its signs and who they go to. An empty text takes what was said by hand away again.

    A site has no other state: it is its signs that hang, are ordered and are put up.
    """
    at_type, at_id = _site_parts(key)
    at_id = _current(conn, at_type, at_id)
    was = _site_row(conn, at_type, at_id, area, author)
    changed = {}
    if no_signs is not None and no_signs != (was["status"] == NO_SIGNS):
        changed["status"] = [was["status"], NO_SIGNS if no_signs else "proposed"]
    for field, value in (("name", name), ("back_text", back_text),
                         ("send_to_name", send_to_name), ("send_to_address", send_to_address)):
        if value is None:
            continue
        value = value.strip() or None
        if value != was[field]:
            changed[field] = [was[field], value]
    if not changed:
        return
    conn.execute("""UPDATE ops.sign_site SET status = %(status)s, name = %(name)s, back_text = %(back_text)s,
                        send_to_name = %(send_to_name)s, send_to_address = %(send_to_address)s,
                        updated_at = now(), updated_by = %(by)s WHERE id = %(id)s""",
                 {**{k: was[k] for k in ("status", "name", "back_text", "send_to_name", "send_to_address")},
                  **{k: v[1] for k, v in changed.items()}, "by": str(author), "id": was["id"]})
    _event(conn, author, "site", f"{at_type}:{at_id}", "site.changed",
           {("no_signs" if k == "status" else k): ([v[0] == NO_SIGNS, v[1] == NO_SIGNS] if k == "status" else v)
            for k, v in changed.items()})


def _next_code(conn, area: str) -> str:
    prefix = f"{area.upper()}-SS-"
    last = conn.execute("""SELECT coalesce(max(substr(code, %s)::int), 0) FROM ops.sign_site
                           WHERE code LIKE %s""", (len(prefix) + 1, prefix + "%")).fetchone()[0]
    return f"{prefix}{last + 1:04d}"


def add_site(conn, author, area: str, lon: float, lat: float, name: str | None = None) -> str:
    """Put a sign site at a spot on a route that is no junction. It has its code from the start."""
    found = _rows(conn, """
        SELECT e.id::text AS edge, l.f * ST_Length(r.geom) AS from_a, (1 - l.f) * ST_Length(r.geom) AS from_b,
               ST_AsEWKT(ST_LineInterpolatePoint(r.geom, l.f)) AS at
        FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833) AS g) p,
             core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_LineLocatePoint(r.geom, p.g) AS f) l
        WHERE e.status = 'active' AND r.kind <> 'water' AND ST_DWithin(r.geom, p.g, %(near)s)
          AND EXISTS (SELECT 1 FROM core.route_leg g JOIN core.route t ON t.id = g.route_id AND t.status = 'active'
                      AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL WHERE g.edge_id = e.id)
        ORDER BY ST_Distance(r.geom, p.g) LIMIT 1""", {"lon": lon, "lat": lat, "near": ON_ROUTE_M})
    if not found:
        raise Invalid(f"a sign site stands on a route: that spot is more than {ON_ROUTE_M} m from every route in use")
    if min(found[0]["from_a"], found[0]["from_b"]) < OFF_JUNCTION_M:
        raise Invalid("that is at a junction, which has a sign site of its own: open that one, and give it another "
                      "post if there is more than one")
    spot = str(conn.execute("""INSERT INTO ops.sign_point (edge_id, geom, created_by) VALUES (%s, %s::geometry, %s)
                               RETURNING id""", (found[0]["edge"], found[0]["at"], str(author))).fetchone()[0])
    code = _next_code(conn, area)
    conn.execute("""INSERT INTO ops.sign_site (at_type, at_id, area, code, name, updated_by)
                    VALUES ('point', %s, %s, %s, %s, %s)""",
                 (spot, area.lower(), code, (name or "").strip() or None, str(author)))
    _event(conn, author, "site", f"point:{spot}", "site.put", {"lon": lon, "lat": lat, "code": code, "edge": found[0]["edge"]})
    return f"point:{spot}"


def remove_site(conn, key: str, author) -> None:
    """Take away a sign site that a person put at a spot. A site the graph gives is not taken away: it is rejected."""
    at_type, at_id = _site_parts(key)
    if at_type != "point":
        raise Invalid("a sign site at a junction or a place is there because the routes are: say 'no signs here' instead")
    if not _is_spot(conn, at_id):
        raise NotFound("no such sign site")
    conn.execute("UPDATE ops.sign_point SET removed = true WHERE id = %s", (at_id,))
    _event(conn, author, "site", key, "site.taken_away", {})


def _post_parts(conn, key: str) -> tuple[str, int]:
    node, _, no = key.partition(":")
    if not no.isdigit() or int(no) < 1:
        raise NotFound("no such post")
    return _current(conn, "node", node), int(no)


def set_post(conn, key: str, author, *, lon: float | None = None, lat: float | None = None,
             assumed: bool = False, taken_out: bool | None = None, further: bool = False,
             name_sign: bool | None = None, name_text: str | None = None, name_to_order: bool | None = None) -> None:
    """Say where a post stands (which turns its arrows), put it back where it is assumed to stand, take it out,
    or say that it carries a sign with the place's name on top (and what that says, if not the site's name)."""
    node, no = _post_parts(conn, key)
    if no > 1 and not further and not conn.execute(
            "SELECT 1 FROM ops.sign_post WHERE node_id = %s AND no = %s", (node, no)).fetchone():
        raise NotFound("no such post")
    conn.execute("""INSERT INTO ops.sign_post (node_id, no, updated_by) VALUES (%s, %s, %s)
                    ON CONFLICT (node_id, no) DO NOTHING""", (node, no, str(author)))
    if lon is not None and lat is not None:
        far = conn.execute("""
            SELECT ST_Distance(n.geom, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833))
            FROM (SELECT geom FROM core.node WHERE id = %s
                  UNION ALL SELECT geom FROM ops.sign_point WHERE id = %s) n""", (lon, lat, node, node)).fetchone()
        if far is None:
            raise NotFound("no such junction")
        if far[0] > 400:
            raise Invalid(f"a post stands by its junction: that is {round(far[0])} m from it. "
                          "For a post at another junction, use the post there")
        conn.execute("""UPDATE ops.sign_post SET geom = ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833),
                            updated_at = now(), updated_by = %s WHERE node_id = %s AND no = %s""",
                     (lon, lat, str(author), node, no))
        _event(conn, author, "post", f"{node}:{no}", "post.moved", {"lon": lon, "lat": lat})
    elif assumed:
        conn.execute("""UPDATE ops.sign_post SET geom = NULL, updated_at = now(), updated_by = %s
                        WHERE node_id = %s AND no = %s""", (str(author), node, no))
        _event(conn, author, "post", f"{node}:{no}", "post.put_back", {})
    if taken_out is not None:
        conn.execute("""UPDATE ops.sign_post SET taken_out = %s, updated_at = now(), updated_by = %s
                        WHERE node_id = %s AND no = %s""", (taken_out, str(author), node, no))
        _event(conn, author, "post", f"{node}:{no}", "post.taken_out" if taken_out else "post.back_in", {})
    if name_sign is not None or name_text is not None:
        conn.execute("""UPDATE ops.sign_post SET name_sign = coalesce(%s, name_sign),
                            name_text = CASE WHEN %s::text IS NULL THEN name_text ELSE nullif(trim(%s), '') END,
                            updated_at = now(), updated_by = %s WHERE node_id = %s AND no = %s""",
                     (name_sign, name_text, name_text, str(author), node, no))
        _event(conn, author, "post", f"{node}:{no}", "post.name_sign",
               {k: v for k, v in (("on", name_sign), ("says", name_text)) if v is not None})
    if name_to_order is not None:
        conn.execute("""UPDATE ops.sign_post SET name_to_order = %s, updated_at = now(), updated_by = %s
                        WHERE node_id = %s AND no = %s""", (name_to_order, str(author), node, no))
        _event(conn, author, "post", f"{node}:{no}", "post.name_sign", {"to_order": name_to_order})


def add_post(conn, node: str, author, lon: float, lat: float) -> str:
    """A further post at a junction, where there is more than one. Signs are moved onto it one by one."""
    node = _current(conn, "node", node)
    no = conn.execute("SELECT coalesce(max(no), 1) + 1 FROM ops.sign_post WHERE node_id = %s", (node,)).fetchone()[0]
    key = f"{node}:{no}"
    set_post(conn, key, author, lon=lon, lat=lat, further=True)
    return key


def mark(conn, keys: list[str], author, to_order: bool = True, why: str | None = None) -> int:
    """Put signs on the order that is to be taken out next, or take them off it: each by its key, name signs too."""
    for key in keys:
        if key.endswith("|name"):
            set_post(conn, key[:-len("|name")], author, name_to_order=to_order)
        else:
            set_sign(conn, key, author, to_order=to_order, **({"why": why} if why is not None else {}))
    return len(keys)


def mark_up(conn, keys: list[str], author, up: bool = True) -> int:
    """Say that signs that were ordered are up (or not after all). A sign that was never ordered is left as it
    is: it hangs already. Answers with how many were said."""
    ordered = _said(conn)["ordered"]
    said = 0
    for key in keys:
        if key not in ordered:
            continue
        order = ordered[key]["order"] if up else None
        if key.endswith("|name"):
            node, no = _post_parts(conn, key[:-len("|name")])
            conn.execute("UPDATE ops.sign_post SET name_up_order = %s, updated_at = now(), updated_by = %s "
                         "WHERE node_id = %s AND no = %s", (order, str(author), node, no))
        else:
            node, edge, to_type, to_id, apart = _key_parts(key)
            conn.execute("""UPDATE ops.sign SET up_order = %s, updated_at = now(), updated_by = %s
                            WHERE node_id = %s AND first_edge = %s AND to_type = %s AND to_id = %s AND route = %s""",
                         (order, str(author), node, edge, to_type, to_id, apart))
        _event(conn, author, "sign", key, "sign.up" if up else "sign.not_up", {"order": ordered[key]["order"]})
        said += 1
    return said


def set_sign(conn, key: str, author, **said) -> None:
    """Say something else of a sign than what is worked out: arrow, colour, text, distance, which post it
    hangs on, or that it is taken out; or that it is to be made (`to_order`, with `why`). An empty value goes
    back to what is worked out."""
    node, edge, to_type, to_id, apart = _key_parts(key)
    node, to_id = _current(conn, "node", node), _current(conn, to_type, to_id)
    edge = edge if edge == NO_STRETCH else _current(conn, "edge", edge)
    unknown = set(said) - {"direction", "colour", "text", "distance_m", "post_no", "taken_out", "to_order", "why"}
    if unknown:
        raise Invalid(f"a sign has no {', '.join(sorted(unknown))}")
    if said.get("direction") not in (None, "", *DIRECTIONS):
        raise Invalid(f"an arrow points: {', '.join(DIRECTIONS)}")
    if said.get("colour") not in (None, "", *COLOURS):
        raise Invalid(f"a sign is: {', '.join(COLOURS)}")
    if said.get("post_no") and not conn.execute(
            "SELECT 1 FROM ops.sign_post WHERE node_id = %s AND no = %s AND NOT taken_out", (node, said["post_no"])
            ).fetchone() and said["post_no"] != 1:
        raise Conflict("there is no such post at that junction")
    if edge == NO_STRETCH and not conn.execute(
            """SELECT 1 FROM ops.sign WHERE node_id = %s AND first_edge = %s AND to_type = %s AND to_id = %s AND added""",
            (node, edge, to_type, to_id)).fetchone():
        raise NotFound("no such sign")
    if edge == NO_STRETCH and to_type == "point" and said.get("text") == "":
        raise Invalid("a sign to a spot on a route says what the spot is called: give it a text, or take it out")
    conn.execute("""INSERT INTO ops.sign (node_id, first_edge, to_type, to_id, route, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                 (node, edge, to_type, to_id, apart, str(author)))
    changed = {}
    for field, value in said.items():
        if value is None:
            continue
        value = (value.strip() or None) if isinstance(value, str) else value
        if field in ("distance_m", "post_no") and not value:
            value = None                                        # 0: back to what is worked out
        conn.execute(f"""UPDATE ops.sign SET {field} = %s, updated_at = now(), updated_by = %s
                         WHERE node_id = %s AND first_edge = %s AND to_type = %s AND to_id = %s AND route = %s""",
                     (value, str(author), node, edge, to_type, to_id, apart))
        changed[field] = value
    if changed:
        _event(conn, author, "sign", sign_key(node, edge, to_type, to_id, apart), "sign.changed", changed)


def add_destination(conn, author, post: str, *, place: str | None = None, lon: float | None = None,
                    lat: float | None = None, name: str | None = None) -> dict:
    """Add a destination to a post that the routes do not give it: a place of ours, or a spot on a route.

    Given a place, or a position: a place of ours within 150 m of it is the
    place meant; else it is a spot on a route, and the sign needs to be told
    what the spot is called. Answers with the sign's key, or with what is
    missing (`needs`), having done nothing.
    """
    node, no = _post_parts(conn, post)
    if place is None:
        if lon is None or lat is None:
            raise Invalid("say where the sign is to point: a place, or a position")
        near = conn.execute("""
            SELECT p.id::text FROM core.place p LEFT JOIN core.node n ON n.place_id = p.id AND n.status = 'active',
                 (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS g) c
            WHERE p.status = 'confirmed' AND (ST_DWithin(p.geom, c.g, %s) OR ST_DWithin(n.geom, c.g, %s))
            ORDER BY least(ST_Distance(p.geom, c.g), coalesce(ST_Distance(n.geom, c.g), 1e9)) LIMIT 1""",
            (lon, lat, PLACE_M, PLACE_M)).fetchone()
        place = near[0] if near else None
    if place is not None:
        to_type, to_id, text = "place", _current(conn, "place", place), None
        if conn.execute("SELECT 1 FROM core.node WHERE id = %s AND place_id = %s", (node, to_id)).fetchone():
            raise Invalid("that is the place the post stands at")
    else:
        text = (name or "").strip()
        found = _rows(conn, """
            SELECT e.id::text AS edge, ST_AsEWKT(ST_LineInterpolatePoint(r.geom, ST_LineLocatePoint(r.geom, p.g))) AS at
            FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833) AS g) p,
                 core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE e.status = 'active' AND ST_DWithin(r.geom, p.g, %(near)s)
              AND EXISTS (SELECT 1 FROM core.route_leg g JOIN core.route t ON t.id = g.route_id AND t.status = 'active'
                          AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL WHERE g.edge_id = e.id)
            ORDER BY ST_Distance(r.geom, p.g) LIMIT 1""", {"lon": lon, "lat": lat, "near": ON_ROUTE_M})
        if not found:
            raise Invalid(f"a sign points to a place of yours or to a spot on a route: that is more than {PLACE_M} m "
                          f"from every place and more than {ON_ROUTE_M} m from every route in use")
        if not text:
            return {"needs": "name", "why": "that is no place of yours, but a spot on a route: say what the sign is to say"}
        to_type, to_id = "point", str(conn.execute(
            "INSERT INTO ops.sign_point (edge_id, geom, created_by) VALUES (%s, %s::geometry, %s) RETURNING id",
            (found[0]["edge"], found[0]["at"], str(author))).fetchone()[0])
    was = conn.execute("""SELECT taken_out FROM ops.sign WHERE node_id = %s AND first_edge = %s AND to_type = %s
                          AND to_id = %s AND route = ''""", (node, NO_STRETCH, to_type, to_id)).fetchone()
    if was and not was[0]:
        raise Conflict("that destination has been added to the post already")
    if to_type == "place":      # what the routes give the post already is not added to it
        hangs = [g for site in listing(conn)["sites"] for p in site["posts"] if p["node"] == node
                 for g in p["signs"] if g["to"]["id"] == to_id and not g["added"]]
        if hangs:
            raise Conflict(f"a sign to {hangs[0]['to']['name']} hangs on that post already, by {', '.join(hangs[0]['routes'])}")
    conn.execute("""
        INSERT INTO ops.sign (node_id, first_edge, to_type, to_id, added, post_no, text, updated_by)
        VALUES (%s, %s, %s, %s, true, %s, %s, %s)
        ON CONFLICT ON CONSTRAINT sign_found_by DO UPDATE
            SET taken_out = false, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (node, NO_STRETCH, to_type, to_id, no if no > 1 else None, text or None, str(author)))
    key = sign_key(node, NO_STRETCH, to_type, to_id)
    _event(conn, author, "sign", key, "sign.added", {"to": f"{to_type}:{to_id}", **({"says": text} if text else {})})
    return {"key": key}


# --- orders ----------------------------------------------------------------------------------------------------

COLUMNS = (("antall", "Antall", 7), ("tekst", "Tekst på skiltet", 34), ("km", "Km", 7), ("pilretning", "Pilretning", 12),
           ("baksidetekst", "Baksidetekst", 36), ("skiltfarge", "Skiltfarge", 11), ("ruter", "Ruter", 14),
           ("sendes_til", "Sendes til (navn, adresse)", 30))


def order_rows(conn, area: str, keys: list[str] | None = None) -> list[dict]:
    """What an order for an area would hold: one row per sign that is marked to be made, or per sign asked for.

    Not the signs of a site where a person has said there are to be none,
    nor a sign or a post that is taken out.
    """
    rows = []
    for site in listing(conn, area)["sites"]:
        if site["no_signs"]:
            continue
        send_to = "\n".join(filter(None, (site["send_to_name"], site["send_to_address"])))
        for post in site["posts"]:
            if post["taken_out"]:
                continue
            top = post["name_sign"]
            if top and (top["to_order"] if keys is None else top["key"] in keys):
                rows.append({"key": top["key"], "site": site["code"], "site_key": site["key"], "site_name": site["name"], "text": top["text"],
                             "km": None, "direction": None, "colour": COLOURS[0], "back_text": site["back_text"],
                             "routes": [], "send_to": send_to, "why": None})
            for sign in post["signs"]:
                if sign["taken_out"] or not (sign["to_order"] if keys is None else sign["key"] in keys):
                    continue
                rows.append({"key": sign["key"], "site": site["code"], "site_key": site["key"], "site_name": site["name"], "text": sign["text"],
                             "km": sign["km"], "direction": sign["direction"], "colour": sign["colour"],
                             "back_text": site["back_text"], "routes": sign["routes"], "send_to": send_to,
                             "why": sign["why"]})
    return rows


def take_out_order(conn, area: str, author, keys: list[str] | None = None, note: str | None = None) -> dict:
    """Take out an order for the signs that are marked to be made (or for those asked for). What each sign says
    now is kept with it, and stays whatever happens to the routes. The signs are then no longer marked."""
    rows = order_rows(conn, area, keys)
    if not rows:
        raise Invalid("nothing to order: no sign is marked to be made. Choose the signs that are to be replaced "
                      "and add them to the order")
    unnamed = [r for r in rows if not r["text"] or "?" in r["text"].split(" via ")[0]]
    if unnamed:
        raise Invalid(f"{len(unnamed)} of the signs point to a place with no name. Name the place, give the sign "
                      "a text, or take the sign off the order")
    codes: dict[str, str] = {}
    for row in rows:            # a site gets its code, like BRE-SS-0001, the first time a sign of it is ordered
        if not row["site"]:
            if row["site_key"] not in codes:
                at_type, at_id = _site_parts(row["site_key"])
                kept = _site_row(conn, at_type, at_id, area, author)
                codes[row["site_key"]] = kept["code"] or _next_code(conn, kept["area"])
                conn.execute("UPDATE ops.sign_site SET code = %s WHERE id = %s", (codes[row["site_key"]], kept["id"]))
            row["site"] = codes[row["site_key"]]
    order_id = conn.execute("""INSERT INTO ops.sign_order (by, area, note, rows) VALUES (%s, %s, %s, %s)
                               RETURNING id""", (str(author), area.lower(), (note or "").strip() or None,
                                                 Jsonb(rows))).fetchone()[0]
    for row in rows:            # ordered: off the list of what is to be ordered
        if row["key"].endswith("|name"):
            node, no = row["key"][:-len("|name")].split(":")
            conn.execute("UPDATE ops.sign_post SET name_to_order = false WHERE node_id = %s AND no = %s", (node, int(no)))
        else:
            node, edge, to_type, to_id, apart = _key_parts(row["key"])
            conn.execute("""UPDATE ops.sign SET to_order = false, why = NULL WHERE node_id = %s AND first_edge = %s
                            AND to_type = %s AND to_id = %s AND route = %s""", (node, edge, to_type, to_id, apart))
    _event(conn, author, "order", str(order_id), "order.taken_out", {"signs": len(rows), "area": area.lower()})
    return {"id": order_id, "signs": len(rows)}


def orders(conn, area: str | None = None) -> list[dict]:
    return _rows(conn, """SELECT id, at, by, area, note, jsonb_array_length(rows) AS signs FROM ops.sign_order
                          WHERE %(a)s::text IS NULL OR area = lower(%(a)s) ORDER BY id DESC""", {"a": area})


def order(conn, order_id: int) -> dict:
    found = _rows(conn, "SELECT id, at, by, area, note, rows FROM ops.sign_order WHERE id = %(id)s", {"id": order_id})
    if not found:
        raise NotFound("no such order")
    return found[0]


def km_text(km: float | None) -> str:
    """Kilometres as a sign has them, by the sign manual: one decimal under 10 km, with a comma; none from 10."""
    return "" if km is None else f"{km:.1f}".replace(".", ",") if km < 10 else str(round(km))


def as_workbook(rows: list[dict], title: str) -> bytes:
    """The list for whoever makes the signs, in the form v1 gave it: one row per sign."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = title[:31]
    for i, (_, heading, width) in enumerate(COLUMNS, start=1):
        cell = sheet.cell(row=1, column=i, value=heading)
        cell.fill, cell.font = PatternFill("solid", fgColor="1A3A5C"), Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.column_dimensions[get_column_letter(i)].width = width
    sheet.freeze_panes = "A2"
    for n, row in enumerate(rows, start=2):
        values = {"antall": 1, "tekst": row["text"], "km": km_text(row["km"]), "pilretning": row["direction"] or "",
                  "baksidetekst": row["back_text"] or "", "skiltfarge": row["colour"], "ruter": ", ".join(row["routes"]),
                  "sendes_til": row["send_to"] or ""}
        for i, (key, _, _) in enumerate(COLUMNS, start=1):
            cell = sheet.cell(row=n, column=i, value=values[key])
            cell.font = Font(name="Calibri", size=11)
            cell.alignment = (Alignment(horizontal="right") if key in ("km", "antall")
                              else Alignment(wrap_text=True, vertical="top"))
    out = BytesIO()
    book.save(out)
    return out.getvalue()
