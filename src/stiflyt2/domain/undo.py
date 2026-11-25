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

What was taken back can be brought back (redo, d0016): when an action is
taken back, the rows as they stood are kept too. Redo is for the one who took
it back, for what they took back last, and in the order it was taken back in
reverse; a change they make after it takes its place, and then there is
nothing to bring back. To get out of a row of changes that went wrong, all of
one's latest changes down to one of them can be taken back in one go, and
brought back one by one.

Saving and discarding is on top of this. What a person has changed since
they last saved is an item of work: their open changeset (`item`). `save`
closes it; `discard` takes all of it back. Nothing is held back until it is
saved: a change is in the data, for everybody, the moment it is made.
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
    "edge.laid_on_track": "a stretch laid on a GPX track",
    "route.laid_beside": "{code} laid on the paths and roads beside it",
    "edge.laid_beside": "a stretch laid on the path or road beside it",
    "edge.redrawn": "a stretch drawn by hand",
    "edge.restored": "a stretch given its earlier line",
    "edge.split": "a stretch divided",
    "edge.changed": "what a stretch is changed",
    "edge.taken_away": "a stretch no route uses taken away",
    "node.moved": "a junction moved",
    "node.merged": "two junctions joined",
    "place.merged": "two places made one",
    "place.merged_into": "two places made one",
    "place.adopted": "{name} made a place",
    "place.retired": "a place withdrawn",
    "place.renamed": "a place renamed",
    "place.reviewed": "a place reviewed",
    "place.moved": "the building mark of a place moved",
    "place.kind_changed": "the kind of a place changed",
    "place.primary_changed": "another arrival point of a place made the primary one",
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


def _standing(conn, mine: tuple[str, str] | None, most: int) -> list[dict]:
    """The latest actions that have not been taken back, newest first: anybody's, or those of one person and way in."""
    return _rows(conn, f"""
        SELECT txid, at, author, through, rows FROM (
            SELECT u.txid, min(u.at) AS at, {WHO} AS author, coalesce({HOW}, %(map)s) AS through,
                   count(*)::int AS rows
            FROM core.undo_log u WHERE u.undone_at IS NULL GROUP BY u.txid) t
        WHERE %(who)s::text IS NULL OR (author = %(who)s AND through = %(how)s)
        ORDER BY txid DESC LIMIT %(most)s""",
        {"map": MAP, "who": mine and mine[0], "how": mine and mine[1], "most": most})


def last(conn, mine: tuple[str, str] | None = None) -> dict | None:
    """The latest action that has not been undone: what an undo would take back.

    Given whose (a name, and how the changes came in), the latest of theirs.
    """
    found = _standing(conn, mine, 1)
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
    return {"undone": latest["what"], "next": last(conn, mine), "again": undone_last(conn, mine)}


def _image(conn, table: str, cols: list[str], where: str, params: dict):
    """A row as it stands, as the log keeps rows: with its geometry as text, which comes back exactly."""
    geom = " || jsonb_build_object('geom', t.geom::text)" if "geom" in cols else ""
    row = conn.execute(f"SELECT to_jsonb(t){geom} FROM core.{table} t WHERE {where}", params).fetchone()
    return row[0] if row else None


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
        if row["op"] != "D":        # the row as it stands, to bring the action back with
            now = _image(conn, table, cols, where, params)
            conn.execute("UPDATE core.undo_log SET new = %s::jsonb WHERE id = %s",
                         (json.dumps(now) if now is not None else None, row["id"]))
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
    conn.execute("UPDATE core.undo_log SET undone_at = now(), undone_by = %s, undone_through = %s WHERE txid = %s",
                 (str(author), getattr(author, "through", None), latest["txid"]))
    conn.execute("SELECT set_config('stiflyt2.undoing', 'off', true)")
    # The history keeps the change, and says that it was taken back.
    conn.execute("""
        INSERT INTO history.event (changeset_id, author, type, subject_type, subject_id, payload)
        SELECT DISTINCT ON (e.subject_type, e.subject_id) %s, %s, 'undone', e.subject_type, e.subject_id,
               jsonb_build_object('what', %s::text)
        FROM history.event e WHERE e.txid = %s""", (changeset, author, latest["what"], latest["txid"]))


