"""Take back what was last decided in operations: your own latest, whatever it was (ADR-004).

Every change to what operations keeps leaves the rows as they were in
`ops.undo_log` (import migration 0014), grouped by the transaction that made
it: one request is one action, whether it moved a post or put forty signs on
the order. Undo puts the rows of an action back, newest first, and can be
repeated to go further back.

Whose latest: the one who asks, made the way they ask. Undo in the page takes
back what that person did in the page; an assistant acting as the same person
takes back what the assistant did. What others have done since stays; where
it is about the same sign, post, site or issue, taking the earlier back would lose
it, and nothing is done.

An order that has been taken out stands: it has gone to whoever makes the
signs. What was decided before it about the signs it holds stands with it.
The events are never taken away: they show the change and that it was undone.

What a person has decided since they last saved is their item of work
(import migration 0015), as with the routes: `save` says it stays, `discard`
takes all of it back. Taking out an order saves.
"""
from __future__ import annotations

import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2.domain.places import Conflict

# What an action is called, by the first kind of event it left.
_WHAT = {
    "site.changed": "what was said of a sign site",
    "site.put": "a sign site put on a route",
    "site.taken_away": "a sign site taken away",
    "site.forgotten": "what was kept about a sign site that is gone, forgotten",
    "post.moved": "a post moved",
    "post.put_back": "a post put back where it is assumed to stand",
    "post.taken_out": "a post taken out",
    "post.back_in": "a post put back",
    "post.name_sign": "a name sign on a post",
    "post.forgotten": "what was kept about a post that is gone, forgotten",
    "sign.changed": "what was said of a sign",
    "sign.added": "a destination added to a post",
    "sign.up": "a sign said to be up",
    "sign.not_up": "a sign said not to be up after all",
    "sign.moved": "a decision moved to the sign it is about now",
    "sign.forgotten": "what was decided about a sign that is gone, forgotten",
    "entry.reported": "an issue reported",
    "entry.changed": "what was said of an issue",
    "entry.taken_away": "an issue taken away",
    "photo.added": "a photo added",
    "photo.changed": "what was said of a photo",
    "photo.taken_away": "a photo taken away",
    "report.added": "a report added",
    "report.changed": "what was said in a report",
    "report.taken_away": "a report taken away",
    "area.route_added": "a route added to an area",
    "area.route_taken_out": "a route taken out of an area",
}


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _whose(author) -> dict:
    return {"who": str(author), "how": getattr(author, "through", "viewer")}


def _describe(conn, txid: int) -> str:
    events = _rows(conn, """SELECT type, payload FROM ops.event
                            WHERE txid = %(t)s AND type NOT LIKE '%%.followed' ORDER BY seq""", {"t": txid})
    if not events:
        return "a change"
    first = events[0]
    on_order = [e for e in events if e["type"] in ("sign.changed", "post.name_sign") and "to_order" in e["payload"]]
    if on_order and len(on_order) == len(events):
        n = len(on_order)
        word = "put on the order" if on_order[0]["payload"]["to_order"] else "taken off the order"
        return f"{n} {'sign' if n == 1 else 'signs'} {word}"
    what = _WHAT.get(first["type"], first["type"])
    same = sum(1 for e in events if e["type"] == first["type"])
    return what if same == 1 else f"{what}, {same} times over"


def last(conn, author) -> dict | None:
    """The latest action of the one who asks that has not been undone: what an undo would take back."""
    found = _rows(conn, """
        SELECT u.txid, min(u.at) AS at, count(*)::int AS rows, bool_or(u.stands) AS stands
        FROM ops.undo_log u
        WHERE u.undone_at IS NULL AND EXISTS (
            SELECT 1 FROM ops.event e WHERE e.txid = u.txid AND e.who = %(who)s AND e.through = %(how)s)
        GROUP BY u.txid ORDER BY u.txid DESC LIMIT 1""", _whose(author))
    if not found or found[0]["stands"]:
        return None             # nothing of theirs, or an order was taken out last: that stands
    latest = found[0]
    after = _rows(conn, """
        SELECT l.txid FROM ops.undo_log l JOIN ops.undo_log mine ON mine.tbl = l.tbl AND mine.key = l.key
        WHERE mine.txid = %(t)s AND l.txid > %(t)s AND l.undone_at IS NULL ORDER BY l.txid LIMIT 1""",
        {"t": latest["txid"]})
    return {"txid": latest["txid"], "at": latest["at"], "what": _describe(conn, latest["txid"]),
            "in_the_way": _describe(conn, after[0]["txid"]) if after else None}


