"""What needs attention in an area, as a list a person can work through."""
from __future__ import annotations

from psycopg.rows import dict_row

from stiflyt2.domain import corrections, kept, network, routes

LOW_SHARE = 0.5   # a route with less than this within 20 m of a mapped path is listed


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def routes_of(conn, area: str, discontinued: bool | None = False) -> list[str]:
    """Your routes of this area: made from its Turrutebasen codes, or with a code of the area themselves.

    Those in use, unless the discontinued ones are asked for instead (True) or as well (None).
    """
    return [str(r) for (r,) in conn.execute("""
        SELECT r.id FROM core.route r
        WHERE r.status = 'active' AND (%(d)s::boolean IS NULL OR (r.discontinued_since IS NOT NULL) = %(d)s)
          AND (lower(r.code) ~ ('^' || %(a)s || '[0-9]') OR EXISTS (
            SELECT 1 FROM core.source_reference sr
            WHERE sr.subject_type = 'route' AND sr.subject_id = r.id
              AND lower(sr.external_id) ~ ('^' || %(a)s || '[0-9]')))""", {"a": area.lower(), "d": discontinued})]


def near_mapped(conn, route_ids: list[str], mapped: str) -> dict[str, float | None]:
    """For each route, how much of its length on the ground lies within 20 m of a mapped path or road.

    None where nothing is mapped within 2 km of it: there is nothing to compare with.
    Worked out stretch by stretch, each once however many routes walk it and
    however often it is asked: laying buffers round the mapped lines is what
    takes the time, and after a change only the stretches changed are new.
    """
    near = kept.answers(conn, "near mapped")       # by revision: its length, whether anything is mapped by it, how much lies by it
    legs = conn.execute("""
        SELECT DISTINCT l.route_id::text, r.id::text
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = ANY(%s::uuid[]) AND r.kind NOT IN ('water', 'glacier')""", (route_ids,)).fetchall()
    new = list({revision for _, revision in legs} - near.keys())
    if new:
        for revision, metres, something, by_it in conn.execute(f"""
                SELECT r.id::text, ST_Length(r.geom),
                       EXISTS (SELECT 1 FROM {mapped} p WHERE ST_DWithin(p.geom, r.geom, 2000)),
                       coalesce(ST_Length(ST_Intersection(r.geom, (
                           SELECT ST_Union(ST_Buffer(p.geom, 20)) FROM {mapped} p
                           WHERE ST_DWithin(p.geom, r.geom, 20)))), 0)
                FROM core.edge_revision r WHERE r.id = ANY(%s::uuid[])""", (new,)):
            near[revision] = (metres, something, by_it)
    share: dict[str, float | None] = {}
    by_route: dict[str, list[tuple[float, bool, float]]] = {}
    for route, revision in legs:
        by_route.setdefault(route, []).append(near[revision])
    for route, stretches in by_route.items():
        metres = sum(m for m, _, _ in stretches)
        share[route] = (sum(n for _, _, n in stretches) / metres
                        if metres and any(s for _, s, _ in stretches) else None)
    return share


def _who(route: dict) -> list[str]:
    """Everybody said to maintain a route of yours: on the route, or on a stretch of it."""
    return sorted({*(route.get("maintained_by") or []), *(m["name"] for m in route.get("maintainers") or [])} - {"not stated"})


