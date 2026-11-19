"""Owned routes and crossings (ADR-003).

Making a route ours turns each Turrutebasen segment it uses into an owned edge,
shared by every route that uses the segment, with nodes at the ends. A crossing
(water, glacier, other) is an owned edge too. A route is an ordered list of
legs over those edges, with a code and a name of its own.

Functions take an open connection (role stiflyt2_api), work inside the
author's changeset and record events. The caller commits.
"""
from __future__ import annotations

import bisect
import heapq
import itertools
import json
import math

from psycopg.rows import dict_row

from stiflyt2 import height
from stiflyt2.domain import identity
from stiflyt2.domain.places import (
    AUTOMATIC,
    Conflict,
    Invalid,
    NotFound,
    close_changeset,
    current_changeset,
    suggest,
)


class NoWay(Invalid):
    """No stretch leads there. Unlike other refusals, another kind of way may still be found."""


NODE_M = 1      # segment ends this close are one node (the tolerance v1 arrived at)
ACCESS_M = 5    # an end this close to a place's access point arrives at it
CROSSINGS = ("water", "glacier", "other", "connector")
KINDS = ("trail", "tractor_road", "road", "glacier", "water", "off_trail", "connector", "other")
CORRIDOR_M = 250   # mapped paths this far from a stretch are considered when snapping it
REACH_M = 60       # each end of the stretch must have a mapped path this close
# What is known about a crossing from its kind alone: (marked, signed).
# A glacier is walked and signposted but never marked.
_FACTS = {"water": (False, None), "glacier": (False, True), "other": (None, None),
          "connector": (None, None)}


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _event(conn, author: str, type_: str, subject_id: str, payload: dict,
           subject: str = "route") -> None:
    conn.execute(
        """INSERT INTO history.event (changeset_id, author, type, subject_type, subject_id, payload)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (current_changeset(conn, author), author, type_, subject, subject_id, json.dumps(payload)))


def _route(conn, route_id: str, version: int | None = None) -> dict:
    found = _rows(conn, """SELECT id, code, code_provisional, name, status, version, reviewed,
                                  maintainers, grading, special_type, discontinued_since, discontinued_why,
                                  description, unpublished_why, utno_id
                           FROM core.route WHERE id = %(id)s FOR UPDATE""", {"id": route_id})
    if not found or found[0]["status"] != "active":
        raise NotFound("no such route")
    if version is not None and found[0]["version"] != version:
        raise Conflict("the route has been changed since you loaded it")
    return found[0]


def _touch(conn, route_id: str, author: str) -> None:
    """Count the change, so that whoever has an older copy of the route is told."""
    conn.execute("UPDATE core.route SET version = version + 1, updated_at = now() WHERE id = %s",
                 (route_id,))


def _node_at(conn, geom) -> str:
    """The node at a point: a place's access point within ACCESS_M, any node within NODE_M, or a new one."""
    found = conn.execute(
        """SELECT id FROM core.node
           WHERE status = 'active' AND ST_DWithin(geom, %(g)s::geometry, %(far)s)
             AND (ST_DWithin(geom, %(g)s::geometry, %(near)s) OR place_id IS NOT NULL)
           ORDER BY geom <-> %(g)s::geometry LIMIT 1""",
        {"g": geom, "far": ACCESS_M, "near": NODE_M}).fetchone()
    if found:
        return str(found[0])
    return str(conn.execute("INSERT INTO core.node (geom) VALUES (%s) RETURNING id",
                            (geom,)).fetchone()[0])


def _new_edge(conn, author: str, geom_sql: str, params: dict, a: str, b: str, kind: str,
              origin: str, marked: bool | None, signed: bool | None,
              maintainers: list[str] | None = None) -> str:
    edge = conn.execute("INSERT INTO core.edge DEFAULT VALUES RETURNING id").fetchone()[0]
    revision = conn.execute(
        f"""INSERT INTO core.edge_revision (edge_id, rev_no, geom, a_node, b_node, kind, origin,
                                            marked, signed, maintainers, changeset_id, author)
            VALUES (%(edge)s, 1, {geom_sql}, %(a)s, %(b)s, %(kind)s, %(origin)s, %(marked)s,
                    %(signed)s, %(maintainers)s, %(changeset)s, %(author)s) RETURNING id""",
        {**params, "edge": edge, "a": a, "b": b, "kind": kind, "origin": origin, "marked": marked,
         "signed": signed, "maintainers": maintainers or [],
         "changeset": current_changeset(conn, author), "author": author},
    ).fetchone()[0]
    conn.execute("UPDATE core.edge SET current_revision_id = %s WHERE id = %s", (revision, edge))
    return str(edge)


def _edges_of(conn, candidate_id: str, author: str) -> list[str]:
    """The owned edges for a candidate's segments, made now where they do not exist yet."""
    return _segment_edges(conn, author, """t.id IN (SELECT segment_id FROM source_index.route_segment
                                                    WHERE route_id = %(id)s)""", candidate_id)


def _segment_edges(conn, author: str, which: str, value: str) -> list[str]:
    """The owned edges for some Turrutebasen segments, made now where they do not exist yet."""
    segments = _rows(conn, f"""
        SELECT t.id, t.version_id, t.geom, t.marked, t.attrs->>'skilting' AS skilting,
               t.attrs->>'rutefolger' AS follows, t.maintainers, ST_Length(t.geom) AS metres,
               ST_StartPoint(t.geom) AS a, ST_EndPoint(t.geom) AS b
        FROM source_index.trail_segment t
        WHERE {which} AND GeometryType(t.geom) = 'LINESTRING' ORDER BY t.id""", {"id": value})
    snapshot = conn.execute("""SELECT snapshot_id FROM import_control.active_snapshot
                               WHERE source_key = 'turrutebasen' AND scope = 'NO'""").fetchone()
    edges = []
    for s in segments:
        # A segment that has been divided is several stretches of yours: all of them.
        known = conn.execute(
            """SELECT DISTINCT e.id FROM core.source_reference sr JOIN core.edge e ON e.id = sr.subject_id
               WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                 AND sr.ref_type = 'adopted_from' AND sr.external_id = %s AND e.status = 'active'""",
            (s["id"],)).fetchall()
        if known:
            edges += [str(e) for (e,) in known]
            continue
        signed = {"JA": True, "NEI": False}.get(s["skilting"])
        # Turrutebasen says what a segment follows. VV is water: a boat, not a walk.
        kind = "water" if s["follows"] == "VV" else "trail"
        a, b = _node_at(conn, s["a"]), _node_at(conn, s["b"])
        if a == b and s["metres"] < 2 * ACCESS_M:
            continue    # a few metres inside one arrival point of a place: nothing to walk
        edge = _new_edge(conn, author, "%(geom)s", {"geom": s["geom"]}, a, b, kind, "adopted",
                         s["marked"], signed, s["maintainers"])
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, snapshot_id, ref_type, detail)
               VALUES ('edge', %s, 'turrutebasen', %s, %s, 'adopted_from', %s)""",
            (edge, s["id"], snapshot[0] if snapshot else None,
             json.dumps({"feature_version": s["version_id"]})))
        edges.append(edge)
    return edges


def _graph(conn, route_id: str) -> dict:
    """The route's legs as a graph: its connected pieces, in walking order, and its loose ends."""
    legs = _rows(conn, """
        SELECT l.edge_id::text AS edge, r.a_node::text AS a, r.b_node::text AS b
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s ORDER BY l.seq""", {"id": route_id})
    at = {str(n): (x, y) for n, x, y in conn.execute(
        """SELECT n.id, ST_X(n.geom), ST_Y(n.geom) FROM core.node n
           WHERE n.id IN (SELECT r.a_node FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                          JOIN core.edge_revision r ON r.id = e.current_revision_id
                          WHERE l.route_id = %(id)s
                          UNION
                          SELECT r.b_node FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                          JOIN core.edge_revision r ON r.id = e.current_revision_id
                          WHERE l.route_id = %(id)s)""", {"id": route_id})}
    # A leg that starts and ends at the same node (a few metres inside a place) is
    # walked but does not make that node a junction.
    touching: dict[str, list[dict]] = {}
    for leg in legs:
        touching.setdefault(leg["a"], []).append(leg)
        if leg["b"] != leg["a"]:
            touching.setdefault(leg["b"], []).append(leg)
    seen: set[str] = set()
    pieces = []
    for start_leg in legs:
        if start_leg["edge"] in seen:
            continue
        # The nodes of this piece.
        nodes, frontier = {start_leg["a"]}, [start_leg["a"]]
        while frontier:
            for leg in touching[frontier.pop()]:
                for n in (leg["a"], leg["b"]):
                    if n not in nodes:
                        nodes.add(n)
                        frontier.append(n)
        ends = sorted((n for n in nodes if sum(g["a"] != g["b"] for g in touching[n]) == 1),
                      key=lambda n: at[n])
        # Walk it from its western end, following unused legs. A round trip has no end: it is
        # walked from where its first leg sets out, the way it was laid, or from a place on it.
        order, here = [], ends[0] if ends else _sets_out(conn, [g for g in legs if g["a"] in nodes], nodes)
        start, stack = here, [here]
        while stack:
            here = stack[-1]
            leg = next((g for g in touching[here] if g["edge"] not in seen), None)
            if leg is None:
                stack.pop()
                continue
            seen.add(leg["edge"])
            order.append((leg["edge"], leg["b"] == here))
            stack.append(leg["a"] if leg["b"] == here else leg["b"])
        pieces.append({"order": order, "ends": ends, "nodes": nodes, "start": start,
                       "west": min(at[n] for n in nodes)})
    pieces = _walked_through(pieces, at)
    ends = [n for p in pieces for n in p["ends"]]
    # A line through N junctions has N - 1 stretches, a round trip N. One more, and somewhere two
    # stretches run between junctions that the route already joins: it is walked double there.
    # Also at an end of the route, where the doubling leaves it with one end only.
    walked = sum(1 for g in legs if g["a"] != g["b"])
    if len(pieces) > 1:
        shape = "pieces"
    elif not walked:
        shape = "loop" if legs else "empty"
    elif walked == len(pieces[0]["nodes"]) - 1:
        shape = "line" if len(ends) == 2 else "branched"
    elif not ends and walked == len(pieces[0]["nodes"]):
        shape = "loop"
    else:
        shape = "doubled"
    return {"pieces": pieces, "ends": ends, "shape": shape, "at": at,
            "starts": pieces[0]["start"] if shape == "loop" else None}


def _walked_through(pieces: list[dict], at: dict) -> list[dict]:
    """The pieces of a route in the order, and each the way round, that they are walked one after the other.

    A piece is found walked from its western end. For a route in several
    pieces that is not enough: Juvvasshytta – Galdhøpiggen – Spiterstulen is
    two lines with the glacier between them, both with their western end at
    the ice, and walked each from the west the route would jump from
    Spiterstulen back up to the glacier. So the pieces are put in the order,
    and each plain line turned the way, that leaves the least to cross
    between where one stops and the next begins; of two ways equally good
    (a walk and the same walk backwards), the one that sets out further west.
    """
    if len(pieces) < 2:
        return pieces

    def plain(p: dict) -> bool:            # one line between two ends: it can be walked either way
        return len(p["ends"]) == 2 and len(p["order"]) == len(p["nodes"]) - 1

    def turned(p: dict) -> dict:
        return {**p, "order": [(edge, not back) for edge, back in reversed(p["order"])],
                "ends": p["ends"][::-1], "start": p["ends"][1]}

    def stops(p: dict) -> str:             # where walking it ends
        return p["ends"][1] if plain(p) else p["start"]

    def left_to_cross(walk: list[dict]) -> float:
        return sum(math.dist(at[stops(a)], at[b["start"]]) for a, b in itertools.pairwise(walk))

    pieces = sorted(pieces, key=lambda p: p["west"])
    if len(pieces) > 4:                    # too many to try every way: each next piece the nearest
        walk, rest = [pieces[0]], pieces[1:]
        while rest:
            here = at[stops(walk[-1])]
            best = min(((math.dist(here, at[c["start"]]), i, c) for i, p in enumerate(rest)
                        for c in ([p, turned(p)] if plain(p) else [p])), key=lambda t: t[:2])
            walk.append(best[2])
            rest.pop(best[1])
        return walk
    ways = []
    for order in itertools.permutations(pieces):
        for turns in itertools.product(*[([False, True] if plain(p) else [False]) for p in order]):
            walk = [turned(p) if turn else p for p, turn in zip(order, turns, strict=True)]
            ways.append((round(left_to_cross(walk), 1), at[walk[0]["start"]], len(ways), walk))
    return min(ways, key=lambda w: w[:3])[3]


def _sets_out(conn, legs: list[dict], nodes: set[str]) -> str:
    """Where a round trip sets out from: the junction its first leg leaves, unless a place lies elsewhere on it."""
    first, second = legs[0], legs[1] if len(legs) > 1 else None
    here = first["a"] if second is None or first["a"] not in (second["a"], second["b"]) else first["b"]
    placed = [n for (n,) in conn.execute("""
        SELECT n.id::text FROM core.node n JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%s::uuid[]) ORDER BY (p.primary_node_id = n.id) DESC NULLS LAST, n.id""", (list(nodes),))]
    return here if here in placed or not placed else placed[0]


def _reorder(conn, route_id: str) -> None:
    """Store the legs in walking order, piece by piece."""
    graph = _graph(conn, route_id)
    # What a leg says for itself (who maintains it, where that is not the route's word) is kept.
    said = dict(conn.execute("DELETE FROM core.route_leg WHERE route_id = %s "
                             "RETURNING edge_id::text, maintainers", (route_id,)).fetchall())
    seq = 0
    for piece in graph["pieces"]:
        for edge, reversed_ in piece["order"]:
            seq += 1
            conn.execute(
                """INSERT INTO core.route_leg (route_id, seq, edge_id, reversed, maintainers)
                   VALUES (%s, %s, %s, %s, %s)""", (route_id, seq, edge, reversed_, said.get(edge)))


def _add_legs(conn, route_id: str, edges: list[str]) -> int:
    have = {str(e) for (e,) in conn.execute(
        "SELECT edge_id FROM core.route_leg WHERE route_id = %s", (route_id,))}
    last = conn.execute("SELECT coalesce(max(seq), 0) FROM core.route_leg WHERE route_id = %s",
                        (route_id,)).fetchone()[0]
    new = [e for e in edges if e not in have]
    for i, edge in enumerate(new, 1):
        conn.execute("INSERT INTO core.route_leg (route_id, seq, edge_id) VALUES (%s, %s, %s)",
                     (route_id, last + i, edge))
    _reorder(conn, route_id)
    return len(new)


def _candidate(conn, candidate_id: str) -> dict:
    found = _rows(conn, """SELECT id, code, label, name, maintainer, grading, special_type
                           FROM source_index.route WHERE id = %(id)s""", {"id": candidate_id})
    if not found:
        raise NotFound("no such route candidate")
    return found[0]


def _refer(conn, route_id: str, candidate: dict) -> None:
    conn.execute(
        """INSERT INTO core.source_reference
               (subject_type, subject_id, source_key, external_id, ref_type, detail)
           VALUES ('route', %s, 'turrutebasen', %s, 'adopted_from', %s)""",
        (route_id, candidate["code"], json.dumps({
            "candidate_id": candidate["id"], "label": candidate["label"],
            "name": candidate["name"], "maintainer": candidate["maintainer"]})))


def _inherit(conn, route_id: str, candidate_id: str, author: str, tell: bool = False) -> None:
    """Take over who maintains the route from Turrutebasen, for the legs that came from a candidate.

    The route gets what holds for most of its length, if it says nothing yet.
    A leg gets a value of its own only where Turrutebasen says otherwise for it.
    """
    legs = _rows(conn, """
        SELECT l.edge_id::text AS edge, rs.maintainers, t.length_m
        FROM source_index.route_segment rs
        JOIN source_index.trail_segment t ON t.id = rs.segment_id
        JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
             AND sr.ref_type = 'adopted_from' AND sr.external_id = rs.segment_id
        JOIN core.route_leg l ON l.edge_id = sr.subject_id AND l.route_id = %(route)s
        WHERE rs.route_id = %(c)s""", {"route": route_id, "c": candidate_id})
    route = _rows(conn, "SELECT maintainers, grading, special_type FROM core.route WHERE id = %(id)s",
                  {"id": route_id})[0]
    whole = sorted(route["maintainers"])
    if not whole and legs:
        length: dict[tuple, float] = {}
        for leg in legs:
            key = tuple(sorted(leg["maintainers"]))
            length[key] = length.get(key, 0) + leg["length_m"]
        whole = list(max(length, key=lambda k: (length[k], k)))
        conn.execute("UPDATE core.route SET maintainers = %s WHERE id = %s", (whole, route_id))
        if whole and tell:
            _event(conn, author, "route.maintainers_changed", route_id,
                   {"from": [], "to": whole, "as": "Turrutebasen has it"})
    for leg in legs:
        if sorted(leg["maintainers"]) != whole:
            conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s AND edge_id = %s",
                         (sorted(leg["maintainers"]), route_id, leg["edge"]))
    candidate = _candidate(conn, candidate_id)
    conn.execute("""UPDATE core.route SET grading = coalesce(grading, %s),
                           special_type = coalesce(special_type, %s) WHERE id = %s""",
                 (candidate["grading"], candidate["special_type"], route_id))


def fill_metadata(conn) -> int:
    """Give every route that says nothing about who maintains it what Turrutebasen has. Returns how many."""
    filled = 0
    for route_id, candidate in conn.execute("""
            SELECT r.id::text, coalesce(c.id, l.id)
            FROM core.route r
            JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = r.id
                 AND sr.source_key = 'turrutebasen'
            LEFT JOIN source_index.route c ON c.id = sr.detail->>'candidate_id'
            LEFT JOIN source_index.route l ON c.id IS NULL AND l.label = sr.detail->>'label'
            WHERE r.status = 'active' AND r.maintainers = '{}' AND coalesce(c.id, l.id) IS NOT NULL
            ORDER BY r.code""").fetchall():
        _inherit(conn, route_id, candidate, AUTOMATIC, tell=True)
        filled += 1
    close_changeset(conn, AUTOMATIC)
    return filled


