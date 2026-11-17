"""A route as it is walked, in facts: what a description of it can be written from.

Everything here is read out of what is already known: the route's stretches
in walking order, the places and junctions it passes, its heights and the
kind of ground (Kartverket's height service), the names that lie along it
(Stedsnavn), and the info points of Turrutebasen. It is cut into sections
where something changes: at a place, where another route joins or leaves,
where path becomes road or glacier, where the marking stops.

It is a fact sheet, not a description. A person, or an assistant for a
person, writes from it; `NOT_KNOWN` says what it cannot tell, and that is
what makes a description worth reading: what the path is like underfoot,
where it is exposed, where a stream is forded.
"""
from __future__ import annotations

import itertools

from psycopg.rows import dict_row

from stiflyt2 import height
from stiflyt2.domain import routes

NEAR_M = 200            # a name this near the line is passed
WIDE_M = 600            # what is large is passed at a distance: its name lies in the middle of it
WIDE = ["isbre", "innsjø", "vann", "dal", "botn", "fjell"]
BESIDE_M = 60           # a bridge, a ford or an info point this near belongs to the route
STEEP = 0.20            # steeper than this, over STEEP_OVER_M or more, is a steep part
STEEP_OVER_M = 200
WATER = {"Elv": "river", "Innsjø": "lake", "InnsjøRegulert": "lake", "FerskvannTørrfall": "dry riverbed",
         "Havflate": "sea"}
SCRAP_M = 30            # a section shorter than this is no section of a walk
WITHIN_PLACE_M = 400    # nor is the way between two arrival points of one place, up to this long
LAKE_LEAST_M = 20       # a line drawn a few metres into a lake it runs along is the map, not the path
GROUND = {"ÅpentOmråde": "open", "Skog": "forest", "Myr": "bog", "SnøIsbre": "snow and ice",
          "DyrketMark": "farmland", "Steintipp": "rock fill", None: "not said", **WATER}
# Of Stedsnavn: what lies in the terrain, and the few built things a walker steers by.
NAME_GROUPS = ["terreng", "ferskvann", "markslag", "sjø"]
NAME_TYPES = ["seterStøl", "turisthytte", "hotell", "fjellgard", "bru", "klopp", "vad", "mindreBrukonstruksjon"]
CROSSINGS = {"bru": "bridge", "klopp": "footbridge", "mindreBrukonstruksjon": "bridge", "vad": "ford"}
KIND = {"trail": "path", "tractor_road": "tractor road", "road": "road", "glacier": "glacier", "water": "water",
        "off_trail": "pathless ground", "connector": "a link at a place", "other": "other ground"}
BRIDGE = 5              # Turrutebasen's code for a bridge among its info points (`tilrettelegging`)
NOT_KNOWN = [
    "What the path is like underfoot (stony, wet, boardwalk, scree), and how clear it is on the ground.",
    "Where it is exposed, where hands are needed, where there are chains or ladders.",
    ("Streams and their crossings. A river or lake is known only where Kartverket's ground has one under the "
     "line, and a bridge or ford only where it has a name in Stedsnavn or is an info point in Turrutebasen. "
     "Most footbridges are neither, and whether a bridge is taken in for the winter is not known."),
    "Views, shelter, water to drink, mobile coverage, and how the route changes with the season.",
    ("Times are Naismith's rule, with time added for going down steeply, for a fit walker on a path with no "
     "rests; a grading by the numbers is what length and climb alone give, and the ground can only make a "
     "route harder than that."),
]
_LEN = "ST_Length(ST_Transform(r.geom, 4326)::geography)"


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _steep(run: list[list], least_m: float = STEEP_OVER_M) -> list[dict]:
    """The steep parts of one unbroken run of the profile, walked in its order."""
    parts: list[dict] = []
    j = 0
    for i, (at, z, *_) in enumerate(run):
        while j < len(run) - 1 and run[j][0] - at < least_m:
            j += 1
        over = run[j][0] - at
        if over < least_m:
            break
        if abs(run[j][1] - z) / over < STEEP:
            continue
        going = "up" if run[j][1] > z else "down"
        if parts and parts[-1]["going"] == going and at <= parts[-1]["to_m"]:
            parts[-1]["to_m"], parts[-1]["_z1"] = run[j][0], run[j][1]
        else:
            parts.append({"from_m": at, "to_m": run[j][0], "going": going, "_z0": z, "_z1": run[j][1]})
    for part in parts:
        climb = part.pop("_z1") - part.pop("_z0")
        part.update(climb_m=round(abs(climb)), percent=round(100 * abs(climb) / (part["to_m"] - part["from_m"])))
    return parts


