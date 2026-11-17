"""What people meet when they use the tool: what it refused, what failed, what they say is missing.

Three kinds are kept, one row each (`feedback.entry`):

- refused: the tool said no, with the sentence the person was told
- failed: an error or a question that took too long, with its trace
- reported: a person's own words, or an assistant's, about what is wrong or cannot be done

A fourth is read out of the undo log and kept nowhere: changes taken back
right after they were made.

Entries that say the same thing, numbers and ids aside, are counted together.
None of this is part of the overlay: it is about the tool, not about the routes.
"""
from __future__ import annotations

import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MESSAGE_MAX = 2000
TRACE_MAX = 8000
ARGUMENTS_MAX = 8000
SOON_S = 120            # a change undone within this long was most likely not what was meant
ORDER = ("reported", "failed", "refused")


class NotFound(Exception):
    pass


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def record(conn, kind: str, through: str, what: str, message: str, *, who: str | None = None,
           status: int | None = None, arguments: dict | None = None, about: tuple[str, str] | None = None,
           trace: str | None = None) -> int:
    """Keep one entry, and return its number."""
    if arguments is not None and len(json.dumps(arguments, default=str)) > ARGUMENTS_MAX:
        arguments = {"cut": json.dumps(arguments, default=str)[:ARGUMENTS_MAX]}
    return conn.execute(
        """INSERT INTO feedback.entry (kind, through, who, what, status, message, arguments,
                                       about_type, about_id, trace)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (kind, through, who or None, what[:200], status, (message.strip() or "(nothing said)")[:MESSAGE_MAX],
         Jsonb(arguments) if arguments else None, *(about or (None, None)),
         trace[-TRACE_MAX:] if trace else None)).fetchone()[0]


def _called(conn, entries: list[dict]) -> None:
    """Say what each entry's route or place is called now. An id says nothing to the one who reads."""
    for kind, table, name in (("route", "core.route", "code"), ("place", "core.place", "name")):
        ids = sorted({e["about_id"] for e in entries if e["about_type"] == kind})
        if not ids or conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
            continue
        names = dict(conn.execute(f"SELECT id::text, {name} FROM {table} WHERE id::text = ANY(%s)", (ids,)).fetchall())
        for e in entries:
            if e["about_type"] == kind:
                e["about"] = names.get(e["about_id"])
    for e in entries:
        e.setdefault("about", None)


def grouped(conn, everything: bool = False) -> list[dict]:
    """What has come in, entries that say the same counted together: a person's words first, then by how often.

    Without `everything`, only what nobody has looked at yet. Each group is
    named by the number of its latest entry.
    """
    groups = _rows(conn, """
        SELECT kind, what, pattern, max(id) AS id, count(*)::int AS times,
               count(DISTINCT who)::int AS people, min(at) AS first, max(at) AS latest,
               (array_agg(message ORDER BY id DESC))[1] AS message,
               array_agg(DISTINCT through ORDER BY through) AS through,
               count(*) FILTER (WHERE seen_at IS NULL)::int AS unseen,
               (array_agg(fixed_in ORDER BY id DESC) FILTER (WHERE fixed_in IS NOT NULL))[1] AS fixed_in
        FROM feedback.entry WHERE %(all)s OR seen_at IS NULL
        GROUP BY kind, what, pattern
        ORDER BY array_position(%(order)s::text[], kind), count(*) DESC, max(at) DESC""",
        {"all": everything, "order": list(ORDER)})
    concerned = _rows(conn, """
        SELECT kind, what, pattern, about_type, about_id, count(*)::int AS times
        FROM feedback.entry WHERE about_id IS NOT NULL AND (%(all)s OR seen_at IS NULL)
        GROUP BY 1, 2, 3, 4, 5 ORDER BY count(*) DESC""", {"all": everything})
    _called(conn, concerned)
    for g in groups:
        g["about"] = [{"type": c["about_type"], "id": c["about_id"], "called": c["about"], "times": c["times"]}
                      for c in concerned if (c["kind"], c["what"], c["pattern"]) == (g["kind"], g["what"], g["pattern"])]
    return groups


def entries(conn, entry_id: int) -> list[dict]:
    """Every entry that says the same as this one, newest first, in full."""
    found = _rows(conn, """
        SELECT e.id, e.at, e.kind, e.through, e.who, e.what, e.status, e.message, e.arguments,
               e.about_type, e.about_id, e.trace, e.seen_at, e.fixed_in
        FROM feedback.entry e JOIN feedback.entry o USING (kind, what, pattern)
        WHERE o.id = %(id)s ORDER BY e.id DESC""", {"id": entry_id})
    if not found:
        raise NotFound(f"no entry {entry_id}")
    _called(conn, found)
    return found


def mark(conn, entry_id: int, fixed_in: str | None = None) -> int:
    """Say that an entry, and every one that says the same, has been looked at; or put right, and in what commit."""
    if not conn.execute("SELECT 1 FROM feedback.entry WHERE id = %s", (entry_id,)).fetchone():
        raise NotFound(f"no entry {entry_id}")
    return conn.execute("""
        UPDATE feedback.entry e SET seen_at = coalesce(e.seen_at, now()), fixed_in = coalesce(%(fixed)s, e.fixed_in)
        FROM feedback.entry o
        WHERE o.id = %(id)s AND (e.kind, e.what, e.pattern) = (o.kind, o.what, o.pattern)
          AND (e.seen_at IS NULL OR (%(fixed)s::text IS NOT NULL AND e.fixed_in IS NULL))""",
        {"id": entry_id, "fixed": fixed_in}).rowcount


def taken_back(conn, days: int = 14, soon_s: int = SOON_S) -> list[dict]:
    """Changes undone right after they were made, in the last days: nobody reports these.

    Read out of the undo log, newest first. A change taken back within two
    minutes was most likely not what the person meant it to do.
    """
    if conn.execute("SELECT to_regclass('core.undo_log')").fetchone()[0] is None:
        return []
    from stiflyt2.domain import undo

    found = _rows(conn, f"""
        SELECT u.txid, min(u.at) AS at, {undo.WHO} AS who,
               extract(epoch FROM max(u.undone_at) - min(u.at))::int AS after_s
        FROM core.undo_log u WHERE u.undone_at IS NOT NULL AND u.at > now() - make_interval(days => %(days)s)
        GROUP BY u.txid HAVING max(u.undone_at) - min(u.at) <= make_interval(secs => %(soon)s)
        ORDER BY u.txid DESC""", {"days": days, "soon": soon_s})
    return [{**f, "what": undo._describe(conn, f["txid"])} for f in found]