def owner_of(conn, candidate_id: str) -> str | None:
    """The active owned route made from this candidate, if there is one.

    It is the route that was made from the candidate's Turrutebasen code and
    holds most of its segments: a route stays the owner when a person takes
    stretches out of it. Judged that way, not by the candidate's id, it still
    holds after the index has been rebuilt with other groupings. A longer
    route that merely runs over the same stretches does not count.
    """
    row = conn.execute(
        """SELECT l.route_id
           FROM source_index.route_segment rs
           JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                                        AND sr.external_id = rs.segment_id
           JOIN core.route_leg l ON l.edge_id = sr.subject_id
           JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
           WHERE rs.route_id = %(c)s AND EXISTS (
               SELECT 1 FROM core.source_reference made
               WHERE made.subject_type = 'route' AND made.subject_id = r.id
                 AND made.external_id = (SELECT code FROM source_index.route WHERE id = %(c)s))
           GROUP BY l.route_id
           HAVING count(DISTINCT rs.segment_id) * 2 > (SELECT count(*) FROM source_index.route_segment
                                                       WHERE route_id = %(c)s)
           ORDER BY count(DISTINCT rs.segment_id) DESC
           LIMIT 1""", {"c": candidate_id}).fetchone()
    return str(row[0]) if row else None


def make(conn, candidate_id: str, author: str, code: str | None = None) -> str:
    """Make a route candidate an owned route.

    The code is ours to choose. Left out, it is the candidate's label, which is
    marked as temporary when it is only a part label such as jot91/1.
    """
    candidate = _candidate(conn, candidate_id)
    if owner_of(conn, candidate_id):
        raise Conflict("this candidate is already part of one of your routes")
    code = (code or candidate["label"]).strip()
    if not code:
        raise Invalid("a route needs a code")
    if conn.execute("SELECT 1 FROM core.route WHERE lower(code) = lower(%s) AND status = 'active'",
                    (code,)).fetchone():
        raise Conflict(f"the code {code} is already used by another of your routes")
    route_id = str(conn.execute(
        "INSERT INTO core.route (code, code_provisional, reviewed) VALUES (%s, %s, %s) RETURNING id",
        (code, "/" in code, False)).fetchone()[0])
    _refer(conn, route_id, candidate)
    legs = _add_legs(conn, route_id, _edges_of(conn, candidate_id, author))
    _event(conn, author, "route.made", route_id,
           {"candidate": candidate["label"], "code": code, "legs": legs})
    _inherit(conn, route_id, candidate_id, author)
    return route_id


def review(conn, route_id: str, version: int, author: str, done: bool = True) -> None:
    """A person says the route is done: looked at, and right as it is. Or not done after all.

    Nothing else sets this: a route that has been changed is not thereby finished.
    """
    route = _route(conn, route_id, version)
    if done and not conn.execute("SELECT 1 FROM core.route_leg WHERE route_id = %s LIMIT 1", (route_id,)).fetchone():
        raise Invalid(f"{route['code']} has no stretches: it is no route, and is withdrawn rather than done")
    conn.execute("UPDATE core.route SET reviewed = %s WHERE id = %s", (done, route_id))
    _event(conn, author, "route.reviewed" if done else "route.reopened", route_id, {})
    _touch(conn, route_id, author)


def discontinue(conn, route_id: str, version: int, author: str, why: str, since=None) -> None:
    """Say that the route is discontinued: given up in the terrain, and kept here.

    It stays a route of yours, with its line and its history. From now on it
    is left out of the network checks, the planner and what is handed on, and
    where it was made from a Turrutebasen route, Kartverket is asked to take
    its number off. Said again, the reason and the date are changed.
    """
    route = _route(conn, route_id, version)
    why = (why or "").strip()
    if not why:
        raise Invalid("say why the route is discontinued")
    since = conn.execute("SELECT coalesce(%s::date, current_date)", (since,)).fetchone()[0]
    conn.execute("UPDATE core.route SET discontinued_since = %s, discontinued_why = %s WHERE id = %s",
                 (since, why, route_id))
    _event(conn, author, "route.discontinued", route_id,
           {"why": why, "since": since.isoformat(),
            **({"was": {"why": route["discontinued_why"], "since": route["discontinued_since"].isoformat()}}
               if route["discontinued_since"] else {})})
    _touch(conn, route_id, author)


def take_up_again(conn, route_id: str, version: int, author: str) -> None:
    """The route is in use after all: it is no longer discontinued."""
    route = _route(conn, route_id, version)
    if not route["discontinued_since"]:
        raise Conflict("the route is not discontinued")
    conn.execute("UPDATE core.route SET discontinued_since = NULL, discontinued_why = NULL WHERE id = %s",
                 (route_id,))
    _event(conn, author, "route.taken_up_again", route_id,
           {"was": {"why": route["discontinued_why"], "since": route["discontinued_since"].isoformat()}})
    _touch(conn, route_id, author)


def unpublish(conn, route_id: str, version: int, author: str, why: str) -> None:
    """Say that the route is not to be published: kept here, and not handed on.

    It stays a route of yours like any other: in the list, on the map, in the
    network checks. It is left out of what is handed to UT.no. Kartverket is
    asked for nothing new on its account, and where Turrutebasen has the route,
    to take its number off. Said again, the reason is changed.
    """
    route = _route(conn, route_id, version)
    why = (why or "").strip()
    if not why:
        raise Invalid("say why the route is not published")
    if why == route["unpublished_why"]:
        raise Conflict("that is what is said of it already")
    conn.execute("UPDATE core.route SET unpublished_why = %s WHERE id = %s", (why, route_id))
    _event(conn, author, "route.unpublished", route_id,
           {"why": why, **({"was": route["unpublished_why"]} if route["unpublished_why"] else {})})
    _touch(conn, route_id, author)


def publish(conn, route_id: str, version: int, author: str) -> None:
    """The route is to be published after all: handed on like the others."""
    route = _route(conn, route_id, version)
    if not route["unpublished_why"]:
        raise Conflict("the route is published already")
    conn.execute("UPDATE core.route SET unpublished_why = NULL WHERE id = %s", (route_id,))
    _event(conn, author, "route.published", route_id, {"was": route["unpublished_why"]})
    _touch(conn, route_id, author)


def withdrawn_by_a_person(conn) -> set[str]:
    """Labels of candidates whose route a person has withdrawn: not to be made again in bulk."""
    return {label for (label,) in conn.execute("""
        SELECT DISTINCT sr.detail->>'label' FROM history.event e
        JOIN core.source_reference sr ON sr.subject_type = 'route' AND sr.subject_id = e.subject_id
        WHERE e.type = 'route.retired' AND e.author <> %s""", (AUTOMATIC,))}


def taken_out_by_a_person(conn) -> dict[str, str]:
    """Labels of candidates that a route of yours was made from and still says so: label -> that route's code.

    Where such a route no longer holds the candidate's segments, a person took
    them out of it: a stray bit with the route's number on it, away from the
    route. The candidate is then not a route waiting to be made yours.
    """
    return dict(conn.execute("""
        SELECT sr.detail->>'label', r.code FROM core.source_reference sr
        JOIN core.route r ON r.id = sr.subject_id AND r.status = 'active'
        WHERE sr.subject_type = 'route' AND sr.source_key = 'turrutebasen' AND sr.detail->>'label' IS NOT NULL"""))


def make_default(conn, area: str) -> dict:
    """Make every clean route candidate of an area yours: one connected line with two ends.

    They are marked as not reviewed and keep the candidate's label as their
    code. Candidates in pieces or with extra ends are left for a person, as
    are those whose route a person has withdrawn, or has taken out of the
    route that was made from them.
    """
    candidates = _rows(conn, """
        SELECT r.id, r.label FROM source_index.route r
        JOIN source_index.route_shape s ON s.route_id = r.id
        WHERE r.area_prefix = lower(%(a)s) AND s.shape = 'line'
        ORDER BY length(r.code), r.label""", {"a": area})
    withdrawn = withdrawn_by_a_person(conn) | set(taken_out_by_a_person(conn))
    taken = {code for (code,) in conn.execute(
        "SELECT lower(code) FROM core.route WHERE status = 'active'")}
    done = {"made": 0, "kept": 0, "left withdrawn": 0, "code taken": 0}
    for c in candidates:
        if owner_of(conn, c["id"]):
            done["kept"] += 1
        elif c["label"] in withdrawn:
            done["left withdrawn"] += 1
        elif c["label"].lower() in taken:
            done["code taken"] += 1
        else:
            make(conn, c["id"], AUTOMATIC)
            taken.add(c["label"].lower())
            done["made"] += 1
    close_changeset(conn, AUTOMATIC)
    return done


def _revise(conn, edge_id: str, author: str, origin: str | None = None, wkt: str | None = None,
            nodes: tuple[str, str] | None = None, **facts) -> dict:
    """A new revision of an edge: other geometry, other end nodes, or other facts about the stretch."""
    found = _rows(conn, """
        SELECT r.rev_no, r.kind, r.marked, r.signed, r.maintainers, r.origin, r.a_node, r.b_node,
               round(ST_Length(ST_Transform(r.geom, 4326)::geography))::int AS length_m
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = %(id)s AND e.status = 'active' FOR UPDATE OF e""", {"id": edge_id})
    if not found:
        raise NotFound("no such stretch")
    was = found[0]
    now = {**{k: was[k] for k in ("kind", "marked", "signed", "maintainers")}, **facts}
    revision = conn.execute(
        """INSERT INTO core.edge_revision (edge_id, rev_no, geom, a_node, b_node, kind, origin,
                                           marked, signed, maintainers, changeset_id, author)
           SELECT r.edge_id, (SELECT max(x.rev_no) + 1 FROM core.edge_revision x WHERE x.edge_id = r.edge_id),
                  coalesce(ST_SetSRID(ST_GeomFromText(%(wkt)s), 25833), r.geom),
                  coalesce(%(a)s::uuid, r.a_node), coalesce(%(b)s::uuid, r.b_node),
                  %(kind)s, %(origin)s, %(marked)s, %(signed)s, %(maintainers)s, %(cs)s, %(author)s
           FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
           WHERE e.id = %(id)s RETURNING id""",
        {"id": edge_id, "wkt": wkt, "origin": origin or was["origin"], "author": author,
         "a": nodes[0] if nodes else None, "b": nodes[1] if nodes else None,
         "cs": current_changeset(conn, author), **now}).fetchone()[0]
    conn.execute("""UPDATE core.edge SET current_revision_id = %s, version = version + 1
                    WHERE id = %s""", (revision, edge_id))
    return was


def as_seen(conn, edge_id: str, rev_no: int | None) -> None:
    """Refuse a change to a stretch that somebody has changed since the one asking last saw it.

    A stretch is shared: two people, or a person and an assistant, may have it
    on their maps at once. Whoever comes second is told, and looks again.
    """
    if rev_no is None:
        return
    now = conn.execute("""SELECT r.rev_no, r.author FROM core.edge e
                          JOIN core.edge_revision r ON r.id = e.current_revision_id
                          WHERE e.id = %s AND e.status = 'active' FOR UPDATE OF e""", (edge_id,)).fetchone()
    if now and now[0] != rev_no:
        raise Conflict(f"{now[1]} has changed that stretch since your map was drawn: look at it as it is now, "
                       "and do it again if it is still wanted")


def latest(conn) -> dict | None:
    """The latest thing anybody changed: for a page to notice that it shows what is no longer so."""
    found = _rows(conn, """SELECT seq, at, author, coalesce(through, 'viewer') AS through, type
                           FROM history.event ORDER BY seq DESC LIMIT 1""", {})
    return found[0] if found else None


def set_stretch(conn, edge_id: str, author: str, **facts) -> None:
    """Change what a stretch is, and whether it is marked and signed.

    Given only a kind: a glacier is signed and not marked, water is not marked.
    The stretch is shared, so the change shows on every route that uses it.
    """
    facts = {k: v for k, v in facts.items() if k in ("kind", "marked", "signed", "maintainers")}
    if facts.get("kind") is not None and facts["kind"] not in KINDS:
        raise Invalid(f"a stretch is one of {', '.join(KINDS)}")
    if facts.get("kind") == "glacier":
        facts = {"marked": False, "signed": True, **facts}
    if facts.get("kind") == "water":
        facts = {"marked": False, **facts}
    was = _revise(conn, edge_id, author, **facts)
    changed = {k: [was[k], v] for k, v in facts.items() if was[k] != v}
    _event(conn, author, "edge.changed", edge_id, changed, subject="edge")


def _order_key(node) -> str:
    """Graph nodes are names and coordinate pairs; the queue needs them comparable."""
    return str(node)


def mapped_lines(conn) -> str:
    """SQL for every mapped line a route can be laid onto: paths, and roads where they are loaded."""
    sql = "SELECT 'fkb_wfs' AS source, id, geom FROM source_index.path"
    if conn.execute("SELECT to_regclass('source_index.road') IS NOT NULL").fetchone()[0]:
        sql += " UNION ALL SELECT 'vegnett', id, geom FROM source_index.road"
    return f"({sql})"


