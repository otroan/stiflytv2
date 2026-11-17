"""Ids that hold: what a thing is now, and what it was called before.

A route, a junction, a stretch and a place keep their id while they are moved,
re-laid or renamed. An id ends only when two junctions are joined, a stretch is
divided or a route is taken into another, and then core.succession says what
came instead (domain migration d0009). Whatever points at an id asks `current`
and gets one of four answers:

  current   it is still itself
  replaced  one thing came instead: follow it
  divided   several things came instead: a person has to choose
  gone      it was withdrawn and nothing came instead

core.alias keeps earlier codes and names, so that `lookup` finds a route or a
place by what it was once called.
"""
from __future__ import annotations

import uuid

from psycopg.rows import dict_row

LIVE = {"route": "active", "node": "active", "edge": "active", "place": "confirmed"}


def succeed(conn, subject: str, old_id: str, new_ids: list[str], how: str) -> None:
    """Say what came instead of a thing that is retired by this change."""
    for new_id in new_ids:
        conn.execute("""INSERT INTO core.succession (subject_type, old_id, new_id, how)
                        VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""", (subject, old_id, new_id, how))


def remember(conn, subject: str, subject_id: str, kind: str, value: str | None) -> None:
    """Keep a code or a name the thing is about to lose."""
    if value and value.strip():
        conn.execute("""INSERT INTO core.alias (subject_type, subject_id, kind, value)
                        VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                     (subject, subject_id, kind, value.strip()))


def earlier(conn, subject: str, subject_id: str) -> list[dict]:
    """The codes and names the thing has had, latest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("""SELECT kind, value, until FROM core.alias
                              WHERE subject_type = %s AND subject_id = %s ORDER BY until DESC, id DESC""",
                           (subject, subject_id)).fetchall()


def current(conn, subject: str, subject_id: str) -> dict:
    """What an id stands for now: itself, what came instead of it, or nothing."""
    from stiflyt2.domain.places import Invalid, NotFound  # places itself keeps names here

    if subject not in LIVE:
        raise Invalid(f"ids are kept for {', '.join(LIVE)}")
    try:
        subject_id = str(uuid.UUID(str(subject_id)))
    except ValueError:
        raise NotFound(f"no such {subject}") from None
    found = conn.execute(f"SELECT status FROM core.{subject} WHERE id = %s", (subject_id,)).fetchone()
    if not found:
        raise NotFound(f"no such {subject}")
    if found[0] == LIVE[subject]:
        return {"type": subject, "id": subject_id, "state": "current", "now": [subject_id], "how": None}
    # Through everything that came instead and has itself been replaced since.
    reached = conn.execute(f"""
        WITH RECURSIVE line AS (
            SELECT s.new_id, s.how, s.id FROM core.succession s
            WHERE s.subject_type = %(s)s AND s.old_id = %(id)s
          UNION
            SELECT s.new_id, line.how, s.id FROM line
            JOIN core.succession s ON s.subject_type = %(s)s AND s.old_id = line.new_id
        )
        SELECT t.id::text, min(line.how), min(line.id) FROM line JOIN core.{subject} t ON t.id = line.new_id
        WHERE t.status = %(live)s GROUP BY t.id ORDER BY 3""",
        {"s": subject, "id": subject_id, "live": LIVE[subject]}).fetchall()
    first = conn.execute("""SELECT how FROM core.succession WHERE subject_type = %s AND old_id = %s
                            ORDER BY id LIMIT 1""", (subject, subject_id)).fetchone()
    now = [r[0] for r in reached]
    return {"type": subject, "id": subject_id, "now": now, "how": first[0] if first else None,
            "state": "gone" if not now else "replaced" if len(now) == 1 else "divided"}


def lookup(conn, text: str) -> list[dict]:
    """Routes and places that are, or once were, known by this code or name.

    What carries it now comes first. An earlier holder that has been replaced
    since is given as what stands in its place.
    """
    text = text.strip()
    if not text:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        held = cur.execute("""
            SELECT 'route' AS type, r.id::text AS id, r.status = 'active' AS live,
                   CASE WHEN lower(r.code) = lower(%(q)s) THEN r.code ELSE r.name END AS matched,
                   CASE WHEN lower(r.code) = lower(%(q)s) THEN 'code' ELSE 'name' END AS kind, NULL::timestamptz AS until
            FROM core.route r
            WHERE r.status IN ('active', 'retired') AND lower(%(q)s) IN (lower(r.code), lower(r.name))
            UNION ALL
            SELECT 'place', p.id::text, p.status = 'confirmed', p.name, 'name', NULL::timestamptz
            FROM core.place p WHERE p.status IN ('confirmed', 'retired') AND lower(p.name) = lower(%(q)s)
            UNION ALL
            SELECT a.subject_type, a.subject_id::text, false, a.value, a.kind, a.until
            FROM core.alias a WHERE lower(a.value) = lower(%(q)s)
            ORDER BY live DESC, until DESC NULLS FIRST""", {"q": text}).fetchall()
        out, seen = [], set()
        for h in held:
            for now_id in ([h["id"]] if h["live"] else current(conn, h["type"], h["id"])["now"]):
                if (h["type"], now_id) in seen:
                    continue
                seen.add((h["type"], now_id))
                table, label = ("route", "code") if h["type"] == "route" else ("place", "name")
                is_now = cur.execute(f"SELECT {label} AS label, name FROM core.{table} WHERE id = %s",
                                     (now_id,)).fetchone()
                out.append({"type": h["type"], "id": now_id, "label": is_now["label"], "name": is_now["name"],
                            "known_as": "now" if h["live"] else "earlier", "matched": h["matched"],
                            "kind": h["kind"]})
        return out