def undo(conn, author, txid: int | None = None) -> dict:
    """Take the latest action of the one who asks back. Given a txid, only if that is it."""
    latest = last(conn, author)
    if not latest:
        raise Conflict("there is nothing of yours to undo here")
    if txid is not None and txid != latest["txid"]:
        raise Conflict("something else has been decided since: look again at what would be undone")
    if latest["in_the_way"]:
        raise Conflict(f"{latest['in_the_way']} came after, and is about the same thing: it would be lost")
    rows = _rows(conn, """SELECT id, tbl, op, key::text AS key, old FROM ops.undo_log
                          WHERE txid = %(t)s AND undone_at IS NULL ORDER BY id DESC FOR UPDATE""",
                 {"t": latest["txid"]})
    conn.execute("SELECT set_config('stiflyt2.undoing', 'on', true)")
    columns: dict[str, str] = {}
    for row in rows:
        table = row["tbl"]
        if table not in columns:
            columns[table] = ", ".join(c for (c,) in conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'ops' AND table_name = %s ORDER BY ordinal_position""", (table,)))
        if row["op"] == "I":
            conn.execute(f"DELETE FROM ops.{table} WHERE id = %s", (row["key"],))
        elif row["op"] == "U":
            conn.execute(
                f"""UPDATE ops.{table} t SET ({columns[table]}) =
                        (SELECT {columns[table]} FROM jsonb_populate_record(NULL::ops.{table}, %(old)s::jsonb))
                    WHERE t.id = %(id)s""", {"old": json.dumps(row["old"]), "id": row["key"]})
        else:
            conn.execute(f"INSERT INTO ops.{table} SELECT * FROM jsonb_populate_record(NULL::ops.{table}, %(old)s::jsonb)",
                         {"old": json.dumps(row["old"])})
    conn.execute("UPDATE ops.undo_log SET undone_at = now() WHERE txid = %s", (latest["txid"],))
    conn.execute("SELECT set_config('stiflyt2.undoing', 'off', true)")
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%(who)s, %(how)s, 'action', %(id)s, 'undone', %(said)s)""",
                 {**_whose(author), "id": str(latest["txid"]), "said": Jsonb({"what": latest["what"]})})
    return {"undone": latest["what"], "next": last(conn, author)}


def item(conn, author) -> dict:
    """What the one who asks has decided since they last saved, and stands: their item of work, newest first.

    Usually about one sign site. With what stands in the way of discarding it
    as a whole: a later decision of somebody else's about the same sign, post
    or site.
    """
    mine = _whose(author)
    mark = conn.execute("SELECT txid FROM ops.saved WHERE who = %(who)s AND through = %(how)s", mine).fetchone()
    changes = _rows(conn, """
        SELECT u.txid, min(u.at) AS at FROM ops.undo_log u
        WHERE u.undone_at IS NULL AND NOT u.stands AND u.txid > %(mark)s AND EXISTS (
            SELECT 1 FROM ops.event e WHERE e.txid = u.txid AND e.who = %(who)s AND e.through = %(how)s)
        GROUP BY u.txid ORDER BY u.txid DESC LIMIT 100""", {**mine, "mark": mark[0] if mark else 0})
    for c in changes:
        c["what"] = _describe(conn, c["txid"])
    txids = [c["txid"] for c in changes]
    late = _rows(conn, """
        SELECT l.txid FROM ops.undo_log l JOIN ops.undo_log mine ON mine.tbl = l.tbl AND mine.key = l.key
        WHERE mine.txid = ANY(%(t)s) AND l.txid > mine.txid AND l.undone_at IS NULL AND l.txid <> ALL(%(t)s)
        ORDER BY l.txid DESC LIMIT 1""", {"t": txids}) if txids else []
    return {"changes": changes, "in_the_way": _describe(conn, late[0]["txid"]) if late else None}


def save(conn, author) -> dict:
    """Say that what was decided since the last save is to stay: the item of work is done, and the next begins."""
    n = len(item(conn, author)["changes"])
    conn.execute("""INSERT INTO ops.saved (who, through, txid) VALUES (%(who)s, %(how)s, txid_current())
                    ON CONFLICT (who, through) DO UPDATE SET txid = EXCLUDED.txid, at = now()""", _whose(author))
    return {"saved": n}


def discard(conn, author) -> dict:
    """Take back all that was decided since the last save, in one go, newest first."""
    now = item(conn, author)
    if not now["changes"]:
        raise Conflict("there is nothing that is not saved")
    if now["in_the_way"]:
        raise Conflict(f"{now['in_the_way']} came after, and is about the same thing: "
                       "it cannot be discarded as a whole")
    told = [undo(conn, author, c["txid"])["undone"] for c in now["changes"]]
    return {"undone": told, "next": last(conn, author)}


def kept_as_made(conn, author, act):
    """Do it, and save it as made when nothing else of the person's waits to be saved or discarded: an issue or a
    photo said in a form, not a decision among others at a sign site. Undo still takes it back."""
    waiting = bool(item(conn, author)["changes"])
    done = act()
    if not waiting:
        save(conn, author)
    return done


def stands(conn) -> None:
    """What this transaction has done is not to be taken back: an order has gone out."""
    conn.execute("UPDATE ops.undo_log SET stands = true WHERE txid = txid_current()")
