"""How what operations keeps stays with what it is about, while the routes are mended (ADR-004).

Operations never changes the routes. What it keeps says which junction,
stretch, place or route it is about by id, as the id was when it was said.
The routes go on being mended underneath: junctions are joined, a stretch is
divided, a route is taken into another, a code is changed. So what is kept is
never read by its ids as they stand. It is asked what stands for them now:

  one thing   it is still itself, or one thing came instead: follow it
  several     it was divided: the kind of thing that asks says which part it means
  nothing     it is gone, and nothing came instead

What cannot be followed is never dropped and never guessed at. It is adrift:
shown to a person with what it said, who moves it to where it belongs now or
says it can be forgotten.
"""
from __future__ import annotations

import uuid

from stiflyt2.domain import identity
from stiflyt2.domain.places import NotFound


def stands_for(conn, kind: str, ids) -> dict[str, list[str]]:
    """For each id of a junction ('node'), stretch ('edge'), place or route: the ids that stand for it now.

    Itself while it is itself; what came instead where it was joined, divided
    or taken into another; nothing when it is gone, or was never there. One
    question to the database for all that are still themselves, which is
    nearly always all of them.
    """
    wanted = set()
    for some_id in ids:
        try:
            wanted.add(str(uuid.UUID(str(some_id))))
        except ValueError:
            continue
    if not wanted:
        return {}
    live = {i for (i,) in conn.execute(
        f"SELECT id::text FROM core.{kind} WHERE id = ANY(%s::uuid[]) AND status = %s",
        (list(wanted), identity.LIVE[kind]))}
    now = {i: [i] for i in live}
    for some_id in wanted - live:
        try:
            now[some_id] = identity.current(conn, kind, some_id)["now"]
        except NotFound:
            now[some_id] = []
    return now


def one(now: list[str] | None) -> str | None:
    """The one thing that stands for an id now, when there is exactly one."""
    return now[0] if now and len(now) == 1 else None


def code_now(conn, code: str) -> str | None:
    """The code a route in use has now, given one it has or once had. None when no route answers to it."""
    found = conn.execute("""SELECT code FROM core.route WHERE status = 'active' AND lower(code) = lower(%s)
                            ORDER BY discontinued_since IS NOT NULL LIMIT 1""", (code,)).fetchone()
    if found:
        return found[0]
    was = conn.execute("""SELECT subject_id::text FROM core.alias
                          WHERE subject_type = 'route' AND kind = 'code' AND lower(value) = lower(%s)
                          ORDER BY until DESC, id DESC LIMIT 1""", (code,)).fetchone()
    route = one(stands_for(conn, "route", [was[0]]).get(was[0])) if was else None
    if not route:
        return None
    return conn.execute("SELECT code FROM core.route WHERE id = %s", (route,)).fetchone()[0]


def place_of(conn, nodes) -> dict[str, str]:
    """The place each of these junctions belongs to now, for those that belong to one."""
    return dict(conn.execute("""
        SELECT n.id::text, p.id::text FROM core.node n
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%s::uuid[])""", (list(nodes),)).fetchall())


def ends_of(conn, edges) -> dict[str, tuple[str, str]]:
    """The two junctions each of these stretches runs between."""
    return {e: (a, b) for e, a, b in conn.execute("""
        SELECT e.id::text, r.a_node::text, r.b_node::text
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = ANY(%s::uuid[])""", (list(edges),))}
