"""What happens on a route, first issues and notices: the feature `issues` (ADR-004, ADR-005).

An issue is something on a route that is wrong or to be done: a tree across
the path, a part to be cleared, marking worn away, a sign to replace, a
bridge that is out, wet ground that wants boards. A notice is said to
walkers: the route is closed (bre59, for works at the dam), or a warning,
from a date until a date that may be left open. Both are entries
(`ops.entry`, import migrations 0017 and 0018), and later kinds (a report
from a work party, an inspection) will be too.

An entry is about a spot on a route, one or more parts of it (from one bend
to another, wherever they begin and end), or the whole route. Where on the
route is kept as positions along our own stretches: a stretch, and how far
along it (0 at its first end, 1 at its last). No line is copied. When a
stretch's line is mended (laid on the mapped path), what lies along it is
drawn along the mended line; when a stretch is divided, a position is found
again on the part it lies on; a route that is taken into another carries its
entries along (`anchor`). What finds nothing to hang on is adrift, and is
shown as such until a person moves it onto a route or takes it away.

Where it is along the route, in metres from the route's start, is worked out
when it is shown, from the route as it is then; and can be given that way.

Every change is an event and can be taken back (`ops/undo.py`).
"""
from __future__ import annotations

import datetime as dt
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2 import features
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import along as on_route
from stiflyt2.ops import anchor
from stiflyt2.ops import areas as areas_of

features.feature("issues", "mark on the map what is wrong or to be done on a route, and say when a route is closed",
                 "ask to be given the feature `issues`")

KINDS = {"closed": "closed", "warning": "warning", "clearing": "to be cleared", "marking": "marking to be mended",
         "sign": "a sign or post", "bridge": "a bridge", "wet": "wet ground", "other": "something else"}
NOTICES = ("closed", "warning")         # said to walkers, from a date until a date
ABOUT = ("spot", "part", "route")
STATES = ("reported", "planned", "done", "dismissed")
OPEN = ("reported", "planned")
MOST_PARTS = on_route.MOST_PARTS


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def _uuid(some) -> bool:
    try:
        uuid.UUID(str(some))
        return True
    except ValueError:
        return False


def _event(conn, author, entry: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, 'entry', %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), entry, type_, Jsonb(payload)))


def _route(conn, route: str) -> dict:
    found = _rows(conn, "SELECT id::text, code FROM core.route WHERE id = %(r)s::uuid AND status = 'active'",
                  {"r": route}) if _uuid(route) else []
    if not found:
        raise NotFound("no route of ours in use has that id")
    return found[0]


def _text(text) -> str:
    text = (text or "").strip()
    if not text:
        raise Invalid("say what it is: the text cannot be empty")
    return text[:2000]


def _dates(kind: str, valid_from, valid_until) -> tuple:
    if kind not in NOTICES:
        return None, None               # only a notice is from and until
    if valid_from and valid_until and valid_until < valid_from:
        raise Invalid("it cannot end before it begins")
    return valid_from, valid_until


# --- where along a route ----------------------------------------------------------------------------------------

def _along(conn, route: on_route.Route, about: str, spot: dict | None, parts: list[dict] | None) -> list:
    """What `ops.entry.along` keeps for a spot, parts, or the whole route."""
    if about == "route":
        return []
    if about == "spot":
        return [on_route.position(conn, route, spot or {})]
    return on_route.parts_of(conn, route, parts)


# --- saying it ----------------------------------------------------------------------------------------------------