MOST = 20       # as many of a route's latest changes are offered to go back over
SITTING = 100   # as many changes as one sitting can hold and still be discarded as a whole

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
    return {"undone": told, "next": last(conn, whose(author)), "again": undone_last(conn, whose(author))}


def recent(conn, mine: tuple[str, str] | None = None) -> list[dict]:
    """Your latest changes that stand, newest first: what can be taken back in one go, down to any of them.

    Each with how many changes go with it, and what stands in the way: a later
    change of somebody else's, or of yours made another way, to the same
    stretches or junctions.
    """
    found = _standing(conn, mine, MOST)
    for i, f in enumerate(found):
        f["what"] = _describe(conn, f["txid"])
        f["with"] = i + 1
        late = _in_the_way(conn, [g["txid"] for g in found[:i + 1]])
        f["in_the_way"] = _describe(conn, late) if late else None
    return found


def _open_item(conn, who: str, how: str):
    return conn.execute("""SELECT id FROM core.changeset WHERE author = %s AND coalesce(through, '') = %s AND state = 'open'
                           ORDER BY created_at DESC LIMIT 1""", (who, how)).fetchone()


def item(conn, mine: tuple[str, str]) -> dict:
    """The item of work a person has open, one way in: what they have changed since they last saved, and stands.

    Every change is in the data the moment it is made; the item is the open
    changeset it was made in (d0017). Saving closes it, discarding takes all
    of it back. With the routes it changed, and what stands in the way of
    discarding it: a later change by somebody else, or by the same person
    another way in, to the same stretches or junctions.
    """
    found = _open_item(conn, mine[0], mine[1])
    if not found:
        return {"changes": [], "in_the_way": None, "routes": []}
    made = {t for (t,) in conn.execute(
        "SELECT DISTINCT txid FROM history.event WHERE changeset_id = %s AND type NOT IN ('undone', 'redone')",
        (found[0],))}
    # (Where no way in is known, from the command line, the changeset alone says whose they are.)
    changes = [f for f in _standing(conn, mine if mine[1] else None, SITTING) if f["txid"] in made]
    for f in changes:
        f["what"] = _describe(conn, f["txid"])
    txids = [f["txid"] for f in changes]
    late = _in_the_way(conn, txids) if txids else None
    routes = _rows(conn, """
        SELECT DISTINCT r.id::text AS id, r.code FROM history.event e JOIN core.route r ON r.id = e.subject_id
        WHERE e.changeset_id = %(c)s AND e.subject_type = 'route' AND e.txid = ANY(%(t)s) AND r.status = 'active'
        ORDER BY r.code""", {"c": found[0], "t": txids})
    return {"changes": changes, "in_the_way": _describe(conn, late) if late else None, "routes": routes}


def save(conn, author: str) -> dict:
    """Say that the item of work is to stay: it is closed, and named after what it changed. The next change opens another."""
    mine = whose(author) or (str(author), "")
    found = _open_item(conn, mine[0], mine[1])
    if not found:
        return {"saved": 0}
    now = item(conn, mine)
    n = len(now["changes"])
    title = (", ".join(r["code"] for r in now["routes"][:6]) or "Changes") + f": {n} change{'' if n == 1 else 's'}"
    conn.execute("UPDATE core.changeset SET state = 'closed', closed_at = now(), title = %s WHERE id = %s",
                 (title, found[0]))
    return {"saved": n, "title": title}


