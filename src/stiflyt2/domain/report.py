"""Where an area stands: how much is yours, how long it is, who maintains it, what is left."""
from __future__ import annotations

from collections import Counter

from stiflyt2.domain import mapdata, worklist

PROBLEMS = {  # in the order of the to-do list
    "shape": "not one line",
    "network": "not joined in the network",
    "geometry": "far from any mapped path or road",
    "name": "without a name",
    "place": "places with several arrival spots",
    "code": "temporary codes",
    "end": "ends at no place",
}


def report(conn, area: str) -> dict:
    """Numbers for one area, with its to-do list. Lengths are in metres."""
    todo = worklist.worklist(conn, area)
    mine = worklist.routes_of(conn, area)
    routes_on_foot, = conn.execute("""
        SELECT coalesce(sum(ST_Length(ST_Transform(r.geom, 4326)::geography)), 0)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = ANY(%s::uuid[]) AND r.kind <> 'water'""", (mine,)).fetchone()
    length, by = Counter(), Counter()
    for kind, marked, m in conn.execute("""
            SELECT r.kind, r.marked, ST_Length(ST_Transform(r.geom, 4326)::geography)
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE e.id IN (SELECT edge_id FROM core.route_leg WHERE route_id = ANY(%s::uuid[]))""",
            (mine,)):
        if kind == "water":
            length["water_m"] += m
            continue
        length["on_foot_m"] += m
        length["glacier_m"] += m if kind == "glacier" else 0
        length["unmarked_m"] += m if marked is False else 0
        length["marking_unknown_m"] += m if marked is None else 0
    # Who maintains a stretch is what each route says for it. Ground that two routes with
    # different maintainers share, or that has shared responsibility, counts for each of them.
    for who, m in conn.execute("""
            SELECT x.who, sum(x.m) FROM (
                SELECT DISTINCT e.id, who, ST_Length(ST_Transform(r.geom, 4326)::geography) AS m
                FROM core.route_leg l JOIN core.route t ON t.id = l.route_id
                JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision r ON r.id = e.current_revision_id,
                     unnest(CASE WHEN coalesce(l.maintainers, t.maintainers) = '{}'
                                 THEN ARRAY['not stated'] ELSE coalesce(l.maintainers, t.maintainers) END) AS who
                WHERE l.route_id = ANY(%s::uuid[]) AND r.kind <> 'water') x
            GROUP BY 1""", (mine,)):
        by[who] += m
    places, places_reviewed = conn.execute("""
        SELECT count(*), count(*) FILTER (WHERE p.reviewed) FROM core.place p
        WHERE p.status = 'confirmed' AND p.id IN (
            SELECT sr.subject_id FROM core.source_reference sr
            JOIN source_index.route_end e ON e.anchor_id = sr.detail->>'anchor_id'
            JOIN source_index.route r ON r.id = e.route_id
            WHERE sr.subject_type = 'place' AND r.area_prefix = %s)""", (todo["area"],)).fetchone()
    problems = Counter(i["problem"] for i in todo["items"])
    return {
        "area": todo["area"],
        "routes": {"candidates": todo["candidates"], "yours": todo["your_routes"],
                   "reviewed": todo["reviewed_routes"],
                   "discontinued": todo["discontinued_routes"],
                   "clean_candidates": todo["clean_candidates"],
                   "withdrawn_candidates": todo["withdrawn_candidates"],
                   "need_a_person": sum(1 for i in todo["items"] if i["about"] == "candidate")},
        "length": {"routes_on_foot_m": round(routes_on_foot),
                   **{k: round(length[k]) for k in ("on_foot_m", "water_m", "glacier_m",
                                                    "unmarked_m", "marking_unknown_m")}},
        "maintainers": [{"name": who, "metres": round(m)} for who, m in by.most_common()],
        "places": {"places": places, "reviewed": places_reviewed},
        "network": todo["network"],
        "to_do": {p: problems[p] for p in PROBLEMS if problems[p]},
        "items": todo["items"],
        "yours": todo["routes"],
        "accepted": todo["accepted"],
        "all": todo["all"],
        "map_data": mapdata.status(conn, area),
    }


