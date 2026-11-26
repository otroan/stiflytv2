"""Finding a route of ours by what one remembers of it: its code, its name, or a place it passes.

For the search at the top of the front page. Looking is not a feature: whoever
is let in may search. The place names of the whole country are searched apart
(`index.query.search_places`); the areas, a handful of codes, in the page.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from stiflyt2 import seasons
from stiflyt2.domain import routes as owned

# In use, and of the season asked for (%(season)s).
IN_USE = "t.status = 'active' AND t.season = %(season)s AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL"


def routes(conn, text: str, limit: int = 10, *, season: str = seasons.SUMMER) -> list[dict]:
    """Routes in use of the season asked for that match: the code itself first, then codes that begin so, then
    names, then the routes that pass a place so named (with the place). Each with its area and what it is called."""
    season = seasons.known(season)
    q = text.strip()
    if len(q) < 2:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        found = cur.execute(f"""
            WITH hit AS (
                SELECT t.id, t.code, t.name, NULL::text AS passes,
                       CASE WHEN lower(t.code) = lower(%(q)s) THEN 0 WHEN lower(t.code) LIKE lower(%(q)s) || '%%' THEN 1
                            ELSE 2 END AS rank
                FROM core.route t
                WHERE {IN_USE} AND (lower(t.code) LIKE lower(%(q)s) || '%%' OR t.name ILIKE '%%' || %(q)s || '%%')
                UNION ALL
                SELECT t.id, t.code, t.name, p.name, 3
                FROM core.route t JOIN core.route_leg l ON l.route_id = t.id
                JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
                JOIN core.node n ON n.id IN (v.a_node, v.b_node)
                JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
                WHERE {IN_USE} AND p.name ILIKE '%%' || %(q)s || '%%')
            SELECT * FROM (SELECT DISTINCT ON (id) id::text, code, name, passes, rank FROM hit ORDER BY id, rank, passes) x
            ORDER BY rank, length(code), code LIMIT %(limit)s""", {"q": q, "limit": limit, "season": season}).fetchall()
    nameless = [r["id"] for r in found if not r["name"]]
    called = owned.called(conn, nameless) if nameless else {}
    return [{"id": r["id"], "code": r["code"], "area": r["code"][:3].lower(), "name": r["name"] or called.get(r["id"]),
             "passes": r["passes"]} for r in found]
