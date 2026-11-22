"""Reports of what was done on the routes: the feature `reports` (step 2 of the plan; import migration 0020).

An inspection, a work party (dugnad) or a diary note says what was done on
which day, and where: whole routes, or parts of them, as issues say where
they are (`along`). For a whole route the stretches it walked that day are
kept, so that a part added to the route later has not been inspected.

When each stretch was last inspected, and last had a work party, is worked
out from the reports each time it is asked, as skisporet.no shows when a ski
track was last prepared: nobody keeps it by hand. A route is due when the
latest has been too long ago (`DUE_YEARS`), or was never; otherwise what is
reported wrong on it (the issues) says what is to be done.

A report may say issues are done, and photos may be of it. The report as a
document is kept elsewhere (SharePoint), and a report here links to it.
Every change is an event and can be taken back (`ops/undo.py`).
"""
from __future__ import annotations

import datetime as dt
import uuid
from urllib.parse import urlparse

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from stiflyt2 import features
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import along as on_route
from stiflyt2.ops import anchor

features.feature("reports", "report inspections and work parties on the routes, and see when each was last done",
                 "ask to be given the feature `reports`")

KINDS = {"inspection": "inspection", "work": "work party", "note": "diary note"}
DONE = ("clearing", "marking", "signs", "bridges", "boards", "drainage", "other")
CONDITIONS = ("good", "fair", "poor")
# Decided 2026-11-23: a route is inspected every third year and has a work party every tenth; between, the
# issues reported on it say what is to be done.
DUE_YEARS = {"inspection": 3, "work": 10}
MOST_ROUTES = 60


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def _uuid(some) -> bool:
    try:
        uuid.UUID(str(some))
        return True
    except ValueError:
        return False


