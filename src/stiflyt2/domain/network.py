"""Does the owned network hang together? Checks of the graph that routes and places make.

The graph is every stretch a route uses, plus the connectors inside places.
A finding is a spot where things that look joined on the map are not joined
in the graph, which is where a shortest-path search would go wrong.
"""
from __future__ import annotations

import heapq
from collections import defaultdict

NEAR_M = 50   # a loose end this close to another stretch was probably meant to meet it
TOUCH_M = 1   # as near as two points may be and still count as the same spot
APART_M = 1000   # a part of the network further than this from the rest is told as a whole, not as a spot

# Every stretch in use: on an active route, or a connector of a place.
_USED = """(SELECT e.id, r.a_node, r.b_node, r.kind, r.geom
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE e.status = 'active' AND (r.kind = 'connector' OR EXISTS (
                SELECT 1 FROM core.route_leg l JOIN core.route t ON t.id = l.route_id
                WHERE l.edge_id = e.id AND t.status = 'active')))"""


def _listed(codes) -> str:
    codes = sorted(codes, key=lambda c: (len(c), c))
    if len(codes) > 3:
        return f"{', '.join(codes[:2])} and {len(codes) - 2} more"
    return " and ".join([", ".join(codes[:-1]), codes[-1]] if len(codes) > 1 else codes) or "a stretch"


def _metres(d: float) -> str:
    return f"{d:.0f} m" if round(d, 1) >= 10 else f"{d:.1f} m"


