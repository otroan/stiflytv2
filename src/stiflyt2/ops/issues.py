"""What happens on a route, first issues and notices: the feature `issues` (ADR-004, ADR-005).

An issue is something on a route that is wrong or to be done: a tree across
the path, marking worn away, a sign to replace, a bridge that is out, wet
ground that wants boards. A notice is said to walkers: the route is closed
(bre59, for works at the dam), or a warning, from a date until a date that
may be left open. Both are entries (`ops.entry`, import migration 0017), and
later kinds (a report from a work party, an inspection) will be too.

An entry is about a spot on a route, a whole stretch of it, or the whole
route. It keeps the ids of the route and the stretch as they were when it was
said, and is read through what stands for them now (`anchor`): a stretch that
is divided carries a spot on the part it lies on, and a stretch issue on both
parts; a route that is taken into another carries its entries along. A spot
keeps its own position, which does not move when the line is mended: the tree
lies where it lies. What finds nothing to hang on is adrift, and is shown as
such until a person moves it onto a route or takes it away.

Every change is an event and can be taken back (`ops/undo.py`).
"""
from __future__ import annotations

import datetime as dt
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2 import features
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import anchor

features.feature("issues", "mark on the map what is wrong or to be done on a route, and say when a route is closed",
                 "ask to be given the feature `issues`")

KINDS = {"closed": "closed", "warning": "warning", "clearing": "to be cleared", "marking": "marking to be mended",
         "sign": "a sign or post", "bridge": "a bridge", "wet": "wet ground", "other": "something else"}
NOTICES = ("closed", "warning")         # said to walkers, from a date until a date
ABOUT = ("spot", "stretch", "route")
STATES = ("reported", "planned", "done", "dismissed")
OPEN = ("reported", "planned")
ON_ROUTE_M = 50                         # a spot is on a route's stretch this near where it was clicked
_POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


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


def _on(conn, routes: list[str] | None, lon: float, lat: float) -> dict:
    """The stretch of these routes (None: of any route in use) nearest a clicked point, and the spot on it."""
    found = _rows(conn, f"""
        SELECT e.id::text AS edge, l.route_id::text AS route, ST_Distance(v.geom, q.p) AS off,
               ST_ClosestPoint(v.geom, q.p) AS spot
        FROM core.route_leg l JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id,
             (SELECT {_POINT} AS p) q
        WHERE (%(routes)s::uuid[] IS NULL OR l.route_id = ANY(%(routes)s::uuid[]))
          AND ST_DWithin(v.geom, q.p, %(near)s)
        ORDER BY off, r.discontinued_since IS NOT NULL LIMIT 1""",
                  {"lon": lon, "lat": lat, "routes": routes, "near": ON_ROUTE_M})
    if not found:
        raise Invalid(f"that is not on {'the route' if routes else 'a route in use'}: click within {ON_ROUTE_M} m of its line")
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


def report(conn, author, *, route: str, kind: str, text: str, about: str = "spot", lon: float | None = None,
           lat: float | None = None, state: str = "reported", valid_from: dt.date | None = None,
           valid_until: dt.date | None = None) -> dict:
    """Say something about a route: at a spot on it, along one of its stretches, or of the whole route.

    A spot or a stretch is where it was clicked (`lon`, `lat`), on the route's nearest stretch within 50 m;
    the spot is moved onto the line. For the whole route a position is not needed.
    """
    if kind not in KINDS or about not in ABOUT or state not in STATES:
        raise Invalid(f"kind is one of {', '.join(KINDS)}; about one of {', '.join(ABOUT)}; state one of {', '.join(STATES)}")
    r = _route(conn, route)
    valid_from, valid_until = _dates(kind, valid_from, valid_until)
    edge, spot = None, None
    if about != "route" or lon is not None:
        if lon is None or lat is None:
            raise Invalid("say where on the route: lon and lat")
        on = _on(conn, [r["id"]], lon, lat)
        edge, spot = on["edge"], on["spot"]
    made = conn.execute("""
        INSERT INTO ops.entry (kind, about, route_id, edge_ids, geom, area, text, state, valid_from, valid_until,
                               made_by, updated_by)
        VALUES (%(kind)s, %(about)s, %(route)s, %(edges)s::uuid[], %(spot)s::geometry, lower(left(%(code)s, 3)),
                %(text)s, %(state)s, %(from)s, %(until)s, %(who)s, %(who)s)
        RETURNING id::text""",
        {"kind": kind, "about": about, "route": r["id"], "edges": [edge] if edge and about != "route" else [],
         "spot": spot, "code": r["code"], "text": _text(text), "state": state, "from": valid_from,
         "until": valid_until, "who": str(author)}).fetchone()[0]
    _event(conn, author, made, "entry.reported", {"kind": kind, "about": about, "route": r["code"], "text": _text(text)})
    return one(conn, made)