def as_text(r: dict) -> str:
    """The report as plain lines, for the terminal."""
    def km(m):
        return f"{m / 1000:,.1f} km"

    ro, le, net = r["routes"], r["length"], r["network"]
    lines = [
        f"Area {r['area']}",
        (f"Routes      {ro['candidates']} candidates in Turrutebasen. {ro['yours']} routes are yours, "
         f"{ro['reviewed']} reviewed. Not yours yet: {ro['clean_candidates']} clean, "
         f"{ro['need_a_person']} that need a person, {ro['withdrawn_candidates']} withdrawn."
         + (f" {ro['discontinued']} discontinued and kept." if ro["discontinued"] else "")),
        (f"Length      {km(le['routes_on_foot_m'])} of route on foot, over {km(le['on_foot_m'])} of "
         f"ground ({km(le['routes_on_foot_m'] - le['on_foot_m'])} shared between routes). "
         f"{km(le['water_m'])} over water, {km(le['glacier_m'])} over glacier."),
        f"Marking     {km(le['unmarked_m'])} not marked, {km(le['marking_unknown_m'])} not known.",
        "Maintained  " + ("; ".join(f"{m['name']} {km(m['metres'])}" for m in r["maintainers"])
                          or "nothing yet"),
        f"Places      {r['places']['places']}, {r['places']['reviewed']} reviewed.",
    ]
    if net["stretches"]:
        lines.append(f"Network     {net['stretches']} stretches between {net['nodes']} nodes, in "
                     f"{net['parts']} {'part' if net['parts'] == 1 else 'parts'} that are not joined "
                     f"to each other; the largest holds {net['largest_share']:.0%} of the length.")
    lines.append("To do       " + ("; ".join(f"{n} {PROBLEMS[p]}" for p, n in r["to_do"].items())
                                   or "nothing"))
    problem = None
    for i in r["items"]:
        if i["problem"] != problem:
            problem = i["problem"]
            lines += ["", PROBLEMS[problem].capitalize()]
        at = f"  ({i['lat']:.5f}, {i['lon']:.5f})" if "lat" in i else ""
        lines.append(f"  {i['label']:<22} {i['text']}{at}")
    return "\n".join(lines)


_GROUND = "ST_Length(ST_Transform(v.geom, 4326)::geography)"


def areas(conn, maintainer: str | None = None) -> dict:
    """Every area side by side: how much ground its routes cover, and how much of that is marked.

    Ground, not route length: a stretch that several routes share is counted
    once in an area. Two sets of numbers per area: what Turrutebasen has, and
    what is yours (routes in use; discontinued ones are left out). Given a
    maintainer, only the ground that maintainer is said to look after. A
    stretch that routes of two areas share counts in both areas, and once in
    the sum. Lengths are in metres.
    """
    from stiflyt2.domain import whose

    who = {"who": maintainer, "kin": whose.kin(maintainer)}
    rows: dict[str, dict] = {}

    def row(area: str) -> dict:
        return rows.setdefault(area, {"area": area, "turrutebasen": dict.fromkeys(
            ("routes", "route_m", "ground_m", "marked_m", "unmarked_m", "water_m"), 0), "yours": dict.fromkeys(
            ("routes", "done", "route_m", "ground_m", "marked_m", "unmarked_m", "unknown_m", "glacier_m", "water_m"), 0)})

    theirs = """
        WITH seg AS (
            SELECT DISTINCT {area} AS area, t.id, t.length_m AS m, t.marked, t.attrs->>'rutefolger' = 'VV' AS water
            FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
            JOIN source_index.trail_segment t ON t.id = rs.segment_id
            WHERE r.area_prefix IS NOT NULL AND """ + whose.sql("rs.maintainers") + """)
        SELECT area, coalesce(sum(m) FILTER (WHERE water IS NOT TRUE), 0),
               coalesce(sum(m) FILTER (WHERE water IS NOT TRUE AND marked), 0),
               coalesce(sum(m) FILTER (WHERE water IS NOT TRUE AND NOT marked), 0),
               coalesce(sum(m) FILTER (WHERE water), 0)
        FROM seg GROUP BY area"""
    for area, ground, marked, unmarked, water in conn.execute(theirs.format(area="r.area_prefix"), who):
        row(area)["turrutebasen"].update(ground_m=round(ground), marked_m=round(marked), unmarked_m=round(unmarked),
                                         water_m=round(water))
    for area, n, metres in conn.execute("""
            SELECT r.area_prefix, count(DISTINCT r.id), coalesce(sum(t.length_m), 0)
            FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
            JOIN source_index.trail_segment t ON t.id = rs.segment_id
            WHERE r.area_prefix IS NOT NULL AND """ + whose.sql("rs.maintainers") + """
            GROUP BY 1""", who):
        row(area)["turrutebasen"].update(routes=n, route_m=round(metres))

    mine = """
        WITH mine AS (
            SELECT DISTINCT a.area, r.id, r.reviewed FROM core.route r, LATERAL (
                SELECT substring(lower(r.code) from '^([a-z]{{3}})[0-9]') AS area
                UNION SELECT substring(lower(sr.external_id) from '^([a-z]{{3}})[0-9]')
                      FROM core.source_reference sr WHERE sr.subject_type = 'route' AND sr.subject_id = r.id) a
            WHERE r.status = 'active' AND r.discontinued_since IS NULL AND a.area IS NOT NULL),
        legs AS (
            SELECT {area} AS area, m.id AS route, m.reviewed, e.id AS edge, v.kind, v.marked, """ + _GROUND + """ AS m
            FROM mine m JOIN core.route_leg l ON l.route_id = m.id JOIN core.route t ON t.id = l.route_id
            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
            WHERE """ + whose.sql("coalesce(l.maintainers, t.maintainers)") + """),
        ground AS (SELECT DISTINCT area, edge, kind, marked, m FROM legs)
        SELECT g.area,
               (SELECT count(DISTINCT route) FROM legs x WHERE x.area IS NOT DISTINCT FROM g.area),
               (SELECT count(DISTINCT route) FILTER (WHERE reviewed) FROM legs x WHERE x.area IS NOT DISTINCT FROM g.area),
               (SELECT coalesce(sum(m) FILTER (WHERE kind <> 'water'), 0) FROM legs x WHERE x.area IS NOT DISTINCT FROM g.area),
               coalesce(sum(m) FILTER (WHERE kind <> 'water'), 0),
               coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked), 0),
               coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked IS FALSE), 0),
               coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked IS NULL), 0),
               coalesce(sum(m) FILTER (WHERE kind = 'glacier'), 0),
               coalesce(sum(m) FILTER (WHERE kind = 'water'), 0)
        FROM ground g GROUP BY g.area"""

    def said(found) -> dict:
        keys = ("routes", "done", "route_m", "ground_m", "marked_m", "unmarked_m", "unknown_m", "glacier_m", "water_m")
        return dict(zip(keys, (v if i < 2 else round(v) for i, v in enumerate(found)), strict=True))

    for area, *found in conn.execute(mine.format(area="m.area"), who):
        row(area)["yours"] = said(found)
    # The sum counts every stretch once, also where routes of two areas run over it.
    total = {"area": "all", "turrutebasen": dict(row("all")["turrutebasen"]), "yours": dict(row("all")["yours"])}
    del rows["all"]
    for _, ground, marked, unmarked, water in conn.execute(theirs.format(area="NULL::text"), who):
        total["turrutebasen"].update(ground_m=round(ground), marked_m=round(marked), unmarked_m=round(unmarked),
                                     water_m=round(water))
    total["turrutebasen"].update(routes=sum(r["turrutebasen"]["routes"] for r in rows.values()),
                                 route_m=sum(r["turrutebasen"]["route_m"] for r in rows.values()))
    for _, *found in conn.execute(mine.format(area="NULL::text"), who):
        total["yours"] = said(found)
    listed = sorted((r for r in rows.values() if r["turrutebasen"]["ground_m"] or r["yours"]["routes"]),
                    key=lambda r: (-r["yours"]["ground_m"], -r["turrutebasen"]["ground_m"], r["area"]))
    return {"maintainer": maintainer, "areas": listed, "all": total}


