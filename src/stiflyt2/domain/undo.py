"""Take a change back: your own latest, whatever it was.

Every change to a route, leg, node, edge or place leaves the rows as they were
in core.undo_log (domain migration d0008), grouped by the transaction that
made it: one request is one action. Undo puts the rows of an action back,
newest first, and can be repeated to go further back. It works the same for a
pulled line, joined junctions, an approved batch of proposals or a route made,
and after the page has been loaded again.

Whose latest: the one who asks, and made the way they ask (d0013). Undo in the
map takes back what that person did in the map; an assistant acting as the
same person takes back what the assistant did. What others have done since
stays. Where one of those later changes touches the same stretches or
junctions, taking the earlier one back would lose it: then nothing is done,
and it is said what stands in the way. From the command line, where nobody in
particular asks, it is the latest change of anybody's.

What is never taken away: events and edge revisions. The history shows the
change and that it was undone. A row the action added is marked `undone`.
"""
from __future__ import annotations

import json

from psycopg.rows import dict_row

from stiflyt2.domain.places import Conflict, current_changeset

# How an action is described, by the kinds of event it left.
_WHAT = {
    "route.made": "{code} made yours",
    "route.retired": "{code} withdrawn",
    "route.reviewed": "{code} said to be done",
    "route.discontinued": "{code} said to be discontinued",
    "route.unpublished": "{code} said not to be published",
    "route.published": "{code} published after all",
    "route.taken_up_again": "{code} said to be in use again",
    "route.reopened": "{code} said not to be done",
    "route.leg_removed": "a stretch taken out of {code}",
    "route.stretch_added": "{code} carried on along a stretch",
    "route.piece_added": "a part added to {code}",
    "route.joined_into": "{code} taken into another route",
    "route.crossing_added": "two ends of {code} joined",
    "route.laid_out_again": "{code} laid out again",
    "route.end_moved": "an end of {code} moved",
    "route.sent_via": "{code} sent another way",
    "route.code_changed": "the code of {code} changed",
    "route.renamed": "{code} renamed",
    "route.described": "the description of {code} changed",
    "route.on_utno": "the page of {code} on UT.no noted",
    "route.maintainers_changed": "who maintains {code} changed",
    "route.grading_changed": "the grading of {code} changed",
    "route.stretch_maintainers": "who maintains a stretch of {code} changed",
    "edge.bent": "a line pulled",
    "edge.snapped": "a stretch laid on a path or road",
    "route.laid_beside": "{code} laid on the paths and roads beside it",
    "edge.laid_beside": "a stretch laid on the path or road beside it",
    "edge.redrawn": "a stretch drawn by hand",
    "edge.restored": "a stretch given its earlier line",
    "edge.split": "a stretch divided",
    "edge.changed": "what a stretch is changed",
    "node.moved": "a junction moved",
    "node.merged": "two junctions joined",
    "place.adopted": "{name} made a place",
    "place.retired": "a place withdrawn",
    "place.renamed": "a place renamed",
    "place.reviewed": "a place reviewed",
    "place.access_point_removed": "an arrival point taken out of a place",
    "place.access_point_added": "an arrival point added to a place",
}
_STATUS = ("route", "node", "edge", "place")     # tables whose added rows are marked undone


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _describe(conn, txid: int) -> str:
    events = _rows(conn, """
        SELECT e.type, e.payload, (SELECT r.code FROM core.route r WHERE r.id = e.subject_id) AS code
        FROM history.event e WHERE e.txid = %(t)s ORDER BY e.seq""", {"t": txid})
    told = []
    for e in events:
        text = _WHAT.get(e["type"], e["type"].replace(".", " ").replace("_", " ")).format(
            code=e["code"] or e["payload"].get("code") or "a route", name=e["payload"].get("name") or "a spot")
        if text not in told:
            told.append(text)
    if not told:
        return "a change"
    return told[0] if len(told) == 1 else f"{told[0]}, and {len(told) - 1} more with it" if len(
        told) > 2 else f"{told[0]} and {told[1]}"


MAP = "viewer"      # how a change came in when nothing else is said
# Whose an action is, and how it came in. A row changed before the action's first event was written
# carries neither (a route withdrawn, a place made): then the event says it.
WHO = """coalesce((array_agg(u.author) FILTER (WHERE u.author IS NOT NULL))[1],
                  (SELECT e.author FROM history.event e WHERE e.txid = u.txid ORDER BY e.seq LIMIT 1))"""
HOW = """coalesce((array_agg(u.through) FILTER (WHERE u.through IS NOT NULL))[1],
                  (SELECT e.through FROM history.event e WHERE e.txid = u.txid ORDER BY e.seq LIMIT 1))"""