def _kept(conn, entry: str) -> dict:
    found = _rows(conn, "SELECT * FROM ops.entry WHERE id = %(e)s::uuid", {"e": entry}) if _uuid(entry) else []
    if not found:
        raise NotFound("no such issue")
    return found[0]


def _uuid(some) -> bool:
    try:
        uuid.UUID(str(some))
        return True
    except ValueError:
        return False


def change(conn, author, entry: str, said: dict) -> dict:
    """Say something else of an entry: its kind, text, state, dates, what it is about, or where it is.

    `said` holds only what changes; a date given as None is taken away. A new position (`lon`, `lat`) puts it
    on the nearest stretch of its route there, or of any route in use when its route is gone.
    """
    row = _kept(conn, entry)
    now = _anchored(conn, [dict(row)])[0]
    sets, was = {}, {}
    if now["route_now"] and (now["route_now"] != str(row["route_id"]) or now["edges_now"] != [str(e) for e in row["edge_ids"]]):
        # Brought to where it belongs now (ADR-004, point 4), so that what is said is said of the same entry.
        sets.update(route_id=now["route_now"], edge_ids=now["edges_now"])
        _event(conn, author, entry, "entry.followed", {"route": now["route_now"], "stretches": now["edges_now"]})
    kind = said.get("kind", row["kind"])
    about = said.get("about", row["about"])
    if kind not in KINDS or about not in ABOUT or said.get("state", row["state"]) not in STATES:
        raise Invalid(f"kind is one of {', '.join(KINDS)}; about one of {', '.join(ABOUT)}; state one of {', '.join(STATES)}")
    for key in ("kind", "state", "about"):
        if key in said and said[key] != row[key]:
            sets[key], was[key] = said[key], [row[key], said[key]]
    if "text" in said and _text(said["text"]) != row["text"]:
        sets["text"], was["text"] = _text(said["text"]), [row["text"], _text(said["text"])]
    valid_from = said.get("valid_from", row["valid_from"])
    valid_until = said.get("valid_until", row["valid_until"])
    valid_from, valid_until = _dates(kind, valid_from, valid_until)
    for key, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value != row[key]:
            sets[key], was[key] = value, [str(row[key]) if row[key] else None, str(value) if value else None]
    if said.get("lon") is not None and said.get("lat") is not None:
        route = sets.get("route_id") or (now["route_now"] if now["route_now"] else None)
        on = _on(conn, [route] if route else None, said["lon"], said["lat"])
        if not route:
            sets["route_id"] = on["route"]
            sets["area"] = conn.execute("SELECT lower(left(code, 3)) FROM core.route WHERE id = %s", (on["route"],)).fetchone()[0]
        sets.update(edge_ids=[on["edge"]], geom=on["spot"])
        was["moved"] = True
    if "about" in sets:
        if about == "route":
            sets["edge_ids"] = []
        elif row["about"] == "route" and not sets.get("edge_ids"):
            raise Invalid("say where on the route it is: give the spot (lon, lat)")
        elif about == "spot" and len(sets.get("edge_ids", now["edges_now"])) > 1:
            sets["edge_ids"] = sets.get("edge_ids", now["edges_now"])[:1]
    if not was and "route_id" not in sets:
        return one(conn, entry)
    columns = ", ".join(f"{k} = %({k})s" + {"edge_ids": "::uuid[]", "geom": "::geometry", "route_id": "::uuid"}.get(k, "")
                        for k in sets)
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


def _anchored(conn, rows: list[dict]) -> list[dict]:
    """What each entry hangs on now: the route, the stretches (`anchor.stands_for`), the routes in use on them."""
    edges = anchor.stands_for(conn, "edge", {str(e) for r in rows for e in r["edge_ids"]})
    routes = anchor.stands_for(conn, "route", {str(r["route_id"]) for r in rows})
    for r in rows:
        now = []
        for e in map(str, r["edge_ids"]):
            parts = edges.get(e, [])
            if r["about"] == "spot" and len(parts) > 1 and r.get("geom") is not None:
                # A spot is on the part of a divided stretch that it lies on.
                parts = [conn.execute("""
                    SELECT e.id::text FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
                    WHERE e.id = ANY(%s::uuid[])
                    ORDER BY ST_Distance(v.geom, (SELECT geom FROM ops.entry WHERE id = %s)) LIMIT 1""",
                    (parts, r["id"])).fetchone()[0]]
            now += parts
        r["edges_now"] = list(dict.fromkeys(now))
        r["route_now"] = anchor.one(routes.get(str(r["route_id"])))
    return rows


