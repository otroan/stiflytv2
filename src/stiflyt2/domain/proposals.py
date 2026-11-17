"""Repairs that can be proposed without a person looking first, for a person to approve.

Each proposal is something simple enough to judge at a glance: a short extra
end to take out, two pieces lying close to join, a few metres that go nowhere,
a route to carry on the last stretch to where a place's routes meet, a named
spot at a route's end to make a place (a hut, a summit, a farm, the road it
ends on) or one more way in to a place, a stretch along a road to lay on the road. Anything less clear-cut is left in
the to-do list. A proposal is tried out before it is offered: it is made in a
transaction that is rolled back, and offered only if the route comes out as
one line.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from stiflyt2.domain import places, routes, worklist
from stiflyt2.domain.places import AUTOMATIC

TWIG_M = 100        # an extra end no longer than this is proposed taken out
GAP_M = 150         # pieces no further apart than this are proposed joined on foot
SNAP_MOVE_M = 60    # a stretch is proposed laid on a road when no point of it moves further
SNAP_WORTH_M = 3    # and when some point of it moves at least this far: less is not worth a change
ALONG_A_WAY = ("BV", "TR", "TV", "SB", "GS")   # Turrutebasen says the segment follows a road or track


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _metres(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{round(m)} m"


def _version(conn, route_id: str) -> int:
    return conn.execute("SELECT version FROM core.route WHERE id = %s", (route_id,)).fetchone()[0]


def _between(conn, route_id: str, a: str, b: str) -> tuple[dict, float] | None:
    """A stretch that already runs between two nodes and is not part of the route: how to add it, and its length.

    One of yours first, else a Turrutebasen segment whose two ends lie at the two nodes.
    """
    yours = conn.execute("""
        SELECT e.id::text, ST_Length(r.geom) FROM core.edge e
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.status = 'active' AND r.kind <> 'connector'
          AND (r.a_node, r.b_node) IN ((%(a)s::uuid, %(b)s::uuid), (%(b)s::uuid, %(a)s::uuid))
          AND NOT EXISTS (SELECT 1 FROM core.route_leg l WHERE l.edge_id = e.id AND l.route_id = %(r)s)
        ORDER BY 2 LIMIT 1""", {"a": a, "b": b, "r": route_id}).fetchone()
    if yours:
        return {"edge_id": yours[0]}, yours[1]
    segment = conn.execute("""
        SELECT t.id, t.length_m FROM core.node x, core.node y, source_index.trail_segment t
        WHERE x.id = %(a)s AND y.id = %(b)s AND GeometryType(t.geom) = 'LINESTRING'
          AND ST_DWithin(t.geom, x.geom, %(m)s) AND ST_DWithin(t.geom, y.geom, %(m)s)
          AND ((ST_DWithin(ST_StartPoint(t.geom), x.geom, %(m)s) AND ST_DWithin(ST_EndPoint(t.geom), y.geom, %(m)s))
            OR (ST_DWithin(ST_StartPoint(t.geom), y.geom, %(m)s) AND ST_DWithin(ST_EndPoint(t.geom), x.geom, %(m)s)))
          AND NOT EXISTS (SELECT 1 FROM core.source_reference sr
                          JOIN core.route_leg l ON l.edge_id = sr.subject_id AND l.route_id = %(r)s
                          WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                            AND sr.external_id = t.id)
        ORDER BY t.length_m LIMIT 1""", {"a": a, "b": b, "r": route_id, "m": routes.ACCESS_M}).fetchone()
    if segment:
        return {"segment_id": segment[0]}, segment[1]
    return None


def _repair(conn, route_id: str, author: str) -> dict | None:
    """Make a route one line by taking short extra ends out or joining close pieces.

    Returns what was done, or None when the route is a line already or cannot
    be made one this way. The caller decides whether the change is kept.
    """
    route = routes.detail(conn, route_id)
    if route["shape"] == "branched":
        extra = route["twigs"][:len(route["twigs"]) - 2]
        if not extra or any(t["metres"] > TWIG_M for t in extra):
            return None
        place = {e["id"]: e for e in route["ends"]}
        for twig in extra:
            for edge in twig["edges"]:
                routes.remove_leg(conn, route_id, _version(conn, route_id), edge, author)
        at = place[extra[0]["end"]]
        ends = " and ".join(f"{_metres(t['metres'])} end"
                            + (f" at {place[t['end']]['place']}" if place[t["end"]]["place"] else "")
                            for t in extra)
        done = {"kind": "twig", "what": f"its {ends}", "lon": at["lon"], "lat": at["lat"],
                "metres": max(t["metres"] for t in extra)}
    elif route["shape"] == "doubled":
        if not route["doubles"]:
            return None         # a wider loop inside the route: for a person to look at
        extra = [g for d in route["doubles"] for g in d["stretches"][1:]]
        for g in extra:
            routes.remove_leg(conn, route_id, _version(conn, route_id), g["edge"], author)
        at = route["doubles"][0]
        done = {"kind": "double", "lon": at["lon"], "lat": at["lat"], "metres": max(g["metres"] for g in extra),
                "what": " and ".join(f"the {_metres(g['metres'])} stretch" for g in extra)
                        + (" that runs" if len(extra) == 1 else " that run")
                        + " between two junctions another stretch of it joins already"}
    elif route["shape"] == "pieces":
        where = {e["id"]: e for e in route["ends"]}
        how = []
        for gap in route["gaps"]:
            # Where a stretch already runs between the two ends, the route is carried on along
            # it, however long it is. Only where there is none are the ends joined by a straight
            # link on foot, and only when they lie close.
            way = _between(conn, route_id, gap["from_node"], gap["to_node"])
            if not way and gap["metres"] > GAP_M:
                return None
            if way:
                routes.add_stretch(conn, route_id, _version(conn, route_id), author, **way[0])
                how.append(f"along the stretch of {_metres(way[1])} that runs between them")
            else:
                routes.add_crossing(conn, route_id, _version(conn, route_id),
                                    gap["from_node"], gap["to_node"], "connector", author)
                how.append(f"on foot across {_metres(gap['metres'])}")
        at = where.get(route["gaps"][0]["from_node"]) or route["ends"][0]
        done = {"kind": "gap", "lon": at["lon"], "lat": at["lat"],
                "metres": max(g["metres"] for g in route["gaps"]),
                "what": f"its {route['pieces']} pieces joined " + " and ".join(how)}
    else:
        return None
    if routes.detail(conn, route_id)["shape"] != "line":
        return None
    return done


def _try(conn, make) -> dict | None:
    """Run a repair to see what it gives, and leave no trace of it."""
    try:
        with conn.transaction(force_rollback=True):
            return make()
    except (places.Invalid, places.Conflict, places.NotFound):
        return None


def proposals(conn, area: str) -> list[dict]:
    """What can be proposed for an area now. Needs a connection that may write; keeps nothing."""
    area = area.lower()
    out = []

    # Candidates that are not one line: would they be, with a short end out or two pieces joined?
    for c in _rows(conn, """
            SELECT r.id, r.label FROM source_index.route r
            JOIN source_index.route_shape s ON s.route_id = r.id
            WHERE r.area_prefix = %(a)s AND s.shape <> 'line' ORDER BY length(r.code), r.label""",
            {"a": area}):
        if routes.owner_of(conn, c["id"]):
            continue
        done = _try(conn, lambda c=c: _repair(conn, routes.make(conn, c["id"], AUTOMATIC), AUTOMATIC))
        if done:
            text = (f"Make {c['label']} yours without {done['what']}" if done["kind"] == "twig"
                    else f"Make {c['label']} yours with {done['what']}")
            out.append({**done, "label": c["label"], "text": text,
                        "action": {"kind": done["kind"], "candidate": c["id"]}})

    mine = worklist.routes_of(conn, area)
    # Your routes that are not one line.
    for route in _rows(conn, """SELECT id::text AS id, code FROM core.route
                                WHERE id = ANY(%(ids)s::uuid[]) ORDER BY length(code), code""",
                       {"ids": mine}):
        done = _try(conn, lambda route=route: _repair(conn, route["id"], AUTOMATIC))
        if done:
            text = (f"Take {done['what']} out of {route['code']}" if done["kind"] in ("twig", "double")
                    else f"Give {route['code']} {done['what']}")
            out.append({**done, "label": route["code"], "text": text.replace("Take its", "Take the"),
                        "action": {"kind": done["kind"], "route": route["id"]}})

    # A few metres that begin and end at the same junction: nothing to walk.
    for loop in _rows(conn, f"""
            SELECT e.id::text AS edge, round({routes._LEN})::int AS metres,
                   string_agg(DISTINCT t.code, ', ') AS codes,
                   ST_X(ST_Transform(n.geom, 4326)) AS lon, ST_Y(ST_Transform(n.geom, 4326)) AS lat,
                   (SELECT p.name FROM core.place p WHERE p.id = n.place_id) AS place
            FROM core.route_leg l JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
            JOIN core.edge e ON e.id = l.edge_id
            JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.node n ON n.id = r.a_node
            WHERE l.route_id = ANY(%(ids)s::uuid[]) AND r.a_node = r.b_node AND e.status = 'active'
            GROUP BY e.id, r.id, n.id""", {"ids": mine}):
        where = f" at {loop['place']}" if loop["place"] else ""
        out.append({"kind": "loop", "label": loop["codes"], "lon": loop["lon"], "lat": loop["lat"],
                    "metres": loop["metres"],
                    "text": f"Take the {loop['metres']} m{where} that begin and end at the same "
                            f"junction out of {loop['codes']}",
                    "action": {"kind": "loop", "edge": loop["edge"]}})

    # A route that ends at a place, one stretch short of where the place's routes meet.
    for short in _rows(conn, f"""
            WITH legs AS (
                SELECT l.route_id, r.a_node, r.b_node FROM core.route_leg l
                JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE l.route_id = ANY(%(ids)s::uuid[]) AND r.a_node <> r.b_node),
            ends AS (SELECT route_id, n FROM (SELECT route_id, a_node AS n FROM legs
                                               UNION ALL SELECT route_id, b_node FROM legs) q
                     GROUP BY 1, 2 HAVING count(*) = 1)
            SELECT t.id::text AS route, t.code, p.name, e.id::text AS edge, round({routes._LEN})::int AS metres,
                   ST_X(ST_Transform(x.geom, 4326)) AS lon, ST_Y(ST_Transform(x.geom, 4326)) AS lat
            FROM ends JOIN core.route t ON t.id = ends.route_id
            JOIN core.node x ON x.id = ends.n
            JOIN core.place p ON p.id = x.place_id AND p.status = 'confirmed' AND p.primary_node_id <> x.id
            JOIN core.edge_revision r ON (r.a_node, r.b_node) IN ((x.id, p.primary_node_id), (p.primary_node_id, x.id))
            JOIN core.edge e ON e.current_revision_id = r.id AND e.status = 'active'
            WHERE r.kind NOT IN ('connector', 'water')
              AND NOT EXISTS (SELECT 1 FROM core.route_leg l WHERE l.route_id = t.id AND l.edge_id = e.id)
            ORDER BY length(t.code), t.code""", {"ids": mine}):
        action = {"kind": "reach", "route": short["route"], "edge": short["edge"]}
        if _try(conn, lambda action=action: approve(conn, AUTOMATIC, action)):
            out.append({"kind": "reach", "label": short["code"], "lon": short["lon"], "lat": short["lat"],
                        "metres": short["metres"], "action": action,
                        "text": f"Carry {short['code']} on to where the routes of {short['name']} meet, "
                                f"along the {_metres(short['metres'])} stretch that runs there"})

    # A route that ends at no place, where something there has a name: a hut, a summit, a farm,
    # a bridge, the road it ends on. A place of that name, or one more way in to the place that has it.
    seen = set()
    for end in _rows(conn, """
            WITH legs AS (
                SELECT l.route_id, r.a_node, r.b_node FROM core.route_leg l
                JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE l.route_id = ANY(%(ids)s::uuid[]) AND r.a_node <> r.b_node),
            ends AS (SELECT route_id, n FROM (SELECT route_id, a_node AS n FROM legs
                                               UNION ALL SELECT route_id, b_node FROM legs) q
                     GROUP BY 1, 2 HAVING count(*) = 1)
            SELECT x.id::text AS node, string_agg(t.code, ', ' ORDER BY length(t.code), t.code) AS codes,
                   ST_X(ST_Transform(x.geom, 4326)) AS lon, ST_Y(ST_Transform(x.geom, 4326)) AS lat
            FROM ends JOIN core.route t ON t.id = ends.route_id
            JOIN core.node x ON x.id = ends.n AND x.place_id IS NULL
            GROUP BY x.id ORDER BY 2""", {"ids": mine}):
        after = places.suggest(conn, end["node"])
        if not after:
            continue        # nothing there has a name: a person names it
        ends = f"{end['codes']} {'end' if ', ' in end['codes'] else 'ends'}"
        about = {"label": end["codes"], "lon": end["lon"], "lat": end["lat"], "metres": after["metres"]}
        existing = places._named(conn, after["name"], after["stedsnavn_id"])
        if existing:
            # What it would be named after is a place already. Near enough, this end is one more way in to it.
            if after["stedsnavn_id"] and after["metres"] <= places.ARRIVAL_M:
                out.append({**about, "kind": "arrival",
                            "action": {"kind": "arrival", "node": end["node"], "place": existing},
                            "text": f"Make where {ends} an arrival point of {after['name']}: "
                                    f"it is {after['metres']} m from {after['what']}"})
            continue
        key = after["stedsnavn_id"] or after["name"].lower()
        if key in seen:
            continue        # two ends beside the same thing: one place first
        seen.add(key)
        out.append({**about, "kind": "place", "action": {"kind": "place", "node": end["node"]},
                    "text": f"Make {after['name']} a place where {ends}: named after {after['after']}"})

    # Where a route steps onto the ice or off it, and no place is: the edge of a glacier is somewhere
    # a sign points to. Named after the glacier and the side of the crossing.
    for edge in _rows(conn, """
            SELECT x.id::text AS node, string_agg(DISTINCT t.code, ', ') AS codes,
                   ST_X(ST_Transform(x.geom, 4326)) AS lon, ST_Y(ST_Transform(x.geom, 4326)) AS lat
            FROM core.route_leg l JOIN core.route t ON t.id = l.route_id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.node x ON x.id IN (r.a_node, r.b_node) AND x.place_id IS NULL AND x.status = 'active'
            WHERE l.route_id = ANY(%(ids)s::uuid[]) AND r.kind = 'glacier'
            GROUP BY x.id ORDER BY 2""", {"ids": mine}):
        after = places.glacier_edge(conn, edge["node"])
        if not after or places._named(conn, after["name"]):
            continue
        steps = f"{edge['codes']} {'step' if ', ' in edge['codes'] else 'steps'}"
        out.append({"kind": "place", "label": edge["codes"], "lon": edge["lon"], "lat": edge["lat"], "metres": 0,
                    "action": {"kind": "place", "node": edge["node"]},
                    "text": f"Make {after['name']} a place where {steps} onto the ice: "
                            f"somewhere a sign can point to"})

    # An arrival point of a place that your routes only pass: left from when the place was made
    # from where Turrutebasen's routes ended, before the routes were mended. Not one where a route
    # of yours ends, nor one where a Turrutebasen segment that is not yours yet ends.
    for spot in _rows(conn, """
            WITH touch AS (
                SELECT l.route_id, x.n AS node, count(*) AS legs
                FROM core.route_leg l JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                JOIN core.edge e ON e.id = l.edge_id AND e.status = 'active'
                JOIN core.edge_revision v ON v.id = e.current_revision_id AND v.a_node <> v.b_node
                     AND v.kind <> 'connector',
                     LATERAL (VALUES (v.a_node), (v.b_node)) x(n)
                GROUP BY 1, 2)
            SELECT p.id::text AS place, p.name, n.id::text AS node,
                   round(ST_Distance(n.geom, pn.geom))::int AS metres,
                   ST_X(ST_Transform(n.geom, 4326)) AS lon, ST_Y(ST_Transform(n.geom, 4326)) AS lat,
                   (SELECT array_agg(t.code ORDER BY length(t.code), t.code) FROM touch x
                    JOIN core.route t ON t.id = x.route_id WHERE x.node = n.id) AS codes
            FROM core.place p JOIN core.node pn ON pn.id = p.primary_node_id
            JOIN core.node n ON n.place_id = p.id AND n.status = 'active' AND n.id <> p.primary_node_id
            WHERE p.status = 'confirmed'
              AND EXISTS (SELECT 1 FROM touch x WHERE x.node = n.id AND x.route_id = ANY(%(ids)s::uuid[]))
              AND NOT EXISTS (SELECT 1 FROM touch x WHERE x.node = n.id AND x.legs = 1)
              AND NOT EXISTS (
                  SELECT 1 FROM source_index.trail_segment s
                  WHERE ST_DWithin(s.geom, n.geom, %(near)s) AND GeometryType(s.geom) = 'LINESTRING'
                    AND (ST_DWithin(ST_StartPoint(s.geom), n.geom, %(near)s)
                         OR ST_DWithin(ST_EndPoint(s.geom), n.geom, %(near)s))
                    AND NOT EXISTS (SELECT 1 FROM core.source_reference sr
                                    JOIN core.edge e ON e.id = sr.subject_id AND e.status = 'active'
                                    WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                                      AND sr.external_id = s.id))
            ORDER BY p.name, 4 DESC""", {"ids": mine, "near": routes.ACCESS_M}):
        codes = spot["codes"]
        named = codes[0] if len(codes) == 1 else ", ".join(codes[:-1]) + " and " + codes[-1]
        out.append({"kind": "passed", "label": spot["name"], "lon": spot["lon"], "lat": spot["lat"],
                    "metres": spot["metres"],
                    "action": {"kind": "passed", "place": spot["place"], "node": spot["node"]},
                    "text": f"Take the arrival point {_metres(spot['metres'])} from where the routes of "
                            f"{spot['name']} meet out of the place: {named} only "
                            f"{'passes' if len(codes) == 1 else 'pass'} it"})

    # Stretches Turrutebasen says follow a road or track: lay them on the mapped line.
    for stretch in _rows(conn, """
            SELECT DISTINCT e.id::text AS edge, t.attrs->>'rutefolger' AS follows,
                   (SELECT string_agg(rt.code, ', ' ORDER BY length(rt.code), rt.code)
                    FROM core.route_leg x JOIN core.route rt ON rt.id = x.route_id AND rt.status = 'active'
                    WHERE x.edge_id = e.id) AS codes
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id AND e.status = 'active'
            JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = e.id
                 AND sr.source_key = 'turrutebasen' AND sr.ref_type = 'adopted_from'
            JOIN source_index.trail_segment t ON t.id = sr.external_id
            WHERE l.route_id = ANY(%(ids)s::uuid[]) AND r.a_node <> r.b_node
              AND r.kind NOT IN ('water', 'glacier') AND t.attrs->>'rutefolger' = ANY(%(way)s)""",
            {"ids": mine, "way": list(ALONG_A_WAY)}):
        try:
            traced = routes._trace(conn, stretch["edge"])
        except (places.Invalid, places.NotFound):
            continue
        moved, lon, lat = conn.execute("""
            SELECT ST_HausdorffDistance(r.geom, g.new),
                   ST_X(ST_Transform(ST_LineInterpolatePoint(g.new, 0.5), 4326)),
                   ST_Y(ST_Transform(ST_LineInterpolatePoint(g.new, 0.5), 4326))
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 LATERAL (SELECT ST_SetSRID(ST_GeomFromText(%s), 25833) AS new) g
            WHERE e.id = %s""", (traced["wkt"], stretch["edge"])).fetchone()
        longer = traced["length_m"] / max(traced["was_m"], 1)
        if moved < SNAP_WORTH_M or moved > SNAP_MOVE_M or not 0.75 <= longer <= 1.33:
            continue        # there already, or too different to propose unseen
        out.append({"kind": "snap", "label": stretch["codes"], "lon": lon, "lat": lat,
                    "metres": traced["was_m"], "moves_m": round(moved),
                    "text": f"Lay {_metres(traced['was_m'])} of {stretch['codes']} on the mapped path or road: "
                            f"it moves up to {round(moved)} m and becomes {_metres(traced['length_m'])}",
                    "geometry": traced["geometry"],
                    "action": {"kind": "snap", "edge": stretch["edge"]}})

    order = {"loop": 0, "double": 1, "twig": 2, "gap": 3, "reach": 4, "arrival": 5, "passed": 6, "place": 7,
             "snap": 8}
    out.sort(key=lambda p: (order[p["kind"]], p.get("moves_m", 0), len(p["label"]), p["label"]))
    return out


def approve(conn, author: str, action: dict) -> str | None:
    """Carry out one proposal, as the person who approved it. Returns the route it concerns, if one."""
    kind = action["kind"]
    if kind == "snap":
        routes.snap(conn, action["edge"], author)
        return None
    if kind == "loop":
        for route_id, version in conn.execute(
                """SELECT t.id::text, t.version FROM core.route_leg l
                   JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                   WHERE l.edge_id = %s""", (action["edge"],)).fetchall():
            routes.remove_leg(conn, route_id, version, action["edge"], author)
        return None
    if kind == "place":
        places.at_node(conn, action["node"], author)
        return None
    if kind == "arrival":
        places.take_in(conn, action["place"], action["node"], author)
        return None
    if kind == "passed":
        places.remove_access_point(conn, action["place"], action["node"], author)
        return None
    if kind == "reach":
        routes.add_stretch(conn, action["route"], _version(conn, action["route"]), author,
                           edge_id=action["edge"])
        if routes.detail(conn, action["route"])["shape"] != "line":
            raise places.Conflict("this no longer applies: the route would not be one line")
        return action["route"]
    route_id = action.get("route") or routes.make(conn, action["candidate"], author)
    done = _repair(conn, route_id, author)
    if not done or done["kind"] != kind:
        raise places.Conflict("this no longer applies: the route has changed since it was proposed")
    return route_id


_TOLD = ("code", "was_m", "length_m", "turrutebasen_m", "beside_m", "followed_m", "stretches", "moves_m")


def against_mapped(conn, area: str) -> dict:
    """Every route of the area against the mapped paths and roads: what laying each on them would give.

    Nothing is changed. A route is listed when a mapped line runs beside it
    somewhere it does not lie on one already.
    """
    done = {str(r) for (r,) in conn.execute("SELECT id FROM core.route WHERE status = 'active' AND reviewed")}
    seen, rows, compared, was, beside = {}, [], 0, 0, 0
    for route_id in worklist.routes_of(conn, area):
        found = routes.compare_with_mapped(conn, route_id, "", seen=seen)
        compared, was, beside = compared + 1, was + found["was_m"], beside + found["beside_m"]
        if found["stretches"]:
            rows.append({"id": route_id, "done": route_id in done, **{k: found[k] for k in _TOLD}})
    rows.sort(key=lambda r: (r["done"], len(r["code"]), r["code"]))
    return {"area": area.lower(), "compared": compared, "was_m": was, "beside_m": beside, "routes": rows}


def lay_all_beside(conn, area: str, author: str) -> dict:
    """Lay every route of the area that is not done on the mapped paths and roads beside it. One action.

    A stretch that a route said to be done runs over is left as it is, also
    where another route shares it: done means a person has found it right.
    """
    mine = worklist.routes_of(conn, area)
    done = {str(r) for (r,) in conn.execute("SELECT id FROM core.route WHERE status = 'active' AND reviewed")}
    leave = frozenset(str(e) for (e,) in conn.execute(
        "SELECT DISTINCT edge_id FROM core.route_leg WHERE route_id = ANY(%s::uuid[])", (sorted(done),)))
    laid, was, now, stretches = 0, 0, 0, 0
    for route_id in mine:
        if route_id in done:
            continue
        found = routes.compare_with_mapped(conn, route_id, author, dry_run=False, leave=leave)
        if found["done"]:
            laid, stretches = laid + 1, stretches + found["stretches"]
            was, now = was + found["was_m"], now + found["length_m"]
    return {"routes": laid, "stretches": stretches, "was_m": was, "length_m": now,
            "left_done": len(done & set(mine))}