def _steepest(run: list[list]) -> dict | None:
    """The steepest 200 m of a run (or all of it, if it is shorter), and which way it goes."""
    if len(run) < 2 or run[-1][0] - run[0][0] < 100:
        return None
    over_least = min(STEEP_OVER_M, run[-1][0] - run[0][0])
    best, j = None, 0
    for at, z, *_ in run:
        while j < len(run) - 1 and run[j][0] - at < over_least:
            j += 1
        over = run[j][0] - at
        if over < over_least:
            break
        grade = (run[j][1] - z) / over
        if best is None or abs(grade) > abs(best[0]):
            best = (grade, at, run[j][0])
    if best is None or abs(best[0]) < 0.05:
        return None
    return {"percent": round(100 * abs(best[0])), "going": "up" if best[0] > 0 else "down",
            "from_m": best[1], "to_m": best[2]}


def _whole_sections(sections: list[dict]) -> list[dict]:
    """The sections without the scraps: a few metres between two junctions, or between two arrival points of
    one place, is no section of a walk. It goes with the section before it (the first, with the one after)."""
    kept: list[dict] = []
    first = None
    for section in sections:
        if first:
            section = {**section, "from_m": first["from_m"], "from": first["from"],
                       "length_m": section["to_m"] - first["from_m"]}
            first = None
        scrap = section["length_m"] < SCRAP_M or (
            section["from"] is not None and section["from"] == section["to"] and section["length_m"] <= WITHIN_PLACE_M)
        if scrap and kept:
            kept[-1].update(to_m=section["to_m"], to=section["to"], length_m=section["to_m"] - kept[-1]["from_m"])
        elif scrap:
            first = section
        else:
            kept.append(section)
    return kept or ([first] if first else [])


def _runs(samples: list[list]) -> list[list[list]]:
    """The profile as unbroken runs: a point with no height is where one ends."""
    runs: list[list[list]] = [[]]
    for point in samples:
        if point[1] is None:
            if runs[-1]:
                runs.append([])
        else:
            runs[-1].append(point)
    return [r for r in runs if len(r) > 1]