def _event(conn, author, subject: str, some_id: str, type_: str, payload: dict) -> None:
    conn.execute("""INSERT INTO ops.event (who, through, subject, subject_id, type, payload)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                 (str(author), getattr(author, "through", "viewer"), subject, some_id, type_, Jsonb(payload)))


def _covers(conn, covers: list[dict]) -> list[dict]:
    """What a report covers, as kept: for each route, the whole of it (its stretches now) or parts of it."""
    if not covers:
        raise Invalid("say which routes it was on")
    if len(covers) > MOST_ROUTES:
        raise Invalid(f"at most {MOST_ROUTES} routes in one report")
    kept = []
    for c in covers:
        r = on_route.in_use(conn, c.get("route"))
        route = on_route.Route(conn, r["id"])
        if c.get("parts"):
            along = on_route.parts_of(conn, route, c["parts"])
            edges = on_route.edges(along)
        else:
            along, edges = [], list(dict.fromkeys(leg["edge"] for leg in route.legs))
        kept.append({"route": r["id"], "code": r["code"], "along": along, "edges": edges})
    return kept


def _said(kind: str, done_on, condition, persons, hours, done, link, text, year_only: bool = False) -> dict:
    if kind not in KINDS:
        raise Invalid(f"a report is one of {', '.join(KINDS)}")
    if isinstance(done_on, str):
        done_on = dt.date.fromisoformat(done_on)
    if year_only and done_on:
        # Only the year is known: counted from the middle of it, or from today in a year that is not half over.
        done_on = min(dt.date(done_on.year, 7, 1), dt.date.today())
    # A day ahead is let through: the page says the day where the person is, the server where it is.
    if not done_on or done_on > dt.date.today() + dt.timedelta(days=1) or done_on.year < 1950:
        raise Invalid("say the day it was done: not a day still to come")
    if condition is not None and condition not in CONDITIONS:
        raise Invalid(f"the state found is one of {', '.join(CONDITIONS)}")
    done = list(dict.fromkeys(done or []))
    if set(done) - set(DONE):
        raise Invalid(f"what was done is among {', '.join(DONE)}")
    link = (link or "").strip() or None
    if link and (urlparse(link).scheme not in ("http", "https") or not urlparse(link).netloc):
        raise Invalid("the link is to be an address beginning with https://")
    return {"kind": kind, "done_on": done_on, "year_only": bool(year_only), "condition": condition if kind == "inspection" else None,
            "persons": persons if kind == "work" else None, "hours": hours if kind == "work" else None,
            "done": done if kind == "work" else [], "link": link, "text": (text or "").strip()[:4000] or None}


def _close(conn, author, report: str, entries: list[str]) -> list[str]:
    """Say these issues are done, by this report."""
    closed = []
    for entry in dict.fromkeys(entries or []):
        found = _rows(conn, "SELECT id::text, state FROM ops.entry WHERE id = %(e)s::uuid", {"e": entry}) if _uuid(entry) else []
        if not found:
            raise NotFound("no such issue")
        if found[0]["state"] in ("done", "dismissed"):
            continue
        conn.execute("""UPDATE ops.entry SET state = 'done', closed_by = %s::uuid, updated_at = now(), updated_by = %s
                        WHERE id = %s::uuid""", (report, str(author), entry))
        _event(conn, author, "entry", entry, "entry.changed", {"state": [found[0]["state"], "done"], "report": report})
        closed.append(entry)
    return closed


def report(conn, author, *, kind: str, done_on, covers: list[dict], text: str | None = None,
           condition: str | None = None, persons: int | None = None, hours: float | None = None,
           done: list[str] | None = None, link: str | None = None, closes: list[str] | None = None,
           year_only: bool = False) -> dict:
    """Report what was done on which day, on which routes: each `{"route": id}` for the whole of it, or with
    `"parts"` as issues have them. Issues it says are done (`closes`) are done, by this report."""
    said = _said(kind, done_on, condition, persons, hours, done, link, text, year_only)
    kept = _covers(conn, covers)
    made = conn.execute("""
        INSERT INTO ops.report (kind, done_on, year_only, area, text, condition, persons, hours, done, link, made_by, updated_by)
        VALUES (%(kind)s, %(done_on)s, %(year_only)s, lower(left(%(code)s, 3)), %(text)s, %(condition)s, %(persons)s, %(hours)s,
                %(done)s, %(link)s, %(who)s, %(who)s) RETURNING id::text""",
        {**said, "code": kept[0]["code"], "who": str(author)}).fetchone()[0]
    _keep_covers(conn, made, kept)
    closed = _close(conn, author, made, closes or [])
    _event(conn, author, "report", made, "report.added", {"kind": kind, "done_on": str(said["done_on"]),
                                                          "routes": [k["code"] for k in kept], "closed": closed})
    return one(conn, made)


def _keep_covers(conn, report: str, kept: list[dict]) -> None:
    for no, k in enumerate(kept):
        conn.execute("""INSERT INTO ops.report_part (report_id, no, route_id, along, edge_ids)
                        VALUES (%s::uuid, %s, %s::uuid, %s, %s::uuid[])""", (report, no, k["route"], Jsonb(k["along"]), k["edges"]))


def _kept(conn, report: str) -> dict:
    found = _rows(conn, "SELECT * FROM ops.report WHERE id = %(r)s::uuid", {"r": report}) if _uuid(report) else []
    if not found:
        raise NotFound("no such report")
    return found[0]


def change(conn, author, report: str, said: dict) -> dict:
    """Say something else in a report; `covers` says anew which routes and parts it was on."""
    was = _kept(conn, report)
    now = _said(said.get("kind", was["kind"]), said.get("done_on", was["done_on"]), said.get("condition", was["condition"]),
                said.get("persons", was["persons"]), said.get("hours", was["hours"]), said.get("done", was["done"]),
                said.get("link", was["link"]), said.get("text", was["text"]), said.get("year_only", was["year_only"]))
    changed = [k for k, v in now.items() if v != was[k] and not (k == "hours" and v is not None and was[k] is not None
                                                                  and float(v) == float(was[k]))]
    if "covers" in said:
        kept = _covers(conn, said["covers"])
        conn.execute("DELETE FROM ops.report_part WHERE report_id = %s::uuid", (report,))
        _keep_covers(conn, report, kept)
        changed.append("covers")
    if not changed:
        return one(conn, report)
    conn.execute("""UPDATE ops.report SET kind = %(kind)s, done_on = %(done_on)s, year_only = %(year_only)s, text = %(text)s, condition = %(condition)s,
                        persons = %(persons)s, hours = %(hours)s, done = %(done)s, link = %(link)s,
                        updated_at = now(), updated_by = %(who)s WHERE id = %(id)s::uuid""",
                 {**now, "who": str(author), "id": report})
    _event(conn, author, "report", report, "report.changed", {"changed": changed})
    return one(conn, report)


def take_away(conn, author, report: str) -> dict:
    """Take a report away. The issues it said are done stay done; Undo brings the report back."""
    was = _kept(conn, report)
    conn.execute("DELETE FROM ops.report_part WHERE report_id = %s::uuid", (report,))
    conn.execute("DELETE FROM ops.report WHERE id = %s::uuid", (report,))
    _event(conn, author, "report", report, "report.taken_away", {"kind": was["kind"], "done_on": str(was["done_on"])})
    return {"taken_away": report}


# --- reading ------------------------------------------------------------------------------------------------------

def _coverage(conn, parts: list[dict]) -> list[dict]:
    """What each part of a report covers now: the stretches, their lines, and for parts where they begin and end."""
    if not parts:
        return []
    whole_edges = anchor.stands_for(conn, "edge", {str(e) for p in parts if not p["along"] for e in p["edge_ids"]})
    routes = anchor.stands_for(conn, "route", {str(p["route_id"]) for p in parts})
    codes = dict(conn.execute("SELECT id::text, code FROM core.route WHERE id = ANY(%s::uuid[]) AND status = 'active'",
                              ([r for v in routes.values() for r in v],)).fetchall())
    walked: dict[str, on_route.Route] = {}
    told = []
    for p in parts:
        route = anchor.one(routes.get(str(p["route_id"])))
        item = {"report": str(p["report_id"]), "route": {"id": route, "code": codes.get(route)} if route in codes else None,
                "whole": not p["along"], "parts": [], "edges": [], "lines": []}
        if not p["along"]:
            item["edges"] = list(dict.fromkeys(e for old in map(str, p["edge_ids"]) for e in whole_edges.get(old, [])))
        elif route in codes:
            r = walked.get(route) or walked.setdefault(route, on_route.Route(conn, route))
            now = [x for x in ({"from": a, "to": b} for a, b in
                               zip(*[iter(on_route.follow(conn, [e for x in p["along"] for e in (x["from"], x["to"])]))] * 2,
                                   strict=True)) if x["from"] and x["to"]]
            item["lines"], item["parts"], item["edges"] = on_route.drawn(conn, r, now)
        told.append(item)
    lines = dict(conn.execute("""
        SELECT e.id::text, ST_AsGeoJSON(ST_Transform(v.geom, 4326), 6)::json -> 'coordinates'
        FROM core.edge e JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE e.id = ANY(%s::uuid[])""", ([e for t in told if t["whole"] for e in t["edges"]],)).fetchall())
    for t in told:
        if t["whole"]:
            t["lines"] = [lines[e] for e in t["edges"] if e in lines]
    return told


def _routes_of(conn, area: str | None) -> list[dict]:
    """The routes in use of an area, with their stretches."""
    found = _rows(conn, """
        SELECT r.id::text, r.code, coalesce(r.name, '') AS name, array_agg(DISTINCT l.edge_id::text) AS edges
        FROM core.route r JOIN core.route_leg l ON l.route_id = r.id
        WHERE r.status = 'active' AND r.discontinued_since IS NULL AND r.unpublished_why IS NULL
          AND (%(area)s::text IS NULL OR lower(left(r.code, 3)) = %(area)s)
        GROUP BY r.id ORDER BY length(r.code), r.code""", {"area": (area or "").lower() or None})
    return found


def _shown(conn, reports: list[dict]) -> list[dict]:
    parts = _rows(conn, "SELECT * FROM ops.report_part WHERE report_id = ANY(%s::uuid[]) ORDER BY report_id, no",
                  ([r["id"] for r in reports],)) if reports else []
    covered = _coverage(conn, parts)
    told = []
    for r in reports:
        mine = [c for c in covered if c["report"] == str(r["id"])]
        told.append({"id": str(r["id"]), "kind": r["kind"], "says": KINDS[r["kind"]], "done_on": r["done_on"], "year_only": r["year_only"],
                     "area": r["area"], "text": r["text"], "condition": r["condition"], "persons": r["persons"],
                     "hours": float(r["hours"]) if r["hours"] is not None else None, "done": r["done"], "link": r["link"],
                     "covers": [{"route": c["route"], "whole": c["whole"], "parts": c["parts"]} for c in mine],
                     "lines": [line for c in mine for line in c["lines"]], "edges": [e for c in mine for e in c["edges"]],
                     "made_at": r["made_at"], "made_by": r["made_by"], "updated_at": r["updated_at"], "updated_by": r["updated_by"]})
    return told


def listing(conn, area: str | None = None, route: str | None = None) -> list[dict]:
    """The reports of an area, or of a route (those that cover some of it now): the latest done first."""
    rows = _rows(conn, """SELECT * FROM ops.report WHERE (%(area)s::text IS NULL OR area = %(area)s)
                          ORDER BY done_on DESC, made_at DESC""", {"area": None if route else (area or "").lower() or None})
    told = _shown(conn, rows)
    if route:
        legs = {e for r in _routes_of(conn, None) if r["id"] == route for e in r["edges"]}
        told = [t for t in told if legs & set(t["edges"])]
    return told


def one(conn, report: str) -> dict:
    """One report, with the issues it said are done, and what has happened to it."""
    told = _shown(conn, [_kept(conn, report)])[0]
    told["closed"] = _rows(conn, "SELECT id::text, kind, text FROM ops.entry WHERE closed_by = %(r)s::uuid ORDER BY made_at",
                           {"r": report})
    told["history"] = _rows(conn, """SELECT at, who, type, payload FROM ops.event WHERE subject = 'report' AND subject_id = %(r)s
                                     ORDER BY seq""", {"r": report})
    return told


def status(conn, area: str | None = None) -> list[dict]:
    """Each route in use of an area: when it was last inspected and last had a work party, whether that covered all
    of it or part, and whether it is due. Those due longest first."""
    routes = _routes_of(conn, area)
    reports = _shown(conn, _rows(conn, "SELECT * FROM ops.report WHERE kind IN ('inspection', 'work') ORDER BY done_on DESC"))
    today = dt.date.today()
    told = []
    for r in routes:
        legs = set(r["edges"])
        item = {"id": r["id"], "code": r["code"], "name": r["name"]}
        for kind in ("inspection", "work"):
            mine = [x for x in reports if x["kind"] == kind and legs & set(x["edges"])]
            last = mine[0] if mine else None
            item[kind] = None if not last else {
                "done_on": last["done_on"], "year_only": last["year_only"], "report": last["id"], "condition": last["condition"],
                "all_of_it": legs <= set(last["edges"]), "years": round((today - last["done_on"]).days / 365.25, 1),
                "due": (today - last["done_on"]).days > DUE_YEARS[kind] * 365.25}
        told.append(item)
    return sorted(told, key=lambda x: (x["inspection"]["done_on"] if x["inspection"] else dt.date.min, x["code"]))


def coverage(conn, area: str | None, kind: str) -> list[dict]:
    """What the reports of a kind cover, as lines with the day each was done: for a map that shows how long since,
    stretch by stretch. The latest are last, to be drawn on top."""
    if kind not in ("inspection", "work"):
        raise Invalid("coverage is of inspections or work parties")
    rows = _rows(conn, """SELECT * FROM ops.report WHERE kind = %(k)s AND (%(area)s::text IS NULL OR area = %(area)s)
                          ORDER BY done_on""", {"k": kind, "area": (area or "").lower() or None})
    return [{"report": t["id"], "done_on": t["done_on"], "line": line} for t in _shown(conn, rows) for line in t["lines"]]
