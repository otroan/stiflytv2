"""Your routes as a file to hand on: one feature per route as it is now, with what has changed.

Corrections for Kartverket are told per Turrutebasen segment
(`domain/corrections.py`). This is the same seen from the route, for UT.no and
anyone else who reads whole routes: the line of each route, what is said about
it, and the changes from Turrutebasen that touch it. Each route carries its
own id, which stays the same from one export to the next.
"""
from __future__ import annotations

from stiflyt2.domain import corrections, routes, worklist


def _concerns(correction: dict) -> set[str]:
    """The route numbers a correction for Kartverket is about, in lower case.

    A line that has moved concerns every route on the segment. A number taken
    off or put on a segment concerns the route of that number, not the others
    that run there.
    """
    if correction["change"] == "remove from route":
        about = [correction["was"].get("rutenummer")]
    elif correction["change"] == "add to route":
        about = correction["now"].get("rutenummer") or []
    else:
        about = correction["rutenummer"]
    return {str(n).lower() for n in about if n}


def routes_geojson(conn, area: str | None = None, changed_only: bool = False, srid: int = 4326,
                   discontinued: bool | None = None) -> dict:
    """Every route of yours that is published, of one area or of all, as GeoJSON features: in use, and discontinued.

    A discontinued route is handed on as long as it is kept here, saying since
    when and why: whoever still shows it is to take it out. Every feature says
    its `state`. Asked for, only the routes in use (False) or only the
    discontinued ones (True) are given. With `changed_only`, the routes that
    differ from Turrutebasen and the discontinued ones: what there is to act on.
    """
    ids = worklist.routes_of(conn, area, discontinued) if area else [
        str(r) for (r,) in conn.execute(
            """SELECT id FROM core.route WHERE status = 'active'
               AND (%(d)s::boolean IS NULL OR (discontinued_since IS NOT NULL) = %(d)s)""", {"d": discontinued})]
    lines = dict(conn.execute("""
        SELECT l.route_id::text,
               ST_AsGeoJSON(ST_Transform(ST_LineMerge(ST_Collect(
                   CASE WHEN l.reversed THEN ST_Reverse(x.geom) ELSE x.geom END ORDER BY l.seq)), %(srid)s),
                   %(digits)s)::json
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node,
             -- A stretch may stop up to a metre short of the junction it hangs in. Handed on, the
             -- line of a route is unbroken: each stretch is drawn right up to its junctions.
             LATERAL (SELECT ST_SetPoint(ST_SetPoint(r.geom, 0, a.geom), -1, b.geom) AS geom) x
        WHERE l.route_id = ANY(%(ids)s::uuid[]) GROUP BY 1""",
        {"ids": ids, "srid": srid, "digits": 7 if srid == 4326 else 2}).fetchall())
    made_from: dict[str, set[str]] = {}
    for route_id, code in conn.execute("""
            SELECT subject_id::text, lower(external_id) FROM core.source_reference
            WHERE subject_type = 'route' AND source_key = 'turrutebasen'
              AND subject_id = ANY(%s::uuid[])""", (ids,)):
        made_from.setdefault(route_id, set()).add(code)
    still = {code for (code,) in conn.execute("SELECT DISTINCT lower(code) FROM source_index.route")}
    differs = [(c, _concerns(c)) for c in corrections.corrections(conn, area)]
    features = []
    for route_id in ids:
        if route_id not in lines:
            continue        # a route with no stretch has no line to hand on
        route = routes.detail(conn, route_id)
        if route["unpublished"]:
            continue        # kept here and not handed on
        codes, gone = made_from.get(route_id, set()), route["discontinued"]
        if gone:
            # What Kartverket is asked about it, segment by segment, is one thing here: it is discontinued.
            changes = [f"discontinued since {gone['since']:%-d.%-m.%Y}: {gone['why']}"]
        else:
            changes = [f"{c['change']}: {c['text']}" for c, about in differs if codes & about]
        if changed_only and not changes:
            continue
        # How much it climbs is handed on only when it is known for the whole of it.
        high = route["height"] if route["height"]["known_m"] == route["height"]["on_foot_m"] else {}
        grading, grading_from = routes.grading_of(route)
        ends = [e["place"] for e in route["ends"]]
        if route["round_trip"]:         # it starts and ends at one junction
            ends = [route["round_trip"]["place"]] * 2
        features.append({"type": "Feature", "id": route_id, "geometry": lines[route_id], "properties": {
            "id": route_id, "code": route["code"], "code_is_temporary": route["code_provisional"],
            "state": "discontinued" if gone else "in use",
            "name": route["name"] or route["generated_name"], "description": route["description"],
            "description_back": route.get("description_back"),
            # The description is of the route walked from here to there; `description_back` the other way.
            "walked_from": (route.get("walked") or {}).get("from"), "walked_to": (route.get("walked") or {}).get("to"),
            "from": ends[0] if ends else None, "to": ends[-1] if len(ends) > 1 else None,
            "round_trip": bool(route["round_trip"]),
            "maintained_by": "; ".join(route["maintained_by"]),
            # Set by a person, or worked out from length and climb where nobody has set one: `grading_from` says which.
            "grading": grading, "grading_from": grading_from,
            "on_foot_m": route["length"]["on_foot_m"], "over_water_m": route["length"]["water_m"],
            "over_glacier_m": route["length"]["glacier_m"],
            # Walked from `from` to `to`; the other way the two change places.
            "ascent_m": high.get("up_m"), "descent_m": high.get("down_m"),
            "lowest_m": high.get("low_m"), "highest_m": high.get("high_m"),
            # By Naismith's rule, and what length and climb alone make of the grading: each way.
            "minutes": (high.get("there") or {}).get("minutes"),
            "minutes_back": (high.get("back") or {}).get("minutes"),
            "grading_at_least": (high.get("there") or {}).get("at_least"),
            "grading_back_at_least": (high.get("back") or {}).get("at_least"),
            "checked_by_a_person": route["reviewed"], "utno_id": route["utno_id"],
            "turrutebasen_rutenummer": ", ".join(sorted(codes)),
            "in_turrutebasen": bool(codes & still),
            "earlier_codes": ", ".join(e["value"] for e in route["earlier"] if e["kind"] == "code"),
            "changed": bool(changes), "changes": len(changes), "what_changed": "\n".join(changes),
            **({"discontinued_since": gone["since"].isoformat(), "discontinued_why": gone["why"]} if gone else {})}})
    features.sort(key=lambda f: (len(f["properties"]["code"]), f["properties"]["code"]))
    collection = {"type": "FeatureCollection",
                  "name": f"stiflyt-routes-{(area or 'all').lower()}"
                          + {None: "", True: "-discontinued", False: "-in-use"}[discontinued],
                  "features": features}
    if srid != 4326:
        collection["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{srid}"}}
    return collection