def of_route(conn, route_id: str) -> dict:
    """The fact sheet of a route of ours, walked from its first end to its last."""
    route = routes.detail(conn, route_id)
    high = height.of_route(conn, route_id, samples=True, ground=True)
    legs = _rows(conn, f"""
        SELECT e.id::text AS edge, l.reversed, r.kind, r.marked, {_LEN} AS metres,
               (CASE WHEN l.reversed THEN r.b_node ELSE r.a_node END)::text AS a,
               (CASE WHEN l.reversed THEN r.a_node ELSE r.b_node END)::text AS b,
               (SELECT coalesce(array_agg(DISTINCT t.code ORDER BY t.code), '{{}}')
                FROM core.route_leg o JOIN core.route t ON t.id = o.route_id AND t.status = 'active'
                     AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL
                WHERE o.edge_id = e.id AND o.route_id <> l.route_id) AS others
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s ORDER BY l.seq""", {"id": route_id})
    so_far = 0.0
    for leg in legs:            # water is travelled and not walked: it has a spot on the way, and no length
        leg["at0"] = so_far
        so_far += 0 if leg["kind"] == "water" else leg["metres"]
        leg["at1"] = so_far
    walked = [g for g in legs if g["kind"] != "water"]
    nodes = sorted({n for g in legs for n in (g["a"], g["b"])})
    place = {r["id"]: r for r in _rows(conn, """
        SELECT n.id::text AS id, p.name, p.kind FROM core.node n
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%(nodes)s::uuid[])""", {"nodes": nodes})}
    touching: dict[str, set[str]] = {}
    for r in _rows(conn, """
            SELECT DISTINCT nd::text AS node, t.code FROM unnest(%(nodes)s::uuid[]) nd
            JOIN core.edge_revision r ON r.a_node = nd OR r.b_node = nd
            JOIN core.edge e ON e.current_revision_id = r.id AND e.status = 'active'
            JOIN core.route_leg o ON o.edge_id = e.id
            JOIN core.route t ON t.id = o.route_id AND t.status = 'active' AND t.id <> %(id)s
                 AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL""", {"nodes": nodes, "id": route_id}):
        touching.setdefault(r["node"], set()).add(r["code"])

    points: list[dict] = []

    def spot(node: str, at: float, before: set[str], after: set[str]) -> str | None:
        """Note what is at a junction of the route, and give the few words a section is said to run to."""
        here = place.get(node)
        met = touching.get(node, set())
        joins, leaves, meets = sorted(after - before), sorted(before - after), sorted(met - before - after)
        if here:
            points.append({"at_m": round(at), "what": "place", "name": here["name"], "kind": here["kind"]})
        if joins or leaves or meets:
            points.append({"at_m": round(at), "what": "junction",
                           **{k: v for k, v in (("joins", joins), ("leaves", leaves), ("meets", meets)) if v}})
        if here:
            return here["name"]
        if joins or leaves or meets:
            return "the junction with " + ", ".join(joins + leaves + meets)
        return None

    sections: list[dict] = []
    first = 0
    labels: dict[int, str | None] = {}
    for boat in (g for g in legs if g["kind"] == "water"):
        points.append({"at_m": round(boat["at0"]), "what": "over water, not walked", "length_m": round(boat["metres"])})
    if walked:      # at its own two ends the route shares what it shares: nothing joins or leaves there
        labels[0] = spot(walked[0]["a"], 0.0, set(walked[0]["others"]), set(walked[0]["others"]))
    for i, leg in enumerate(walked):
        last = i == len(walked) - 1
        nxt = None if last else walked[i + 1]
        there = spot(leg["b"], leg["at1"], set(leg["others"]), set((nxt or leg)["others"]))
        if not (last or there or nxt["kind"] != leg["kind"] or nxt["marked"] != leg["marked"]
                or nxt["others"] != leg["others"] or nxt["a"] != leg["b"]):
            continue
        if not there and not last:
            there = ("where the route goes over water, or breaks off" if nxt["a"] != leg["b"]
                     else f"where {KIND[leg['kind']]} becomes {KIND[nxt['kind']]}" if nxt["kind"] != leg["kind"]
                     else "where the marking " + ("begins" if nxt["marked"] else "ends" if leg["marked"] else "is no longer known")
                     if nxt["marked"] != leg["marked"] else None)
        sections.append({"from_m": round(walked[first]["at0"]), "to_m": round(leg["at1"]),
                         "length_m": round(leg["at1"] - walked[first]["at0"]),
                         "from": labels.get(first), "to": there, "kind": leg["kind"], "marked": leg["marked"],
                         "also": leg["others"]})
        first = i + 1
        labels[first] = there

    sections = _whole_sections(sections)
    runs = _runs(high["samples"])
    for section in sections:        # what each section climbs, what it takes, the ground and its steepest part
        inside = [[p for p in run if section["from_m"] <= p[0] <= section["to_m"]] for run in runs]
        inside = [r for r in inside if len(r) > 1]
        known = sum(r[-1][0] - r[0][0] for r in inside)
        if known < 0.9 * section["length_m"]:
            section.update(up_m=None, down_m=None, minutes=None, minutes_back=None, ground=None, steepest=None)
            continue
        up = down = steep_up = steep_down = 0.0
        under: dict[str, float] = {}
        for run in inside:
            gained, lost = height.climb([p[1] for p in run])
            steeply_up, steeply_down = height.steep([(p[0], p[1]) for p in run])
            up, down, steep_up, steep_down = up + gained, down + lost, steep_up + steeply_up, steep_down + steeply_down
            for a, b in itertools.pairwise(run):
                under[GROUND.get(a[5], a[5])] = under.get(GROUND.get(a[5], a[5]), 0.0) + b[0] - a[0]
        steepest = max((s for s in map(_steepest, inside) if s), key=lambda s: s["percent"], default=None)
        section.update(up_m=round(up), down_m=round(down),
                       minutes=height.minutes(section["length_m"], up, steep_down),
                       minutes_back=height.minutes(section["length_m"], down, steep_up),
                       ground={k: round(v) for k, v in sorted(under.items(), key=lambda kv: -kv[1]) if v >= 20},
                       steepest=steepest)

    names, info = _named(conn, walked), _info(conn, walked)
    for run in runs:                # where the ground under the line is water
        i = 0
        while i < len(run):
            what = WATER.get(run[i][5])
            if not what:
                i += 1
                continue
            j = i
            while j + 1 < len(run) and WATER.get(run[j + 1][5]) == what:
                j += 1
            over = run[j][0] - run[i][0]
            if what != "lake" or over >= LAKE_LEAST_M:
                middle = (run[i][0] + run[j][0]) / 2
                by = min((n for n in names if n["type"] in CROSSINGS and abs(n["at_m"] - middle) <= over / 2 + BESIDE_M),
                         key=lambda n: abs(n["at_m"] - middle), default=None)
                marked = min((p for p in info if p["bridge"] and abs(p["at_m"] - middle) <= over / 2 + BESIDE_M),
                             key=lambda p: abs(p["at_m"] - middle), default=None)
                points.append({"at_m": round(run[i][0]), "what": f"{what} under the line", "over_m": round(max(over, height.STEP_M)),
                               "crossing": ({"what": CROSSINGS[by["type"]], "name": by["name"]} if by
                                            else {"what": "bridge", "name": marked["says"]} if marked
                                            else "no bridge or ford is known here: the data has few of them")})
            i = j + 1
    for n in names:
        if n["type"] in CROSSINGS and n["off_m"] <= BESIDE_M:
            points.append({"at_m": n["at_m"], "what": CROSSINGS[n["type"]], "name": n["name"], "off_m": n["off_m"]})
    for p in info:
        points.append({"at_m": p["at_m"], "what": "bridge (info point in Turrutebasen)" if p["bridge"]
                       else "info point in Turrutebasen", "says": p["says"], "off_m": p["off_m"]})
    with_height = [p for run in runs for p in run]
    if with_height:
        top, bottom = max(with_height, key=lambda p: p[1]), min(with_height, key=lambda p: p[1])
        points.append({"at_m": top[0], "what": "highest point", "height_m": round(top[1])})
        points.append({"at_m": bottom[0], "what": "lowest point", "height_m": round(bottom[1])})
    points.sort(key=lambda p: p["at_m"])

    mentioned = sorted({c for p in points if p["what"] == "junction" for k in ("joins", "leaves", "meets")
                        for c in p.get(k, [])} | {c for s in sections for c in s["also"]})
    ends = [e["place"] for e in route["ends"]]
    if route["round_trip"]:
        ends = [route["round_trip"]["place"]] * 2
    by_node = {e["id"]: e["place"] for e in route["ends"]}
    start, finish = (ends if route["round_trip"] else (by_node.get(high["from_node"]), by_node.get(high["to_node"])))
    return {
        "route": {"code": route["code"], "name": route["name"] or route["generated_name"], "shape": route["shape"],
                  "from": start, "to": finish, "round_trip": bool(route["round_trip"]),
                  "grading": route["grading"], "grading_by_numbers": route["grading_by_numbers"],
                  "maintained_by": route["maintained_by"],
                  "done": route["reviewed"], "discontinued": route["discontinued"],
                  "unpublished": route["unpublished"], "description": route["description"],
                  "utno_id": route["utno_id"]},
        "order": ("walked from its first end to its last" if route["shape"] in ("line", "loop") else
                  f"the route is {route['shape']}, not one line: the order below is the order its stretches are "
                  "kept in, and need not be a walk from one end to the other"),
        "whole": {"on_foot_m": route["length"]["on_foot_m"], "over_water_m": route["length"]["water_m"],
                  "over_glacier_m": route["length"]["glacier_m"], "not_marked_m": route["length"]["unmarked_m"],
                  **{k: high[k] for k in ("known_m", "up_m", "down_m", "low_m", "high_m", "ice_m", "there", "back")}},
        "sections": sections,
        "points": points,
        "steep": [s for run in runs for s in _steep(run)],
        "names": names,
        "other_routes": _called(conn, mentioned),
        "not_known": NOT_KNOWN,
    }