def validate(conn, route_ids: list[str]) -> dict:
    """Check the part of the network these routes use, against everything owned.

    Findings, each with a position:
    - touch: a route meets another at a point where the other is not divided
    - near: a loose end stops short of another stretch
    - crossing: two stretches cross, or lie on each other, without a junction
    - island: routes that hang together but not with the rest
    - open end: a route ends where a candidate that is not yours carries on
    - dead end: a route ends at no place and no other route
    """
    edges = {}                       # id -> (a, b, kind, metres, codes)
    reach = defaultdict(list)        # node -> [(other node, metres, edge)]
    for edge, a, b, kind, metres, codes in conn.execute(f"""
            SELECT u.id::text, u.a_node::text, u.b_node::text, u.kind, ST_Length(u.geom),
                   (SELECT array_agg(t.code) FROM core.route_leg l
                    JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                    WHERE l.edge_id = u.id)
            FROM {_USED} u"""):
        edges[edge] = (a, b, kind, metres, codes or [])
        reach[a].append((b, metres, edge))
        reach[b].append((a, metres, edge))
    mine = [str(e) for (e,) in conn.execute(
        "SELECT DISTINCT edge_id FROM core.route_leg WHERE route_id = ANY(%s::uuid[])", (route_ids,))
        if str(e) in edges]
    nodes = sorted({n for e in mine for n in edges[e][:2]})
    result = {"stretches": len(mine), "nodes": len(nodes), "parts": 0, "largest_share": None,
              "findings": []}
    if not mine:
        return result
    findings = result["findings"]

    def codes_at(node: str) -> set[str]:
        return {c for _, _, e in reach[node] for c in edges[e][4]}

    def walk(start: str, limit: float) -> dict[str, float]:
        """Walking distance from a node to every node within the limit."""
        best, queue = {start: 0.0}, [(0.0, start)]
        while queue:
            d, node = heapq.heappop(queue)
            if d > best.get(node, limit):
                continue
            for other, metres, _ in reach[node]:
                if d + metres < best.get(other, limit):
                    best[other] = d + metres
                    heapq.heappush(queue, (d + metres, other))
        return best

    where = {n: (lon, lat, at_place) for n, lon, lat, at_place in conn.execute("""
        SELECT n.id::text, ST_X(ST_Transform(n.geom, 4326)), ST_Y(ST_Transform(n.geom, 4326)),
               n.place_id IS NOT NULL
        FROM core.node n WHERE n.id = ANY(%s::uuid[])""", (nodes,))}

    # A node beside a stretch it is not an end of. It is a finding only when the
    # way round on foot is much longer than the gap: close on the ground, far in the graph.
    close = defaultdict(list)
    for node, edge, d, along in conn.execute(f"""
            SELECT n.id::text, near.id::text, near.d, near.along
            FROM core.node n CROSS JOIN LATERAL (
                SELECT u.id, ST_Distance(u.geom, n.geom) AS d,
                       ST_LineLocatePoint(u.geom, n.geom) AS along
                FROM {_USED} u
                WHERE ST_DWithin(u.geom, n.geom, %(near)s) AND u.kind <> 'connector'
                  AND n.id NOT IN (u.a_node, u.b_node)
                ORDER BY 2 LIMIT 4) near
            WHERE n.id = ANY(%(nodes)s::uuid[]) ORDER BY 1, 3""", {"nodes": nodes, "near": NEAR_M}):
        close[node].append((edge, d, along))
    flagged, pairs = set(), set()
    for d, node, edge, along in sorted((d, node, edge, along) for node, found in close.items()
                                       for edge, d, along in found):
        loose = len(reach[node]) == 1
        if node in flagged or (d > TOUCH_M and not loose):
            continue
        if any(frozenset((e, edge)) in pairs for _, _, e in reach[node]):
            continue        # the same two stretches are already said, where they are closest
        a, b, _, metres, codes = edges[edge]
        limit = 4 * d + NEAR_M
        near = walk(node, limit)
        if min(near.get(a, limit) + along * metres, near.get(b, limit) + (1 - along) * metres) < limit:
            continue        # joined a few steps away: a short spur or a stretch drawn twice
        other = a if along < 1e-6 else b if along > 1 - 1e-6 else None
        here, there = _listed(codes_at(node)), _listed(codes)
        if d <= TOUCH_M:
            what = "An end" if loose else "A junction"
            kind, text = "touch", (f"{what} of {here} lies on {there}, which is not divided "
                                   "at this point, so they are not joined")
        elif other and len(reach[other]) == 1:
            kind, text = "near", f"An end of {here} and an end of {there} are {_metres(d)} apart"
            flagged.add(other)
        else:
            kind, text = "near", f"An end of {here} stops {_metres(d)} short of {there}"
        lon, lat, _ = where[node]
        findings.append({"kind": kind, "label": here, "text": text, "lon": lon, "lat": lat,
                         "key": f"{kind}:{node}:{edge}"})
        flagged.add(node)
        pairs.update(frozenset((e, edge)) for _, _, e in reach[node])

    # Two stretches that cross, or lie on each other, away from their ends. A knot
    # beside a junction they both hang in is not a finding: the way round is a few steps.
    crossed = defaultdict(list)
    for one, two, lon, lat, along_one, along_two in conn.execute(f"""
            SELECT m.id::text, u.id::text, ST_X(ST_Transform(x.pt, 4326)), ST_Y(ST_Transform(x.pt, 4326)),
                   ST_LineLocatePoint(m.geom, x.pt), ST_LineLocatePoint(u.geom, x.pt)
            FROM {_USED} m
            JOIN {_USED} u ON u.id <> m.id AND ST_Intersects(m.geom, u.geom)
                 AND (u.id > m.id OR NOT u.id = ANY(%(edges)s::uuid[]))
            JOIN core.node ma ON ma.id = m.a_node JOIN core.node mb ON mb.id = m.b_node
            JOIN core.node ua ON ua.id = u.a_node JOIN core.node ub ON ub.id = u.b_node
            CROSS JOIN LATERAL (SELECT ST_PointOnSurface(d.geom) AS pt
                                FROM ST_Dump(ST_Intersection(m.geom, u.geom)) d) x
            WHERE m.id = ANY(%(edges)s::uuid[])
              AND m.kind NOT IN ('connector', 'water') AND u.kind NOT IN ('connector', 'water')
              AND least(ST_Distance(x.pt, ma.geom), ST_Distance(x.pt, mb.geom),
                        ST_Distance(x.pt, ua.geom), ST_Distance(x.pt, ub.geom)) > %(touch)s
            ORDER BY 1, 2, 5""", {"edges": mine, "touch": TOUCH_M}):
        if frozenset((one, two)) in pairs:
            continue        # already said as an end that stops short or touches
        (a1, b1, _, m1, _), (a2, b2, _, m2, _) = edges[one], edges[two]
        round_about = min(
            off1 + walk(n1, NEAR_M).get(n2, NEAR_M) + off2
            for n1, off1 in ((a1, along_one * m1), (b1, (1 - along_one) * m1))
            for n2, off2 in ((a2, along_two * m2), (b2, (1 - along_two) * m2)))
        if round_about >= NEAR_M:
            crossed[one, two].append((lon, lat))
    for (one, two), spots in crossed.items():
        here, there = _listed(edges[one][4]), _listed(edges[two][4])
        text = (f"Two stretches of {here} cross" if here == there
                else f"A stretch of {here} crosses a stretch of {there}")
        times = f" at {len(spots)} spots" if len(spots) > 1 else ""
        findings.append({"kind": "crossing", "label": here, "lon": spots[0][0], "lat": spots[0][1],
                         "text": f"{text}{times} without a junction", "key": f"crossing:{one}:{two}"})

    # Parts that hang together. Everything owned counts, so two routes of this
    # area may be joined by a route of another.
    part = {}

    def root(n: str) -> str:
        while part.setdefault(n, n) != n:
            part[n] = part[part[n]]
            n = part[n]
        return n

    for a, b, *_ in edges.values():
        part[root(a)] = root(b)
    parts = defaultdict(list)
    for e in mine:
        parts[root(edges[e][0])].append(e)
    by_length = sorted(parts.values(), key=lambda es: -sum(edges[e][3] for e in es))
    result["parts"] = len(by_length)
    result["largest_share"] = round(sum(edges[e][3] for e in by_length[0])
                                    / sum(edges[e][3] for e in mine), 3)
    for island in by_length[1:]:
        d, lon, lat, mid_lon, mid_lat, box = conn.execute("""
            SELECT ST_Distance(a.g, b.g), ST_X(c.p), ST_Y(c.p), ST_X(m.p), ST_Y(m.p),
                   ARRAY[ST_XMin(x.g), ST_YMin(x.g), ST_XMax(x.g), ST_YMax(x.g)]
            FROM (SELECT ST_Collect(r.geom) AS g FROM core.edge e
                  JOIN core.edge_revision r ON r.id = e.current_revision_id
                  WHERE e.id = ANY(%s::uuid[])) a,
                 (SELECT ST_Collect(r.geom) AS g FROM core.edge e
                  JOIN core.edge_revision r ON r.id = e.current_revision_id
                  WHERE e.id = ANY(%s::uuid[])) b,
                 LATERAL (SELECT ST_Transform(ST_ClosestPoint(a.g, b.g), 4326) AS p) c,
                 LATERAL (SELECT ST_Transform(ST_ClosestPoint(a.g, ST_Centroid(a.g)), 4326) AS p) m,
                 LATERAL (SELECT ST_Transform(ST_Envelope(a.g), 4326) AS g) x""",
            (island, by_length[0])).fetchone()
        codes = {c for e in island for c in edges[e][4]}
        gap = ("it touches or crosses it without a junction" if d <= TOUCH_M
               else f"the nearest stretch is {_metres(d) if d < 1000 else f'{d / 1000:.1f} km'} away")
        finding = {"kind": "island", "label": _listed(codes), "lon": lon, "lat": lat,
                   "text": f"Not joined to the largest part of the area's network: {gap}",
                   "key": "island:" + ",".join(sorted(codes))}
        if d > APART_M:
            # Far from the rest there is no spot where it should have been joined: the finding is
            # about the part as a whole, and is shown as that.
            finding.update(lon=mid_lon, lat=mid_lat, whole=True, bbox=[round(v, 6) for v in box])
        findings.append(finding)

    # Ends at nothing. Where Turrutebasen has a candidate carrying on from the
    # spot, the end is open only until that candidate is made yours.
    ends = [n for n in nodes if len(reach[n]) == 1 and not where[n][2] and n not in flagged]
    onward = dict(conn.execute("""
        SELECT n.id::text, array_agg(DISTINCT rt.label)
        FROM core.node n
        JOIN source_index.trail_segment t ON ST_DWithin(t.geom, n.geom, %(touch)s)
        JOIN source_index.route rt ON rt.id = ANY(t.route_ids)
        WHERE n.id = ANY(%(nodes)s::uuid[]) AND NOT EXISTS (
            SELECT 1 FROM core.source_reference sr
            JOIN core.edge e ON e.id = sr.subject_id AND e.status = 'active'
            WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
              AND sr.external_id = t.id)
        GROUP BY 1""", {"nodes": ends, "touch": TOUCH_M}))
    for node in ends:
        here, (lon, lat, _) = _listed(codes_at(node)), where[node]
        if node in onward:
            kind = "open end"
            text = f"Ends where {_listed(onward[node])} carries on, which is not yours yet"
        else:
            kind, text = "dead end", "Ends at no place and no other route"
        findings.append({"kind": kind, "label": here, "text": text, "lon": lon, "lat": lat,
                         "key": f"end:{node}"})
    return result