def _shown(conn, rows: list[dict]) -> list[dict]:
    """Entries as they are told: what they hang on now, where they are on the map, and whether they are adrift."""
    if not rows:
        return []
    _anchored(conn, rows)
    on = {}
    for edge, rid, code in conn.execute("""
            SELECT l.edge_id::text, r.id::text, r.code FROM core.route_leg l
            JOIN core.route r ON r.id = l.route_id AND r.status = 'active'
            WHERE l.edge_id = ANY(%s::uuid[])""", ([e for r in rows for e in r["edges_now"]],)):
        on.setdefault(edge, {})[rid] = code
    whole = dict(conn.execute("SELECT id::text, code FROM core.route WHERE id = ANY(%s::uuid[])",
                              ([r["route_now"] for r in rows if r["route_now"]],)).fetchall())
    lines = {e: c for e, c in conn.execute("""
            SELECT e.id::text, ST_AsGeoJSON(ST_Transform(v.geom, 4326), 6)::json -> 'coordinates'
            FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
            WHERE e.id = ANY(%s::uuid[])""", ([e for r in rows for e in r["edges_now"]],))}
    points = dict(conn.execute("""
            SELECT id::text, ARRAY[round(ST_X(p)::numeric, 6), round(ST_Y(p)::numeric, 6)]::float8[]
            FROM (SELECT id, ST_Transform(geom, 4326) AS p FROM ops.entry WHERE id = ANY(%s::uuid[]) AND geom IS NOT NULL) x""",
            ([r["id"] for r in rows],)).fetchall())
    # What is said of a whole route, and not at a spot, is shown at the middle of the route.
    centres = dict(conn.execute("""
            SELECT DISTINCT ON (l.route_id) l.route_id::text,
                   ARRAY[round(ST_X(p)::numeric, 6), round(ST_Y(p)::numeric, 6)]::float8[]
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id,
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(v.geom, 0.5), 4326) AS p) m
            WHERE l.route_id = ANY(%s::uuid[])
            ORDER BY l.route_id, abs(l.seq - (SELECT avg(seq) FROM core.route_leg x WHERE x.route_id = l.route_id))""",
            ([r["route_now"] for r in rows if r["route_now"] and r["about"] == "route"],)).fetchall())
    today = dt.date.today()
    told = []
    for r in rows:
        routes = {}
        for e in r["edges_now"]:
            routes.update(on.get(e, {}))
        if r["about"] == "route" and r["route_now"]:
            routes = {r["route_now"]: whole.get(r["route_now"])} | routes
        adrift = (None if routes
                  else "the route is gone, and nothing came instead" if r["about"] == "route"
                  else "its stretch is gone, and nothing came instead" if not r["edges_now"]
                  else "it lies on no route in use now")
        at = points.get(str(r["id"])) or (centres.get(r["route_now"]) if r["about"] == "route" else None)
        if not at and r["edges_now"] and lines.get(r["edges_now"][0]):
            coords = lines[r["edges_now"][0]]
            at = coords[len(coords) // 2]
        notice = r["kind"] in NOTICES
        told.append({
            "id": str(r["id"]), "kind": r["kind"], "says": KINDS[r["kind"]], "notice": notice, "about": r["about"],
            "text": r["text"], "state": r["state"], "open": r["state"] in OPEN,
            "valid_from": r["valid_from"], "valid_until": r["valid_until"],
            "active": notice and r["state"] in OPEN and (not r["valid_from"] or r["valid_from"] <= today)
                      and (not r["valid_until"] or r["valid_until"] >= today),
            "lapsed": bool(notice and r["valid_until"] and r["valid_until"] < today),
            "routes": [{"id": i, "code": c} for i, c in sorted(routes.items(), key=lambda x: x[1] or "")],
            "area": r["area"], "adrift": adrift,
            "lon": at[0] if at else None, "lat": at[1] if at else None,
            "lines": [lines[e] for e in r["edges_now"] if e in lines] if r["about"] == "stretch" else [],
            "made_at": r["made_at"], "made_by": r["made_by"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]})
    return told


def _order(e: dict) -> tuple:
    return (not e["open"], not e["active"], -e["made_at"].timestamp())


def listing(conn, area: str | None = None, route: str | None = None) -> list[dict]:
    """The entries of an area (on its routes now, or said there and on none now), or of one route: open first,
    notices in force first among them, then the newest."""
    rows = _rows(conn, "SELECT * FROM ops.entry ORDER BY made_at")
    told = _shown(conn, rows)
    if area:
        area = area.lower()
        told = [e for e in told if any(r["code"] and r["code"][:3].lower() == area for r in e["routes"])
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