def report(conn, author, *, route: str, kind: str, text: str, about: str = "spot", spot: dict | None = None,
           parts: list[dict] | None = None, state: str = "reported", valid_from: dt.date | None = None,
           valid_until: dt.date | None = None) -> dict:
    """Say something about a route: at a spot on it, along parts of it, or of the whole route.

    A spot is `{"point": [lon, lat]}` (a click within 50 m of the route's line) or `{"m": metres from the
    route's start}`. A part is `{"from_point": .., "to_point": ..}` or `{"from_m": .., "to_m": ..}`, or one of each.
    """
    if kind not in KINDS or about not in ABOUT or state not in STATES:
        raise Invalid(f"kind is one of {', '.join(KINDS)}; about one of {', '.join(ABOUT)}; state one of {', '.join(STATES)}")
    r = _route(conn, route)
    valid_from, valid_until = _dates(kind, valid_from, valid_until)
    along = _along(conn, on_route.Route(conn, r["id"]), about, spot, parts)
    made = conn.execute("""
        INSERT INTO ops.entry (kind, about, route_id, edge_ids, along, area, text, state, valid_from, valid_until,
                               made_by, updated_by)
        VALUES (%(kind)s, %(about)s, %(route)s, %(edges)s::uuid[], %(along)s, lower(left(%(code)s, 3)),
                %(text)s, %(state)s, %(from)s, %(until)s, %(who)s, %(who)s)
        RETURNING id::text""",
        {"kind": kind, "about": about, "route": r["id"], "edges": on_route.edges(along), "along": Jsonb(along),
         "code": r["code"], "text": _text(text), "state": state, "from": valid_from, "until": valid_until,
         "who": str(author)}).fetchone()[0]
    _event(conn, author, made, "entry.reported", {"kind": kind, "about": about, "route": r["code"], "text": _text(text)})
    return one(conn, made)


def _kept(conn, entry: str) -> dict:
    found = _rows(conn, "SELECT * FROM ops.entry WHERE id = %(e)s::uuid", {"e": entry}) if _uuid(entry) else []
    if not found:
        raise NotFound("no such issue")
    return found[0]


def change(conn, author, entry: str, said: dict) -> dict:
    """Say something else of an entry: its kind, text, state, dates, what it is about, or where it is.

    `said` holds only what changes; a date given as None is taken away. `spot` or `parts` (as for `report`) say
    anew where on the route it is: on its route as it is now, or for one that is adrift, a spot clicked on any
    route in use.
    """
    row = _kept(conn, entry)
    now = _anchored(conn, [dict(row)])[0]
    sets, was = {}, {}
    if now["route_now"] and not now["lost"] and (now["route_now"] != str(row["route_id"]) or now["along_now"] != row["along"]):
        # Brought to where it belongs now (ADR-004, point 4), so that what is said is said of the same entry.
        sets.update(route_id=now["route_now"], along=now["along_now"])
        _event(conn, author, entry, "entry.followed", {"route": now["route_now"], "along": now["along_now"]})
    kind, about = said.get("kind", row["kind"]), said.get("about", row["about"])
    if kind not in KINDS or about not in ABOUT or said.get("state", row["state"]) not in STATES:
        raise Invalid(f"kind is one of {', '.join(KINDS)}; about one of {', '.join(ABOUT)}; state one of {', '.join(STATES)}")
    for key in ("kind", "state", "about"):
        if key in said and said[key] != row[key]:
            sets[key], was[key] = said[key], [row[key], said[key]]
    if "text" in said and _text(said["text"]) != row["text"]:
        sets["text"], was["text"] = _text(said["text"]), [row["text"], _text(said["text"])]
    valid_from, valid_until = _dates(kind, said.get("valid_from", row["valid_from"]), said.get("valid_until", row["valid_until"]))
    for key, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value != row[key]:
            sets[key], was[key] = value, [str(row[key]) if row[key] else None, str(value) if value else None]
    if said.get("spot") or said.get("parts") or "about" in sets:
        if about != "route" and not said.get("spot" if about == "spot" else "parts"):
            raise Invalid(f"say where on the route it is: {'the spot' if about == 'spot' else 'the parts'}")
        route = now["route_now"]
        if not route and about == "spot" and (said.get("spot") or {}).get("point"):
            # Adrift: put on whichever route in use is clicked.
            route = on_route.on(conn, None, *said["spot"]["point"])["route"]
            sets["area"] = conn.execute("SELECT lower(left(code, 3)) FROM core.route WHERE id = %s", (route,)).fetchone()[0]
        if not route:
            raise Invalid("its route is gone: say where it is by clicking a route in use")
        along = _along(conn, on_route.Route(conn, route), about, said.get("spot"), said.get("parts"))
        if along != (sets.get("along") or row["along"]) or route != str(row["route_id"]):
            sets.update(route_id=route, along=along)
            was["moved"] = True
    if not was and not sets:
        return one(conn, entry)
    if "along" in sets:
        sets["edge_ids"] = on_route.edges(sets["along"])
        sets["along"] = Jsonb(sets["along"])
    columns = ", ".join(f"{k} = %({k})s" + {"edge_ids": "::uuid[]", "route_id": "::uuid"}.get(k, "") for k in sets)
    conn.execute(f"UPDATE ops.entry SET {columns}, updated_at = now(), updated_by = %(who)s WHERE id = %(id)s::uuid",
                 {**sets, "who": str(author), "id": entry})
    if was:
        _event(conn, author, entry, "entry.changed", was)
    return one(conn, entry)


