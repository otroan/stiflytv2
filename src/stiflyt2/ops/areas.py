"""What an area is: the routes whose codes begin with its three letters, and routes added to it from another area.

Breheimen is the bre routes and sun27: a route of Sunnmøre that the same
people look after. An area's lists (its routes, issues, reports, photos, a
trip of all its routes) have the added routes too; their codes stay what they
are. A route added is kept by its id as it was, and read through what stands
for it now (ADR-004): one taken into another is followed, one withdrawn drops
out. Adding and taking out is for the route responsible, and can be undone.
"""
from __future__ import annotations

import re

from psycopg.types.json import Jsonb

from stiflyt2 import seasons
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import anchor

_AREA = re.compile(r"^[a-z]{3}$")


def added(conn, *, season: str = seasons.SUMMER) -> dict[str, list[str]]:
    """The routes in use of the season asked for added to each area from another, by area: their ids now."""
    season = seasons.known(season)
    rows = conn.execute("SELECT area, route_id::text FROM ops.area_route ORDER BY added_at").fetchall()
    now = anchor.stands_for(conn, "route", {r for _, r in rows})
    in_use = {i for (i,) in conn.execute("""SELECT id::text FROM core.route WHERE id = ANY(%s::uuid[]) AND status = 'active'
                                            AND season = %s AND discontinued_since IS NULL AND unpublished_why IS NULL""",
                                         ([i for v in now.values() for i in v], season))}
    told: dict[str, list[str]] = {}
    for area, route in rows:
        for r in now.get(route, []):
            if r in in_use and r not in told.setdefault(area, []):
                told[area].append(r)
    return told


def added_to(conn, area: str, *, season: str = seasons.SUMMER) -> list[str]:
    """The routes in use of the season asked for added to this area from another: their ids now."""
    return added(conn, season=season).get((area or "").lower(), [])


def listing(conn) -> dict[str, list[dict]]:
    """The routes added to each area, of both seasons, with their codes: for a page to show and take out."""
    ids: dict[str, list[str]] = {}
    for season in seasons.SEASONS:
        for a, v in added(conn, season=season).items():
            ids.setdefault(a, []).extend(v)
    codes = dict(conn.execute("SELECT id::text, code FROM core.route WHERE id = ANY(%s::uuid[])",
                              ([i for v in ids.values() for i in v],)).fetchall())
    return {a: [{"id": i, "code": codes.get(i)} for i in v] for a, v in ids.items()}


def _event(conn, author, area: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, 'area', %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), area, type_, Jsonb(payload)))


def _route(conn, code: str) -> dict:
    found = conn.execute("""SELECT id::text, code, season FROM core.route WHERE status = 'active' AND lower(code) = lower(%s)
                            AND discontinued_since IS NULL""", (code.strip(),)).fetchone()
    if not found:
        raise NotFound(f"no route of ours in use is called {code}")
    return {"id": found[0], "code": found[1], "season": found[2]}


def add(conn, author, area: str, code: str) -> dict:
    """Add a route of another area to this one, by its code."""
    area = (area or "").lower()
    if not _AREA.match(area):
        raise Invalid("an area is three letters")
    r = _route(conn, code)
    if r["code"][:3].lower() == area:
        raise Invalid(f"{r['code']} is of {area} already, by its code")
    if r["id"] in added_to(conn, area, season=r["season"]):
        raise Invalid(f"{r['code']} is added to {area} already")
    conn.execute("INSERT INTO ops.area_route (area, route_id, added_by) VALUES (%s, %s::uuid, %s)", (area, r["id"], str(author)))
    _event(conn, author, area, "area.route_added", {"route": r["code"]})
    return {"area": area, "added": listing(conn).get(area, [])}


def take_out(conn, author, area: str, route: str) -> dict:
    """Take a route added from another area out of this one again. Undo puts it back."""
    area = (area or "").lower()
    rows = conn.execute("SELECT id::text, route_id::text FROM ops.area_route WHERE area = %s", (area,)).fetchall()
    now = anchor.stands_for(conn, "route", {r for _, r in rows})
    gone = [i for i, r in rows if route in now.get(r, [])]
    if not gone:
        raise NotFound(f"that route is not added to {area}")
    code = conn.execute("SELECT code FROM core.route WHERE id = %s::uuid", (route,)).fetchone()
    conn.execute("DELETE FROM ops.area_route WHERE id = ANY(%s::uuid[])", (gone,))
    _event(conn, author, area, "area.route_taken_out", {"route": code[0] if code else route})
    return {"area": area, "added": listing(conn).get(area, [])}