def _along(walked: list[dict]) -> dict:
    return {"edges": [g["edge"] for g in walked], "reversed": [g["reversed"] for g in walked],
            "at0": [g["at0"] for g in walked], "metres": [g["metres"] for g in walked]}


_LEGS = """
    legs AS (SELECT v.at0, v.metres, CASE WHEN v.reversed THEN ST_Reverse(r.geom) ELSE r.geom END AS line
             FROM unnest(%(edges)s::uuid[], %(reversed)s::boolean[], %(at0)s::float8[], %(metres)s::float8[])
                  AS v(edge, reversed, at0, metres)
             JOIN core.edge e ON e.id = v.edge JOIN core.edge_revision r ON r.id = e.current_revision_id)"""


def _named(conn, walked: list[dict]) -> list[dict]:
    """The names along the route: each with how far along, how far off, and on which side as it is walked."""
    if not walked:
        return []
    found = _rows(conn, f"""
        WITH {_LEGS},
        near AS (SELECT p.place_id, p.name, p.type, p.language, g.line, p.geom AS pt, g.at0, g.metres,
                        ST_Distance(g.line, p.geom) AS off_m, ST_LineLocatePoint(g.line, p.geom) AS f
                 FROM legs g JOIN source_index.place p ON ST_DWithin(g.line, p.geom, %(wide_m)s)
                 WHERE NOT p.alternative AND p.name_status = 'hovednavn'
                   AND (p.main_group = ANY(%(groups)s) OR p.type = ANY(%(types)s))
                   AND (p.type = ANY(%(wide)s) OR ST_DWithin(g.line, p.geom, %(near)s)))
        SELECT DISTINCT ON (n.place_id) n.name, n.type, round(n.off_m)::int AS off_m,
               round(n.at0 + n.f * n.metres)::int AS at_m,
               CASE WHEN (ST_X(d.b) - ST_X(d.a)) * (ST_Y(n.pt) - ST_Y(d.a))
                       - (ST_Y(d.b) - ST_Y(d.a)) * (ST_X(n.pt) - ST_X(d.a)) > 0 THEN 'left' ELSE 'right' END AS side
        FROM near n, LATERAL (SELECT ST_LineInterpolatePoint(n.line, greatest(n.f - 0.02, 0)) AS a,
                                     ST_LineInterpolatePoint(n.line, least(n.f + 0.02, 1)) AS b) d
        ORDER BY n.place_id, n.off_m, (n.language = 'norsk') DESC, n.name""",
        {**_along(walked), "near": NEAR_M, "wide_m": WIDE_M, "wide": WIDE, "groups": NAME_GROUPS, "types": NAME_TYPES})
    nearest: dict[tuple, dict] = {}
    for n in sorted(found, key=lambda n: n["off_m"]):       # one name is told once, where it is nearest
        nearest.setdefault((n["name"], n["type"]), n)
    for n in nearest.values():
        if n["off_m"] < 15:
            n["side"] = "on the route"
    return sorted(nearest.values(), key=lambda n: (n["at_m"], n["name"]))