def discard(conn, author: str) -> dict:
    """Take back all of the item of work, in one go. It stays open: what is brought back is in it again."""
    mine = whose(author) or (str(author), "")
    now = item(conn, mine)
    if not now["changes"]:
        raise Conflict("there is nothing that is not saved")
    if now["in_the_way"]:
        raise Conflict(f"{now['in_the_way']} came after, and touches the same stretches or junctions: "
                       "it cannot be discarded as a whole")
    return back_to(conn, author, now["changes"][-1]["txid"])


def open_items(conn, but: tuple[str, str] | None = None) -> list[dict]:
    """The items of work that are open with changes in them, of everybody but one: who is at what, not yet saved.

    Of people working in the map only. What an assistant or a script changes is never said to be saved by
    anybody, and is not something that waits.
    """
    told = []
    for who, how in conn.execute("""SELECT DISTINCT author, coalesce(through, '') FROM core.changeset
                                    WHERE state = 'open' AND through = %s ORDER BY 1, 2""", (MAP,)).fetchall():
        if (who, how) == but or who.startswith("Stiflyt ("):
            continue
        now = item(conn, (who, how))
        if now["changes"]:
            told.append({"author": who, "through": how or MAP, "changes": len(now["changes"]), "routes": now["routes"]})
    return told


def back_to(conn, author: str, txid: int) -> dict:
    """Take back one of your latest changes and every change of yours after it, in one go.

    For when a row of changes went wrong. What others did in between stays;
    where that touches the same stretches or junctions, nothing is done.
    Each can be brought back afterwards, the earliest first.
    """
    mine = whose(author)
    going = [f["txid"] for f in _standing(conn, mine, SITTING)]
    if txid not in going:
        raise Conflict("that change is not among your latest: look again")
    going = going[:going.index(txid) + 1]      # newest first
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
    if mine:
        _nothing_left_hanging(conn, touched)
    return {"undone": told, "next": last(conn, mine), "again": undone_last(conn, mine)}


def undone_last(conn, mine: tuple[str, str] | None = None) -> dict | None:
    """What a redo would bring back: the action you took back last, while nothing of yours has come in its place.

    Of several taken back in one go, the earliest first, so that they come
    back in the order they were made. An action taken back before changes
    could be brought back (d0016) is not among them.
    """
    found = _rows(conn, """
        SELECT u.txid, max(u.undone_at) AS undone_at, count(*)::int AS rows
        FROM core.undo_log u
        WHERE u.undone_at IS NOT NULL AND u.undone_by IS NOT NULL
          AND (%(who)s::text IS NULL OR (u.undone_by = %(who)s AND coalesce(u.undone_through, %(map)s) = %(how)s))
        GROUP BY u.txid ORDER BY max(u.undone_at) DESC, u.txid LIMIT 1""",
        {"map": MAP, "who": mine and mine[0], "how": mine and mine[1]})
    if not found:
        return None
    since = _standing(conn, mine, 1)
    if since and since[0]["at"] > found[0]["undone_at"]:
        return None         # something was changed after it was taken back: that stands in its place
    # What it would be brought back over: a later change to the same stretches or junctions that stands,
    # or an earlier one that was taken back after it (by somebody else: one's own come back first).
    late, earlier = conn.execute("""
        SELECT max(o.txid) FILTER (WHERE o.undone_at IS NULL AND o.at > s.undone_at),
               max(o.txid) FILTER (WHERE o.txid < s.txid AND o.undone_at > s.undone_at)
        FROM core.undo_log s JOIN core.undo_log o ON o.tbl = s.tbl AND o.key = s.key
        WHERE s.txid = %(t)s AND o.txid <> s.txid""", {"t": found[0]["txid"]}).fetchone()
    return {**found[0], "what": _describe(conn, found[0]["txid"]),
            "in_the_way": f"{_describe(conn, late)} came after it was taken back" if late
            else f"{_describe(conn, earlier)} was taken back after it" if earlier else None}