def whose(author) -> tuple[str, str] | None:
    """Whose changes an undo is about: the one asking and how they ask, when that is known. Else anybody's."""
    through = getattr(author, "through", None)
    return (str(author), through) if through else None


def last(conn, mine: tuple[str, str] | None = None) -> dict | None:
    """The latest action that has not been undone: what an undo would take back.

    Given whose (a name, and how the changes came in), the latest of theirs.
    """
    found = _rows(conn, f"""
        SELECT txid, at, author, through, rows FROM (
            SELECT u.txid, min(u.at) AS at, {WHO} AS author, coalesce({HOW}, %(map)s) AS through,
                   count(*)::int AS rows
            FROM core.undo_log u WHERE u.undone_at IS NULL GROUP BY u.txid) t
        WHERE %(who)s::text IS NULL OR (author = %(who)s AND through = %(how)s)
        ORDER BY txid DESC LIMIT 1""",
        {"map": MAP, "who": mine and mine[0], "how": mine and mine[1]})
    if not found:
        return None
    late = _in_the_way(conn, [found[0]["txid"]]) if mine else None
    return {**found[0], "what": _describe(conn, found[0]["txid"]),
            "in_the_way": _describe(conn, late) if late else None}


def _columns(conn, table: str) -> list[str]:
    return [c for (c,) in conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'core' AND table_name = %s ORDER BY ordinal_position""", (table,))]


def undo(conn, author: str, txid: int | None = None) -> dict:
    """Take the latest action back: of the one who asks, where that is known. Given a txid, only if that is it."""
    mine = whose(author)
    latest = last(conn, mine)
    if not latest:
        raise Conflict("there is nothing of yours to undo" if mine else "there is nothing to undo")
    if txid is not None and txid != latest["txid"]:
        raise Conflict("something else has been changed since: look again at what would be undone")
    if latest["in_the_way"]:
        raise Conflict(f"{latest['in_the_way']} came after, and touches the same stretches or junctions: it would "
                       "be lost. Take that back first, or put the route back from its own history")
    touched = _touched(conn, [latest["txid"]])
    _take_back(conn, author, latest["txid"], latest["what"])
    if mine:
        _nothing_left_hanging(conn, touched)
    return {"undone": latest["what"], "next": last(conn, mine)}


def _take_back(conn, author: str, txid: int, what: str) -> None:
    """Put the rows of one action back as they were before it."""
    latest = {"txid": txid, "what": what}
    rows = _rows(conn, """SELECT id, tbl, op, key, old FROM core.undo_log
                          WHERE txid = %(t)s AND undone_at IS NULL ORDER BY id DESC FOR UPDATE""",
                 {"t": latest["txid"]})
    changeset = current_changeset(conn, author)     # before the switch, so that nothing of ours is logged
    conn.execute("SELECT set_config('stiflyt2.undoing', 'on', true)")
    columns: dict[str, list[str]] = {}
    for row in rows:
        table, key = row["tbl"], row["key"]
        cols = columns.setdefault(table, _columns(conn, table))
        where = " AND ".join(f"t.{k} = %(k_{k})s" for k in key)
        params = {f"k_{k}": v for k, v in key.items()}
        if row["op"] == "I":
            if table in _STATUS:
                conn.execute(f"UPDATE core.{table} t SET status = 'undone' WHERE {where}", params)
            else:
                conn.execute(f"DELETE FROM core.{table} t WHERE {where}", params)
        elif row["op"] == "U":
            listed = ", ".join(cols)
            conn.execute(
                f"""UPDATE core.{table} t SET ({listed}) =
                        (SELECT {listed} FROM jsonb_populate_record(NULL::core.{table}, %(old)s::jsonb))
                    WHERE {where}""", {**params, "old": json.dumps(row["old"])})
        else:
            conn.execute(f"INSERT INTO core.{table} SELECT * FROM "
                         f"jsonb_populate_record(NULL::core.{table}, %(old)s::jsonb)",
                         {"old": json.dumps(row["old"])})
    conn.execute("UPDATE core.undo_log SET undone_at = now() WHERE txid = %s", (latest["txid"],))
    conn.execute("SELECT set_config('stiflyt2.undoing', 'off', true)")
    # The history keeps the change, and says that it was taken back.
    conn.execute("""
        INSERT INTO history.event (changeset_id, author, type, subject_type, subject_id, payload)
        SELECT DISTINCT ON (e.subject_type, e.subject_id) %s, %s, 'undone', e.subject_type, e.subject_id,
               jsonb_build_object('what', %s::text)
        FROM history.event e WHERE e.txid = %s""", (changeset, author, latest["what"], latest["txid"]))


MOST = 20       # as many of a route's latest changes are offered to go back over

# The changes that concern a route: to the route itself, to which stretches it uses, and to the
# stretches it uses or used. Read out of the rows each action left.
_OF_ROUTE = """
    WITH stretches AS (
        SELECT edge_id::text AS id FROM core.route_leg WHERE route_id = %(r)s::uuid
        UNION SELECT old->>'edge_id' FROM core.undo_log
              WHERE tbl = 'route_leg' AND key->>'route_id' = %(r)s::text AND old IS NOT NULL)
    SELECT u.txid, min(u.at) AS at, """ + WHO + """ AS author
    FROM core.undo_log u
    WHERE u.undone_at IS NULL AND ((u.tbl = 'route' AND u.key->>'id' = %(r)s::text)
          OR (u.tbl = 'route_leg' AND u.key->>'route_id' = %(r)s::text)
          OR (u.tbl = 'edge' AND u.key->>'id' IN (SELECT id FROM stretches)))
    GROUP BY u.txid ORDER BY u.txid DESC"""


def of_route(conn, route_id: str) -> list[dict]:
    """The actions that changed a route or its line and have not been taken back, newest first.

    Each with what stands in the way of taking it back together with those
    after it: a later change to something else that touches the same rows.
    """
    found = _rows(conn, _OF_ROUTE, {"r": str(route_id)})[:MOST]
    for i, f in enumerate(found):
        f["what"] = _describe(conn, f["txid"])
        f["with"] = i + 1       # how many actions go when this one and those after it are taken back
        late = _in_the_way(conn, [g["txid"] for g in found[:i + 1]])
        f["in_the_way"] = _describe(conn, late) if late else None
    return found


def _in_the_way(conn, txids: list[int]) -> int | None:
    """A later action, not among these, that changed a row one of these changed: the latest such, if any."""
    row = conn.execute("""
        SELECT max(o.txid) FROM core.undo_log s JOIN core.undo_log o ON o.tbl = s.tbl AND o.key = s.key
        WHERE s.txid = ANY(%(s)s) AND s.undone_at IS NULL AND o.undone_at IS NULL
          AND o.txid > s.txid AND o.txid <> ALL(%(s)s)""", {"s": txids}).fetchone()
    return row[0]


def back(conn, author: str, route_id: str, txid: int) -> dict:
    """Put a route back as it was before one of its changes: that change and every later one to it are taken back.

    Changes to other things made in between stay, whoever made them. Where
    one of them touches the same stretches or junctions, it would be lost or
    left hanging: then nothing is done, and it is said what stands in the way.
    """
    mine = _rows(conn, _OF_ROUTE, {"r": str(route_id)})
    if txid not in [m["txid"] for m in mine]:
        raise Conflict("that change is not among those that can be taken back for this route: look again")
    going = [m["txid"] for m in mine if m["txid"] >= txid]      # newest first
    late = _in_the_way(conn, going)
    if late:
        raise Conflict(f"{_describe(conn, late)} came after, and touches the same stretches or junctions: "
                       "take that back first")
    touched = _touched(conn, going)
    told = []
    for t in going:
        what = _describe(conn, t)
        _take_back(conn, author, t, what)
        told.append(what)
    _nothing_left_hanging(conn, touched)
    return {"undone": told, "next": last(conn, whose(author))}


def _touched(conn, txids: list[int]) -> list[dict]:
    return _rows(conn, "SELECT DISTINCT tbl, key->>'id' AS id FROM core.undo_log "
                       "WHERE txid = ANY(%(s)s) AND tbl IN ('edge', 'node')", {"s": txids})


def _nothing_left_hanging(conn, touched: list[dict]) -> None:
    """Refuse where a later change builds on what was just taken back: a stretch or junction that is now gone."""
    edges = [t["id"] for t in touched if t["tbl"] == "edge"]
    nodes = [t["id"] for t in touched if t["tbl"] == "node"]
    hanging = conn.execute("""
        SELECT (SELECT count(*) FROM core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
                JOIN core.edge e ON e.id = l.edge_id
                WHERE e.id = ANY(%(e)s::uuid[]) AND e.status = 'undone')
             + (SELECT count(*) FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
                JOIN core.node n ON n.id IN (v.a_node, v.b_node)
                WHERE e.status = 'active' AND n.id = ANY(%(n)s::uuid[]) AND n.status = 'undone')""",
        {"e": edges, "n": nodes}).fetchone()[0]
    if hanging:
        raise Conflict("a later change to another route uses a stretch or junction that this would take away: "
                       "take that back first")
