"""What to ask Kartverket to change in Turrutebasen.

The difference between what is yours and the Turrutebasen that is loaded now,
told per segment by Turrutebasen's own id (`lokalid`) and in its own attribute
names (`merking`, `skilting`, `rutefolger`, `rutenummer`, `rutenavn`). Nothing
is stored: when Kartverket has made a change and it has been imported, that
correction is no longer in the list.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from stiflyt2 import height
from stiflyt2.domain import routes

OFFSET_M = 1.0    # a line that has moved less than this is not reported
COVERED = 0.95    # a withdrawn route this much of which other routes of yours run over was one number too many
STRAY_M = 100     # and so was a withdrawn route shorter than this: a number on a stray bit of line, never a route

_LAID_ON = {"fkb_wfs": "the mapped path (FKB)", "vegnett": "the road centre line (NVDB)"}
_FOLLOWS = {"water": "VV"}      # where a kind of ours has a value of rutefolger
_KIND = {"trail": "path", "tractor_road": "tractor road", "road": "road", "glacier": "glacier",
         "water": "water", "off_trail": "no path, in terrain", "connector": "short link on foot",
         "other": "other"}
_YES = {True: "JA", False: "NEI"}


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def corrections(conn, area: str | None = None, srid: int = 4326) -> list[dict]:
    """Every change to Turrutebasen that follows from your routes, for one area prefix or for all.

    Kinds of change: geometry, attributes, delete (a segment that is no stretch
    at all), remove from route, add to route, new segment, route attribute (who
    maintains it, grading), route name, route code.
    """
    digits = 7 if srid == 4326 else 2
    params = {"area": (area or "").lower(), "srid": srid, "digits": digits}
    out: list[dict] = []

    def add(change, text, *, lokalid=None, versjonid=None, rutenummer=(), was=None, now=None,
            geometry=None, was_geometry=None, at=None, **more):
        out.append({"change": change, "lokalid": lokalid, "versjonid": versjonid,
                    "rutenummer": sorted(rutenummer), "text": text, "was": was or {},
                    "now": now or {}, "geometry": geometry, "was_geometry": was_geometry,
                    "lon": at[0] if at else None, "lat": at[1] if at else None, **more})

    # Segments that have become stretches of yours: has the line or what is said about it changed?
    segments = _rows(conn, f"""
        SELECT t.id AS lokalid, t.attrs->>'versjonid' AS versjonid, t.codes,
               bool_or(e.status = 'active') AS live, count(*) FILTER (WHERE e.status = 'active') AS parts,
               array_agg(DISTINCT r.kind) FILTER (WHERE e.status = 'active') AS kinds,
               array_agg(DISTINCT r.marked) FILTER (WHERE e.status = 'active') AS marked,
               array_agg(DISTINCT r.signed) FILTER (WHERE e.status = 'active') AS signed,
               coalesce(bool_and(r.a_node = r.b_node) FILTER (WHERE e.status = 'active'), false) AS loop,
               t.attrs->>'merking' AS merking, t.attrs->>'skilting' AS skilting,
               t.attrs->>'rutefolger' AS rutefolger,
               ST_HausdorffDistance(ST_LineMerge(ST_Collect(r.geom) FILTER (WHERE e.status = 'active')),
                                    t.geom) AS moved,
               round(t.length_m)::int AS was_m,
               round(sum({routes._LEN}) FILTER (WHERE e.status = 'active'))::int AS now_m,
               ST_AsGeoJSON(ST_Transform(ST_LineMerge(ST_Collect(r.geom) FILTER (WHERE e.status = 'active')),
                                         %(srid)s), %(digits)s)::json AS geometry,
               ST_AsGeoJSON(ST_Transform(t.geom, %(srid)s), %(digits)s)::json AS was_geometry,
               ARRAY[ST_X(c.p), ST_Y(c.p)] AS at,
               (SELECT array_agg(DISTINCT x.source_key) FROM core.source_reference x
                JOIN core.edge xe ON xe.id = x.subject_id AND xe.status = 'active'
                JOIN core.source_reference own ON own.subject_type = 'edge' AND own.subject_id = xe.id
                     AND own.source_key = 'turrutebasen' AND own.external_id = t.id
                WHERE x.subject_type = 'edge' AND x.ref_type = 'traced') AS laid_on,
               (array_agg(p.name) FILTER (WHERE p.name IS NOT NULL))[1] AS place
        FROM core.source_reference sr JOIN core.edge e ON e.id = sr.subject_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN source_index.trail_segment t ON t.id = sr.external_id
        LEFT JOIN core.node n ON n.id = r.b_node
        LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed',
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(ST_GeometryN(t.geom, 1), 0.5), 4326) AS p) c
        WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen' AND sr.ref_type = 'adopted_from'
          AND e.status <> 'undone'
          AND (%(area)s = '' OR EXISTS (SELECT 1 FROM unnest(t.codes) code
                                        WHERE code ~ ('^' || %(area)s || '[0-9]')))
        GROUP BY t.id, c.p ORDER BY t.id""", params)
    gone = set()
    for s in segments:
        about = {"lokalid": s["lokalid"], "versjonid": s["versjonid"], "rutenummer": s["codes"],
                 "at": s["at"]}
        if not s["live"] and s["was_m"] >= 2 * routes.ACCESS_M:
            continue        # a real stretch no route of yours uses any more: told per route below
        if s["loop"] or not s["live"]:
            where = f", the arrival point of {s['place']}" if s["place"] else ""
            add("delete", f"Remove the segment: it is {s['was_m']} m long and it begins and ends "
                          f"at the same junction{where}",
                geometry=s["was_geometry"], was={"length_m": s["was_m"]}, **about)
            gone.add(s["lokalid"])
            continue
        if s["moved"] > OFFSET_M:
            onto = " and ".join(_LAID_ON.get(k, k) for k in sorted(s["laid_on"] or []))
            add("geometry",
                f"Move the line: it lies up to {s['moved']:.0f} m from where it should, and becomes "
                f"{s['now_m']} m from {s['was_m']} m" + (f". Laid on {onto}" if onto else ""),
                geometry=s["geometry"], was_geometry=s["was_geometry"], offset_m=round(s["moved"], 1),
                was={"length_m": s["was_m"]}, now={"length_m": s["now_m"]}, **about)
        if len(s["kinds"]) > 1 or len(s["marked"]) > 1 or len(s["signed"]) > 1:
            # You have divided the segment and its parts are not alike: say so, part by part.
            parts = _rows(conn, f"""
                SELECT r.kind, r.marked, r.signed, round({routes._LEN})::int AS metres
                FROM core.source_reference sr JOIN core.edge e ON e.id = sr.subject_id AND e.status = 'active'
                JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                  AND sr.ref_type = 'adopted_from' AND sr.external_id = %(id)s
                ORDER BY (sr.detail->>'from')::float8 NULLS FIRST""", {"id": s["lokalid"]})
            told = "; ".join(f"{p['metres']} m {_KIND.get(p['kind'], p['kind'])}"
                             + ("" if p["marked"] is None else ", merking " + _YES[p["marked"]])
                             + ("" if p["signed"] is None else ", skilting " + _YES[p["signed"]])
                             for p in parts)
            add("attributes", f"Divide the segment, its parts are not alike: {told}",
                geometry=s["geometry"], now={"parts": told}, **about)
            continue
        kind, marked, signed = s["kinds"][0], s["marked"][0], s["signed"][0]
        was, now = {}, {}
        if marked is not None and _YES[marked] != s["merking"]:
            was["merking"], now["merking"] = s["merking"], _YES[marked]
        if signed is not None and _YES[signed] != s["skilting"]:
            was["skilting"], now["skilting"] = s["skilting"], _YES[signed]
        follows = _FOLLOWS.get(kind)
        if (kind != "trail" and follows != s["rutefolger"]) or (
                s["rutefolger"] == "VV" and kind != "water"):
            was["rutefolger"], now["rutefolger"] = s["rutefolger"], follows or f"({_KIND[kind]})"
        if now:
            add("attributes", "Change " + "; ".join(
                f"{k} from {was[k] or 'nothing'} to {now[k]}" for k in now),
                geometry=s["geometry"], was=was, now=now, **about)

    # Routes: which segments they run over, and what they are called.
    made = _rows(conn, """
        SELECT r.id::text AS id, r.code, r.code_provisional, r.name, r.reviewed, r.grading,
               c.grading AS gradering, sr.external_id AS rutenummer,
               coalesce(c.id, l.id) AS candidate, coalesce(c.name, l.name, sr.detail->>'name') AS rutenavn
        FROM core.route r
        JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                                     AND sr.source_key = 'turrutebasen'
        LEFT JOIN source_index.route c ON c.id = sr.detail->>'candidate_id'
        LEFT JOIN source_index.route l ON c.id IS NULL AND l.label = sr.detail->>'label'
        WHERE r.status = 'active' AND r.discontinued_since IS NULL
          AND r.unpublished_why IS NULL      -- not published: nothing is added for it; its number is asked off, below
          AND (%(area)s = '' OR lower(sr.external_id) ~ ('^' || %(area)s || '[0-9]'))
        ORDER BY length(r.code), r.code""", params)
    ids = sorted({m["id"] for m in made})
    legs = _rows(conn, f"""
        SELECT l.route_id::text AS route, e.id::text AS edge, ad.external_id AS lokalid, t.codes,
               t.attrs->>'versjonid' AS versjonid, rv.kind, round({routes._LEN.replace('r.geom', 'rv.geom')})::int AS metres,
               ST_AsGeoJSON(ST_Transform(rv.geom, %(srid)s), %(digits)s)::json AS geometry,
               ARRAY[ST_X(c.p), ST_Y(c.p)] AS at
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision rv ON rv.id = e.current_revision_id
        LEFT JOIN core.source_reference ad ON ad.subject_type = 'edge' AND ad.subject_id = e.id
             AND ad.source_key = 'turrutebasen' AND ad.ref_type = 'adopted_from'
        LEFT JOIN source_index.trail_segment t ON t.id = ad.external_id,
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(rv.geom, 0.5), 4326) AS p) c
        WHERE l.route_id = ANY(%(ids)s::uuid[])""", {**params, "ids": ids})
    numbers: dict[str, set] = {}        # route -> the Turrutebasen codes it was made from
    for m in made:
        numbers.setdefault(m["id"], set()).add(m["rutenummer"])
    used: dict[str, set] = {}           # Turrutebasen code -> segments your routes with that code use
    added: set[tuple] = set()
    lacking: dict[str, set] = {}        # a stretch Turrutebasen has no segment for -> the numbers that run along it
    for leg in legs:
        if leg["lokalid"] is None:
            lacking.setdefault(leg["edge"], set()).update(numbers[leg["route"]])
    for leg in legs:
        codes = numbers[leg["route"]]
        if leg["lokalid"] is None:
            # One segment to add, with every number that runs along it: two routes that cross the same
            # ice by one stretch are one segment to Kartverket, not two on top of each other.
            together = lacking.pop(leg["edge"], None)
            if together:
                add("new segment",
                    f"Add a segment for {', '.join(sorted(together))}: {leg['metres']} m, "
                    f"{_KIND.get(leg['kind'], leg['kind'])}",
                    rutenummer=together, geometry=leg["geometry"], at=leg["at"],
                    now={"length_m": leg["metres"], "rutefolger": _FOLLOWS.get(leg["kind"])})
            continue
        for code in codes:
            used.setdefault(code, set()).add(leg["lokalid"])
        if leg["lokalid"] not in gone and not codes & set(leg["codes"] or []) and (
                leg["lokalid"], tuple(sorted(codes))) not in added:
            added.add((leg["lokalid"], tuple(sorted(codes))))
            add("add to route", f"Add rutenummer {', '.join(sorted(codes))} to the segment: the route runs here",
                lokalid=leg["lokalid"], versjonid=leg["versjonid"], rutenummer=leg["codes"] or [],
                geometry=leg["geometry"], at=leg["at"], now={"rutenummer": sorted(codes)})
    dropped = _rows(conn, """
        SELECT DISTINCT ON (c.code, t.id) c.code AS rutenummer, t.id AS lokalid, t.codes,
               t.attrs->>'versjonid' AS versjonid, round(t.length_m)::int AS metres,
               ST_AsGeoJSON(ST_Transform(t.geom, %(srid)s), %(digits)s)::json AS geometry,
               ARRAY[ST_X(p.p), ST_Y(p.p)] AS at,
               -- A few metres with both ends inside one arrival point: never made a stretch.
               (SELECT coalesce(pl.name, '') FROM core.node n LEFT JOIN core.place pl ON pl.id = n.place_id
                WHERE n.status = 'active' AND t.length_m < %(short)s
                  AND ST_DWithin(n.geom, ST_StartPoint(ST_GeometryN(t.geom, 1)), %(reach)s)
                  AND ST_DWithin(n.geom, ST_EndPoint(ST_GeometryN(t.geom, 1)), %(reach)s)
                ORDER BY n.place_id IS NULL LIMIT 1) AS inside
        FROM source_index.route c JOIN source_index.route_segment rs ON rs.route_id = c.id
        JOIN source_index.trail_segment t ON t.id = rs.segment_id,
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(ST_GeometryN(t.geom, 1), 0.5), 4326) AS p) p
        WHERE c.id = ANY(%(candidates)s)""",
        {**params, "candidates": [m["candidate"] for m in made if m["candidate"]],
         "short": 2 * routes.ACCESS_M, "reach": routes.ACCESS_M})
    for d in dropped:
        if d["lokalid"] in gone or d["lokalid"] in used.get(d["rutenummer"], set()):
            continue
        if d["inside"] is not None:
            where = f", the arrival point of {d['inside']}" if d["inside"] else ""
            add("delete", f"Remove the segment: it is {d['metres']} m long and it begins and ends "
                          f"at the same junction{where}",
                lokalid=d["lokalid"], versjonid=d["versjonid"], rutenummer=d["codes"],
                geometry=d["geometry"], at=d["at"], was={"length_m": d["metres"]})
            gone.add(d["lokalid"])
            continue
        add("remove from route",
            f"Take rutenummer {d['rutenummer']} off the segment ({d['metres']} m): the route does not run here",
            lokalid=d["lokalid"], versjonid=d["versjonid"], rutenummer=d["codes"],
            geometry=d["geometry"], at=d["at"], was={"rutenummer": d["rutenummer"]})
    # A route a person has withdrawn, where other routes of yours run over what it ran over: a
    # misspelt number or the same route twice. Its number is one too many on those segments. A
    # route withdrawn that nothing else of yours covers is a route left to others, and says nothing.
    withdrawn: dict[str, list] = {}
    for w in _rows(conn, """
            WITH gone AS (
                SELECT DISTINCT c.id, c.code, NULL::text AS why FROM history.event e
                JOIN core.route r ON r.id = e.subject_id AND r.status = 'retired'
                JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                     AND sr.source_key = 'turrutebasen'
                JOIN source_index.route c ON c.label = sr.detail->>'label'
                WHERE e.type = 'route.retired' AND e.author <> %(auto)s
                  AND (%(area)s = '' OR lower(c.code) ~ ('^' || %(area)s || '[0-9]'))
                  AND NOT EXISTS (SELECT 1 FROM core.source_reference x
                                  JOIN core.route y ON y.id = x.subject_id AND y.status = 'active'
                                  WHERE x.subject_type = 'route' AND x.source_key = 'turrutebasen'
                                    AND x.detail->>'label' = c.label)
              UNION
                SELECT DISTINCT c.id, c.code,
                       'discontinued since ' || to_char(r.discontinued_since, 'FMDD.FMMM.YYYY') || ': ' || r.discontinued_why
                FROM core.route r
                JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                     AND sr.source_key = 'turrutebasen'
                JOIN source_index.route c ON c.label = sr.detail->>'label'
                WHERE r.status = 'active' AND r.discontinued_since IS NOT NULL
                  AND (%(area)s = '' OR lower(c.code) ~ ('^' || %(area)s || '[0-9]'))
              UNION     -- kept and not published: not to be shown by Turrutebasen either
                SELECT DISTINCT c.id, c.code, 'not published: ' || r.unpublished_why
                FROM core.route r
                JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                     AND sr.source_key = 'turrutebasen'
                JOIN source_index.route c ON c.label = sr.detail->>'label'
                WHERE r.status = 'active' AND r.unpublished_why IS NOT NULL AND r.discontinued_since IS NULL
                  AND (%(area)s = '' OR lower(c.code) ~ ('^' || %(area)s || '[0-9]')))
            SELECT g.code AS rutenummer, g.why, t.id AS lokalid, t.codes, t.attrs->>'versjonid' AS versjonid,
                   t.length_m AS metres, ST_AsGeoJSON(ST_Transform(t.geom, %(srid)s), %(digits)s)::json AS geometry,
                   ARRAY[ST_X(p.p), ST_Y(p.p)] AS at,
                   (SELECT array_agg(DISTINCT y.code ORDER BY y.code) FROM core.source_reference ad
                    JOIN core.route_leg l ON l.edge_id = ad.subject_id
                    JOIN core.route y ON y.id = l.route_id AND y.status = 'active'
                    WHERE ad.subject_type = 'edge' AND ad.source_key = 'turrutebasen'
                      AND ad.ref_type = 'adopted_from' AND ad.external_id = t.id) AS others
            FROM gone g JOIN source_index.route_segment rs ON rs.route_id = g.id
            JOIN source_index.trail_segment t ON t.id = rs.segment_id AND g.code = ANY(t.codes),
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(ST_GeometryN(t.geom, 1), 0.5), 4326) AS p) p
            ORDER BY g.code, t.id""", {**params, "auto": routes.AUTOMATIC}):
        withdrawn.setdefault(w["rutenummer"], []).append(w)
    for code, segments in withdrawn.items():
        why = segments[0]["why"]        # set for a route discontinued or not published: its number comes off wherever it is
        whole = sum(s["metres"] for s in segments)
        covered = sum(s["metres"] for s in segments if s["others"]) / (whole or 1)
        if not why and covered < COVERED and whole >= STRAY_M:
            continue
        for s in segments:
            if s["lokalid"] in gone:
                continue
            others = [o for o in (s["others"] or []) if o.lower() != code.lower()]
            named = others[0] if len(others) == 1 else ", ".join(others[:-1]) + " and " + others[-1] if others else ""
            runs = f", and {named} {'runs' if len(others) == 1 else 'run'} here" if others else ""
            add("remove from route",
                f"Take rutenummer {code} off the segment ({round(s['metres'])} m): {code} is "
                + (why if why else f"withdrawn as no route of its own{runs}"
                   + (f": all of it is {round(whole)} m" if whole < STRAY_M else "")),
                lokalid=s["lokalid"], versjonid=s["versjonid"], rutenummer=s["codes"],
                geometry=s["geometry"], at=s["at"], was={"rutenummer": code})
    # Who maintains: Turrutebasen keeps it per route on each segment. A change is told once for
    # all the segments of a route it holds for. Where you say nobody and Turrutebasen names somebody,
    # that is a change too: a route made from Turrutebasen starts with who Turrutebasen has, so nobody
    # is what a person has said.
    for change in _rows(conn, """
            SELECT sr.external_id AS rutenummer, rs.maintainers AS was,
                   coalesce(l.maintainers, r.maintainers) AS now,
                   array_agg(rs.segment_id ORDER BY rs.segment_id) AS lokalids,
                   (SELECT count(*) FROM core.route_leg x WHERE x.route_id = r.id) AS legs,
                   ST_AsGeoJSON(ST_Transform(ST_Collect(t.geom), %(srid)s), %(digits)s)::json AS geometry
            FROM core.route r
            JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                 AND sr.source_key = 'turrutebasen'
            JOIN core.route_leg l ON l.route_id = r.id
            JOIN core.source_reference ad ON ad.subject_type = 'edge' AND ad.subject_id = l.edge_id
                 AND ad.source_key = 'turrutebasen' AND ad.ref_type = 'adopted_from'
            JOIN source_index.route c ON c.code = sr.external_id
            JOIN source_index.route_segment rs ON rs.route_id = c.id AND rs.segment_id = ad.external_id
            JOIN source_index.trail_segment t ON t.id = rs.segment_id
            WHERE r.id = ANY(%(ids)s::uuid[])
              AND (SELECT array_agg(x ORDER BY x) FROM unnest(rs.maintainers) x)
                  IS DISTINCT FROM (SELECT array_agg(x ORDER BY x)
                                    FROM unnest(coalesce(l.maintainers, r.maintainers)) x)
            GROUP BY sr.external_id, rs.maintainers, coalesce(l.maintainers, r.maintainers), r.id
            ORDER BY 1""", {**params, "ids": ids}):
        n = len(change["lokalids"])
        where = "all its segments" if n >= change["legs"] else f"{n} of its segments"
        add("route attribute",
            f"Change vedlikeholdsansvarlig for {change['rutenummer']} from "
            f"{'; '.join(sorted(change['was'])) or 'nothing'} to {'; '.join(sorted(change['now']))}, on {where}"
            if change["now"] else
            f"Take {'; '.join(sorted(change['was']))} off as vedlikeholdsansvarlig for {change['rutenummer']}, "
            f"on {where}: nobody is said to maintain it there",
            rutenummer=[change["rutenummer"]], geometry=change["geometry"],
            was={"vedlikeholdsansvarlig": sorted(change["was"])},
            now={"vedlikeholdsansvarlig": sorted(change["now"])}, lokalids=change["lokalids"])
    for m in made:
        # A name is proposed only for a route a person has looked at: a name worked out
        # from the places at the ends of a route nobody has checked is a guess.
        theirs = m["rutenavn"] or ""
        ours = m["name"] or (m["reviewed"] and routes.name_from_ends(conn, m["id"])) or None
        same = ours and all(part.strip().lower() in theirs.lower() for part in ours.split(" – "))
        if ours and ours != theirs and not same:
            add("route name", f"Call {m['rutenummer']} “{ours}”: Turrutebasen has “{theirs}”",
                rutenummer=[m["rutenummer"]], was={"rutenavn": theirs}, now={"rutenavn": ours})
        # A grading a person has set is asked for as it is. Where nobody has set one, the grading the
        # route's length and climb give is asked for, once a person has looked at the route: worked
        # out for a line nobody has checked, it is a guess like the name.
        grading, how = m["grading"], ""
        if m["candidate"] and m["reviewed"] and (not grading or grading == "I"):
            high = height.of_route(conn, m["id"])
            by_numbers = height.harder(high["there"]["at_least"], high["back"]["at_least"]) if high["there"] else None
            if by_numbers:
                grading, how = by_numbers, ", by its length and climb"
        if m["candidate"] and (grading or None) != (m["gradering"] or None) and grading:
            add("route attribute", f"Change gradering for {m['rutenummer']} from "
                                   f"{m['gradering'] or 'nothing'} to {grading}{how}",
                rutenummer=[m["rutenummer"]], was={"gradering": m["gradering"]},
                now={"gradering": grading})
        if not m["code_provisional"] and m["code"].lower() != m["rutenummer"].lower():
            add("route code", f"Number {m['rutenummer']} as {m['code']}",
                rutenummer=[m["rutenummer"]], was={"rutenummer": m["rutenummer"]},
                now={"rutenummer": m["code"]})

    order = ["delete", "geometry", "attributes", "remove from route", "add to route", "new segment",
             "route attribute", "route name", "route code"]
    out.sort(key=lambda c: (order.index(c["change"]),
                            [(len(n), n) for n in c["rutenummer"]], c["lokalid"] or ""))
    return out


def as_geojson(found: list[dict], srid: int = 4326) -> dict:
    """The corrections as features: the line as it should be, and for a moved line also as it is now."""
    features = []
    for c in found:
        properties = {"change": c["change"], "lokalid": c["lokalid"], "versjonid": c["versjonid"],
                      "rutenummer": ", ".join(c["rutenummer"]), "text": c["text"],
                      **{f"was_{k}": v for k, v in c["was"].items()},
                      **{f"now_{k}": v for k, v in c["now"].items()}}
        if "offset_m" in c:
            properties["offset_m"] = c["offset_m"]
        if "lokalids" in c:
            properties["lokalids"] = ", ".join(c["lokalids"])
        features.append({"type": "Feature", "geometry": c["geometry"],
                         "properties": {**properties, "state": "proposed"}})
        if c["was_geometry"]:
            features.append({"type": "Feature", "geometry": c["was_geometry"],
                             "properties": {**properties, "state": "in Turrutebasen now"}})
    collection = {"type": "FeatureCollection", "name": "stiflyt-corrections-to-turrutebasen",
                  "features": features}
    if srid != 4326:
        collection["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{srid}"}}
    return collection


def as_text(found: list[dict]) -> str:
    """The corrections as plain lines, one per change, under a heading per kind of change."""
    headings = {"delete": "Segments to remove", "geometry": "Lines to move",
                "attributes": "Attributes to change", "remove from route": "Route numbers to take off segments",
                "add to route": "Route numbers to add to segments", "new segment": "Segments to add",
                "route attribute": "What is said about a route",
                "route name": "Route names", "route code": "Route numbers"}
    lines, change = [f"{len(found)} corrections to Turrutebasen"], None
    for c in found:
        if c["change"] != change:
            change = c["change"]
            lines += ["", headings[change]]
        who = (c["lokalid"] or "(no segment yet)"
               if c["change"] not in ("route name", "route code", "route attribute") else "")
        lines.append(f"  {who:<38}{', '.join(c['rutenummer']):<24} {c['text']}".rstrip())
    return "\n".join(lines)