def take_away(conn, author, entry: str) -> dict:
    """Take an entry away: it was wrong, or is not worth keeping. Undo brings it back."""
    row = _kept(conn, entry)
    conn.execute("DELETE FROM ops.entry WHERE id = %s::uuid", (entry,))
    _event(conn, author, entry, "entry.taken_away", {"kind": row["kind"], "text": row["text"]})
    return {"taken_away": entry}


# --- reading it ---------------------------------------------------------------------------------------------------

def _anchored(conn, rows: list[dict]) -> list[dict]:
    """What each entry hangs on now: its route, and its positions along the stretches as they stand now."""
    positions = [p for r in rows for x in r["along"] for p in ([x] if "edge" in x else [x["from"], x["to"]])]
    followed = iter(on_route.follow(conn, positions))
    routes = anchor.stands_for(conn, "route", {str(r["route_id"]) for r in rows})
    for r in rows:
        along, lost = [], False
        for x in r["along"]:
            if "edge" in x:
                p = next(followed)
                lost |= p is None
                along += [p] if p else []
            else:
                a, b = next(followed), next(followed)
                lost |= a is None or b is None
                along += [{"from": a, "to": b}] if a and b else []
        r["along_now"], r["lost"] = along, lost or (r["about"] != "route" and not along)
        r["route_now"] = anchor.one(routes.get(str(r["route_id"])))
    return rows