def worklist(conn, area: str) -> dict:
    """Candidates still to be made yours, and what is wrong with your routes, places and network.

    An area is a three-letter route code prefix, such as jot.
    """
    area = area.lower()
    items = []
    candidates = _rows(conn, """
        SELECT r.id, r.label, r.name, s.shape, s.pieces, s.ends, r.maintainers,
               round((r.length_m / 1000)::numeric, 1)::float8 AS length_km
        FROM source_index.route r JOIN source_index.route_shape s ON s.route_id = r.id
        WHERE r.area_prefix = %(a)s ORDER BY length(r.code), r.label""", {"a": area})
    clean, left = 0, 0
    withdrawn = routes.withdrawn_by_a_person(conn)
    taken_out = routes.taken_out_by_a_person(conn)
    rows = []       # every route of the area, yours or not, for the one list a person works from
    for c in candidates:
        if routes.owner_of(conn, c["id"]):
            continue
        row = {"kind": "candidate", "id": c["id"], "code": c["label"], "km": c["length_km"],
               "name": None if (c["name"] or "Ukjent") == "Ukjent" else c["name"], "done": False,
               "maintainers": sorted(c["maintainers"] or [])}
        rows.append(row)
        if c["label"] in taken_out:
            # A route of yours was made from it and no longer runs over it: a person took it out.
            # A number on a bit of line away from its route, not a route to make yours.
            left += 1
            row["withdrawn"] = True
            over = [code for (code,) in conn.execute("""
                SELECT DISTINCT y.code FROM source_index.route_segment rs
                JOIN core.source_reference ad ON ad.subject_type = 'edge' AND ad.source_key = 'turrutebasen'
                     AND ad.ref_type = 'adopted_from' AND ad.external_id = rs.segment_id
                JOIN core.route_leg l ON l.edge_id = ad.subject_id
                JOIN core.route y ON y.id = l.route_id AND y.status = 'active'
                WHERE rs.route_id = %s ORDER BY 1""", (c["id"],))]
            named = over[0] if len(over) == 1 else ", ".join(over[:-1]) + " and " + over[-1] if over else ""
            row["problems"] = [f"Taken out of {taken_out[c['label']]}, which was made from it"
                               + (f": {named} {'runs' if len(over) == 1 else 'run'} there" if over else "")
                               + ". Kartverket is asked to take the number off"]
            continue
        if c["shape"] == "line":
            if c["label"] in withdrawn:
                left += 1  # a person withdrew its route: making in bulk leaves it alone
                row["withdrawn"] = True
                # Where other routes of yours run over all of it, it was a number too many, not a route.
                covered = conn.execute("""
                    SELECT coalesce(sum(t.length_m) FILTER (WHERE EXISTS (
                               SELECT 1 FROM core.source_reference ad JOIN core.route_leg l ON l.edge_id = ad.subject_id
                               JOIN core.route y ON y.id = l.route_id AND y.status = 'active'
                               WHERE ad.subject_type = 'edge' AND ad.source_key = 'turrutebasen'
                                 AND ad.ref_type = 'adopted_from' AND ad.external_id = t.id)), 0)
                           / nullif(sum(t.length_m), 0)
                    FROM source_index.route_segment rs JOIN source_index.trail_segment t ON t.id = rs.segment_id
                    WHERE rs.route_id = %s""", (c["id"],)).fetchone()[0] or 0
                row["problems"] = ["You withdrew it: other routes of yours run there, and Kartverket is asked "
                                   "to take the number off" if covered >= corrections.COVERED
                                   else "You withdrew it: a number on a stray bit of line, which Kartverket is asked "
                                        "to take off" if c["length_km"] * 1000 < corrections.STRAY_M
                                   else "You withdrew it: it is left as a candidate"]
            else:
                clean += 1
                row["problems"] = ["One clean line, not made yours yet"]
            continue
        what = (f"in {c['pieces']} pieces" if c["shape"] == "pieces"
                else "a loop" if c["shape"] == "loop" else f"{c['ends']} ends")
        row["problems"] = [f"Not one line: {what}"]
        items.append({"about": "candidate", "id": c["id"], "label": c["label"],
                      "problem": "shape", "text": f"Not yours yet: {what}"})

    mine = routes_of(conn, area)
    mapped = routes.mapped_lines(conn)      # paths, and roads where they are loaded
    share = near_mapped(conn, mine, mapped)
    reviewed, yours = 0, []
    for route_id in mine:
        r = routes.detail(conn, route_id)
        reviewed += r["reviewed"]
        yours.append({"id": r["id"], "code": r["code"], "name": r["name"] or r["generated_name"],
                      "reviewed": r["reviewed"], "shape": r["shape"], "maintainers": _who(r),
                      "unpublished": r["unpublished"] and r["unpublished"]["why"],
                      "km": round(r["length"]["on_foot_m"] / 1000, 1)})

        def add(problem, text, route=r):
            items.append({"about": "route", "id": route["id"], "label": route["code"],
                          "problem": problem, "text": text})

        if r["shape"] == "pieces":
            gap = max(g["metres"] for g in r["gaps"])
            add("shape", f"In {r['pieces']} pieces, the widest gap {gap} m")
        elif r["shape"] == "branched":
            add("shape", f"{len(r['ends'])} ends: a spur or a fork")
        elif r["shape"] == "doubled":
            twice = [g["metres"] for d in r["doubles"] for g in d["stretches"][1:]]
            add("shape", f"Two stretches between the same two junctions ({max(twice)} m): one too many"
                if twice else "It goes round a loop on its way: more stretches than one line has")
        if not r["name"] and not r["generated_name"]:
            loose = sum(1 for e in r["ends"] if not e["place"])
            add("name", "No name: " + ("a round trip is not named after its ends" if r["shape"] == "loop"
                                       else "it does not run between two places" if r["shape"] != "line"
                                       else f"{loose} of its ends {'is' if loose == 1 else 'are'} not at a place"))
        if share.get(route_id) is not None and share[route_id] < LOW_SHARE and r["length"]["on_foot_m"] > 500:
            add("geometry", f"Only {round(share[route_id] * 100)} % lies within 20 m of a mapped path or road")
        if r["code_provisional"]:
            add("code", "Has a temporary code")

    for p in _rows(conn, """
            SELECT p.id, p.name, (SELECT count(*) FROM core.node n
                                  WHERE n.place_id = p.id AND n.status = 'active')::int AS points
            FROM core.place p
            WHERE p.status = 'confirmed' AND NOT p.reviewed AND p.id IN (
                SELECT sr.subject_id FROM core.source_reference sr
                JOIN source_index.route_end e ON e.anchor_id = sr.detail->>'anchor_id'
                JOIN source_index.route r ON r.id = e.route_id
                WHERE sr.subject_type = 'place' AND r.area_prefix = %(a)s)
            ORDER BY 3 DESC, 2""", {"a": area}):
        if p["points"] > 1:
            items.append({"about": "place", "id": str(p["id"]), "label": p["name"], "problem": "place",
                          "text": f"Routes arrive at {p['points']} spots: check which belong"})
    net = network.validate(conn, mine)
    # A finding a person has looked at and found right is kept apart, and can be brought back.
    settled = {key for (key,) in conn.execute("SELECT key FROM core.accepted_finding")}
    accepted = []
    for f in net.pop("findings"):
        item = {"about": "network", "id": None, "label": f["label"], "kind": f["kind"],
                "problem": "end" if f["kind"] in ("open end", "dead end") else "network",
                "text": f["text"], "lon": f["lon"], "lat": f["lat"], "key": f["key"],
                "whole": f.get("whole", False), "bbox": f.get("bbox")}
        (accepted if f["key"] in settled else items).append(item)
    # Discontinued routes are kept and listed, and nothing is asked of them.
    for route_id in routes_of(conn, area, discontinued=True):
        r = routes.detail(conn, route_id)
        rows.append({"kind": "yours", "id": r["id"], "code": r["code"], "name": r["name"] or r["generated_name"],
                     "km": round(r["length"]["on_foot_m"] / 1000, 1), "done": r["reviewed"], "discontinued": True,
                     "maintainers": _who(r),
                     "unpublished": r["unpublished"] and r["unpublished"]["why"],
                     "problems": [f"Discontinued since {r['discontinued']['since']:%-d %b %Y}: {r['discontinued']['why']}"]})
    for r in yours:
        rows.append({"kind": "yours", "id": r["id"], "code": r["code"], "name": r["name"], "km": r["km"],
                     "done": r["reviewed"], "unpublished": r["unpublished"], "maintainers": r["maintainers"],
                     "problems": [i["text"] for i in items if i["about"] == "route" and i["id"] == r["id"]]})
    rows.sort(key=lambda r: (len(r["code"].split("/")[0]), r["code"]))
    order = {"shape": 0, "network": 1, "geometry": 2, "name": 3, "place": 4, "code": 5, "end": 6}
    items.sort(key=lambda i: (order[i["problem"]], i["about"], i.get("kind", ""),
                              len(i["label"]), i["label"]))
    return {"area": area, "candidates": len(candidates), "clean_candidates": clean,
            "withdrawn_candidates": left,
            "your_routes": len(mine), "reviewed_routes": reviewed,
            "discontinued_routes": sum(1 for r in rows if r.get("discontinued")), "network": net, "items": items,
            "accepted": accepted, "all": rows,
            "routes": sorted(yours, key=lambda r: (len(r["code"]), r["code"]))}