class _Paths:
    """Mapped paths and roads as a graph that a way can be found over.

    Lines join where their ends meet. An end lying on the middle of another
    line joins it there too: a path coming out on a road is cut into the road.
    """

    def __init__(self, lines):
        """lines: (ref, coords, cuts). A cut is (fraction along, x, y): another line's end on this one."""
        self.pieces = []            # (ref, coords, key at its start, key at its end)
        self.graph: dict = {}
        self.stops = 0
        self._grid = None
        for ref, coords, cuts in lines:
            for u, v, part in self._split(coords, cuts):
                self.pieces.append((ref, part, u, v))
                self._link(u, v, part, ref)

    @classmethod
    def near(cls, conn, geom, margin: float = CORRIDOR_M) -> _Paths:
        """The mapped lines within the margin of a geometry."""
        rows = conn.execute(f"""
            WITH net AS MATERIALIZED (
                SELECT m.source, m.id, m.geom FROM {mapped_lines(conn)} m
                WHERE ST_DWithin(m.geom, %s::geometry, %s) AND GeometryType(m.geom) = 'LINESTRING')
            SELECT a.source, a.id, ST_AsGeoJSON(a.geom)::json,
                   (SELECT json_agg(json_build_array(ST_LineLocatePoint(a.geom, e.pt),
                                                     ST_X(e.pt), ST_Y(e.pt)))
                    FROM net b, LATERAL (VALUES (ST_StartPoint(b.geom)), (ST_EndPoint(b.geom))) e(pt)
                    WHERE (b.source, b.id) <> (a.source, a.id) AND ST_DWithin(a.geom, e.pt, %s))
            FROM net a""", (geom, margin, NODE_M)).fetchall()
        return cls([((source, pid), [tuple(c[:2]) for c in g["coordinates"]], cuts or [])
                    for source, pid, g, cuts in rows])

    @staticmethod
    def along(coords) -> float:
        return sum(math.dist(p, q) for p, q in itertools.pairwise(coords))

    @staticmethod
    def _key(point):
        return (round(point[0], 1), round(point[1], 1))

    @classmethod
    def _split(cls, coords, cuts) -> list:
        """The line in pieces, cut where other lines end on it, each piece with the keys of its ends."""
        total = cls.along(coords)
        inner = sorted((f * total, (x, y)) for f, x, y in cuts if 1e-9 < f < 1 - 1e-9)
        pieces, part, start, walked, k = [], [coords[0]], cls._key(coords[0]), 0.0, 0
        for p, q in itertools.pairwise(coords):
            step = math.dist(p, q)
            while k < len(inner) and step > 0 and inner[k][0] <= walked + step:
                t = (inner[k][0] - walked) / step
                at, node = (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])), cls._key(inner[k][1])
                pieces.append((start, node, [*part, at]))
                part, start, k = [at], node, k + 1
            part.append(q)
            walked += step
        pieces.append((start, cls._key(coords[-1]), part))
        for f, x, y in cuts:        # ends that nearly meet: a step between them
            if not 1e-9 < f < 1 - 1e-9:
                end = coords[0] if f <= 1e-9 else coords[-1]
                if cls._key(end) != cls._key((x, y)):
                    pieces.append((cls._key(end), cls._key((x, y)), [end, (x, y)]))
        return pieces

    def _link(self, u, v, coords, ref) -> None:
        self.graph.setdefault(u, []).append((v, self.along(coords), coords, ref))
        self.graph.setdefault(v, []).append((u, self.along(coords), coords[::-1], ref))

    CELL_M = 50

    def _cells(self) -> dict:
        """Every part of every piece, by the squares of the ground it passes through."""
        if self._grid is None:
            self._grid = {}
            for n, (_, coords, _, _) in enumerate(self.pieces):
                for i, (p, q) in enumerate(itertools.pairwise(coords)):
                    for cx in range(int(min(p[0], q[0]) // self.CELL_M), int(max(p[0], q[0]) // self.CELL_M) + 1):
                        for cy in range(int(min(p[1], q[1]) // self.CELL_M), int(max(p[1], q[1]) // self.CELL_M) + 1):
                            self._grid.setdefault((cx, cy), []).append((n, i))
        return self._grid

    def _nearest(self, at, parts) -> tuple | None:
        best = None
        for n, i in parts:
            p, q = self.pieces[n][1][i], self.pieces[n][1][i + 1]
            dx, dy = q[0] - p[0], q[1] - p[1]
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, (
                (at[0] - p[0]) * dx + (at[1] - p[1]) * dy) / (dx * dx + dy * dy)))
            foot = (p[0] + t * dx, p[1] + t * dy)
            d = math.dist(at, foot)
            if best is None or (d, n, i) < best[:3]:
                best = (d, n, i, foot)
        return best

    def locate(self, at, reach: float) -> dict | None:
        """The nearest point on any mapped line within reach of a point, not yet tied into the network."""
        grid, span = self._cells(), range(-int(reach // self.CELL_M) - 1, int(reach // self.CELL_M) + 2)
        cx, cy = int(at[0] // self.CELL_M), int(at[1] // self.CELL_M)
        best = self._nearest(at, {part for dx in span for dy in span for part in grid.get((cx + dx, cy + dy), ())})
        if best is None or best[0] > reach:
            return None
        _, n, i, foot = best
        ref, coords, _, _ = self.pieces[n]
        return {"tied": True, "piece": n, "ref": ref, "coords": coords, "i": i, "foot": foot}

    def named(self, spot: dict) -> dict:
        """Tie a located point into the network as a stop that a way can start or end at."""
        if "name" not in spot:
            _, coords, u, v = self.pieces[spot["piece"]]
            self.stops += 1
            spot["name"] = f"stop {self.stops}"
            self._link(spot["name"], u, [spot["foot"], *coords[spot["i"]::-1]], spot["ref"])
            self._link(spot["name"], v, [spot["foot"], *coords[spot["i"] + 1:]], spot["ref"])
        return spot

    def attach(self, at, reach: float) -> dict:
        """Tie a point to the nearest point on any mapped line, if one lies within reach."""
        spot = self.locate(at, reach)
        if spot:
            return self.named(spot)
        # Nothing within reach: how far off the nearest is, for saying so.
        best = self._nearest(at, ((n, i) for n, (_, coords, _, _) in enumerate(self.pieces)
                                  for i in range(len(coords) - 1)))
        return {"tied": False, "metres": best[0] if best else None}

    def parts(self, start: dict, end: dict) -> list[tuple[list, tuple]] | None:
        """The shortest way over the lines between two tied points, as its parts in order: (line, which mapped line)."""
        if start["piece"] == end["piece"]:  # on the same piece: the part of it between them
            lo, hi = sorted((start, end), key=lambda a: (a["i"], math.dist(a["coords"][a["i"]], a["foot"])))
            between = [lo["foot"], *lo["coords"][lo["i"] + 1:hi["i"] + 1], hi["foot"]]
            self._link(start["name"], end["name"], between if lo is start else between[::-1],
                       start["ref"])
        first, last = start["name"], end["name"]
        best, queue, came = {first: 0.0}, [(0.0, _order_key(first), first)], {}
        while queue:
            cost, _, here = heapq.heappop(queue)
            if here == last:
                break
            if cost > best.get(here, math.inf):
                continue
            for there, weight, coords, ref in self.graph.get(here, []):
                if cost + weight < best.get(there, math.inf):
                    best[there] = cost + weight
                    came[there] = (here, coords, ref)
                    heapq.heappush(queue, (cost + weight, _order_key(there), there))
        if last not in came:
            return None
        found, here = [], last
        while here != first:
            here, part, ref = came[here]
            found.append((part, ref))
        return found[::-1]

    def reach(self, start: dict, goals: set[str], first: str) -> list[tuple[list, tuple]] | None:
        """The shortest way from a tied point to the nearest of some stops, setting out along its line towards `first`.

        `first` is one of the two ends of the piece the point lies on: the way leaves the point in that direction.
        """
        origin = start["name"]
        best, queue, came = {origin: 0.0}, [], {}
        for there, weight, coords, ref in self.graph.get(origin, []):
            if there == first and weight < best.get(there, math.inf):
                best[there] = weight
                came[there] = (origin, coords, ref)
                heapq.heappush(queue, (weight, _order_key(there), there))
        last = None
        while queue:
            cost, _, here = heapq.heappop(queue)
            if here in goals:
                last = here
                break
            if cost > best.get(here, math.inf):
                continue
            for there, weight, coords, ref in self.graph.get(here, []):
                if there != origin and cost + weight < best.get(there, math.inf):
                    best[there] = cost + weight
                    came[there] = (here, coords, ref)
                    heapq.heappush(queue, (cost + weight, _order_key(there), there))
        if last is None:
            return None
        found, here = [], last
        while here != origin:
            here, part, ref = came[here]
            found.append((part, ref))
        return found[::-1]

    def way(self, start: dict, end: dict) -> tuple[list, set] | None:
        """The shortest way over the lines between two tied points, and the lines it uses."""
        found = self.parts(start, end)
        if found is None:
            return None
        return [c for part, _ in found for c in part], {ref for _, ref in found}


def _line(conn, coords) -> dict:
    """A line in map coordinates as text, as GeoJSON and with its length on the ground."""
    coords = [c for i, c in enumerate(coords) if i == 0 or math.dist(c, coords[i - 1]) > 0.01]
    if len(coords) < 2:
        coords = [coords[0], coords[0]]
    wkt = "LINESTRING(" + ",".join(f"{x:.2f} {y:.2f}" for x, y in coords) + ")"
    geometry, length = conn.execute(
        """SELECT ST_AsGeoJSON(ST_Transform(g, 4326), 6)::json,
                  round(ST_Length(ST_Transform(g, 4326)::geography))::int
           FROM (SELECT ST_SetSRID(ST_GeomFromText(%s), 25833) AS g) x""", (wkt,)).fetchone()
    return {"wkt": wkt, "geometry": geometry, "length_m": length, "planar": _Paths.along(coords)}


def _trace(conn, edge_id: str) -> dict:
    """The mapped path between the two ends of a stretch, found over the path network."""
    found = _rows(conn, """
        SELECT r.geom, r.kind, ST_X(a.geom) AS ax, ST_Y(a.geom) AS ay, ST_X(b.geom) AS bx,
               ST_Y(b.geom) AS by, ST_Length(r.geom) AS planar,
               round(ST_Length(ST_Transform(r.geom, 4326)::geography))::int AS length_m
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
        WHERE e.id = %(id)s AND e.status = 'active'""", {"id": edge_id})
    if not found:
        raise NotFound("no such stretch")
    edge = found[0]
    if edge["kind"] in ("water", "glacier"):
        raise Invalid("a crossing has no mapped path to follow")
    paths = _Paths.near(conn, edge["geom"])
    if not paths.pieces:
        raise Invalid("no mapped paths are loaded along this stretch")
    a, b = (edge["ax"], edge["ay"]), (edge["bx"], edge["by"])
    start, end = paths.attach(a, REACH_M), paths.attach(b, REACH_M)
    for tied in (start, end):
        if not tied["tied"]:
            raise Invalid(f"no mapped path within {REACH_M} m of one end of this stretch "
                          f"(the nearest is {round(tied['metres'])} m away)")
    way = paths.way(start, end)
    if not way:
        raise Invalid("the mapped paths do not connect the two ends of this stretch")
    line = _line(conn, [a, *way[0], b])
    if line["planar"] > 3 * edge["planar"] + 500:
        raise Invalid(f"the mapped way between the two ends is {round(line['planar'])} m, against "
                      f"{round(edge['planar'])} m now, so it is probably not the same path")
    return {**line, "was_m": edge["length_m"], "paths": sorted(way[1])}


VIA_REACH_M = 25   # a clicked point this near a mapped path is taken to mean the path


def _locate(coords, p) -> float:
    """How far along a line its nearest point to p lies."""
    best, walked = (math.inf, 0.0), 0.0
    for u, v in itertools.pairwise(coords):
        dx, dy = v[0] - u[0], v[1] - u[1]
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, (
            (p[0] - u[0]) * dx + (p[1] - u[1]) * dy) / (dx * dx + dy * dy)))
        d = math.dist(p, (u[0] + t * dx, u[1] + t * dy))
        step = math.hypot(dx, dy)
        if d < best[0]:
            best = (d, walked + t * step)
        walked += step
    return best[1]


def _between(coords, lo: float, hi: float) -> list:
    """The part of a line between two distances along it, both ends included."""
    out, walked = [], 0.0
    for u, v in itertools.pairwise(coords):
        step = math.dist(u, v)
        for at in (lo, hi):
            if walked <= at <= walked + step and step > 0:
                t = (at - walked) / step
                out.append((at, (u[0] + t * (v[0] - u[0]), u[1] + t * (v[1] - u[1]))))
        if lo < walked + step < hi:
            out.append((walked + step, v))
        walked += step
    out.sort(key=lambda x: x[0])
    points = [q for i, (_, q) in enumerate(out) if i == 0 or math.dist(q, out[i - 1][1]) > 1e-6]
    return points or [coords[0] if lo <= 0 else coords[-1]]


def _follow(conn, a, b, via: list, end_reach: float = REACH_M) -> tuple[list, set, int]:
    """A line from a to b through the points, along mapped paths wherever they join two stops.

    Each stop is tied to the nearest mapped path within reach. Between two
    tied stops the line takes the way over the paths, unless that is a long
    way round; otherwise it runs straight. Returns the line, the paths used
    and how many of its parts follow a path.
    """
    stops = [a, *via, b]
    paths = _Paths.near(
        conn, "SRID=25833;LINESTRING(" + ",".join(f"{x:.2f} {y:.2f}" for x, y in stops) + ")")
    tied = [paths.attach(p, end_reach if i in (0, len(stops) - 1) else VIA_REACH_M)
            for i, p in enumerate(stops)]
    coords, used, followed = [a], set(), 0
    for i in range(len(stops) - 1):
        way = paths.way(tied[i], tied[i + 1]) if tied[i]["tied"] and tied[i + 1]["tied"] else None
        if way and _Paths.along(way[0]) > 4 * math.dist(stops[i], stops[i + 1]) + 200:
            way = None      # a long way round is not what was meant
        if way:
            coords += way[0]
            used |= way[1]
            followed += 1
        last = i + 1 == len(stops) - 1
        if last or not tied[i + 1]["tied"]:
            coords.append(stops[i + 1])     # an end stays where it is; a free point is kept as clicked
        elif not way:
            coords.append(tied[i + 1]["foot"])
    return coords, used, followed


def snap_preview(conn, edge_id: str) -> dict:
    """What snapping a stretch to the mapped path would give, without changing anything."""
    traced = _trace(conn, edge_id)
    return {k: traced[k] for k in ("geometry", "length_m", "was_m")}


def snap(conn, edge_id: str, author: str) -> dict:
    """Replace a stretch's line with the mapped path between its two ends.

    The ends stay where they are. The stretch is shared, so the new line shows
    on every route that uses it. The earlier line remains as the old revision.
    """
    traced = _trace(conn, edge_id)
    if conn.execute(
            """SELECT ST_HausdorffDistance(r.geom, ST_SetSRID(ST_GeomFromText(%s), 25833)) < 0.5
               FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
               WHERE e.id = %s""", (traced["wkt"], edge_id)).fetchone()[0]:
        raise Conflict("this stretch already follows the mapped path")
    before = _revise(conn, edge_id, author, origin="edited", wkt=traced["wkt"])
    for source, line in traced["paths"]:
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type)
               VALUES ('edge', %s, %s, %s, 'traced')""", (edge_id, source, line))
    _event(conn, author, "edge.snapped", edge_id,
           {"from_m": traced["was_m"], "to_m": traced["length_m"], "paths": len(traced["paths"])},
           subject="edge")
    return {**{k: traced[k] for k in ("geometry", "length_m", "was_m")}, "undo_rev": before["rev_no"]}


BESIDE_M = 25       # a mapped path or road this near a stretch runs beside it
BESIDE_STEP_M = 10  # how often along a stretch it is compared with the mapped lines
BESIDE_RUN = 12     # looks in a row on a mapped line before it counts as followed (120 m)
BESIDE_PASSES = 16   # at most this many times over a stretch before what is found is taken as it is
BESIDE_SAME_M = 0.5 # two lines no further apart than this are the same line


def _thin(coords: list, within: float = 0.02) -> list:
    """The line without the points that lie on the straight between their neighbours: it is the same line."""
    out = [coords[0]]
    for p, q in itertools.pairwise(coords[1:]):
        a = out[-1]
        dx, dy = q[0] - a[0], q[1] - a[1]
        span = math.hypot(dx, dy)
        off = math.dist(p, a) if span == 0 else abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / span
        along = 0 if span == 0 else ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / span
        if off > within or not 0 <= along <= span:
            out.append(p)
    out.append(coords[-1])
    return out


def _beside(conn, coords: list) -> dict:
    """A line laid on the mapped paths and roads wherever one runs beside it, and left as it is elsewhere.

    The line is looked at every 10 m. Where looks in a row lie within 25 m of a
    mapped line, and the way over the mapped lines between them is about as
    long as the line itself there, that way is taken, if it goes on for 120 m
    or more. Where no mapped line runs beside it, or one only crosses it, the
    line keeps its own shape.
    """
    walked = [0.0, *itertools.accumulate(math.dist(p, q) for p, q in itertools.pairwise(coords))]
    total = walked[-1]
    n = max(1, round(total / BESIDE_STEP_M))

    def look(d: float) -> tuple:
        """The point this far along the line, and which of its points comes next."""
        k = min(max(bisect.bisect_right(walked, d), 1), len(coords) - 1)
        step = walked[k] - walked[k - 1]
        t = min(max((d - walked[k - 1]) / step, 0.0), 1.0) if step else 0.0
        p, q = coords[k - 1], coords[k]
        return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])), k

    looks = [look(total * i / n) for i in range(n + 1)]
    spots = [point for point, _ in looks]
    paths = _Paths.near(conn, "SRID=25833;LINESTRING(" + ",".join(f"{x:.2f} {y:.2f}" for x, y in coords) + ")",
                        margin=2 * BESIDE_M)
    tied = [paths.locate(p, BESIDE_M) for p in spots]
    ways = []
    for i in range(n):
        start, end, way = tied[i], tied[i + 1], None
        if start and end and start["piece"] == end["piece"]:    # on one mapped line: the part of it between them
            lo, hi = sorted((start, end), key=lambda a: (a["i"], math.dist(a["coords"][a["i"]], a["foot"])))
            part = [lo["foot"], *lo["coords"][lo["i"] + 1:hi["i"] + 1], hi["foot"]]
            way = (part if lo is start else part[::-1], {start["ref"]})
        elif start and end:
            way = paths.way(paths.named(start), paths.named(end))
        gap = math.dist(spots[i], spots[i + 1])
        if way and not 0.5 * gap <= _Paths.along(way[0]) <= 1.5 * gap + 15:
            way = None      # it only crosses there, or the mapped way goes round
        ways.append(way)
    i = 0
    while i < n:            # a few looks in a row on a mapped line is a chance meeting, not a path followed
        j = i
        while j < n and ways[j]:
            j += 1
        if 0 < j - i < BESIDE_RUN and not (i == 0 and j == n):
            ways[i:j] = [None] * (j - i)
        i = max(j, i + 1)
    out, used, followed, moved = [coords[0]], set(), 0.0, 0.0
    for i in range(n):
        if ways[i]:
            out += ways[i][0]
            used |= ways[i][1]
            followed += _Paths.along(ways[i][0])
            moved = max(moved, math.dist(spots[i], tied[i]["foot"]), math.dist(spots[i + 1], tied[i + 1]["foot"]))
            continue
        own = [spots[i], *coords[looks[i][1]:looks[i + 1][1]], spots[i + 1]]
        # Where it leaves or joins a mapped line it runs straight to its own next point: no step sideways.
        if i > 0 and ways[i - 1]:
            own = own[1:]
        if i + 1 < n and ways[i + 1]:
            own = own[:-1]
        out += own
    out.append(coords[-1])      # the ends stay exactly where the junctions are
    return {"coords": _thin(out), "used": used, "followed_m": followed, "moved_m": moved,
            "beside_m": total * sum(1 for t in tied if t) / len(tied)}


def _apart(conn, a: list, b: list) -> float:
    """How far apart two lines lie where they lie furthest apart."""
    wkt = ["LINESTRING(" + ",".join(f"{x:.2f} {y:.2f}" for x, y in c) + ")" for c in (a, b)]
    return conn.execute("SELECT ST_HausdorffDistance(ST_GeomFromText(%s), ST_GeomFromText(%s))", wkt).fetchone()[0]


def lay_beside(conn, edge_id: str, author: str, dry_run: bool = False) -> dict:
    """Lay a stretch on the mapped paths and roads wherever one runs beside it; the rest stays as it is.

    Unlike `snap`, which replaces the whole line with the mapped way between
    its ends, this keeps the line where nothing is mapped beside it: for a
    long stretch that is a mapped path in the valley and cairns over the top.
    Returns how much of it has a mapped line beside it and follows one, its
    length before and after, and how far it moves at most. A dry run changes
    nothing.
    """
    found = conn.execute(f"""
        SELECT ST_AsGeoJSON(r.geom)::json, round({_LEN})::int, r.kind
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = %s AND e.status = 'active'""", (edge_id,)).fetchone()
    if not found:
        raise NotFound("no such stretch")
    old, was_m, kind = [tuple(c[:2]) for c in found[0]["coordinates"]], found[1], found[2]
    if kind in ("water", "glacier", "connector") or len(old) < 2 or _Paths.along(old) < 1:
        raise Invalid("a crossing has no mapped path to follow")
    # Laid once, the line meets the mapped lines a little differently, and a further look can take
    # a few metres more. It is laid again until nothing more changes, so that doing this twice
    # gives what doing it once gives.
    first = laid = _beside(conn, old)
    used = set(laid["used"])
    for _ in range(BESIDE_PASSES - 1):
        again = _beside(conn, laid["coords"])
        if _apart(conn, again["coords"], laid["coords"]) < BESIDE_SAME_M:
            break
        laid = again
        used |= again["used"]
    laid = {**laid, "used": used, "beside_m": first["beside_m"]}
    line = _line(conn, laid["coords"])
    differs = _apart(conn, laid["coords"], old)
    result = {"edge_id": edge_id, "geometry": line["geometry"], "length_m": line["length_m"], "was_m": was_m,
              "beside_m": round(laid["beside_m"]), "followed_m": round(laid["followed_m"]),
              "moves_m": round(differs, 1), "changes": bool(laid["used"]) and differs >= BESIDE_SAME_M}
    if dry_run or not result["changes"]:
        return result
    result["undo_rev"] = _revise(conn, edge_id, author, origin="edited", wkt=line["wkt"])["rev_no"]
    for source, mapped in sorted(laid["used"]):
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type)
               VALUES ('edge', %s, %s, %s, 'traced')""", (edge_id, source, mapped))
    _event(conn, author, "edge.laid_beside", edge_id,
           {"from_m": was_m, "to_m": line["length_m"], "followed_m": round(laid["followed_m"]),
            "moves_m": round(differs, 1)}, subject="edge")
    return result


def compare_with_mapped(conn, route_id: str, author: str, version: int | None = None,
                        dry_run: bool = True, seen: dict | None = None,
                        leave: frozenset = frozenset()) -> dict:
    """The whole route against the mapped paths and roads: how much runs beside one, and what laying it there gives.

    A dry run changes nothing and returns the lines as they would be. Otherwise
    every stretch that has a mapped line beside it is laid on it, as one action.
    Comparing many routes, `seen` keeps what is found for a stretch they share.
    Stretches in `leave` are not changed.
    """
    if dry_run:     # only looked at: nothing is locked
        route = (_rows(conn, "SELECT code FROM core.route WHERE id = %(id)s AND status = 'active'",
                       {"id": route_id}) or [None])[0]
        if not route:
            raise NotFound("no such route")
    else:
        route = _route(conn, route_id, version)
    legs = conn.execute("""
        SELECT l.edge_id::text, r.kind FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s AND r.a_node <> r.b_node ORDER BY l.seq""", (route_id,)).fetchall()
    was = now = beside = followed = 0
    moves, changed, lines = 0.0, 0, []
    for edge_id, kind in legs:
        if kind in ("water", "glacier", "connector"):
            continue        # not walked on a path: left out of the comparison
        if edge_id in leave:
            one = lay_beside(conn, edge_id, author, dry_run=True)
            was, now, beside = was + one["was_m"], now + one["was_m"], beside + one["beside_m"]
            continue
        one = seen[edge_id] if seen is not None and edge_id in seen else lay_beside(conn, edge_id, author, dry_run)
        if seen is not None:
            seen[edge_id] = one
        was, now = was + one["was_m"], now + (one["length_m"] if one["changes"] else one["was_m"])
        beside, followed = beside + one["beside_m"], followed + one["followed_m"]
        if one["changes"]:
            changed, moves = changed + 1, max(moves, one["moves_m"])
            lines.append(one["geometry"])
    turrutebasen = conn.execute("""
        SELECT round(sum(length_m))::int FROM (
            SELECT DISTINCT t.id, t.length_m FROM core.route_leg l
            JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = l.edge_id
                 AND sr.source_key = 'turrutebasen' AND sr.ref_type = 'adopted_from'
            JOIN source_index.trail_segment t ON t.id = sr.external_id
            WHERE l.route_id = %s) x""", (route_id,)).fetchone()[0]
    if not dry_run and changed:
        _event(conn, author, "route.laid_beside", route_id,
               {"stretches": changed, "from_m": was, "to_m": now, "followed_m": followed})
        _touch(conn, route_id, author)
    return {"code": route["code"], "beside_m": min(beside, was), "followed_m": followed,
            "was_m": was, "length_m": now, "turrutebasen_m": turrutebasen, "stretches": changed,
            "moves_m": round(moves, 1), "done": not dry_run and changed > 0,
            "geometry": {"type": "GeometryCollection", "geometries": lines}}


MOVE_M = 500     # as far as a junction is moved in one go
DRAWN_POINTS = 2000


def redraw(conn, edge_id: str, author: str, points: list[tuple[float, float]], *,
           follow: bool = False, dry_run: bool = False) -> dict:
    """Replace a stretch's line with one laid through these points (lon, lat).

    The ends stay at the stretch's two nodes, and the points may run from
    either end. With follow, the line takes the mapped paths between the
    points wherever they join them, and runs straight elsewhere; without, it
    is straight from point to point. A dry run returns the line and changes
    nothing. The stretch is shared, so the line shows on every route using it.
    """
    if len(points) > DRAWN_POINTS:
        raise Invalid(f"a drawn line has at most {DRAWN_POINTS} points")
    found = conn.execute(f"""
        SELECT ST_X(a.geom), ST_Y(a.geom), ST_X(b.geom), ST_Y(b.geom), ST_Length(r.geom),
               round({_LEN})::int
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
        WHERE e.id = %s AND e.status = 'active'""", (edge_id,)).fetchone()
    if not found:
        raise NotFound("no such stretch")
    a, b, was, was_m = found[:2], found[2:4], found[4], found[5]
    xy = conn.execute("""
        SELECT ST_X(p.g), ST_Y(p.g)
        FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS t(lon, lat, i),
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326), 25833) AS g) p
        ORDER BY t.i""", ([p[0] for p in points], [p[1] for p in points])).fetchall()
    if xy and (math.dist(xy[0], a) + math.dist(xy[-1], b)
               > math.dist(xy[0], b) + math.dist(xy[-1], a)):
        xy.reverse()        # drawn from the other end
    reach = max(2000, 2 * was)
    if any(min(math.dist(q, a), math.dist(q, b)) > reach for q in xy):
        raise Invalid("a point of the drawn line lies far from the stretch")
    coords, used, followed = _follow(conn, a, b, xy) if follow else ([a, *xy, b], set(), 0)
    line = _line(conn, coords)
    result = {"geometry": line["geometry"], "length_m": line["length_m"], "was_m": was_m,
              "parts": len(xy) + 1, "followed": followed}
    if dry_run:
        return result
    result["undo_rev"] = _revise(conn, edge_id, author, origin="edited", wkt=line["wkt"])["rev_no"]
    for source, line in sorted(used):
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type)
               VALUES ('edge', %s, %s, %s, 'traced')""", (edge_id, source, line))
    _event(conn, author, "edge.redrawn", edge_id,
           {"from_m": was_m, "to_m": line["length_m"], "points": len(xy), "followed": followed},
           subject="edge")
    return result


PULL_M = 500     # as far as a line is pulled in one go


FREE = ("glacier", "water", "off_trail")    # stretches drawn by hand over ground with no path: a line of straights
FREE_PULL_M = 10_000


def _stretched(old: list, at: float, to: tuple, near: float) -> list:
    """A line of straights pulled like a rubber band: the point that was grabbed goes to the spot.

    Grabbed at one of its corners, that corner moves. Grabbed between two, a new
    corner is made there. A corner let go on the one before or after it goes
    away. The two ends stay where they are.
    """
    walked = [0.0, *itertools.accumulate(math.dist(p, q) for p, q in itertools.pairwise(old))]
    corner = min(range(len(old)), key=lambda i: abs(walked[i] - at))
    if 0 < corner < len(old) - 1 and abs(walked[corner] - at) <= near:
        if min(math.dist(to, old[corner - 1]), math.dist(to, old[corner + 1])) <= near:
            return [*old[:corner], *old[corner + 1:]]
        return [*old[:corner], to, *old[corner + 1:]]
    after = next(i for i in range(1, len(old)) if walked[i] >= at or i == len(old) - 1)
    return [*old[:after], to, *old[after:]]


def bend(conn, edge_id: str, author: str, grab: tuple[float, float], drop: tuple[float, float],
         dry_run: bool = False, corner_m: float = 25.0) -> dict:
    """Pull a stretch, where it was grabbed, over to another spot (both as lon, lat).

    Only the line near the grab changes: a part three times as long as the pull
    to each side, at least 50 m. That part is laid through the spot, along the
    mapped paths and roads where they join it up, and straight otherwise. The
    rest of the line, and both ends, stay as they are.

    A stretch over a glacier, over water or through the terrain has no path to
    follow and is a line of straights. It is pulled like a rubber band instead:
    the corner that was grabbed (within corner_m) moves, or a new corner is
    made where it was grabbed, however far it is pulled.
    """
    found = conn.execute(f"""
        SELECT ST_AsGeoJSON(r.geom)::json, round({_LEN})::int, r.kind,
               ST_X(g.p), ST_Y(g.p), ST_X(d.p), ST_Y(d.p)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS p) g,
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS p) d
        WHERE e.id = %s AND e.status = 'active'""", (*grab, *drop, edge_id)).fetchone()
    if not found:
        raise NotFound("no such stretch")
    old, was_m, kind = [tuple(c[:2]) for c in found[0]["coordinates"]], found[1], found[2]
    grabbed, to = found[3:5], found[5:7]
    at = _locate(old, grabbed)
    on_line = _between(old, at, at)[0]
    pull = math.dist(on_line, to)
    if math.dist(grabbed, on_line) > max(100, 4 * corner_m):
        raise Invalid("the stretch was not grabbed on its line")
    if kind in FREE:
        if pull > FREE_PULL_M:
            raise Invalid(f"that is {pull / 1000:.0f} km away: a line is pulled at most {FREE_PULL_M // 1000} km at a time")
        line = _line(conn, _stretched(old, at, to, corner_m))
        result = {"geometry": line["geometry"], "length_m": line["length_m"], "was_m": was_m,
                  "pulled_m": round(pull, 1), "followed": False, "corners": len(old)}
        if dry_run:
            return result
        result["undo_rev"] = _revise(conn, edge_id, author, origin="edited", wkt=line["wkt"])["rev_no"]
        _event(conn, author, "edge.bent", edge_id,
               {"from_m": was_m, "to_m": line["length_m"], "pulled_m": round(pull, 1), "followed": False}, subject="edge")
        return result
    if pull > PULL_M:
        raise Invalid(f"that is {pull:.0f} m away: a line is pulled at most {PULL_M} m at a time")
    reach = max(3 * pull, 50)
    head = _between(old, 0, max(0.0, at - reach))
    tail = _between(old, min(_Paths.along(old), at + reach), _Paths.along(old))
    if head[0] != old[0]:
        head.insert(0, old[0])
    if tail[-1] != old[-1]:
        tail.append(old[-1])    # the ends stay exactly where the junctions are
    middle, used, followed = _follow(conn, head[-1], tail[0], [to], end_reach=min(REACH_M, pull + 15))
    line = _line(conn, [*head[:-1], *middle, *tail[1:]])
    result = {"geometry": line["geometry"], "length_m": line["length_m"], "was_m": was_m,
              "pulled_m": round(pull, 1), "followed": followed > 0}
    if dry_run:
        return result
    result["undo_rev"] = _revise(conn, edge_id, author, origin="edited", wkt=line["wkt"])["rev_no"]
    for source, mapped in sorted(used):
        conn.execute(
            """INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, ref_type)
               VALUES ('edge', %s, %s, %s, 'traced')""", (edge_id, source, mapped))
    _event(conn, author, "edge.bent", edge_id,
           {"from_m": was_m, "to_m": line["length_m"], "pulled_m": round(pull, 1),
            "followed": followed > 0}, subject="edge")
    return result


def restore(conn, edge_id: str, rev_no: int, author: str) -> None:
    """Give a stretch back the line and the facts it had at an earlier revision: undo."""
    old = conn.execute("""
        SELECT ST_AsText(o.geom), o.kind, o.marked, o.signed,
               (o.a_node, o.b_node) = (r.a_node, r.b_node)
               AND ST_Distance(ST_StartPoint(o.geom), a.geom) + ST_Distance(ST_EndPoint(o.geom), b.geom) < 0.5
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.edge_revision o ON o.edge_id = e.id AND o.rev_no = %s
        JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
        WHERE e.id = %s AND e.status = 'active'""", (rev_no, edge_id)).fetchone()
    if not old:
        raise NotFound("no such earlier line")
    if not old[4]:
        raise Conflict("the ends of the stretch have moved since, so the earlier line no longer fits")
    _revise(conn, edge_id, author, origin="edited", wkt=old[0], kind=old[1], marked=old[2],
            signed=old[3])
    _event(conn, author, "edge.restored", edge_id, {"to_revision": rev_no}, subject="edge")


def merge_node(conn, node_id: str, into_id: str, author: str) -> dict:
    """Join one junction to another: every stretch that met at the first now meets at the second.

    A stretch that ran between the two is left with nowhere to go, and is taken
    out of the routes that used it. The first junction is retired.
    """
    found = {str(n): (place, primary) for n, place, primary in conn.execute("""
        SELECT n.id, n.place_id, EXISTS (SELECT 1 FROM core.place p WHERE p.primary_node_id = n.id)
        FROM core.node n WHERE n.id = ANY(%s::uuid[]) AND n.status = 'active' FOR UPDATE""",
        ([node_id, into_id],))}
    if node_id == into_id or len(found) != 2:
        raise NotFound("no such pair of junctions")
    if found[node_id][0] and found[node_id][0] != found[into_id][0]:
        raise Invalid("that is an arrival point of a place: take it out of the place first")
    apart, to = conn.execute("""SELECT ST_Distance(a.geom, b.geom), b.geom FROM core.node a, core.node b
                                WHERE a.id = %s AND b.id = %s""", (node_id, into_id)).fetchone()
    if apart > MOVE_M:
        raise Invalid(f"they are {apart:.0f} m apart: junctions are joined over at most {MOVE_M} m")
    meeting = conn.execute("""
        SELECT e.id::text, r.a_node::text, r.b_node::text, ST_AsText(
                   CASE WHEN r.b_node = %(n)s THEN ST_SetPoint(x.g, -1, %(to)s::geometry) ELSE x.g END)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT CASE WHEN r.a_node = %(n)s THEN ST_SetPoint(r.geom, 0, %(to)s::geometry)
                                  ELSE r.geom END AS g) x
        WHERE e.status = 'active' AND %(n)s IN (r.a_node, r.b_node)""",
        {"n": node_id, "to": to}).fetchall()
    dropped = 0
    for edge, a, b, wkt in meeting:
        a, b = (into_id if a == node_id else a), (into_id if b == node_id else b)
        if a == b:      # it ran between the two junctions, or back to where it started
            for (route_id,) in conn.execute(
                    "DELETE FROM core.route_leg WHERE edge_id = %s RETURNING route_id", (edge,)).fetchall():
                _reorder(conn, str(route_id))
                _event(conn, author, "route.leg_removed", str(route_id),
                       {"edge_id": edge, "because": "its two ends were joined"})
                _touch(conn, str(route_id), author)
            conn.execute("UPDATE core.edge SET status = 'retired', version = version + 1 WHERE id = %s",
                         (edge,))
            dropped += 1
        else:
            _revise(conn, edge, author, origin="edited", wkt=wkt, nodes=(a, b))
    if found[node_id][1]:
        conn.execute("UPDATE core.place SET primary_node_id = %s, version = version + 1 "
                     "WHERE primary_node_id = %s", (into_id, node_id))
    conn.execute("UPDATE core.node SET status = 'retired', place_id = NULL, version = version + 1 "
                 "WHERE id = %s", (node_id,))
    identity.succeed(conn, "node", node_id, [into_id], "joined")
    _event(conn, author, "node.merged", node_id,
           {"into": into_id, "metres": round(apart, 1), "stretches": len(meeting) - dropped,
            "dropped": dropped}, subject="node")
    return {"moved_m": round(apart, 1), "stretches": len(meeting) - dropped, "dropped": dropped}


def move_node(conn, node_id: str, author: str, lon: float, lat: float,
              snap_m: float | None = None, dry_run: bool = False) -> dict:
    """Move a junction or an end. Every stretch that meets there follows: its last point moves along.

    With snap_m, a spot that near another junction means that junction, and the
    two are joined; a spot that near a mapped path or road lands on it. A dry
    run only says which of these it would be.
    """
    reach = min(snap_m or 0, 50)
    found = conn.execute(f"""
        SELECT ST_Distance(n.geom, s.g), s.g, s.g IS DISTINCT FROM p.g, p.g,
               ST_X(ST_Transform(n.geom, 4326)), ST_Y(ST_Transform(n.geom, 4326))
        FROM core.node n,
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833) AS g) p,
             LATERAL (SELECT coalesce((
                 SELECT ST_ClosestPoint(m.geom, p.g) FROM {mapped_lines(conn)} m
                 WHERE ST_DWithin(m.geom, p.g, %(snap)s) ORDER BY m.geom <-> p.g LIMIT 1), p.g) AS g) s
        WHERE n.id = %(id)s AND n.status = 'active' {"" if dry_run else "FOR UPDATE OF n"}""",
        {"lon": lon, "lat": lat, "id": node_id, "snap": reach}).fetchone()
    if not found:
        raise NotFound("no such junction")
    moved, to, snapped, clicked, was_lon, was_lat = found
    # Dropped on another junction in use: the two become one.
    other = conn.execute("""
        SELECT n.id::text, (SELECT p.name FROM core.place p
                            WHERE p.id = n.place_id AND p.status = 'confirmed')
        FROM core.node n
        WHERE n.status = 'active' AND n.id <> %(id)s AND ST_DWithin(n.geom, %(at)s::geometry, %(reach)s)
          AND (n.place_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE e.status = 'active' AND n.id IN (r.a_node, r.b_node)))
        ORDER BY n.geom <-> %(at)s::geometry LIMIT 1""",
        {"id": node_id, "at": clicked, "reach": max(reach, NODE_M)}).fetchone()
    if other:
        if dry_run:
            # The stretches that run between the two would have nowhere to go.
            between = _rows(conn, """
                SELECT round(ST_Length(r.geom))::int AS metres,
                       (SELECT string_agg(t.code, ', ' ORDER BY t.code) FROM core.route_leg l
                        JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                        WHERE l.edge_id = e.id) AS routes
                FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE e.status = 'active' AND r.kind <> 'connector'
                  AND (r.a_node, r.b_node) IN ((%(a)s::uuid, %(b)s::uuid), (%(b)s::uuid, %(a)s::uuid))""",
                {"a": node_id, "b": other[0]})
            return {"outcome": "join", "place": other[1], "drops": between}
        return {**merge_node(conn, node_id, other[0], author), "outcome": "join", "place": other[1]}
    if moved > MOVE_M:
        raise Invalid(f"that is {moved:.0f} m away: a junction is moved at most {MOVE_M} m at a time")
    if dry_run:
        return {"outcome": "snap" if snapped else "move"}
    meeting = conn.execute("""
        SELECT e.id::text, ST_AsText(
                   CASE WHEN r.b_node = %(n)s THEN ST_SetPoint(x.g, -1, %(to)s::geometry) ELSE x.g END)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT CASE WHEN r.a_node = %(n)s THEN ST_SetPoint(r.geom, 0, %(to)s::geometry)
                                  ELSE r.geom END AS g) x
        WHERE e.status = 'active' AND %(n)s IN (r.a_node, r.b_node)""",
        {"n": node_id, "to": to}).fetchall()
    for edge, wkt in meeting:
        _revise(conn, edge, author, origin="edited", wkt=wkt)
    conn.execute("UPDATE core.node SET geom = %s, version = version + 1 WHERE id = %s", (to, node_id))
    _event(conn, author, "node.moved", node_id,
           {"metres": round(moved, 1), "stretches": len(meeting)}, subject="node")
    return {"moved_m": round(moved, 1), "stretches": len(meeting), "snapped": snapped,
            "outcome": "snap" if snapped else "move", "from": [was_lon, was_lat]}


def add_stretch(conn, route_id: str, version: int, author: str, *, edge_id: str | None = None,
                segment_id: str | None = None) -> None:
    """Carry a route on along a stretch that exists and touches it.

    The stretch is one of yours (shared from then on), or a Turrutebasen
    segment, which becomes yours now.
    """
    _route(conn, route_id, version)
    if segment_id:
        made = _segment_edges(conn, author, "t.id = %(id)s", segment_id)
        if not made:
            raise NotFound("no such segment")
        edge_id = made[0]
    ends = conn.execute("""
        SELECT r.a_node::text, r.b_node::text, round(ST_Length(r.geom))::int
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = %s AND e.status = 'active'""", (edge_id,)).fetchone()
    if not ends:
        raise NotFound("no such stretch")
    if conn.execute("SELECT 1 FROM core.route_leg WHERE route_id = %s AND edge_id = %s",
                    (route_id, edge_id)).fetchone():
        raise Conflict("that stretch is already part of this route")
    on_route = _graph(conn, route_id)["at"]
    if on_route and not {ends[0], ends[1]} & set(on_route):
        raise Invalid("that stretch does not touch this route")
    _add_legs(conn, route_id, [edge_id])
    _event(conn, author, "route.stretch_added", route_id, {"edge_id": edge_id, "metres": ends[2]})
    _touch(conn, route_id, author)


SPLIT_END_M = 3      # a stretch is not divided nearer one of its ends than this: that end is meant


def split(conn, edge_id: str, author: str, lon: float, lat: float) -> str:
    """Divide a stretch in two at the spot on it nearest a point, and return the junction made there.

    Every route that used the stretch now uses both parts, and each part still
    says which Turrutebasen segment it came from. Asked nearer than a few
    metres from an end, nothing is divided and that end is returned.
    """
    found = conn.execute("""
        SELECT r.a_node::text, r.b_node::text, r.kind, r.origin, r.marked, r.signed, r.maintainers,
               ST_Length(r.geom), ST_LineLocatePoint(r.geom, p.g), r.geom
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) AS g) p
        WHERE e.id = %s AND e.status = 'active' FOR UPDATE OF e""", (lon, lat, edge_id)).fetchone()
    if not found:
        raise NotFound("no such stretch")
    a, b, kind, origin, marked, signed, maintainers, metres, at, geom = found
    if at * metres < SPLIT_END_M or a == b:
        return a
    if (1 - at) * metres < SPLIT_END_M:
        return b
    node = str(conn.execute(
        "INSERT INTO core.node (geom, kind) VALUES (ST_LineInterpolatePoint(%s::geometry, %s), 'junction') "
        "RETURNING id", (geom, at)).fetchone()[0])
    parts = []
    for n, (lo, hi, start, end) in enumerate(((0, at, a, node), (at, 1, node, b)), 1):
        part = _new_edge(conn, author, "ST_LineSubstring(%(g)s::geometry, %(lo)s, %(hi)s)",
                         {"g": geom, "lo": lo, "hi": hi}, start, end, kind, origin, marked, signed,
                         maintainers)
        conn.execute("""
            INSERT INTO core.source_reference
                   (subject_type, subject_id, source_key, external_id, snapshot_id, ref_type, detail)
            SELECT 'edge', %s, source_key, external_id, snapshot_id, ref_type,
                   detail || jsonb_build_object('part', %s::int, 'of', %s::text, 'from', %s::float8, 'to', %s::float8)
            FROM core.source_reference WHERE subject_type = 'edge' AND subject_id = %s""",
            (part, n, edge_id, lo, hi, edge_id))
        parts.append(part)
    # Each route walks both parts where it walked the one.
    for route_id, said in conn.execute(
            "DELETE FROM core.route_leg WHERE edge_id = %s RETURNING route_id::text, maintainers",
            (edge_id,)).fetchall():
        _add_legs(conn, route_id, parts)
        if said is not None:
            conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s "
                         "AND edge_id = ANY(%s::uuid[])", (said, route_id, parts))
        _touch(conn, route_id, author)
    conn.execute("UPDATE core.edge SET status = 'retired', version = version + 1 WHERE id = %s", (edge_id,))
    identity.succeed(conn, "edge", edge_id, parts, "divided")
    _event(conn, author, "edge.split", edge_id,
           {"at_m": round(at * metres), "of_m": round(metres), "into": parts, "node": node}, subject="edge")
    return node


# ---- A route laid out between points: the shortest way over what exists. ----

WAY_NODE_M = 30        # a point this near a junction or an end means that junction
WAY_LINE_M = 300       # else it means the spot on the nearest stretch, when one lies this near


_WALKED = """e.status = 'active' AND r.a_node <> r.b_node AND r.kind <> 'water'
             AND NOT (r.kind = 'connector' AND r.origin = 'derived')
             AND NOT (EXISTS (SELECT 1 FROM core.route_leg dl JOIN core.route dr ON dr.id = dl.route_id
                              WHERE dl.edge_id = e.id AND dr.status = 'active'
                                AND dr.discontinued_since IS NOT NULL)
                      AND NOT EXISTS (SELECT 1 FROM core.route_leg ul JOIN core.route ur ON ur.id = ul.route_id
                                      WHERE ul.edge_id = e.id AND ur.status = 'active'
                                        AND ur.discontinued_since IS NULL))"""


class _Net:
    """Your stretches and Turrutebasen's segments around a line, as one graph to find a way over.

    Turrutebasen segments that are not stretches yet are made stretches, so
    that both kinds have the junctions a route made from them will have: all
    segments around the line, or only the ones named. Building it therefore
    changes things. A plan builds it in a transaction that is rolled back; the
    real thing builds it again with only the segments the plan used.
    """

    def __init__(self, conn, author: str, xy: list, only: list[str] | None = None):
        self.conn, self.author = conn, author
        straight = sum(math.dist(a, b) for a, b in itertools.pairwise(xy))
        if straight > 150_000:
            raise Invalid("the points lie too far apart to find a way between them in one go")
        self.around = "SRID=25833;LINESTRING(" + ",".join(
            f"{x:.1f} {y:.1f}" for x, y in (xy if len(xy) > 1 else [xy[0], xy[0]])) + ")"
        self.margin = max(1500.0, 0.5 * straight)
        self.fresh: dict[str, str] = {}     # stretch made here -> the segment it stands for
        self.divided = 0
        for (segment,) in conn.execute("""
                SELECT t.id FROM source_index.trail_segment t
                WHERE ST_DWithin(t.geom, %(g)s::geometry, %(m)s) AND GeometryType(t.geom) = 'LINESTRING'
                  AND (%(only)s::text[] IS NULL OR t.id = ANY(%(only)s::text[]))
                  AND NOT EXISTS (SELECT 1 FROM core.source_reference sr
                                  JOIN core.edge known ON known.id = sr.subject_id AND known.status <> 'undone'
                                  WHERE sr.subject_type = 'edge' AND sr.source_key = 'turrutebasen'
                                    AND sr.ref_type = 'adopted_from' AND sr.external_id = t.id)""",
                {"g": self.around, "m": self.margin, "only": only}).fetchall():
            for edge in _segment_edges(conn, author, "t.id = %(id)s", segment):
                self.fresh[edge] = segment

    def stop(self, x: float, y: float, reach: float, what: str = "that point", far: float | None = None) -> str:
        """The junction a point means: one within reach, else the spot on the nearest stretch, divided there.

        How far off a stretch may lie and still be meant: 300 m for a point given by its position (a hut
        beside the path), or `far` where the point was pointed at on the map and means what is under it.
        """
        far = max(reach, WAY_LINE_M) if far is None else far
        here = {"x": x, "y": y, "reach": reach, "far": far}
        node = self.conn.execute(f"""
            SELECT n.id::text FROM (SELECT ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25833) AS g) p, core.node n
            WHERE n.status = 'active' AND ST_DWithin(n.geom, p.g, %(reach)s) AND EXISTS (
                SELECT 1 FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE {_WALKED} AND n.id IN (r.a_node, r.b_node))
            ORDER BY n.geom <-> p.g LIMIT 1""", here).fetchone()
        if node:
            return node[0]
        line = self.conn.execute(f"""
            SELECT e.id::text, ST_Distance(r.geom, p.g) FROM (SELECT ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25833) AS g) p,
                 core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE {_WALKED} AND ST_DWithin(r.geom, p.g, %(far)s)
            ORDER BY r.geom <-> p.g LIMIT 1""", here).fetchone()
        if not line:
            raise NoWay(f"no stretch within {far:.0f} m of {what}")
        if line[1] > reach:
            # The point was not put on a stretch. Where a mapped path or road lies clearly nearer
            # it than any stretch does, that is what was pointed at.
            mapped = self.conn.execute(f"""
                SELECT min(ST_Distance(m.geom, p.g)) FROM (SELECT ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25833) AS g) p,
                     {mapped_lines(self.conn)} m WHERE ST_DWithin(m.geom, p.g, %(near)s)""",
                {**here, "near": min(line[1], MAPPED_REACH_M)}).fetchone()[0]
            if mapped is not None and mapped < line[1] - 20:
                raise NoWay(f"a mapped path or road lies nearer {what} than any stretch")
        lon, lat = self.conn.execute(
            "SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
            (x, y)).fetchone()
        was = line[0]
        at = split(self.conn, was, self.author, lon, lat)
        if was in self.fresh:       # its two parts stand for the same segment
            for (part,) in self.conn.execute(
                    """SELECT e.id::text FROM core.source_reference sr JOIN core.edge e ON e.id = sr.subject_id
                       WHERE sr.subject_type = 'edge' AND sr.external_id = %s AND e.status = 'active'""",
                    (self.fresh[was],)).fetchall():
                self.fresh[part] = self.fresh[was]
        self.divided += self.conn.execute("SELECT status = 'retired' FROM core.edge WHERE id = %s",
                                          (was,)).fetchone()[0]
        return at

    def load(self) -> None:
        """Read the graph. After every stop has been found, since finding one may divide a stretch."""
        self.reach_from: dict[str, list] = {}
        for edge, a, b, metres in self.conn.execute(f"""
                SELECT e.id::text, r.a_node::text, r.b_node::text, ST_Length(r.geom)
                FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                WHERE {_WALKED} AND ST_DWithin(r.geom, %s::geometry, %s)""",
                (self.around, self.margin)).fetchall():
            self.reach_from.setdefault(a, []).append((b, metres, edge))
            self.reach_from.setdefault(b, []).append((a, metres, edge))

    def shortest(self, start: str, goals: set[str], avoid: frozenset = frozenset()) -> tuple[list[str], str]:
        """The stretches of the shortest way from a junction to the nearest of some junctions, and which."""
        best, queue, came = {start: 0.0}, [(0.0, start)], {}
        while queue:
            cost, here = heapq.heappop(queue)
            if here in goals and here != start:
                way, at = [], here
                while at != start:
                    at, edge = came[at]
                    way.append(edge)
                return way[::-1], here
            if cost > best.get(here, math.inf):
                continue
            for there, metres, edge in self.reach_from.get(here, []):
                if edge not in avoid and cost + metres < best.get(there, math.inf):
                    best[there] = cost + metres
                    came[there] = (here, edge)
                    heapq.heappush(queue, (cost + metres, there))
        raise NoWay("no way over your stretches and Turrutebasen's joins those points")

    def told(self, edges: list[str], stops: list[str]) -> dict:
        """A way as it is reported: its line, its length, and what of it is new."""
        geometry, metres = self.conn.execute(f"""
            SELECT ST_AsGeoJSON(ST_Transform(ST_Collect(r.geom ORDER BY o.i), 4326), 6)::json,
                   coalesce(round(sum({_LEN})), 0)::int
            FROM unnest(%s::uuid[]) WITH ORDINALITY AS o(id, i)
            JOIN core.edge e ON e.id = o.id JOIN core.edge_revision r ON r.id = e.current_revision_id""",
            (edges,)).fetchone()
        where = _rows(self.conn, """
            SELECT ST_X(ST_Transform(n.geom, 4326)) AS lon, ST_Y(ST_Transform(n.geom, 4326)) AS lat,
                   (SELECT pl.name FROM core.place pl WHERE pl.id = n.place_id AND pl.status = 'confirmed') AS place
            FROM unnest(%(ids)s::uuid[]) WITH ORDINALITY AS o(id, i) JOIN core.node n ON n.id = o.id
            ORDER BY o.i""", {"ids": stops})
        segments = sorted({self.fresh[e] for e in edges if e in self.fresh})
        return {"edges": edges, "segments": segments, "stops": where, "length_m": metres,
                "stretches": len(edges), "new": len(segments), "divided": self.divided,
                "geometry": geometry}


def _to_xy(conn, points: list[tuple[float, float]]) -> list:
    return conn.execute("""
        SELECT ST_X(p.g), ST_Y(p.g)
        FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS t(lon, lat, i),
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(t.lon, t.lat), 4326), 25833) AS g) p
        ORDER BY t.i""", ([p[0] for p in points], [p[1] for p in points])).fetchall()


def _way(conn, author: str, points: list[tuple[float, float]], reach_m: float | None = None,
         only: list[str] | None = None) -> dict:
    """The shortest way through the points (lon, lat), over your stretches and Turrutebasen's segments.

    A point within reach_m of a junction or end means that junction. Otherwise
    it means the spot on the nearest stretch (a hut beside the path), and the
    stretch is divided there. Place links and water are not walked.

    A point with no stretch near it means the mapped path or road it was put
    on, and two points that no stretch joins are joined along the mapped paths
    and roads, which become new stretches. That is how a route is laid where
    Turrutebasen has nothing: with a point or two along the way it is to take.

    A point with nothing at all under it, no stretch and no mapped line, is
    the point itself: the way goes straight to it and on from it, and the
    points of that kind in a row become one stretch in the terrain. That is
    how a route is laid over a glacier or across open ground. Two points on
    lines that nothing joins also go straight to each other, as do two within
    500 m of each other where the mapped way between them goes far round.
    """
    if not 2 <= len(points) <= 50:
        raise Invalid("a route is laid out between at least two points")
    reach = min(reach_m or WAY_NODE_M, 500)
    xy = _to_xy(conn, points)
    net = _Net(conn, author, xy, only)
    lines: list = []        # the mapped paths and roads, read when a point or a step needs them

    def mapped() -> _Paths:
        if not lines:
            longest = max(math.dist(a, b) for a, b in itertools.pairwise(xy))
            lines.append(_Paths.near(conn, "SRID=25833;LINESTRING(" + ",".join(f"{x:.1f} {y:.1f}" for x, y in xy) + ")",
                                     margin=max(1500.0, 0.5 * longest)))
        return lines[0]

    def spot_of(node: str) -> tuple:
        return tuple(conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s", (node,)).fetchone())

    # Each point: a junction or a spot on a stretch; else the mapped path or road it was put on;
    # else, with nothing there, the point itself, which the way goes straight through.
    stops: list = []
    free: set[int] = set()      # points that are themselves, with no line under them
    for i, (x, y) in enumerate(xy):
        try:
            with conn.transaction():        # finding a spot may divide a stretch: not to stay if it is refused
                at = net.stop(x, y, reach, f"point {i + 1}")
        except NoWay:
            on = mapped().locate((x, y), MAPPED_REACH_M)
            if on:
                at = _node_at(conn, f"SRID=25833;POINT({on['foot'][0]:.2f} {on['foot'][1]:.2f})")
            elif i in (0, len(xy) - 1):     # where the route begins or ends, a junction is made
                at = _node_at(conn, f"SRID=25833;POINT({x:.2f} {y:.2f})")
                free.add(i)
            else:
                at = None
                free.add(i)
        if at and stops and stops[-1] == at:
            raise Invalid(f"points {i} and {i + 1} mean the same junction")
        stops.append(at)
    net.load()
    steps: list[str] = []
    along = straight = 0.0
    i = 0
    while i < len(stops) - 1:
        j = i + 1
        while stops[j] is None:
            j += 1          # the points between that are only themselves
        start, end = stops[i], stops[j]
        a, b = spot_of(start), spot_of(end)
        if j > i + 1 or i in free or j in free:
            # Nothing is mapped at one of the points: straight from point to point, as one stretch
            # in the terrain. What it is (a glacier, a marked way over scree) is said afterwards.
            line = _line(conn, [a, *xy[i + 1:j], b])
            longest = max(math.dist(p, q) for p, q in itertools.pairwise([a, *xy[i + 1:j], b]))
            if longest > STRAIGHT_M:
                raise Invalid(f"nothing is mapped between points {i + 1} and {j + 1}, and they lie "
                              f"{longest / 1000:.0f} km apart: put points between them, {STRAIGHT_M // 1000} km apart at most")
            steps.append(_new_edge(conn, author, "ST_SetSRID(ST_GeomFromText(%(wkt)s), 25833)", {"wkt": line["wkt"]},
                                   start, end, "off_trail", "drawn", None, None))
            straight += line["planar"]
            i = j
            continue
        try:
            steps += net.shortest(start, {end})[0]
            i = j
            continue
        except NoWay:
            pass
        # No stretch joins the two. The way between them along the mapped paths and roads, as new
        # stretches, unless that way goes far round: then a short hop goes straight across, and a
        # longer one is for the person to steer.
        gap = math.dist(a, b)
        tied = mapped().attach(a, REACH_M), mapped().attach(b, REACH_M)
        found = mapped().parts(*tied) if tied[0]["tied"] and tied[1]["tied"] else None
        metres = sum(_Paths.along(part) for part, _ in found) if found else 0.0
        if found and metres <= 4 * gap + 500:
            steps += _draw_along(conn, author, found, start, a, end, b)
            along += metres
        elif found and gap > HOP_M:
            raise Invalid(f"the mapped way between points {i + 1} and {j + 1} is {metres / 1000:.1f} km for "
                          f"{gap / 1000:.1f} km as the crow flies: put a point between them on the way it is to take")
        elif gap > STRAIGHT_M:
            raise Invalid(f"nothing joins points {i + 1} and {j + 1}, and they lie {gap / 1000:.0f} km apart: "
                          f"put points between them, {STRAIGHT_M // 1000} km apart at most")
        else:
            line = _line(conn, [a, b])
            steps.append(_new_edge(conn, author, "ST_SetSRID(ST_GeomFromText(%(wkt)s), 25833)", {"wkt": line["wkt"]},
                                   start, end, "off_trail", "drawn", None, None))
            straight += line["planar"]
        i = j
    if len(set(steps)) != len(steps):
        raise Invalid("that way would walk the same stretch twice: a route is one line")
    return {**net.told(steps, [s for s in stops if s]), "mapped_m": round(along), "straight_m": round(straight)}


def plan(conn, points: list[tuple[float, float]], reach_m: float | None = None) -> dict:
    """What a route laid out through the points would be. Needs a connection that may write; keeps nothing."""
    with conn.transaction(force_rollback=True):
        way = _way(conn, AUTOMATIC, points, reach_m)
    return {k: v for k, v in way.items() if k != "edges"}


def _lay(conn, author: str, points: list[tuple[float, float]], reach_m: float | None) -> dict:
    """Find the way as `plan` does, then make it real: only the segments on it become stretches."""
    return _way(conn, author, points, reach_m, only=plan(conn, points, reach_m)["segments"])


def lay_out(conn, author: str, points: list[tuple[float, float]], code: str,
            name: str | None = None, reach_m: float | None = None) -> str:
    """Make a new route through the points, along the shortest way over what exists."""
    code = (code or "").strip()
    if not code:
        raise Invalid("a route needs a code")
    if conn.execute("SELECT 1 FROM core.route WHERE lower(code) = lower(%s) AND status = 'active'",
                    (code,)).fetchone():
        raise Conflict(f"the code {code} is already used by another of your routes")
    way = _lay(conn, author, points, reach_m)
    route_id = str(conn.execute(
        "INSERT INTO core.route (code, code_provisional, reviewed, name) VALUES (%s, %s, false, %s) "
        "RETURNING id", (code, "/" in code, (name or "").strip() or None)).fetchone()[0])
    _add_legs(conn, route_id, way["edges"])
    if _graph(conn, route_id)["shape"] != "line":
        raise Invalid("that way does not come out as one line")
    _event(conn, author, "route.made", route_id,
           {"code": code, "legs": way["stretches"], "laid_out_between": [
               s["place"] or [round(s["lon"], 5), round(s["lat"], 5)] for s in way["stops"]]})
    return route_id


def lay_out_again(conn, route_id: str, version: int, author: str,
                  points: list[tuple[float, float]], reach_m: float | None = None) -> None:
    """Replace the stretches of a route by the shortest way through the points.

    The route stays the same route: its code, name and what is said about it
    are kept, as is what a stretch said for itself where the stretch remains.
    """
    _route(conn, route_id, version)
    way = _lay(conn, author, points, reach_m)
    said = dict(conn.execute("DELETE FROM core.route_leg WHERE route_id = %s "
                             "RETURNING edge_id::text, maintainers", (route_id,)).fetchall())
    _add_legs(conn, route_id, way["edges"])
    for edge in way["edges"]:
        if said.get(edge) is not None:
            conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s AND edge_id = %s",
                         (said[edge], route_id, edge))
    if _graph(conn, route_id)["shape"] != "line":
        raise Invalid("that way does not come out as one line")
    _event(conn, author, "route.laid_out_again", route_id,
           {"from_legs": len(said), "to_legs": len(way["edges"]),
            "kept": len(set(said) & set(way["edges"])), "metres": way["length_m"]})
    _touch(conn, route_id, author)


ROUND_TRIP = ("the route is a round trip, with no ends to lay it between: take a stretch out of it to open it, "
              "change it, and drop its end on its start again to close it")


def _in_order(conn, route_id: str) -> tuple[list[str], list[str]]:
    """A route that is one line, as its junctions in walking order and the stretches between them."""
    graph = _graph(conn, route_id)
    if graph["shape"] == "loop":
        raise Invalid(ROUND_TRIP)
    if graph["shape"] != "line":
        raise Invalid("the route must be one line with two ends for this: mend its shape first")
    legs = _rows(conn, """
        SELECT l.edge_id::text AS edge, r.a_node::text AS a, r.b_node::text AS b
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s AND r.a_node <> r.b_node""", {"id": route_id})
    at: dict[str, list] = {}
    for leg in legs:
        at.setdefault(leg["a"], []).append(leg)
        at.setdefault(leg["b"], []).append(leg)
    here, nodes, edges, used = graph["ends"][0], [graph["ends"][0]], [], set()
    while True:
        leg = next((g for g in at[here] if g["edge"] not in used), None)
        if leg is None:
            return nodes, edges
        used.add(leg["edge"])
        here = leg["b"] if leg["a"] == here else leg["a"]
        nodes.append(here)
        edges.append(leg["edge"])


def _piece_of(conn, route_id: str, end_node: str) -> tuple[list[str], list[str], list[str], int]:
    """The part of a route that ends at a junction: its junctions and stretches in order from its other end.

    For a route that is one line, that is all of it. For a route in pieces it is
    the piece with that loose end, so that an end can be moved to join it to
    another piece. Also the stretches of the other pieces, and how many pieces there are.
    """
    graph = _graph(conn, route_id)
    if graph["shape"] == "line":
        nodes, edges = _in_order(conn, route_id)
        if end_node not in (nodes[0], nodes[-1]):
            raise Invalid("that is not an end of the route")
        return (nodes, edges, [], 1) if nodes[-1] == end_node else (nodes[::-1], edges[::-1], [], 1)
    if graph["shape"] == "loop":
        raise Invalid(ROUND_TRIP)
    if graph["shape"] != "pieces":
        raise Invalid("the route must be one line with two ends for this: mend its shape first")
    piece = next((p for p in graph["pieces"] if end_node in p["ends"]), None)
    if not piece:
        raise Invalid("that is not an end of the route")
    if len(piece["ends"]) != 2:
        raise Invalid("that piece of the route has a fork: take the extra end out first")
    mine = {edge for edge, _ in piece["order"]}
    legs = [g for g in _rows(conn, """
        SELECT l.edge_id::text AS edge, r.a_node::text AS a, r.b_node::text AS b
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s ORDER BY l.seq""", {"id": route_id})]
    at: dict[str, list] = {}
    for leg in legs:
        if leg["edge"] in mine and leg["a"] != leg["b"]:
            at.setdefault(leg["a"], []).append(leg)
            at.setdefault(leg["b"], []).append(leg)
    here = next(n for n in piece["ends"] if n != end_node)
    nodes, edges, used = [here], [], set()
    while True:
        leg = next((g for g in at.get(here, []) if g["edge"] not in used), None)
        if leg is None:
            break
        used.add(leg["edge"])
        here = leg["b"] if leg["a"] == here else leg["a"]
        nodes.append(here)
        edges.append(leg["edge"])
    return nodes, edges, [g["edge"] for g in legs if g["edge"] not in mine], len(graph["pieces"])


def _relay(conn, route_id: str, edges: list[str], pieces: int = 1, round_trip: bool = False) -> None:
    """Give a route these stretches instead of the ones it had, keeping what a remaining stretch said for itself.

    It must come out as one line; a route that was in pieces, in no more pieces than it was. Or,
    where that is what was asked for, as a round trip: one line that ends where it starts.
    """
    if len(set(edges)) != len(edges):
        raise Invalid("that would walk the same stretch twice: a route is one line")
    if not edges:
        raise Invalid("that would leave the route with no stretches")
    said = dict(conn.execute("DELETE FROM core.route_leg WHERE route_id = %s "
                             "RETURNING edge_id::text, maintainers", (route_id,)).fetchall())
    _add_legs(conn, route_id, edges)
    for edge in edges:
        if said.get(edge) is not None:
            conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s AND edge_id = %s",
                         (said[edge], route_id, edge))
    graph = _graph(conn, route_id)
    if round_trip and graph["shape"] == "loop":
        return
    if graph["shape"] != "line" and not (graph["shape"] == "pieces" and len(graph["pieces"]) <= pieces):
        raise Invalid("the route would not come out as one line")


MAPPED_REACH_M = 150     # where an end is dropped, a mapped path or road this near is what is meant
STRAIGHT_M = 10_000      # as far as a way goes straight from one point to the next where nothing is mapped
HOP_M = 500              # two points this near go straight to each other where the mapped way goes far round
_KIND_OF = {"vegnett": "road", "fkb_wfs": "trail"}


def _draw_along(conn, author: str, found: list, from_node: str, from_xy: tuple | None,
                to_node: str, to_xy: tuple | None) -> list[str]:
    """New stretches of yours along a way over the mapped lines, between two junctions.

    One stretch for each run of the same kind of mapped line (paths, roads); a
    few metres of another kind do not make a stretch of their own. Each says
    which mapped lines it follows. The way is tied to the junctions' own spots
    where those are given.
    """
    runs: list[dict] = []
    for part, ref in found:
        if runs and (runs[-1]["source"] == ref[0] or _Paths.along(part) < 5):
            runs[-1]["coords"] += part
            runs[-1]["used"].add(ref)
        else:
            runs.append({"source": ref[0], "coords": list(part), "used": {ref}})
    if len(runs) > 1 and _Paths.along(runs[0]["coords"]) < 5:
        runs[1]["coords"], runs[1]["used"] = runs[0]["coords"] + runs[1]["coords"], runs[0]["used"] | runs[1]["used"]
        runs.pop(0)
    if not runs:
        return []
    if from_xy:
        runs[0]["coords"].insert(0, from_xy)
    if to_xy:
        runs[-1]["coords"].append(to_xy)
    made, here = [], from_node
    for i, run in enumerate(runs):
        tip = run["coords"][-1]
        there = to_node if i == len(runs) - 1 else _node_at(conn, f"SRID=25833;POINT({tip[0]:.2f} {tip[1]:.2f})")
        line = _line(conn, run["coords"])
        if here == there or line["planar"] < NODE_M:
            continue
        edge = _new_edge(conn, author, "ST_SetSRID(ST_GeomFromText(%(wkt)s), 25833)", {"wkt": line["wkt"]},
                         here, there, _KIND_OF.get(run["source"], "trail"), "drawn", None, None)
        for source, mapped in sorted(run["used"]):
            conn.execute(
                """INSERT INTO core.source_reference
                       (subject_type, subject_id, source_key, external_id, ref_type)
                   VALUES ('edge', %s, %s, %s, 'traced')""", (edge, source, mapped))
        made.append(edge)
        here = there
    return made


def _onward_mapped(conn, net: _Net, route_id: str, author: str, end_node: str, to: tuple, reach: float,
                   mapped_reach: float = MAPPED_REACH_M) -> dict:
    """Carry a route on from an end to a spot where no stretch leads: along the mapped paths and roads.

    The way becomes new stretches of yours, one for each run of paths and each
    run of roads, drawn along the mapped lines. It ends at a place's arrival
    point or a junction of yours if one lies where it was dropped, else on the
    mapped line nearest that spot.

    Where the mapped lines do not lead there either (none at the end, none at
    the spot, the two not joined, or joined only far round) and the spot is no
    further off than a hop, the route goes straight to it, as a way laid out
    between two points does: one stretch in the terrain, for a person to say
    what it is.
    """
    nodes, edges, rest, pieces = _piece_of(conn, route_id, end_node)
    a = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s", (end_node,)).fetchone()
    straight = math.dist(a, to)
    if straight > 30_000:
        raise Invalid("that is more than 30 km away: carry the route on in steps")
    paths = _Paths.near(conn, f"SRID=25833;LINESTRING({a[0]:.1f} {a[1]:.1f},{to[0]:.1f} {to[1]:.1f})",
                        margin=max(400.0, 0.75 * straight))
    start, end = paths.attach(a, REACH_M), paths.attach(to, mapped_reach)
    found = paths.parts(start, end) if start["tied"] and end["tied"] else None
    metres = sum(_Paths.along(part) for part, _ in found) if found else 0.0
    hop = not found or metres > 4 * straight + 500
    if hop and straight > HOP_M:
        further = f": that is further than the {HOP_M} m a route goes straight where nothing leads"
        if not start["tied"]:
            raise NoWay(f"no stretch leads there, and no mapped path or road lies within {REACH_M} m of that end "
                        f"of the route{further}")
        if not end["tied"]:
            raise NoWay(f"no stretch leads there, and no mapped path or road lies within {mapped_reach:.0f} m "
                        f"of where the end was dropped{further}. Over open ground or ice, lay the route out "
                        f"between points instead")
        if not found:
            raise NoWay("neither your stretches nor the mapped paths and roads join that end to where it was "
                        f"dropped{further}")
        raise Invalid(f"the mapped way there is {metres / 1000:.1f} km for {straight / 1000:.1f} km as the crow flies: "
                      f"carry the route on in steps")
    # Where it ends: an arrival point or a junction of yours at the spot, else the mapped line there.
    at = conn.execute("""
        SELECT n.id::text, ST_X(n.geom), ST_Y(n.geom) FROM core.node n, (SELECT ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25833) AS g) p
        WHERE n.status = 'active' AND n.id <> %(from)s AND ST_DWithin(n.geom, p.g, %(reach)s)
          AND (n.place_id IS NOT NULL OR EXISTS (
              SELECT 1 FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
              WHERE e.status = 'active' AND n.id IN (r.a_node, r.b_node) AND e.id <> ALL(%(fresh)s::uuid[])))
        ORDER BY n.place_id IS NULL, n.geom <-> p.g LIMIT 1""",
        {"x": to[0], "y": to[1], "from": end_node, "reach": max(reach, WAY_NODE_M), "fresh": list(net.fresh)}).fetchone()
    tip = (at[1], at[2]) if at else tuple(end["foot"]) if end["tied"] else to
    last = at[0] if at else _node_at(conn, f"SRID=25833;POINT({tip[0]:.2f} {tip[1]:.2f})")
    round_trip = last == nodes[0] and pieces == 1
    if hop:
        line = _line(conn, [tuple(a), tip])
        if last == end_node or line["planar"] < NODE_M:
            raise Invalid("the route already ends there")
        made = [_new_edge(conn, author, "ST_SetSRID(ST_GeomFromText(%(wkt)s), 25833)", {"wkt": line["wkt"]},
                          end_node, last, "off_trail", "drawn", None, None)]
        _relay(conn, route_id, edges + made + rest, pieces, round_trip)
        told = net.told(edges + made + rest, [nodes[0], last])
        told.update(cut_m=0, added=1, dropped=0, segments=[], mapped_m=0, straight_m=round(line["planar"]),
                    round_trip=round_trip, joined=pieces > 1 and _graph(conn, route_id)["shape"] == "line")
        return told
    if round_trip:
        # Back to its own start: by another way than it came, or it is no round trip.
        way = _line(conn, [c for part, _ in found for c in part])
        beside = conn.execute("""
            SELECT ST_Length(ST_Intersection(w.g, ST_Buffer(ST_Collect(r.geom), %s))) / nullif(ST_Length(w.g), 0)
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 (SELECT ST_GeomFromText(%s, 25833) AS g) w
            WHERE e.id = ANY(%s::uuid[]) GROUP BY w.g""", (BESIDE_M, way["wkt"], edges)).fetchone()
        if beside and (beside[0] or 0) > 0.5:
            raise Invalid("the shortest mapped way back to where the route starts is the way it came: carry it on "
                          "a bit at a time, the way round it is to go, and drop its end on its start last")
    made = _draw_along(conn, author, found, end_node, tuple(a), last, (at[1], at[2]) if at else None)
    here = last
    if not made:
        raise Invalid("the route already ends there")
    _relay(conn, route_id, edges + made + rest, pieces, round_trip)
    told = net.told(edges + made + rest, [nodes[0], here])
    told.update(cut_m=0, added=len(made), dropped=0, segments=[], mapped_m=round(metres), round_trip=round_trip,
                joined=pieces > 1 and _graph(conn, route_id)["shape"] == "line")
    return told


def _end(conn, route_id: str, author: str, end_node: str, lon: float, lat: float,
         reach_m: float | None, only: list[str] | None) -> dict:
    _piece_of(conn, route_id, end_node)     # refuses what is not an end
    (x, y), = _to_xy(conn, [(lon, lat)])
    ex, ey = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s", (end_node,)).fetchone()
    net = _Net(conn, author, [(ex, ey), (x, y)], only)
    reach = min(reach_m or WAY_NODE_M, 500)
    # With a reach, the spot was pointed at on a map: it means what lies under the pointer, as wide as that
    # is, and nothing further off. Without, it is a position somebody gave, and a line near it is meant.
    pointed = reach_m is not None
    try:
        # Finding the spot may divide a stretch. Where no way is found, that is not to stay.
        with conn.transaction():
            return _end_over_stretches(conn, net, route_id, end_node, x, y, reach, reach if pointed else None)
    except NoWay:
        return _onward_mapped(conn, net, route_id, author, end_node, (x, y), reach,
                              reach if pointed else MAPPED_REACH_M)


def _end_over_stretches(conn, net: _Net, route_id: str, end_node: str, x: float, y: float, reach: float,
                        far: float | None = None) -> dict:
    to = net.stop(x, y, reach, "where the end was dropped", far)
    # Again: finding the spot may have divided a stretch. Walked from the end that stays; of a
    # route in pieces, the piece that has this end, with the other pieces left as they are.
    nodes, edges, rest, pieces = _piece_of(conn, route_id, end_node)
    if to == end_node:
        # The end itself is the nearest junction with a stretch. Let go on another mark right there (an arrival
        # point beside it that no stretch leads to), that mark is what was meant: it is carried on to it.
        if conn.execute("""
                SELECT 1 FROM core.node n WHERE n.status = 'active' AND n.id <> %(end)s
                  AND ST_DWithin(n.geom, ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25833), %(near)s)
                  AND (n.place_id IS NOT NULL OR EXISTS (
                      SELECT 1 FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                      WHERE e.status = 'active' AND n.id IN (r.a_node, r.b_node)))""",
                {"end": end_node, "x": x, "y": y, "near": ALREADY_M}).fetchone():
            raise NoWay("no stretch leads to the mark the end was dropped on")
        raise Invalid("the route already ends there")
    net.load()
    round_trip = to == nodes[0] and pieces == 1
    if round_trip:                                   # dropped on its own start: carried on to there another way
        back, _ = net.shortest(to, {end_node}, frozenset(edges))
        keep, onward = edges, back[::-1]
    elif to in nodes:                                # dropped on the route itself: it is cut back to there
        keep, onward, joins = edges[:nodes.index(to)], [], to
    else:                                            # carried on from the nearest spot on the route
        back, joins = net.shortest(to, set(nodes), frozenset(rest))
        keep, onward = edges[:nodes.index(joins)], back[::-1]
    _relay(conn, route_id, keep + onward + rest, pieces, round_trip)
    told = net.told(keep + onward + rest, [nodes[0], to])
    told["round_trip"] = round_trip
    told.update(cut_m=round(sum(conn.execute(
        "SELECT ST_Length(r.geom) FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id "
        "WHERE e.id = %s", (e,)).fetchone()[0] for e in edges[len(keep):])),
        added=len(onward), dropped=len(edges) - len(keep),
        segments=sorted({net.fresh[e] for e in onward if e in net.fresh}), mapped_m=0,
        joined=pieces > 1 and _graph(conn, route_id)["shape"] == "line")
    return told


def _two_phase(conn, real: bool, run) -> dict:
    """Find out what a change of way would be in a transaction that is rolled back; then, if asked, do it."""
    with conn.transaction(force_rollback=True):
        planned = run(None)
    return run(planned["segments"]) if real else planned


def move_end(conn, route_id: str, version: int, author: str, end_node: str, lon: float, lat: float,
             reach_m: float | None = None, dry_run: bool = False) -> dict:
    """Move one end of a route to another spot, changing only which stretches the route uses.

    Dropped on the route itself, the route is cut back to there. Elsewhere, it
    is carried on to the spot along the shortest way from the nearest junction
    of the route, and what lay beyond that junction is left out. No stretch is
    moved and no other route is changed.

    Where no stretch of yours and no Turrutebasen segment leads to the spot,
    the route is carried on from that end along the mapped paths and roads,
    which become new stretches.

    A route in pieces has an end at each side of every gap. Moving one of them
    moves the end of its piece, and dropped on another piece it joins the two.

    Dropped on the route's own other end, the route is closed as a round trip:
    carried on to its start by another way than it came, and nothing is cut.
    """
    _route(conn, route_id, version)
    told = _two_phase(conn, not dry_run, lambda only: _end(
        conn, route_id, AUTOMATIC if only is None else author, end_node, lon, lat, reach_m, only))
    if not dry_run:
        _event(conn, author, "route.end_moved", route_id,
               {"to": told["stops"][-1]["place"] or [round(told["stops"][-1]["lon"], 5),
                                                     round(told["stops"][-1]["lat"], 5)],
                "added": told["added"], "dropped": told["dropped"], "metres": told["length_m"],
                **({"along_mapped_m": told["mapped_m"]} if told["mapped_m"] else {}),
                **({"straight_m": told["straight_m"]} if told.get("straight_m") else {}),
                **({"joined": True} if told.get("joined") else {}),
                **({"round_trip": True} if told.get("round_trip") else {})})
        _touch(conn, route_id, author)
    return {k: v for k, v in told.items() if k != "edges"}


JOINS_M = 30       # a mapped way this near the route has come back to it, where the way round is a wide one
ALREADY_M = 5      # a spot this near the route is on it
_GOES_THERE = "the route already goes there"


def _via_mapped(conn, net: _Net, route_id: str, author: str, to: tuple,
                mapped_reach: float = MAPPED_REACH_M) -> dict:
    """Send a route by way of a spot that no stretch leads to: along the mapped paths and roads.

    The spot lies on a mapped line. That line is followed from the spot in
    both directions, over the mapped lines, until each way meets the route.
    Between the two meeting points the route takes this way instead of its
    own, as new stretches; before and after it runs as it did. The stretches
    it leaves stay, for the routes that share them. How near the route a way
    must come to have met it follows from how far off the spot is: a way
    round of 30 m is as possible as one of 3 km.
    """
    nodes, edges = _in_order(conn, route_id)
    own: list = []      # the route's line from its first junction to its last
    for i, edge in enumerate(edges):
        coords, a_node = conn.execute(
            """SELECT ST_AsGeoJSON(r.geom)::json, r.a_node::text FROM core.edge e
               JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s""", (edge,)).fetchone()
        part = [tuple(c[:2]) for c in coords["coordinates"]]
        own += part if a_node == nodes[i] else part[::-1]
    far = math.dist(_between(own, _locate(own, to), _locate(own, to))[0], to)
    paths = _Paths.near(conn, f"SRID=25833;POINT({to[0]:.1f} {to[1]:.1f})", margin=max(600.0, 2.5 * far + 500))
    spot = paths.attach(to, mapped_reach)
    if not spot["tied"]:
        raise NoWay(f"no stretch leads there, and no mapped path or road lies within {mapped_reach:.0f} m "
                    f"of where the line was dropped")
    off = math.dist(_between(own, _locate(own, spot["foot"]), _locate(own, spot["foot"]))[0], spot["foot"])
    if off <= ALREADY_M:
        raise Invalid("the route already goes there")
    joins = min(JOINS_M, max(ALREADY_M, 0.5 * off))
    wkt = "LINESTRING(" + ",".join(f"{x:.2f} {y:.2f}" for x, y in own) + ")"
    # Where the mapped lines come to the route: looked for every 25 m along it, around the spot.
    total, goals = _Paths.along(own), set()
    for k in range(int(total // 25) + 1):
        at = _between(own, 25.0 * k, 25.0 * k)[0]
        if math.dist(at, spot["foot"]) <= max(600.0, 2.5 * far + 500):
            near = paths.locate(at, joins)
            if near:
                goals.add(paths.named(near)["name"])
    arms = []       # from the spot each way along its line: the way until it meets the route, and where
    for first in dict.fromkeys(paths.pieces[spot["piece"]][2:4]):
        found = paths.reach(spot, goals, first)
        if not found:
            continue
        # The way in steps of a few metres, to find where it first comes to the route.
        flat = []
        for n, (part, _) in enumerate(found):
            for p, q in itertools.pairwise(part):
                steps = max(1, int(math.dist(p, q) // 4))
                flat += [((p[0] + (q[0] - p[0]) * t / steps, p[1] + (q[1] - p[1]) * t / steps), n)
                         for t in range(1, steps + 1)]
        first_near = conn.execute("""
            SELECT min(i) FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY AS p(x, y, i)
            WHERE ST_DWithin(ST_MakePoint(p.x, p.y), ST_GeomFromText(%s), %s)""",
            ([float(p[0]) for p, _ in flat], [float(p[1]) for p, _ in flat], wkt, joins)).fetchone()[0]
        if first_near is None:
            continue
        meets, n = flat[first_near - 1]
        kept = [(part, ref) for part, ref in found[:n]]
        cut, ref = found[n]
        upto = [cut[0]]
        for p, q in itertools.pairwise(cut):        # the part of this line up to where it meets the route
            if abs(math.dist(p, meets) + math.dist(meets, q) - math.dist(p, q)) < 0.01:
                upto.append(meets)
                break
            upto.append(q)
        kept.append((upto, ref))
        arms.append({"parts": kept, "meets": meets, "at": _locate(own, meets)})
    if len(arms) < 2:
        raise NoWay("the mapped path or road there leads to the route in one direction only, or not at all: "
                    "it is no other way for the route to go")
    arms.sort(key=lambda arm: arm["at"])
    if arms[1]["at"] - arms[0]["at"] < 2 * joins:
        raise Invalid("the mapped paths and roads lead from there to the route at one spot only: "
                      "that would be there and back, not another way")
    # A junction where the way leaves the route and one where it comes back, dividing the stretches there.
    forks = []
    for arm in arms:
        lon, lat = conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                                arm["meets"]).fetchone()
        nodes, edges = _in_order(conn, route_id)
        on = conn.execute("""
            SELECT e.id::text FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE e.id = ANY(%s::uuid[]) ORDER BY r.geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 25833) LIMIT 1""",
            (edges, *arm["meets"])).fetchone()[0]
        forks.append(split(conn, on, author, lon, lat))
    nodes, edges = _in_order(conn, route_id)
    if nodes.index(forks[0]) >= nodes.index(forks[1]):
        raise Invalid("the mapped way there meets the route in the wrong order: carry it on in steps instead")
    middle = _node_at(conn, f"SRID=25833;POINT({spot['foot'][0]:.2f} {spot['foot'][1]:.2f})")
    xy = dict(conn.execute("SELECT id::text, ARRAY[ST_X(geom), ST_Y(geom)] FROM core.node WHERE id = ANY(%s::uuid[])",
                           (forks,)).fetchall())
    out = [(part[::-1], ref) for part, ref in arms[0]["parts"][::-1]]
    there = _draw_along(conn, author, out, forks[0], tuple(xy[forks[0]]), middle, None)
    back = _draw_along(conn, author, arms[1]["parts"], middle, None, forks[1], tuple(xy[forks[1]]))
    if not there or not back:
        raise Invalid("the route already goes there")
    head, tail = edges[:nodes.index(forks[0])], edges[nodes.index(forks[1]):]
    _relay(conn, route_id, head + there + back + tail)
    told = net.told(head + there + back + tail, [nodes[0], middle, nodes[-1]])
    metres = sum(_Paths.along(part) for arm in arms for part, _ in arm["parts"])
    told.update(added=len(there) + len(back), dropped=len(edges) - len(head) - len(tail), segments=[],
                mapped_m=round(metres))
    return told


def _via(conn, route_id: str, author: str, edge_id: str, lon: float, lat: float,
         reach_m: float | None, only: list[str] | None) -> dict:
    nodes, edges = _in_order(conn, route_id)
    if edge_id not in edges:
        raise Invalid("that stretch is not part of the route")
    i = edges.index(edge_id)
    before, after = nodes[i], nodes[i + 1]
    (x, y), = _to_xy(conn, [(lon, lat)])
    ends = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = ANY(%s::uuid[])",
                        ([before, after],)).fetchall()
    net = _Net(conn, author, [ends[0], (x, y), ends[-1]], only)
    reach, pointed = min(reach_m or WAY_NODE_M, 500), reach_m is not None      # as for an end that is moved
    try:
        # Finding the spot may divide a stretch. Where no way is found, that is not to stay.
        with conn.transaction():
            return _via_over_stretches(conn, net, route_id, before, after, x, y, reach, reach if pointed else None)
    except NoWay as first:
        try:
            return _via_mapped(conn, net, route_id, author, (x, y), reach if pointed else MAPPED_REACH_M)
        except NoWay as second:
            if str(first) == _GOES_THERE and "no mapped path or road lies within" in str(second):
                raise Invalid(_GOES_THERE) from None
            raise


def _via_over_stretches(conn, net: _Net, route_id: str, before: str, after: str, x: float, y: float,
                        reach: float, far: float | None = None) -> dict:
    to = net.stop(x, y, reach, "where the line was dropped", far)
    nodes, edges = _in_order(conn, route_id)        # again: finding the spot may have divided a stretch
    if to in nodes:
        # The nearest junction is one the route passes already. Let go right on it, there is nothing to do.
        # Let go beside it, what was pointed at may be a mapped path or road there: that is tried instead.
        off = conn.execute("SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25833)) FROM core.node "
                           "WHERE id = %s", (x, y, to)).fetchone()[0]
        if off > ALREADY_M:
            raise NoWay(_GOES_THERE)
        raise Invalid(_GOES_THERE)
    head, tail = edges[:nodes.index(before)], edges[nodes.index(after):]
    net.load()
    there = net.shortest(before, {to}, frozenset(head + tail))[0]
    back = net.shortest(to, {after}, frozenset(head + tail + there))[0]
    _relay(conn, route_id, head + there + back + tail)
    told = net.told(head + there + back + tail, [nodes[0], to, nodes[-1]])
    told.update(added=len(there) + len(back), dropped=len(edges) - len(head) - len(tail),
                segments=sorted({net.fresh[e] for e in there + back if e in net.fresh}), mapped_m=0)
    return told


def send_via(conn, route_id: str, version: int, author: str, edge_id: str, lon: float, lat: float,
             reach_m: float | None = None, dry_run: bool = False) -> dict:
    """Send a route by way of another spot: the stretch that was grabbed gives way to the way round.

    The route goes from the junction before the grabbed stretch to the spot,
    and from there to the junction after it, by the shortest way each time,
    without walking any stretch twice. No stretch is moved and no other route
    is changed.

    Where no stretch leads to the spot, the route goes there along the mapped
    paths and roads instead: it leaves its line where that way leaves it and
    comes back where that way comes back, and the way becomes new stretches.
    """
    _route(conn, route_id, version)
    told = _two_phase(conn, not dry_run, lambda only: _via(
        conn, route_id, AUTOMATIC if only is None else author, edge_id, lon, lat, reach_m, only))
    if not dry_run:
        _event(conn, author, "route.sent_via", route_id,
               {"via": told["stops"][1]["place"] or [round(told["stops"][1]["lon"], 5),
                                                    round(told["stops"][1]["lat"], 5)],
                "added": told["added"], "dropped": told["dropped"], "metres": told["length_m"],
                **({"along_mapped_m": told["mapped_m"]} if told["mapped_m"] else {})})
        _touch(conn, route_id, author)
    return {k: v for k, v in told.items() if k != "edges"}


def absorb(conn, route_id: str, other_id: str, author: str) -> None:
    """Take another route of yours into this one, as a further piece of it. The other is then no route.

    Its stretches keep who maintains them, and what it was made from now
    belongs to this route. The two pieces are left for a person to join: over
    a glacier, over water or on foot.
    """
    this, other = _route(conn, route_id), _route(conn, other_id)
    legs = conn.execute("SELECT edge_id::text, maintainers FROM core.route_leg WHERE route_id = %s ORDER BY seq",
                        (other_id,)).fetchall()
    have = {str(e) for (e,) in conn.execute("SELECT edge_id FROM core.route_leg WHERE route_id = %s",
                                            (route_id,))}
    _add_legs(conn, route_id, [e for e, _ in legs])
    for edge, said in legs:
        theirs = said if said is not None else other["maintainers"]
        if edge not in have and sorted(theirs) != sorted(this["maintainers"]):
            conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s AND edge_id = %s",
                         (sorted(theirs), route_id, edge))
    conn.execute("""
        INSERT INTO core.source_reference (subject_type, subject_id, source_key, external_id, snapshot_id,
                                           ref_type, detail)
        SELECT subject_type, %s, source_key, external_id, snapshot_id, ref_type, detail
        FROM core.source_reference WHERE subject_type = 'route' AND subject_id = %s""", (route_id, other_id))
    conn.execute("DELETE FROM core.route_leg WHERE route_id = %s", (other_id,))
    conn.execute("""UPDATE core.route SET status = 'retired', version = version + 1, updated_at = now()
                    WHERE id = %s""", (other_id,))
    identity.succeed(conn, "route", other_id, [route_id], "absorbed")
    _event(conn, author, "route.joined_into", other_id, {"into": this["code"]})
    _event(conn, author, "route.piece_added", route_id,
           {"candidate": other["code"], "legs": len(legs), "was_a_route_of_yours": True})
    _touch(conn, route_id, author)


def add_piece(conn, route_id: str, version: int, candidate_id: str, author: str) -> None:
    """Add another candidate's segments to the route, as a further piece of it.

    Where the candidate is a route of yours already (two parts of one code, made
    separately), that route is taken into this one instead.
    """
    _route(conn, route_id, version)
    candidate = _candidate(conn, candidate_id)
    owner = owner_of(conn, candidate_id)
    if owner and str(owner) == route_id:
        raise Conflict("this candidate is already part of the route")
    if owner:
        absorb(conn, route_id, str(owner), author)
        return
    _refer(conn, route_id, candidate)
    legs = _add_legs(conn, route_id, _edges_of(conn, candidate_id, author))
    _event(conn, author, "route.piece_added", route_id,
           {"candidate": candidate["label"], "legs": legs})
    _inherit(conn, route_id, candidate_id, author)
    _touch(conn, route_id, author)


def add_crossing(conn, route_id: str, version: int, from_node: str, to_node: str, kind: str,
                 author: str) -> str:
    """Join two nodes of the route with a crossing: a straight line to begin with.

    kind: water (boat, never counted as walking distance), glacier (walked and
    signed, never marked), other, or connector (a short walk between two ends).
    """
    _route(conn, route_id, version)
    if kind not in CROSSINGS:
        raise Invalid(f"a crossing is one of {', '.join(CROSSINGS)}")
    on_route = _graph(conn, route_id)["at"]
    if from_node == to_node or from_node not in on_route or to_node not in on_route:
        raise Invalid("a crossing joins two different points of the route")
    metres = round(math.dist(on_route[from_node], on_route[to_node]))
    # A crossing of that kind between the same two junctions, which another route uses: the ice is crossed
    # once, by both. A second one would lie on top of the first, and the two would be signed apart.
    there = conn.execute("""
        SELECT e.id::text FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.status = 'active' AND r.kind = %(kind)s
          AND (r.a_node, r.b_node) IN ((%(a)s::uuid, %(b)s::uuid), (%(b)s::uuid, %(a)s::uuid))
          AND NOT EXISTS (SELECT 1 FROM core.route_leg l WHERE l.route_id = %(route)s AND l.edge_id = e.id)
        ORDER BY e.created_at LIMIT 1""", {"kind": kind, "a": from_node, "b": to_node, "route": route_id}).fetchone()
    if there:
        _add_legs(conn, route_id, [there[0]])
        _event(conn, author, "route.crossing_added", route_id, {"kind": kind, "metres": metres, "shared": there[0]})
        _touch(conn, route_id, author)
        return there[0]
    marked, signed = _FACTS[kind]
    edge = _new_edge(
        conn, author,
        """(SELECT ST_MakeLine(a.geom, b.geom) FROM core.node a, core.node b
            WHERE a.id = %(a)s AND b.id = %(b)s)""", {}, from_node, to_node, kind, "drawn",
        marked, signed)
    _add_legs(conn, route_id, [edge])
    _event(conn, author, "route.crossing_added", route_id, {"kind": kind, "metres": metres})
    _touch(conn, route_id, author)
    return edge


def take_away(conn, edge_id: str, author: str) -> None:
    """Take away a stretch that was made here and that no route uses: a crossing made twice, a line drawn and left.

    A stretch a route runs along is taken out of the route first. One that
    came from Turrutebasen is not taken away: what is wrong with it is told
    to Kartverket. The stretch is kept in the history, and can be undone.
    """
    found = conn.execute("""
        SELECT e.status, round(ST_Length(r.geom))::int, r.kind,
               (SELECT string_agg(t.code, ', ' ORDER BY t.code) FROM core.route_leg l
                JOIN core.route t ON t.id = l.route_id AND t.status = 'active' WHERE l.edge_id = e.id),
               EXISTS (SELECT 1 FROM core.source_reference sr WHERE sr.subject_type = 'edge' AND sr.subject_id = e.id
                       AND sr.source_key = 'turrutebasen')
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id::text = %s FOR UPDATE OF e""", (edge_id,)).fetchone()
    if not found or found[0] != "active":
        raise NotFound("no such stretch")
    _, metres, kind, used_by, theirs = found
    if used_by:
        raise Conflict(f"{used_by} {'run' if ', ' in used_by else 'runs'} along that stretch: take it out of the route first")
    if theirs:
        raise Invalid("that stretch came from Turrutebasen: it is not taken away here")
    conn.execute("UPDATE core.edge SET status = 'retired', version = version + 1 WHERE id = %s", (edge_id,))
    _event(conn, author, "edge.taken_away", edge_id, {"kind": kind, "metres": metres}, subject="edge")


def remove_leg(conn, route_id: str, version: int, edge_id: str, author: str) -> None:
    """Take a leg out of the route. The edge stays; other routes may use it.

    Not its only leg: a route with no stretches is no route, and is withdrawn instead.
    """
    route = _route(conn, route_id, version)
    if conn.execute("SELECT count(*) FROM core.route_leg WHERE route_id = %s", (route_id,)).fetchone()[0] == 1:
        raise Invalid(f"that is the only stretch of {route['code']}: withdraw the route instead")
    gone = conn.execute("DELETE FROM core.route_leg WHERE route_id = %s AND edge_id = %s",
                        (route_id, edge_id)).rowcount
    if not gone:
        raise NotFound("the route has no such leg")
    _reorder(conn, route_id)
    _event(conn, author, "route.leg_removed", route_id, {"edge_id": edge_id})
    _touch(conn, route_id, author)


GRADINGS = ("G", "B", "R", "S", "I")     # green, blue, red, black, not graded: as Turrutebasen has them


def named_after(shape: str, places: list[str | None]) -> str | None:
    """What a route with no name of its own is called: the places at its two ends, when it is one line between two."""
    return " – ".join(places) if shape == "line" and all(places) and places[0] != places[1] else None


def name_from_ends(conn, route_id: str) -> str | None:
    """The name a route gets from the places at its ends, worked out without the rest of what is said of it."""
    graph = _graph(conn, route_id)
    place = dict(conn.execute("""
        SELECT n.id::text, p.name FROM core.node n
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%s::uuid[])""", (graph["ends"],)).fetchall())
    return named_after(graph["shape"], [place.get(n) for n in graph["ends"]])


def called(conn, route_ids: list[str]) -> dict[str, str | None]:
    """What each of some routes is called: the name it was given, else the one its end places give."""
    found = {}
    for route_id, name in conn.execute("SELECT id::text, name FROM core.route WHERE id = ANY(%s::uuid[]) "
                                       "AND status = 'active'", (route_ids,)).fetchall():
        found[route_id] = name or name_from_ends(conn, route_id)
    return found


def grading_of(route: dict) -> tuple[str | None, str | None]:
    """The grading of a route and where it is from: set (by a person, or taken from Turrutebasen), or worked
    out from its length and climb where none is set. "Not graded" (I) is nobody having set one."""
    if route["grading"] and route["grading"] != "I":
        return route["grading"], "set"
    if route["grading_by_numbers"]:
        return route["grading_by_numbers"], "length and climb"
    return route["grading"], "set" if route["grading"] else None


def update(conn, route_id: str, version: int, author: str, *, code: str | None = None,
           name: str | None = None, code_provisional: bool | None = None,
           maintainers: list[str] | None = None, everywhere: bool = False,
           grading: str | None = None, description: str | None = None, utno_id: int | None = None) -> None:
    """Change what is said about the route as a whole: code, name, who maintains it, grading, description.

    An empty name goes back to naming the route after the places at its ends.
    Who maintains it holds for every stretch that does not say otherwise; with
    everywhere, the stretches that do say otherwise are brought into line too.
    An empty grading means none is given. `utno_id` is the number of the
    route's page on UT.no, 0 to say it has none; a description given together
    with it is recorded as taken from there.
    """
    route = _route(conn, route_id, version)
    if utno_id is not None and (utno_id or None) != route["utno_id"]:
        conn.execute("UPDATE core.route SET utno_id = %s WHERE id = %s", (utno_id or None, route_id))
        _event(conn, author, "route.on_utno", route_id, {"from": route["utno_id"], "to": utno_id or None})
    if maintainers is not None:
        to = sorted({m.strip() for m in maintainers if m.strip()})
        cleared = conn.execute("""UPDATE core.route_leg SET maintainers = NULL
                                  WHERE route_id = %s AND maintainers IS NOT NULL
                                    AND (%s OR maintainers = %s)""",
                               (route_id, everywhere, to)).rowcount
        if to != sorted(route["maintainers"]) or cleared:
            conn.execute("UPDATE core.route SET maintainers = %s WHERE id = %s", (to, route_id))
            _event(conn, author, "route.maintainers_changed", route_id,
                   {"from": sorted(route["maintainers"]), "to": to, "exceptions_dropped": cleared})
    if grading is not None and (grading or None) != route["grading"]:
        if grading and grading not in GRADINGS:
            raise Invalid(f"a grading is one of {', '.join(GRADINGS)}")
        conn.execute("UPDATE core.route SET grading = %s WHERE id = %s", (grading or None, route_id))
        _event(conn, author, "route.grading_changed", route_id,
               {"from": route["grading"], "to": grading or None})
    if description is not None and (description.strip() or None) != route["description"]:
        conn.execute("UPDATE core.route SET description = %s WHERE id = %s", (description.strip() or None, route_id))
        _event(conn, author, "route.described", route_id,
               {"from": route["description"], "to": description.strip() or None,
                **({"taken_from": "ut.no"} if utno_id else {})})
    if code is not None and code.strip() != route["code"]:
        code = code.strip()
        if not code:
            raise Invalid("a route needs a code")
        if conn.execute(
                """SELECT 1 FROM core.route WHERE lower(code) = lower(%s) AND status = 'active'
                   AND id <> %s""", (code, route_id)).fetchone():
            raise Conflict(f"the code {code} is already used by another of your routes")
        conn.execute("UPDATE core.route SET code = %s, code_provisional = %s WHERE id = %s",
                     (code, "/" in code, route_id))
        identity.remember(conn, "route", route_id, "code", route["code"])
        _event(conn, author, "route.code_changed", route_id, {"from": route["code"], "to": code})
    if code_provisional is not None:
        conn.execute("UPDATE core.route SET code_provisional = %s WHERE id = %s",
                     (code_provisional, route_id))
    if name is not None and (name.strip() or None) != route["name"]:
        conn.execute("UPDATE core.route SET name = %s WHERE id = %s",
                     (name.strip() or None, route_id))
        identity.remember(conn, "route", route_id, "name", route["name"])
        _event(conn, author, "route.renamed", route_id,
               {"from": route["name"], "to": name.strip() or None})
    _touch(conn, route_id, author)


def set_leg(conn, route_id: str, version: int, edge_id: str, author: str,
            maintainers: list[str] | None) -> None:
    """Say who maintains one stretch of the route, where that is not who maintains the route.

    None, or the same as the route says, takes the exception away again.
    """
    route = _route(conn, route_id, version)
    to = None if maintainers is None else sorted({m.strip() for m in maintainers if m.strip()})
    if to == sorted(route["maintainers"]):
        to = None
    was = conn.execute("SELECT maintainers FROM core.route_leg WHERE route_id = %s AND edge_id = %s",
                       (route_id, edge_id)).fetchone()
    if not was:
        raise NotFound("the route has no such stretch")
    if was[0] != to:
        conn.execute("UPDATE core.route_leg SET maintainers = %s WHERE route_id = %s AND edge_id = %s",
                     (to, route_id, edge_id))
        _event(conn, author, "route.stretch_maintainers", route_id,
               {"edge_id": edge_id, "from": was[0], "to": to})
        _touch(conn, route_id, author)


def retire(conn, route_id: str, author: str) -> None:
    """Withdraw the route. Its edges and nodes stay, for other routes; its candidates are free again."""
    _route(conn, route_id)
    conn.execute("""UPDATE core.route SET status = 'retired', version = version + 1,
                    updated_at = now() WHERE id = %s""", (route_id,))
    _event(conn, author, "route.retired", route_id, {})


_LEN = "ST_Length(ST_Transform(r.geom, 4326)::geography)"


def doubles(conn, route_id: str) -> list[dict]:
    """Where a route runs along more than one stretch between the same two junctions: each such place.

    Turrutebasen has a few of these, two segments drawn for one bit of path.
    Of each set, the stretches are given with the one to keep first: the one
    that most of your other routes use, then the shorter.
    """
    legs = _rows(conn, f"""
        SELECT l.edge_id::text AS edge, least(r.a_node, r.b_node)::text AS a, greatest(r.a_node, r.b_node)::text AS b,
               round({_LEN})::int AS metres,
               (SELECT count(*) FROM core.route_leg o JOIN core.route t ON t.id = o.route_id AND t.status = 'active'
                WHERE o.edge_id = l.edge_id AND o.route_id <> l.route_id)::int AS others,
               round(ST_X(ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326))::numeric, 6)::float8 AS lon,
               round(ST_Y(ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326))::numeric, 6)::float8 AS lat
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s AND r.a_node <> r.b_node ORDER BY l.seq""", {"id": route_id})
    between: dict[tuple, list[dict]] = {}
    for leg in legs:
        between.setdefault((leg["a"], leg["b"]), []).append(leg)
    return [{"stretches": sorted(same, key=lambda g: (-g["others"], g["metres"], g["edge"])),
             "lon": same[0]["lon"], "lat": same[0]["lat"]}
            for same in between.values() if len(same) > 1]


def _twigs(conn, route_id: str) -> list[dict]:
    """For a route with more than two ends: each end with the stretches from it in to the fork.

    Taking the stretches of one twig out leaves the route with one end fewer.
    """
    legs = _rows(conn, f"""
        SELECT l.edge_id::text AS edge, r.a_node::text AS a, r.b_node::text AS b, {_LEN} AS metres
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s AND r.a_node <> r.b_node""", {"id": route_id})
    touching: dict[str, list[dict]] = {}
    for leg in legs:
        touching.setdefault(leg["a"], []).append(leg)
        touching.setdefault(leg["b"], []).append(leg)
    twigs = []
    for end in (n for n, at in touching.items() if len(at) == 1):
        here, edges, metres, came = end, [], 0.0, None
        while len(touching[here]) <= 2:
            leg = next((g for g in touching[here] if g is not came), None)
            if leg is None:
                break       # the other end of a plain line
            edges.append(leg["edge"])
            metres += leg["metres"]
            here, came = (leg["b"] if leg["a"] == here else leg["a"]), leg
        twigs.append({"end": end, "metres": round(metres), "edges": edges})
    return sorted(twigs, key=lambda t: t["metres"])


def detail(conn, route_id: str) -> dict:
    found = _rows(conn, """SELECT id, code, code_provisional, name, version, reviewed,
                                  maintainers AS maintained_by, grading, special_type,
                                  discontinued_since, discontinued_why, description, unpublished_why, utno_id
                           FROM core.route WHERE id = %(id)s AND status = 'active'""",
                  {"id": route_id})
    if not found:
        raise NotFound("no such route")
    route = found[0]
    since, why = route.pop("discontinued_since"), route.pop("discontinued_why")
    route["discontinued"] = {"since": since, "why": why} if since else None
    kept_back = route.pop("unpublished_why")
    route["unpublished"] = {"why": kept_back} if kept_back else None
    graph = _graph(conn, route_id)
    # Water is travelled, not walked: it joins the route but is not in its walking distance.
    route["length"] = _rows(conn, f"""
        SELECT round(coalesce(sum(m) FILTER (WHERE kind <> 'water'), 0))::int AS on_foot_m,
               round(coalesce(sum(m) FILTER (WHERE kind = 'water'), 0))::int AS water_m,
               round(coalesce(sum(m) FILTER (WHERE kind = 'glacier'), 0))::int AS glacier_m,
               round(coalesce(sum(m) FILTER (WHERE kind <> 'water' AND marked IS FALSE), 0))::int
                   AS unmarked_m,
               count(*)::int AS legs
        FROM (SELECT r.kind, r.marked, {_LEN} AS m
              FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
              JOIN core.edge_revision r ON r.id = e.current_revision_id
              WHERE l.route_id = %(id)s) x""", {"id": route_id})[0]
    # Who maintains how much of it: the route's own word, and each stretch that says otherwise.
    route["maintainers"] = _rows(conn, f"""
        SELECT m AS name, round(sum({_LEN}))::int AS length_m
        FROM core.route_leg l JOIN core.route t ON t.id = l.route_id
        JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             unnest(coalesce(l.maintainers, t.maintainers)) AS m
        WHERE l.route_id = %(id)s AND r.kind <> 'water' GROUP BY 1 ORDER BY 2 DESC, 1""",
        {"id": route_id})
    route["exceptions"] = _rows(conn, f"""
        SELECT l.edge_id::text AS edge_id, l.maintainers, round({_LEN})::int AS metres
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(id)s AND l.maintainers IS NOT NULL ORDER BY l.seq""", {"id": route_id})
    route["shape"], route["pieces"] = graph["shape"], len(graph["pieces"])
    piece_of = {n: i for i, p in enumerate(graph["pieces"]) for n in p["nodes"]}
    ends = _rows(conn, """
        SELECT n.id::text AS id, p.name AS place, p.id AS place_id,
               round(ST_X(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lon,
               round(ST_Y(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lat
        FROM core.node n LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = ANY(%(ids)s::uuid[])""", {"ids": graph["ends"]})
    by_id = {e["id"]: e for e in ends}
    for e in ends:      # an end at no place: what a place there would be called, if something there has a name
        after = None if e["place"] else suggest(conn, e["id"])
        e["near"] = after["name"] if after else None
    route["ends"] = [{**by_id[n], "piece": piece_of[n]} for n in graph["ends"]]
    # A round trip has no ends: it starts and ends at one junction.
    route["round_trip"] = _rows(conn, """
        SELECT n.id::text AS id, p.name AS place, p.id AS place_id,
               round(ST_X(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lon,
               round(ST_Y(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lat
        FROM core.node n LEFT JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed'
        WHERE n.id = %(id)s""", {"id": graph["starts"]})[0] if graph["starts"] else None
    # Every node: where one stretch stops and the next begins.
    route["nodes"] = _rows(conn, """
        SELECT n.id::text AS id, round(ST_X(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lon,
               round(ST_Y(ST_Transform(n.geom, 4326))::numeric, 6)::float8 AS lat
        FROM core.node n WHERE n.id = ANY(%(ids)s::uuid[])""", {"ids": list(graph["at"])})
    # Named after the places at its two ends, unless named by hand.
    route["generated_name"] = named_after(graph["shape"], [e["place"] for e in route["ends"]])
    # For a route in pieces: the closest pair of loose ends between neighbouring pieces.
    at, gaps = graph["at"], []
    for i in range(len(graph["pieces"]) - 1):
        pairs = [(math.dist(at[a], at[b]), a, b) for a in graph["pieces"][i]["ends"] or
                 graph["pieces"][i]["nodes"] for b in graph["pieces"][i + 1]["ends"] or
                 graph["pieces"][i + 1]["nodes"]]
        metres, a, b = min(pairs)
        gaps.append({"from_node": a, "to_node": b, "metres": round(metres)})
    route["gaps"] = gaps
    route["twigs"] = _twigs(conn, route_id) if graph["shape"] == "branched" else []
    route["doubles"] = doubles(conn, route_id) if graph["shape"] == "doubled" else []
    route["sources"] = _rows(conn, """
        SELECT sr.external_id AS code, sr.detail->>'label' AS label, sr.detail->>'name' AS name,
               sr.detail->>'candidate_id' AS candidate_id
        FROM core.source_reference sr WHERE sr.subject_type = 'route' AND sr.subject_id = %(id)s
        ORDER BY sr.id""", {"id": route_id})
    route["geometry"] = conn.execute("""
        SELECT ST_AsGeoJSON(ST_Transform(ST_Collect(r.geom), 4326), 6)::json
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s""", (route_id,)).fetchone()[0]
    route["bbox"] = conn.execute("""
        SELECT ARRAY[ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b)]
        FROM (SELECT Box2D(ST_Transform(ST_Collect(r.geom), 4326)) AS b
              FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
              JOIN core.edge_revision r ON r.id = e.current_revision_id
              WHERE l.route_id = %s) x""", (route_id,)).fetchone()[0]
    route["earlier"] = identity.earlier(conn, "route", route_id)
    # What it climbs, walked from its first end, as far as heights are known for its line.
    route["height"] = height.of_route(conn, route_id)
    # The grading its numbers give, the harder of the two ways. It stands in where nobody has set one.
    route["grading_by_numbers"] = (height.harder(route["height"]["there"]["at_least"], route["height"]["back"]["at_least"])
                                   if route["height"]["there"] else None)
    route["history"] = _rows(conn, """
        SELECT at, author, type, payload FROM history.event
        WHERE subject_type = 'route' AND subject_id = %(id)s ORDER BY seq DESC LIMIT 30""",
        {"id": route_id})
    return route


# A stretch as the map shows it: one row per stretch that an active route of yours uses.
_ON_THE_MAP = """
    SELECT e.id::text AS edge, {geom} AS geom,
           jsonb_build_object('edge_id', e.id, 'kind', r.kind, 'marked', r.marked,
                              'signed', r.signed, 'codes', string_agg(rt.code, ', ' ORDER BY rt.code),
                              'route_id', min(rt.id::text),
                              'rev', r.rev_no,
                              'discontinued', bool_and(rt.discontinued_since IS NOT NULL),
                              'unpublished', bool_and(rt.unpublished_why IS NOT NULL),
                              'routes', jsonb_agg(jsonb_build_object('id', rt.id, 'code', rt.code,
                                  'in_use', rt.discontinued_since IS NULL AND rt.unpublished_why IS NULL)
                                                  ORDER BY length(rt.code), rt.code)) AS said
    FROM core.route_leg l JOIN core.route rt ON rt.id = l.route_id AND rt.status = 'active'
    JOIN core.edge e ON e.id = l.edge_id
    JOIN core.edge_revision r ON r.id = e.current_revision_id
    {where}
    GROUP BY e.id, r.id"""


def collection(conn, edges: list[str] | None = None) -> dict:
    """Every edge used by an active owned route, as GeoJSON lines for the map. Or only some of them, by id."""
    rows = conn.execute(_ON_THE_MAP.format(
        geom="ST_AsGeoJSON(ST_Transform(r.geom, 4326), 6)::json",
        where="" if edges is None else "WHERE e.id = ANY(%(e)s::uuid[])"), {"e": edges}).fetchall()
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": g, "properties": p} for _, g, p in rows]}


def marks(conn) -> dict[str, str]:
    """For every stretch on the map, a mark that is another when anything the map says of it has changed.

    A page that holds the lines asks for the marks after a change and reads
    anew only the stretches whose mark is new to it: its line (a new revision
    is a new mark), what it is, and which routes use it and what they are.
    """
    return dict(conn.execute(
        f"SELECT edge, md5(said::text) FROM ({_ON_THE_MAP.format(geom='NULL', where='')}) x").fetchall())
