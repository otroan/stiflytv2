"""The front door: the areas that have routes of ours in use, and one area with its routes and what they add up to."""
from __future__ import annotations

from collections import Counter

from psycopg.rows import dict_row

from stiflyt2 import height
from stiflyt2.domain import routes

_IN_USE = "t.status = 'active' AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL"
_LEGS = """core.route t JOIN core.route_leg l ON l.route_id = t.id
           JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id"""
_LEN = "ST_Length(ST_Transform(v.geom, 4326)::geography)"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def areas(conn) -> list[dict]:
    """The areas that have routes of ours in use, by the three letters their codes start with.

    For each how many routes, and how far they go on foot when each is
    walked for itself. Lengths are in metres.
    """
    return _rows(conn, f"""
        SELECT lower(left(t.code, 3)) AS area, count(DISTINCT t.id)::int AS routes,
               round(coalesce(sum({_LEN}) FILTER (WHERE v.kind <> 'water'), 0))::int AS route_m
        FROM core.route t LEFT JOIN core.route_leg l ON l.route_id = t.id
        LEFT JOIN core.edge e ON e.id = l.edge_id LEFT JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE {_IN_USE} AND t.code ~* '^[a-z]{{3}}'
        GROUP BY 1 ORDER BY 2 DESC, 1""")


def area(conn, area: str) -> dict:
    """One area: its routes in use, each with its length, climb, time and grading, and what they add up to.

    `statistics` counts ground once where routes share a stretch, and says
    how much of it is marked; the highest and lowest the routes go; how many
    routes there are of each grading (the one set, or the one length and
    climb give where none is set); and the places the routes pass, by kind.
    Lengths and heights are in metres.
    """
    a = {"a": area.lower()}
    mine = f"{_IN_USE} AND lower(left(t.code, 3)) = %(a)s"
    listed = _rows(conn, f"""
        SELECT t.id::text AS id, t.code, t.name, t.grading,
               round(coalesce(sum({_LEN}) FILTER (WHERE v.kind <> 'water'), 0))::int AS on_foot_m,
               round(coalesce(sum({_LEN}) FILTER (WHERE v.kind = 'water'), 0))::int AS water_m,
               round(coalesce(sum({_LEN}) FILTER (WHERE v.kind = 'glacier'), 0))::int AS glacier_m
        FROM core.route t LEFT JOIN core.route_leg l ON l.route_id = t.id
        LEFT JOIN core.edge e ON e.id = l.edge_id LEFT JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE {mine} GROUP BY t.id ORDER BY length(t.code), t.code""", a)
    graphs = {r["id"]: routes._graph(conn, r["id"]) for r in listed}
    place = {n["id"]: n["name"] for n in _rows(conn, """
        SELECT n.id::text AS id, p.name FROM core.node n
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%(ids)s::uuid[])""", {"ids": [n for g in graphs.values() for n in g["ends"]]})}
    for r in listed:
        graph, high = graphs[r["id"]], height.of_route(conn, r["id"])
        by_numbers = height.harder(high["there"]["at_least"], high["back"]["at_least"]) if high["there"] else None
        grading, said_by = routes.grading_of({"grading": r["grading"], "grading_by_numbers": by_numbers})
        r.update(name=r["name"] or routes.named_after(graph["shape"], [place.get(n) for n in graph["ends"]]),
                 shape=graph["shape"], grading=grading, grading_from=said_by,
                 up_m=high["up_m"], down_m=high["down_m"], low_m=high["low_m"], high_m=high["high_m"],
                 minutes=high["there"] and high["there"]["minutes"], minutes_back=high["back"] and high["back"]["minutes"])
    ground = _rows(conn, f"""
        SELECT round(coalesce(sum(m) FILTER (WHERE kind <> 'water'), 0))::int AS ground_m,
               round(coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked), 0))::int AS marked_m,
               round(coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked IS FALSE), 0))::int AS unmarked_m,
               round(coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked IS NULL), 0))::int AS unknown_m,
               round(coalesce(sum(m) FILTER (WHERE kind = 'glacier'), 0))::int AS glacier_m,
               round(coalesce(sum(m) FILTER (WHERE kind = 'water'), 0))::int AS water_m
        FROM (SELECT DISTINCT e.id, v.kind, v.marked, {_LEN} AS m FROM {_LEGS} WHERE {mine}) x""", a)[0]
    passed = _rows(conn, f"""
        SELECT p.kind, count(DISTINCT p.id)::int AS n
        FROM {_LEGS} JOIN core.node n ON n.id IN (v.a_node, v.b_node)
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE {mine} GROUP BY 1 ORDER BY 2 DESC, 1""", a)
    with_heights = [r for r in listed if r["high_m"] is not None]
    top = max(with_heights, key=lambda r: r["high_m"], default=None)
    bottom = min(with_heights, key=lambda r: r["low_m"], default=None)
    return {"area": a["a"], "routes": listed, "statistics": {
        "routes": len(listed),
        "route_m": sum(r["on_foot_m"] for r in listed), **ground,
        "highest": top and {"metres": top["high_m"], "route": top["code"]},
        "lowest": bottom and {"metres": bottom["low_m"], "route": bottom["code"]},
        "gradings": dict(Counter(r["grading"] or "none" for r in listed)),
        "places": {p["kind"]: p["n"] for p in passed}}}