def redo(conn, author: str, txid: int | None = None) -> dict:
    """Bring back the action that was taken back last. Given a txid, only if that is it."""
    mine = whose(author)
    again = undone_last(conn, mine)
    if not again:
        raise Conflict("there is nothing to bring back: only what you took back last can be, "
                       "until you change something else")
    if txid is not None and txid != again["txid"]:
        raise Conflict("something else has been changed since: look again at what would be brought back")
    if again["in_the_way"]:
        raise Conflict(f"{again['in_the_way']}, and touches the same stretches or junctions: "
                       "it cannot be brought back over that")
    _bring_back(conn, author, again["txid"], again["what"])
    return {"redone": again["what"], "next": last(conn, mine), "again": undone_last(conn, mine)}


def _bring_back(conn, author: str, txid: int, what: str) -> None:
    """Put the rows of one action as they stood when it was taken back, in the order they were changed."""
    rows = _rows(conn, """SELECT id, tbl, op, key, new FROM core.undo_log
                          WHERE txid = %(t)s AND undone_at IS NOT NULL ORDER BY id FOR UPDATE""", {"t": txid})
    changeset = current_changeset(conn, author)
    conn.execute("SELECT set_config('stiflyt2.undoing', 'on', true)")
    columns: dict[str, list[str]] = {}
    for row in rows:
        table, key = row["tbl"], row["key"]
        cols = columns.setdefault(table, _columns(conn, table))
        where = " AND ".join(f"t.{k} = %(k_{k})s" for k in key)
        params = {f"k_{k}": v for k, v in key.items()}
        if row["op"] == "D":
            conn.execute(f"DELETE FROM core.{table} t WHERE {where}", params)
        elif row["new"] is None:
            continue        # the row was not there when the action was taken back
        elif row["op"] == "I" and table not in _STATUS:
            # With the number it had: a row that others point at by it comes back as itself.
            conn.execute(f"INSERT INTO core.{table} OVERRIDING SYSTEM VALUE SELECT * FROM "
                         f"jsonb_populate_record(NULL::core.{table}, %(new)s::jsonb)", {"new": json.dumps(row["new"])})
        else:
            listed = ", ".join(cols)
            conn.execute(
                f"""UPDATE core.{table} t SET ({listed}) =
                        (SELECT {listed} FROM jsonb_populate_record(NULL::core.{table}, %(new)s::jsonb))
                    WHERE {where}""", {**params, "new": json.dumps(row["new"])})
    conn.execute("""UPDATE core.undo_log SET undone_at = NULL, new = NULL, undone_by = NULL, undone_through = NULL
                    WHERE txid = %s""", (txid,))
    conn.execute("SELECT set_config('stiflyt2.undoing', 'off', true)")
    # Nothing it stands on may be gone: a junction or stretch that somebody else took back in between.
    edges = [r["key"]["id"] for r in rows if r["tbl"] == "edge"]
    routes = sorted({r["key"]["route_id"] for r in rows if r["tbl"] == "route_leg"})
    hanging = conn.execute("""
        SELECT (SELECT count(*) FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
                JOIN core.node n ON n.id IN (v.a_node, v.b_node)
                WHERE e.id = ANY(%(e)s::uuid[]) AND e.status = 'active' AND n.status = 'undone')
             + (SELECT count(*) FROM core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
                JOIN core.edge e ON e.id = l.edge_id
                WHERE l.route_id = ANY(%(r)s::uuid[]) AND e.status = 'undone')""",
        {"e": edges, "r": routes}).fetchone()[0]
    if hanging:
        raise Conflict("it stands on a stretch or junction that has been taken back since: bring that back first")
    conn.execute("""
        INSERT INTO history.event (changeset_id, author, type, subject_type, subject_id, payload)
        SELECT DISTINCT ON (e.subject_type, e.subject_id) %s, %s, 'redone', e.subject_type, e.subject_id,
               jsonb_build_object('what', %s::text)
        FROM history.event e WHERE e.txid = %s""", (changeset, author, what, txid))


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