def _info(conn, walked: list[dict]) -> list[dict]:
    """Turrutebasen's info points beside the route: what each says, and whether it is a bridge."""
    if not walked:
        return []
    found = _rows(conn, f"""
        WITH {_LEGS}
        SELECT DISTINCT ON (i.id) i.facility, nullif(trim(i.info), '') AS says,
               round(ST_Distance(g.line, i.geom))::int AS off_m,
               round(g.at0 + ST_LineLocatePoint(g.line, ST_Centroid(i.geom)) * g.metres)::int AS at_m
        FROM legs g JOIN source_index.info_point i ON ST_DWithin(g.line, i.geom, %(beside)s)
        ORDER BY i.id, ST_Distance(g.line, i.geom)""", {**_along(walked), "beside": BESIDE_M})
    return sorted(({"at_m": i["at_m"], "off_m": i["off_m"], "says": i["says"], "bridge": i["facility"] == BRIDGE}
                   for i in found if i["says"] or i["facility"] == BRIDGE), key=lambda i: i["at_m"])


def _called(conn, codes: list[str]) -> dict[str, str | None]:
    """What each of the other routes is called: its name, or the places at its two ends."""
    if not codes:
        return {}
    found = _rows(conn, """
        WITH met AS (SELECT t.id, t.code, t.name FROM core.route t
                     WHERE t.status = 'active' AND t.code = ANY(%(codes)s)),
        touched AS (SELECT l.route_id, unnest(ARRAY[r.a_node, r.b_node]) AS node
                    FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                    JOIN core.edge_revision r ON r.id = e.current_revision_id
                    WHERE l.route_id IN (SELECT id FROM met) AND r.a_node <> r.b_node),
        ends AS (SELECT route_id, node FROM touched GROUP BY 1, 2 HAVING count(*) = 1)
        SELECT m.code, m.name,
               coalesce(array_agg(p.name ORDER BY ST_X(n.geom)) FILTER (WHERE p.name IS NOT NULL), '{}') AS places,
               count(ends.node)::int AS ends
        FROM met m LEFT JOIN ends ON ends.route_id = m.id LEFT JOIN core.node n ON n.id = ends.node
        LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        GROUP BY m.code, m.name""", {"codes": codes})
    return {r["code"]: r["name"] or (" – ".join(r["places"]) if r["ends"] == 2 and len(r["places"]) == 2 else None)
            for r in found}