def _shown(conn, rows: list[dict]) -> list[dict]:
    """Entries as they are told: what they hang on now, where they are on the map and along the route, and whether
    they are adrift."""
    if not rows:
        return []
    _anchored(conn, rows)
    edges_now = {r["id"]: on_route.edges(r["along_now"]) for r in rows}
    on = {}
    for edge, rid, code in conn.execute("""
            SELECT l.edge_id::text, r.id::text, r.code FROM core.route_leg l
            JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
            WHERE l.edge_id = ANY(%s::uuid[])""", ([e for v in edges_now.values() for e in v],)):
        on.setdefault(edge, {})[rid] = code
    whole = dict(conn.execute("SELECT id::text, code FROM core.route WHERE id = ANY(%s::uuid[]) AND status = 'active'",
                              ([r["route_now"] for r in rows if r["route_now"]],)).fetchall())
    spots = [x for r in rows if r["about"] == "spot" for x in r["along_now"]]
    points = {(e, at): [lon, lat] for e, at, lon, lat in conn.execute("""
            SELECT c.edge::text, c.at, round(ST_X(p)::numeric, 6)::float8, round(ST_Y(p)::numeric, 6)::float8
            FROM unnest(%s::uuid[], %s::float8[]) c(edge, at)
            JOIN core.edge e ON e.id = c.edge JOIN core.edge_revision v ON v.id = e.current_revision_id,
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(v.geom, c.at), 4326) AS p) m""",
            ([s["edge"] for s in spots], [s["at"] for s in spots]))}
    # What is said of a whole route is shown at the middle of the route.
    centres = dict(conn.execute("""
            SELECT DISTINCT ON (l.route_id) l.route_id::text,
                   ARRAY[round(ST_X(p)::numeric, 6), round(ST_Y(p)::numeric, 6)]::float8[]
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id,
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(v.geom, 0.5), 4326) AS p) m
            WHERE l.route_id = ANY(%s::uuid[])
            ORDER BY l.route_id, abs(l.seq - (SELECT avg(seq) FROM core.route_leg x WHERE x.route_id = l.route_id))""",
            ([r["route_now"] for r in rows if r["route_now"] and r["about"] == "route"],)).fetchall())
    walked = {}
    today = dt.date.today()
    told = []
    for r in rows:
        routes = {}
        for e in edges_now[r["id"]]:
            routes.update(on.get(e, {}))
        lines, parts, at, at_m, here = [], [], None, None, None
        if r["about"] == "route":
            here = r["route_now"] if r["route_now"] in whole else None
            at = centres.get(here)
            if here:
                routes = {here: whole[here]} | routes
        elif not r["lost"]:
            # Measured along the route it was said on; when that no longer walks there, along another that does.
            for rid in dict.fromkeys(x for x in (r["route_now"], *routes) if x in whole or x in routes):
                route = walked.get(rid) or walked.setdefault(rid, on_route.Route(conn, rid))
                if r["about"] == "part":
                    drawn, said, covered = on_route.drawn(conn, route, r["along_now"])
                    if len(said) == len(r["along_now"]):
                        here, lines, parts = rid, drawn, said
                        # A part is on every route that walks some of it, not only where it begins and ends.
                        routes.update(conn.execute("""
                            SELECT DISTINCT r.id::text, r.code FROM core.route_leg l
                            JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
                            WHERE l.edge_id = ANY(%s::uuid[])""", (covered,)).fetchall())
                        break
                elif (m := route.metres_at(r["along_now"][0])) is not None:
                    here, at_m = rid, round(m)
                    break
            at = (points.get((r["along_now"][0]["edge"], r["along_now"][0]["at"])) if r["about"] == "spot"
                  else lines[0][len(lines[0]) // 2] if lines else None)
        adrift = (None if here
                  else "the route is gone, and nothing came instead" if r["about"] == "route"
                  else "what it lay along is gone, and nothing came instead" if r["lost"]
                  else "no route in use walks all of it now" if routes
                  else "it lies on no route in use now")
        notice = r["kind"] in NOTICES
        told.append({
            "id": str(r["id"]), "kind": r["kind"], "says": KINDS[r["kind"]], "notice": notice, "about": r["about"],
            "text": r["text"], "state": r["state"], "open": r["state"] in OPEN,
            "valid_from": r["valid_from"], "valid_until": r["valid_until"],
            "active": notice and r["state"] in OPEN and (not r["valid_from"] or r["valid_from"] <= today)
                      and (not r["valid_until"] or r["valid_until"] >= today),
            "lapsed": bool(notice and r["valid_until"] and r["valid_until"] < today),
            "route": {"id": here, "code": whole.get(here) or routes.get(here)} if here else None,
            "routes": [{"id": i, "code": c} for i, c in sorted(routes.items(), key=lambda x: x[1] or "")],
            "area": r["area"], "adrift": adrift,
            "lon": at[0] if at else None, "lat": at[1] if at else None,
            "at_m": at_m, "parts": parts, "lines": lines,
            "made_at": r["made_at"], "made_by": r["made_by"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]})
    return told


def _order(e: dict) -> tuple:
    return (not e["open"], not e["active"], -e["made_at"].timestamp())


def listing(conn, area: str | None = None, route: str | None = None) -> list[dict]:
    """The entries of an area (on its routes now, or said there and on none now), or of one route: open first,
    notices in force first among them, then the newest."""
    told = _shown(conn, _rows(conn, "SELECT * FROM ops.entry ORDER BY made_at"))
    if area:
        area, more = area.lower(), set(areas_of.added_to(conn, area))
        told = [e for e in told if any(r["code"] and r["code"][:3].lower() == area or r["id"] in more for r in e["routes"])
                or (not e["routes"] and e["area"] == area)]
    if route:
        told = [e for e in told if any(r["id"] == route for r in e["routes"])]
    return sorted(told, key=_order)


def one(conn, entry: str) -> dict:
    """One entry, with what has happened to it: who said what, and when."""
    told = _shown(conn, [_kept(conn, entry)])[0]
    told["history"] = _rows(conn, """SELECT at, who, through, type, payload FROM ops.event
                                     WHERE subject = 'entry' AND subject_id = %(e)s AND type <> 'entry.followed'
                                     ORDER BY seq""", {"e": entry})
    return told


def parts(conn, route: str, said: list[dict]) -> dict:
    """What some parts of a route would be, drawn, and how long: for a page to show them while they are chosen."""
    r = on_route.Route(conn, _route(conn, route)["id"])
    lines, told, _ = on_route.drawn(conn, r, _along(conn, r, "part", None, said))
    return {"lines": lines, "parts": told, "route_m": round(r.metres)}