def areas_as_text(found: dict) -> str:
    """The areas as a table for the terminal: kilometres of ground, shared stretches counted once."""
    def km(m):
        return f"{m / 1000:,.1f}" if m else "-"

    head = ("area", "routes", "ground", "marked", "routes", "done", "ground", "marked", "not marked", "not said",
            "glacier", "water")
    lines = [("Ground covered by routes, in km. A stretch several routes share is counted once."
              + (f" Only what {found['maintainer']} is said to maintain." if found["maintainer"] else "")),
             f"{'':<5} {'Turrutebasen':^26} | {'Yours, in use':^72}",
             f"{head[0]:<5} {head[1]:>6} {head[2]:>9} {head[3]:>9} | "
             + " ".join(f"{h:>{w}}" for h, w in zip(head[4:], (6, 5, 9, 9, 10, 9, 8, 7), strict=True))]
    for r in [*found["areas"], found["all"]]:
        t, y = r["turrutebasen"], r["yours"]
        if r["area"] == "all":
            lines.append("")
        lines.append(f"{r['area']:<5} {t['routes']:>6} {km(t['ground_m']):>9} {km(t['marked_m']):>9} | "
                     f"{y['routes'] or '-':>6} {y['done'] or '-':>5} {km(y['ground_m']):>9} {km(y['marked_m']):>9} "
                     f"{km(y['unmarked_m']):>10} {km(y['unknown_m']):>9} {km(y['glacier_m']):>8} {km(y['water_m']):>7}")
    return "\n".join(lines)


def maintainers(conn, least_m: float = 25_000) -> list[dict]:
    """Who maintains routes in Turrutebasen, by the ground they look after: those with at least this much.

    For choosing whose routes to look at. Ground counted once per maintainer.
    """
    return [{"name": name, "metres": round(m), "areas": n} for name, m, n in conn.execute("""
        SELECT who, sum(m) FILTER (WHERE once = 1), count(DISTINCT area) FROM (
            SELECT who, id, m, area, row_number() OVER (PARTITION BY who, id ORDER BY area) AS once FROM (
                SELECT DISTINCT who, t.id, t.length_m AS m, r.area_prefix AS area
                FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
                JOIN source_index.trail_segment t ON t.id = rs.segment_id, unnest(rs.maintainers) AS who
                WHERE r.area_prefix IS NOT NULL AND t.attrs->>'rutefolger' IS DISTINCT FROM 'VV') s) x
        GROUP BY who HAVING sum(m) FILTER (WHERE once = 1) >= %s
        ORDER BY 2 DESC, who""", (least_m,))]
