"""The route card: one page that represents a route, to print or keep as PDF (step 4 of the plan).

Everything on it is read from what is known already, when it is asked for:
the route's facts and sections as it is walked (`factsheet`), its heights, its
line on Kartverket's topographic map, its description, the heritage sites
(Kulturminnesøk) near it, its open issues and notices, when it was last
inspected and worked on, photos near it, the signs along it, and who owns the
ground it crosses. The person making it chooses which of these are on it
(`PARTS`); the facts and any notice in force always are. Looking is not a
feature (ADR-005): whoever is let in may make a card. A part that is a feature of
its own (issues, reports, photos, who owns the ground) is on it only for one who
has that feature (`NEEDS`).

The card is HTML with print styles and no script of its own, so the browser
prints it, or saves it as PDF, as it is shown. The map is Kartverket's tiles laid
side by side under the route drawn in SVG, worked out here: nothing to wait for
when printing.
"""
from __future__ import annotations

import html
import math
import uuid
from datetime import UTC, datetime

from psycopg.rows import dict_row

from stiflyt2 import height, utno
from stiflyt2.domain import factsheet
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import along as on_route
from stiflyt2.ops import issues, photos, reports, signs

# What may be on a card, in the order it comes, with whether it is chosen when nobody has said.
PARTS = {
    "map": ("Map", True),
    "profile": ("Height profile", True),
    "description": ("Description", True),
    "walk": ("The route section by section", True),
    "heritage": ("Heritage sites near it", True),
    "issues": ("Open issues and notices", True),
    "upkeep": ("Inspections and work parties", True),
    "photos": ("Photos near it", False),
    "signs": ("Signs along it", False),
    "owners": ("Who owns the ground (looked up now)", False),
}
NEEDS = {"issues": "issues", "upkeep": "reports", "photos": "photos", "owners": "landowners"}
HERITAGE_M = 150            # a heritage site this near the route is on the card
TILES = "https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator"
WIDTH, MAP_H, PROFILE_H = 718, 540, 170
PROTECTION = {"AUT": "automatically protected", "VED": "protected by decision", "FJE": "removed",
              "IKKE": "not protected", "UAV": "not determined", "LIST": "listed"}
GROUND = {"trail": "path", "tractor_road": "tractor road", "road": "road", "glacier": "glacier", "water": "over water",
          "off_trail": "no path", "connector": "connector"}
ISSUE_WORDS = {"closed": "Closed", "warning": "Warning"}
e = html.escape


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


def chosen(asked: str | None, has=lambda feature: True) -> list[str]:
    """The parts asked for that one may have, in the card's own order; nothing said is those chosen by default."""
    said = {k for k, (_, default) in PARTS.items() if default} if asked is None else {w.strip() for w in asked.split(",")}
    return [k for k in PARTS if k in said and (k not in NEEDS or has(NEEDS[k]))]


# --- what is on it --------------------------------------------------------------------------------------------------

def gather(conn, route: str, parts: list[str], notices: bool = True) -> dict:
    """What a card of a route says, for the parts asked for. `owners` is not gathered here: it asks the register.
    `notices`: the notices in force are on it (for one who may see issues)."""
    try:
        uuid.UUID(route)
    except ValueError:
        raise NotFound(f"no route in use has the id {route}") from None
    row = conn.execute("SELECT code, season FROM core.route WHERE id = %s AND status = 'active'", (route,)).fetchone()
    if not row:
        raise NotFound(f"no route in use has the id {route}")
    code, season = row
    facts = factsheet.of_route(conn, route)
    walked = on_route.Route(conn, route)
    card = {"id": route, "code": code, "area": code[:3].lower(), "parts": parts, "facts": facts, "metres": walked.metres,
            "made": datetime.now(UTC)}
    entries = issues.listing(conn, route=route) if notices or "issues" in parts else []
    card["notices"] = [x for x in entries if x["notice"] and x["active"]] if notices else []
    card["issues"] = _numbered([x for x in entries if x["open"] and not x["active"]])
    said = facts["route"]
    if "description" in parts and not (said.get("description") or said.get("description_back")) and said.get("utno_id"):
        try:        # none of our own: UT.no's, read from its page, each way under ours, said as UT.no's
            found = utno.fetch(int(said["utno_id"]))
            card["utno"] = {"number": said["utno_id"], **utno.ways(found, said.get("from"), said.get("to"))}
        except (utno.Unavailable, Invalid, NotFound):
            card["utno"] = None
    if "map" in parts or "profile" in parts:
        card["heights"] = height.of_route(conn, route, samples=True)
    if "map" in parts:
        card["lines"] = _lines(conn, route)
    if "heritage" in parts:
        card["heritage"] = _heritage(conn, route, walked)
    if "upkeep" in parts:
        card["upkeep"] = next((s for s in reports.status(conn, card["area"], season=season) if s["id"] == route), None)
        card["reports"] = reports.listing(conn, route=route)[:10]
    if "photos" in parts:
        card["photos"] = photos.listing(conn, route=route)[:12]
    if "signs" in parts:
        card["signs"] = _signs(conn, route, code, card["area"], walked)
    return card


def _numbered(found: list[dict]) -> list[dict]:
    """Open issues in the order they lie along the route, the whole route's first, numbered as on the map."""
    def first(x):
        return x["at_m"] if x.get("at_m") is not None else min((p["from_m"] for p in x.get("parts") or []), default=-1)
    found = sorted(found, key=lambda x: (x["about"] != "route", first(x)))
    return [{**x, "n": i} for i, x in enumerate(found, start=1)]


def _lines(conn, route: str) -> list[dict]:
    """The route's stretches as walked, in degrees, with what each is."""
    return _rows(conn, """
        SELECT l.seq, v.kind, v.marked, ST_AsGeoJSON(ST_Transform(CASE WHEN l.reversed THEN ST_Reverse(v.geom) ELSE v.geom END, 4326), 6)::json
               -> 'coordinates' AS coords
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE l.route_id = %(r)s ORDER BY l.seq""", {"r": route})


def _metres(walked: on_route.Route, seq: int, at: float) -> float:
    leg = next(x for x in walked.legs if x["seq"] == seq)
    return leg["start"] + leg["m"] * ((1 - at) if leg["reversed"] else at)


def _heritage(conn, route: str, walked: on_route.Route) -> list[dict]:
    """Heritage sites near the route, a site once with the monuments of it, in the order they are passed."""
    found = _rows(conn, """
        SELECT DISTINCT ON (h.kind, h.id) h.kind, h.id, h.name, h.category, h.protection, h.url, h.info, h.visible, h.site_id,
               l.seq, ST_LineLocatePoint(v.geom, ST_ClosestPoint(v.geom, h.geom)) AS at, ST_Distance(v.geom, h.geom) AS off,
               ST_X(ST_Transform(ST_PointOnSurface(h.geom), 4326)) AS lon, ST_Y(ST_Transform(ST_PointOnSurface(h.geom), 4326)) AS lat
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
        JOIN source_index.heritage h ON ST_DWithin(h.geom, v.geom, %(near)s)
        WHERE l.route_id = %(r)s AND h.kind IN ('lokalitet', 'enkeltminne')
        ORDER BY h.kind, h.id, ST_Distance(v.geom, h.geom)""", {"r": route, "near": HERITAGE_M})
    sites: dict[str, dict] = {}
    for h in sorted(found, key=lambda h: h["kind"] != "lokalitet"):      # a site before its monuments
        # A monument names its site by the site's number; the site itself is that number and its version ("281482-0").
        key = h["site_id"] or (h["id"].split("-")[0] if h["kind"] == "lokalitet" else f"{h['kind']}:{h['id']}")
        at_m = _metres(walked, h["seq"], h["at"])
        if key in sites:
            s = sites[key]
            s["monuments"] += h["kind"] == "enkeltminne"
            s["info"] = s["info"] or h["info"]
            s["at_m"], s["off_m"] = (at_m, h["off"]) if h["off"] < s["off_m"] else (s["at_m"], s["off_m"])
        else:
            sites[key] = {**h, "at_m": at_m, "off_m": h["off"], "monuments": int(h["kind"] == "enkeltminne")}
    return sorted(sites.values(), key=lambda s: s["at_m"])


def _signs(conn, route: str, code: str, area: str, walked: on_route.Route) -> list[dict]:
    """The sign sites the route passes, in order, with the signs that point along it."""
    out = []
    for site in signs.listing(conn, area)["sites"]:
        if code not in site["routes"] or site["no_signs"]:
            continue
        try:
            pos = on_route.on(conn, [route], site["lon"], site["lat"])
            at_m = walked.metres_at(pos)
        except (Invalid, NotFound):     # a site further off the route than a click on it would be: no km
            at_m = None
        boards = [s for p in site["posts"] if not p["taken_out"] for s in p["signs"]
                  if not s.get("taken_out") and code in (s.get("routes") or [])]
        out.append({"name": site["name"] or (f"by {site['near']['name']}" if site.get("near") else "A sign site"),
                    "code": site.get("code"), "at_m": at_m, "lon": site["lon"], "lat": site["lat"], "signs": boards})
    return sorted(out, key=lambda s: (s["at_m"] is None, s["at_m"] or 0))


# --- the page -------------------------------------------------------------------------------------------------------

def _hm(minutes: int | None) -> str:
    if minutes is None:
        return "–"
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def _km(m: float | None, digits: int = 1) -> str:
    return "–" if m is None else f"{m / 1000:.{digits}f} km".replace(".", ",")


def _length(m: float) -> str:
    """A length as one says it: metres under a kilometre."""
    return f"{round(m)} m" if m < 1000 else _km(m)


def _first(text: str | None) -> str:
    return (text or "")[:1].upper() + (text or "")[1:]


def _metres_text(m: float | None) -> str:
    return "–" if m is None else f"{round(m):,} m".replace(",", " ")


def _paras(text: str) -> str:
    return "".join(f"<p>{e(p.strip())}</p>" for p in text.replace("\r", "").split("\n\n") if p.strip())


STONE = ('<svg width="34" height="32" viewBox="0 0 32 30" aria-hidden="true">'
         '<path d="M3.6 10.2C4.8 4.9 9.9 2.4 16 2.6c6.6.2 11.5 2.5 12.6 8.1 1 5.1.4 10.4-2.6 14.2-2.8 3.5-7.6 4.6-12.3 4.2'
         '-5-.4-8.9-2.4-10.4-6.6C2 18.8 2.8 14.3 3.6 10.2z" fill="#ebe7de"/>'
         '<path d="M8.4 8.9c5-.6 10-.7 15-.1l-.2 4.3c-1.6-.1-3.2-.1-4.8-.1l.3 10.6-4.7.1-.2-10.6c-1.8 0-3.5.1-5.2.3z" fill="#d3161b"/></svg>')


def _mercator(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 256 * 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def _map(card: dict) -> str:
    """Kartverket's topographic tiles under the route, its places, issues, heritage sites and sign sites."""
    lines = [x for x in card.get("lines", []) if x["coords"]]
    if not lines:
        return '<p class="none">The route has no line to draw.</p>'
    points = [p for x in lines for p in x["coords"]]
    lo_lon, hi_lon = min(p[0] for p in points), max(p[0] for p in points)
    lo_lat, hi_lat = min(p[1] for p in points), max(p[1] for p in points)
    z = 8
    for zoom in range(16, 7, -1):          # the closest that holds it all, with some land round it
        x0, y1 = _mercator(lo_lon, lo_lat, zoom)
        x1, y0 = _mercator(hi_lon, hi_lat, zoom)
        if x1 - x0 <= WIDTH * 0.84 and y1 - y0 <= MAP_H * 0.84:
            z = zoom
            break
    x0, y1 = _mercator(lo_lon, lo_lat, z)
    x1, y0 = _mercator(hi_lon, hi_lat, z)
    # Between two zooms: the tiles drawn larger, so that the route fills the frame.
    f = max(1.0, min(1.9, WIDTH * 0.84 / max(x1 - x0, 1), MAP_H * 0.84 / max(y1 - y0, 1)))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ox, oy = cx - WIDTH / 2 / f, cy - MAP_H / 2 / f          # the frame's corner, in the zoom's own pixels

    def at(lon: float, lon_lat: float) -> tuple[float, float]:
        x, y = _mercator(lon, lon_lat, z)
        return round((x - ox) * f, 1), round((y - oy) * f, 1)

    # Tiles one zoom closer, at half size or a little more: sharper on paper.
    tiles, side = [], 128 * f
    for tx in range(math.floor(ox / 128), math.floor((ox + WIDTH / f) / 128) + 1):
        for ty in range(math.floor(oy / 128), math.floor((oy + MAP_H / f) / 128) + 1):
            tiles.append(f'<img src="{TILES}/{z + 1}/{ty}/{tx}.png" alt="" style="left:{(tx * 128 - ox) * f:.1f}px;'
                         f'top:{(ty * 128 - oy) * f:.1f}px;width:{side + 0.6:.1f}px;height:{side + 0.6:.1f}px">')
    paths = []
    for x in lines:
        d = "M" + " L".join(f"{a},{b}" for a, b in (at(*p) for p in x["coords"]))
        style = "ice" if x["kind"] == "glacier" else "water" if x["kind"] == "water" else "route" if x["marked"] is not False else "route unmarked"
        paths.append(f'<path class="casing" d="{d}"/><path class="{style}" d="{d}"/>')
    marks = []
    for h in card.get("heritage") or []:
        cx, cy = at(h["lon"], h["lat"])
        marks.append(f'<rect class="heritage" x="{cx - 4}" y="{cy - 4}" width="8" height="8" transform="rotate(45 {cx} {cy})"/>')
    for s in card.get("signs") or []:
        cx, cy = at(s["lon"], s["lat"])
        marks.append(f'<circle class="sign" cx="{cx}" cy="{cy}" r="4"/>')
    named: list[tuple[str, float, float]] = []
    for p in card["facts"]["points"]:
        spot = _spot(card, p.get("at_m")) if p["what"] == "place" else None
        if spot:
            cx, cy = at(*spot)
            marks.append(f'<circle class="place {e(p.get("kind") or "")}" cx="{cx}" cy="{cy}" r="5.5"/>')
            if any(n == p["name"] and abs(nx - cx) < 60 and abs(ny - cy) < 30 for n, nx, ny in named):
                continue            # a place with two arrival points is named once
            named.append((p["name"], cx, cy))
            left = cx > WIDTH - 140         # near the right edge the name reads leftwards, inside the frame
            marks.append(f'<text class="label" x="{cx - 9 if left else cx + 9}" y="{cy + 4}"{" text-anchor=" + chr(34) + "end" + chr(34) if left else ""}>'
                         f'{e(p["name"])}</text>')
    for x in card["issues"]:
        if x.get("lon") is None:
            continue
        cx, cy = at(x["lon"], x["lat"])
        marks.append(f'<circle class="issue {"notice" if x["notice"] else ""}" cx="{cx}" cy="{cy}" r="8"/>'
                     f'<text class="n" x="{cx}" y="{cy + 3.5}">{x["n"]}</text>')
    for p in {tuple(lines[0]["coords"][0]), tuple(lines[-1]["coords"][-1])}:      # where it begins and ends
        cx, cy = at(*p)
        marks.append(f'<circle class="end" cx="{cx}" cy="{cy}" r="4"/>')
    scale = _scale(z, (lo_lat + hi_lat) / 2, f)
    return (f'<div class="map" style="width:{WIDTH}px;height:{MAP_H}px">{"".join(tiles)}'
            f'<svg viewBox="0 0 {WIDTH} {MAP_H}" width="{WIDTH}" height="{MAP_H}">{"".join(paths)}{"".join(marks)}{scale}</svg>'
            '<div class="credit">© Kartverket</div></div>')


def _spot(card: dict, metres: float | None) -> tuple[float, float] | None:
    """Where on the map a point this far along the route is: from the heights, a point every 10 m, else along its line."""
    if metres is None:
        return None
    samples = [s for s in (card.get("heights") or {}).get("samples") or [] if s[3] is not None]
    if samples:
        best = min(samples, key=lambda s: abs(s[0] - metres))
        return best[3], best[4]
    walked = 0.0
    for x in card.get("lines") or []:
        for (a, b), (c, d) in zip(x["coords"], x["coords"][1:]):
            step = math.hypot((c - a) * 111320 * math.cos(math.radians(b)), (d - b) * 110540)
            if walked + step >= metres:
                t = (metres - walked) / step if step else 0
                return a + (c - a) * t, b + (d - b) * t
            walked += step
    return None


def _scale(z: int, lat: float, f: float = 1.0) -> str:
    """A scale bar of a round length, bottom left."""
    per_px = 156543.03 * math.cos(math.radians(lat)) / 2 ** z / f
    metres = next(m for m in (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000) if m / per_px >= 80)
    w, y = metres / per_px, MAP_H - 16
    said = f"{metres // 1000} km" if metres >= 1000 else f"{metres} m"
    return (f'<g class="scale"><rect x="10" y="{y - 14}" width="{w + 16}" height="22" rx="3"/>'
            f'<path d="M18 {y}h{w:.0f}M18 {y - 5}v5M{18 + w:.0f} {y - 5}v5"/><text x="{18 + w / 2:.0f}" y="{y - 3}">{said}</text></g>')


def _profile(card: dict) -> str:
    """Heights along the route, with the places passed and where the issues are."""
    h = card.get("heights") or {}
    samples = [(m, z, ice) for m, z, ice, *_ in h.get("samples") or []]
    known = [s for s in samples if s[1] is not None]
    if len(known) < 2:
        return '<p class="none">No heights are known for its line yet.</p>'
    total = max(card["metres"], known[-1][0]) or 1
    zs = [s[1] for s in known]
    step = 100 if max(zs) - min(zs) > 300 else 50
    lo, hi = math.floor(min(zs) / step) * step, math.ceil(max(zs) / step) * step
    left, right, top, bottom = 46, 10, 14, 26
    w, hgt = WIDTH - left - right, PROFILE_H - top - bottom

    def x(m: float) -> float:
        return round(left + m / total * w, 1)

    def y(z: float) -> float:
        return round(top + (hi - z) / (hi - lo or 1) * hgt, 1)

    runs, run = [], []
    for m, z, ice in samples:
        if z is None:
            if run:
                runs.append(run)
            run = []
        else:
            run.append((m, z, ice))
    if run:
        runs.append(run)
    shapes = []
    for r in runs:
        line = " L".join(f"{x(m)},{y(z)}" for m, z, _ in r)
        shapes.append(f'<path class="ground" d="M{x(r[0][0])},{y(lo)} L{line} L{x(r[-1][0])},{y(lo)}Z"/><path class="line" d="M{line}"/>')
        for i in range(1, len(r)):
            if r[i][2] and r[i - 1][2]:
                shapes.append(f'<path class="ice" d="M{x(r[i - 1][0])},{y(r[i - 1][1])} L{x(r[i][0])},{y(r[i][1])}"/>')
    km_step = next(k for k in (0.5, 1, 2, 5, 10, 20) if total / 1000 / k <= 12)
    ticks = []
    k = 0.0
    while k * 1000 <= total + 1:
        ticks.append(f'<path class="tick" d="M{x(k * 1000)},{top + hgt}v4"/><text class="km" x="{x(k * 1000)}" y="{top + hgt + 16}">{k:g}</text>')
        k += km_step
    for z in sorted({lo, hi, round((lo + hi) / 2 / step) * step}):
        said = f"{z:,.0f}".replace(",", " ") + (" m" if z == hi else "")
        ticks.append(f'<path class="grid" d="M{left},{y(z)}H{left + w}"/><text class="z" x="{left - 10}" y="{y(z) + 4}">{said}</text>')
    marks, said, rows = [], [], [[], [], []]     # names that would run into one another go a row lower
    for p in card["facts"]["points"]:
        if p["what"] == "place" and p.get("at_m") is not None:
            if any(n == p["name"] and abs(m - p["at_m"]) < total * 0.05 for n, m in said):
                continue            # a place with two arrival points is named once
            said.append((p["name"], p["at_m"]))
            near_end = p["at_m"] > total * 0.75          # a name near the right end reads leftwards, inside the frame
            at, wide = x(p["at_m"]), len(p["name"]) * 6.0 + 6
            span = (at - wide, at) if near_end else (at, at + wide)
            row = next((i for i, r in enumerate(rows) if all(span[1] < a or span[0] > b for a, b in r)), len(rows) - 1)
            rows[row].append(span)
            anchor = ' text-anchor="end"' if near_end else ""
            marks.append(f'<path class="placemark" d="M{at},{top}V{top + hgt}"/>'
                         f'<text class="placename" x="{at + (-3 if near_end else 3)}" y="{top + 9 + 11 * row}"{anchor}>{e(p["name"])}</text>')
    for i in card["issues"]:
        m = i.get("at_m") if i.get("at_m") is not None else (i["parts"][0]["from_m"] if i.get("parts") else None)
        if m is not None:
            marks.append(f'<circle class="issue {"notice" if i["notice"] else ""}" cx="{x(m)}" cy="{top + hgt}" r="7"/>'
                         f'<text class="n" x="{x(m)}" y="{top + hgt + 3.5}">{i["n"]}</text>')
    return (f'<svg class="profile" viewBox="0 0 {WIDTH} {PROFILE_H}" width="{WIDTH}" height="{PROFILE_H}">'
            f'{"".join(ticks)}{"".join(shapes)}{"".join(marks)}'
            f'<text class="unit" x="{left + w}" y="{PROFILE_H - 2}">km</text></svg>')


def _facts(card: dict) -> str:
    f, whole = card["facts"]["route"], card["facts"]["whole"]
    there, back = whole.get("there") or {}, whole.get("back") or {}
    length = (f"{_km(whole['on_foot_m'])} on foot"
              + (f", {_km(whole['over_glacier_m'])} of it over glacier" if whole.get("over_glacier_m") else "")
              + (f", and {_km(whole['over_water_m'])} over water" if whole.get("over_water_m") else ""))
    rows = [("Length", length)]
    if there:
        rows.append(("Climbs", (f"{_metres_text(there['up_m'])} up and {_metres_text(there['down_m'])} down, "
                                f"walked from {e(f['from'] or 'its start')}")))
        rows.append(("Time", (f"{_hm(there.get('minutes'))} that way, {_hm(back.get('minutes'))} back "
                              "<span class='sub'>(Naismith's rule, no rests)</span>")))
    if whole.get("high_m") is not None:
        rows.append(("Height", f"from {_metres_text(whole['low_m'])} to {_metres_text(whole['high_m'])} above sea level"))
    grading = {"G": "Green, easy", "B": "Blue, medium", "R": "Red, demanding", "S": "Black, expert"}
    if f.get("grading"):
        rows.append(("Grading", f'<span class="grade {f["grading"]}"></span>{grading.get(f["grading"], f["grading"])}'))
    elif f.get("grading_by_numbers"):
        rows.append(("Grading", (f'<span class="grade {f["grading_by_numbers"]}"></span>{grading.get(f["grading_by_numbers"])} '
                                 "<span class='sub'>by its length and climb alone: nobody has set it</span>")))
    if whole.get("not_marked_m"):
        rows.append(("Marking", f"{_km(whole['not_marked_m'])} of it is not marked"))
    if f.get("maintained_by"):
        rows.append(("Looked after by", e(", ".join(m.replace("DNT | ", "") for m in f["maintained_by"]))))
    state = [w for w, on in (("discontinued", f.get("discontinued")), ("not published", f.get("unpublished"))) if on]
    if state:
        rows.append(("State", e(", ".join(state))))
    return '<dl class="facts">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"


def _where(x: dict) -> str:
    if x["about"] == "route":
        return "the whole route"
    if x.get("parts"):
        return ", ".join(f"{_km(p['from_m'])}–{_km(p['to_m'])}".replace(" km–", "–") for p in x["parts"])
    return f"at {_km(x['at_m'])}" if x.get("at_m") is not None else "not on the route any more"


def _issue_rows(found: list[dict]) -> str:
    rows = []
    for x in found:
        when = x["made_at"].strftime("%-d %b %Y") if hasattr(x["made_at"], "strftime") else str(x["made_at"])[:10]
        valid = ""
        if x["notice"]:
            valid = f" until {x['valid_until']}" if x.get("valid_until") else " until further notice"
        rows.append(f'<tr><td class="num"><span class="issue {"notice" if x["notice"] else ""}">{x.get("n", "")}</span></td>'
                    f'<td><strong>{e(x["says"][:1].upper() + x["says"][1:])}</strong>{valid}<div>{e(x["text"])}</div></td>'
                    f'<td class="where">{_where(x)}</td><td class="when">{e(x["state"])}<div class="sub">{when}</div></td></tr>')
    return "<table class='list issues'>" + "".join(rows) + "</table>"


def render(card: dict, owners: dict | None = None, by: str | None = None) -> str:
    """The card as a page."""
    f, parts = card["facts"], card["parts"]
    route = f["route"]
    title = f"{card['code']} {route.get('name') or ''}".strip()
    body = [(f'<header class="band">{STONE}<div><div class="kind">Route card</div><h1><span class="code">{e(card["code"])}</span> '
             f'{e(route.get("name") or "")}</h1></div></header>')]
    for n in card["notices"]:
        body.append(f'<div class="notice"><strong>{e(ISSUE_WORDS.get(n["kind"], n["says"]))}</strong> '
                    f'{"until " + e(str(n["valid_until"])) if n.get("valid_until") else "until further notice"}: {e(n["text"])}</div>')
    body.append(_facts(card))
    if "map" in parts:
        body.append(f'<section class="mapbox">{_map(card)}<div class="legend"><span><i class="l route"></i>marked</span>'
                    '<span><i class="l unmarked"></i>not marked</span><span><i class="l ice"></i>glacier</span>'
                    '<span><i class="d place"></i>hut, parking, place</span><span><i class="d issue"></i>issue</span>'
                    + ('<span><i class="d heritage"></i>heritage site</span>' if "heritage" in parts else "")
                    + ('<span><i class="d sign"></i>sign site</span>' if "signs" in parts else "") + "</div></section>")
    if "profile" in parts:
        body.append(f"<section><h2>Height profile</h2>{_profile(card)}</section>")
    if "description" in parts:
        theirs = card.get("utno") or {}
        ours = (route.get("description"), route.get("description_back"))
        texts = ours if any(ours) else (theirs.get("there"), theirs.get("back"))
        body.append("<section><h2>Description</h2>" + (
            (f'<p class="sub">From UT.no (ut.no/rutebeskrivelse/{theirs["number"]}): none of our own is written yet.</p>'
             if not any(ours) else "") + _both_ways(route, *texts)
            if any(texts) else '<p class="none">None is written yet.</p>')
                    + (f'<p class="sub">On UT.no: ut.no/rutebeskrivelse/{e(str(route["utno_id"]))}</p>' if route.get("utno_id") else "") + "</section>")
    if "walk" in parts:
        rows = []
        for s in f["sections"]:
            ground = ", ".join(k for k, _ in sorted((s.get("ground") or {}).items(), key=lambda g: -g[1])[:2] if k != "not said")
            what = GROUND.get(s["kind"], s["kind"]) + ("" if s["marked"] is not False else ", not marked")
            rows.append(f"<tr><td class='km'>{_km(s['from_m'])}</td><td><strong>{e(_first(s['from']))}</strong> → {e(s['to'] or 'its end')}"
                        f"<div class='sub'>{e(what)}{(', ' + e(ground)) if ground else ''}"
                        f"{(', with ' + e(', '.join(s['also']))) if s.get('also') else ''}</div></td>"
                        f"<td class='num'>{_length(s['length_m'])}</td><td class='num'>↑{s['up_m']} ↓{s['down_m']}</td>"
                        f"<td class='num'>{_hm(s['minutes'])}</td></tr>")
        steep = f.get("steep") or []
        body.append("<section><h2>Section by section</h2><table class='list walk'><tr><th>km</th><th>From → to</th><th>Length</th>"
                    "<th>Up, down (m)</th><th>Time</th></tr>" + "".join(rows) + "</table>"
                    + ("<p class='sub'>Steep: " + "; ".join(f"{_km(s['from_m'])}–{_km(s['to_m'])} {s['going']}, {s['percent']} %" for s in steep[:8])
                       + ".</p>" if steep else "") + "</section>")
    if "heritage" in parts:
        found = card.get("heritage") or []
        rows = [f"<tr><td class='km'>{_km(h['at_m'])}</td><td><strong>{e(h['name'] or ('A heritage site' if h['kind'] == 'lokalitet' else 'A monument'))}</strong>"
                f"{(' <span class=sub>(' + str(h['monuments']) + ' monuments)</span>') if h['monuments'] > 1 else ''}"
                f"<div class='sub'>{e(PROTECTION.get(h['protection'] or '', h['protection'] or ''))}"
                f"{(', no. ' + e(str(h['site_id'] or h['id'].split('-')[0]))) if h.get('id') else ''}"
                f"{', not visible on the ground' if h['visible'] is False else ''}, {round(h['off_m'])} m from the route</div>"
                f"{('<div class=info>' + e(_cut(h['info'], 260)) + '</div>') if h.get('info') else ''}</td></tr>" for h in found]
        body.append("<section><h2>Heritage sites near it</h2>"
                    + (f"<table class='list heritage'>{''.join(rows)}</table><p class='sub'>Within {HERITAGE_M} m of the route, "
                       "from Kulturminnesøk (Riksantikvaren). Automatically protected sites may not be disturbed.</p>"
                       if found else f"<p class='none'>None is known within {HERITAGE_M} m of it.</p>") + "</section>")
    if "issues" in parts:
        body.append("<section><h2>Open issues and notices</h2>"
                    + (_issue_rows(card["issues"]) if card["issues"] else "<p class='none'>Nothing is open on this route.</p>") + "</section>")
    if "upkeep" in parts:
        s = card.get("upkeep") or {}
        def last(kind: str, word: str) -> str:
            x = s.get(kind)
            if not x:
                return f"<div>{word}: <strong>never reported</strong></div>"
            day = str(x["done_on"])[:4] if x.get("year_only") else str(x["done_on"])
            return (f"<div>{word}: <strong>{day}</strong>{', part of it' if not x.get('all_of_it') else ''}"
                    f"{', due' if x.get('due') else ''}</div>")
        rows, before = [], None
        for r in card.get("reports") or []:      # the same text as the report above is not said again
            said = r.get("text") if r.get("text") != before else None
            before = r.get("text")
            rows.append(f"<tr><td class='when'>{str(r['done_on'])[:4] if r['year_only'] else r['done_on']}</td>"
                        f"<td><strong>{e(r['says'][:1].upper() + r['says'][1:])}</strong>"
                        f"{('<div>' + e(_cut(said, 200)) + '</div>') if said else ''}</td></tr>")
        body.append("<section><h2>Inspections and work parties</h2>" + last("inspection", "Last inspected") + last("work", "Last work party")
                    + (f"<table class='list reports'>{''.join(rows)}</table>" if rows else "") + "</section>")
    if "photos" in parts:
        shots = card.get("photos") or []
        body.append("<section><h2>Photos near it</h2>" + (
            "<div class='photos'>" + "".join(f"<figure><img src='/api/v1/photos/{p['id']}/thumb' alt=''>"
                                              f"<figcaption>{e(p.get('caption') or '')} {str(p['taken_at'])[:10] if p.get('taken_at') else ''}</figcaption></figure>"
                                              for p in shots) + "</div>" if shots else "<p class='none'>None taken near it.</p>") + "</section>")
    if "signs" in parts:
        rows = []
        arrow = {"venstre": "←", "høyre": "→", "rett fram": "↑"}
        for s in card.get("signs") or []:
            boards = "".join(f"<div>{arrow.get(b.get('direction'), '')} {e(b.get('text') or 'no name')}"
                             f"{(' ' + str(b['km']).replace('.', ',') + ' km') if b.get('km') is not None else ''}</div>" for b in s["signs"])
            rows.append(f"<tr><td class='km'>{_km(s['at_m'])}</td><td><strong>{e(s['name'])}</strong>"
                        f"{(' <span class=sub>' + e(s['code']) + '</span>') if s.get('code') else ''}{boards}</td></tr>")
        body.append("<section><h2>Signs along it</h2>" + (f"<table class='list signs'>{''.join(rows)}</table>" if rows
                                                          else "<p class='none'>No sign sites on it.</p>") + "</section>")
    if "owners" in parts and owners is not None:
        body.append("<section><h2>Who owns the ground</h2>" + _owners(owners) + "</section>")
    made = card["made"].strftime("%-d %b %Y")
    body.append(f"<footer>Made {made}{' by ' + e(by) if by else ''} in Stiflyt v2. Sources: Kartverket (Turrutebasen, "
                "topographic map, heights, property map), Riksantikvaren (Kulturminnesøk), DNT's own records."
                + (" Names and addresses of owners are personal data: for the work on the route, not to be passed on." if owners else "")
                + "</footer>")
    return PAGE.format(title=e(title), body="".join(body))


def _both_ways(route: dict, there: str | None, back: str | None) -> str:
    """A route's description each way, under the places it goes between: the way it is walked first."""
    a, b = route.get("from"), route.get("to")
    heads = ((f"{a} → {b}", f"{b} → {a}") if a and b and a != b
             else (f"From {a}, as the route runs", f"From {a}, the other way round") if a else ("As the route is walked", "The other way"))
    said = [(h, t) for h, t in zip(heads, (there, back), strict=True) if t]
    if len(said) == 1 and not back:
        return _paras(said[0][1])
    return "".join(f'<h3 class="way">{e(h)}</h3>{_paras(t)}' for h, t in said)


def _cut(text: str | None, most: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= most else text[:most].rsplit(" ", 1)[0] + " …"


def _owners(found: dict) -> str:
    if found.get("refused"):
        return f"<p class='none'>{e(found['refused'])}</p>"
    rows = []
    for p in found["properties"]:
        spans = ", ".join(f"{_km(s['from_m'])}–{_km(s['to_m'])}".replace(" km–", "–") for s in p["spans"])
        who = "".join(f"<div>{e(o['name'] or '')}{(' ' + e(o['share'])) if o.get('share') and o['share'] != '1/1' else ''}"
                      f"{(' <span class=sub>(' + e(o['said']) + ')</span>') if o.get('role') != 'owner' and o.get('said') else ''}"
                      f"{''.join('<div class=sub>' + e(n['name'] or '') + '</div>' for n in o.get('owners') or [])}</div>"
                      for o in p.get("owners") or [])
        rows.append(f"<tr><td class='km'>{spans}</td><td><strong>{e(p['label'])}</strong> {e(p.get('name') or '')}"
                    f"<div class='sub'>{e(p.get('municipality') or '')}</div></td><td>{who}</td></tr>")
    note = f"<p class='error'>{e(found['owners_unavailable'])}</p>" if found.get("owners_unavailable") else ""
    return note + (f"<table class='list owners'>{''.join(rows)}</table>" if rows else "<p class='none'>No property is known along it.</p>")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}: route card</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=Barlow+Semi+Condensed:wght@500;600;700&display=swap">
<style>
@page {{ size: A4; margin: 11mm 10mm; }}
:root {{ --ink: #16242e; --muted: #5a6872; --line: #dfe4e8; --band: #c8161d; --route: #7a1e2c; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #e8ecef; color: var(--ink); font: 14px/1.42 "Barlow", "Helvetica Neue", Arial, sans-serif; }}
.page {{ width: 758px; margin: 18px auto; background: #fff; padding: 0 20px 18px; box-shadow: 0 1px 6px #0002; }}
.tools {{ width: 758px; margin: 14px auto 0; text-align: right; }}
.tools button {{ font: 600 14px "Barlow Semi Condensed", sans-serif; padding: 6px 14px; border: 1px solid #b4bec6; border-radius: 4px; background: #fff; cursor: pointer; }}
header.band {{ display: flex; align-items: center; gap: 12px; margin: 0 -20px 12px; padding: 12px 20px; background: var(--band); color: #fff; }}
header.band svg {{ flex: none; filter: drop-shadow(0 1px 1px #0004); }}
header .kind {{ font: 600 12px "Barlow Semi Condensed", sans-serif; letter-spacing: .08em; text-transform: uppercase; opacity: .85; }}
h1 {{ margin: 0; font: 700 25px/1.1 "Barlow Semi Condensed", sans-serif; }}
h1 .code {{ font-weight: 700; margin-right: 4px; }}
h2 {{ font: 700 16px "Barlow Semi Condensed", sans-serif; margin: 16px 0 6px; padding-bottom: 3px; border-bottom: 2px solid var(--band); break-after: avoid; }}
section {{ break-inside: auto; }}
.notice {{ background: #fbdada; color: #7d1515; border-left: 4px solid #c62828; padding: 7px 10px; margin: 0 0 10px; }}
dl.facts {{ display: grid; grid-template-columns: 120px 1fr; gap: 3px 12px; margin: 0 0 12px; }}
dl.facts dt {{ color: var(--muted); }}
dl.facts dd {{ margin: 0; }}
.sub {{ color: var(--muted); font-size: 12.5px; }}
.none {{ color: var(--muted); }}
.error {{ color: #a12020; }}
.grade {{ display: inline-block; width: 11px; height: 11px; margin-right: 6px; vertical-align: -1px; border-radius: 2px; }}
.grade.G {{ background: #2e8b3a; }} .grade.B {{ background: #1565c0; }} .grade.R {{ background: #c62828; }} .grade.S {{ background: #111; }}
.mapbox {{ break-inside: avoid; }}
.map {{ position: relative; overflow: hidden; border: 1px solid var(--line); }}
.map img {{ position: absolute; width: 128px; height: 128px; }}
.map svg {{ position: absolute; left: 0; top: 0; }}
.map .casing {{ fill: none; stroke: #fff; stroke-width: 7; stroke-linecap: round; stroke-linejoin: round; opacity: .85; }}
.map .route {{ fill: none; stroke: var(--route); stroke-width: 3.6; stroke-linecap: round; stroke-linejoin: round; }}
.map .unmarked {{ stroke-dasharray: 7 5; }}
.map .ice {{ fill: none; stroke: #1f6fc5; stroke-width: 3.6; stroke-dasharray: 1 6; stroke-linecap: round; }}
.map .water {{ fill: none; stroke: #4a90c8; stroke-width: 2.5; stroke-dasharray: 2 4; }}
.map .place {{ fill: #fff; stroke: var(--route); stroke-width: 2.5; }}
.map .place.hut {{ fill: var(--route); stroke: #fff; }}
.map .label {{ font: 600 12.5px "Barlow Semi Condensed", sans-serif; fill: var(--ink); paint-order: stroke; stroke: #fff; stroke-width: 3.5px; stroke-linejoin: round; }}
.map .end {{ fill: var(--band); stroke: #fff; stroke-width: 2; }}
.map .heritage {{ fill: #8d5a2b; stroke: #fff; stroke-width: 1.5; }}
.map .sign {{ fill: #fff; stroke: #114232; stroke-width: 2.5; }}
.map .scale rect {{ fill: #fffd; }} .map .scale path {{ stroke: var(--ink); stroke-width: 1.5; fill: none; }}
.map .scale text {{ font: 600 11px "Barlow Semi Condensed", sans-serif; text-anchor: middle; }}
.map .credit {{ position: absolute; right: 0; bottom: 0; background: #fffc; font-size: 11px; padding: 1px 5px; }}
.issue {{ fill: #ef8f00; stroke: #fff; stroke-width: 2; }}
.issue.notice {{ fill: #c62828; }}
text.n {{ font: 700 10.5px "Barlow Semi Condensed", sans-serif; fill: #fff; text-anchor: middle; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 12px; color: var(--muted); margin-top: 4px; }}
.legend i {{ display: inline-block; vertical-align: middle; margin-right: 5px; }}
.legend i.l {{ width: 22px; height: 0; border-top: 3.5px solid var(--route); }}
.legend i.l.unmarked {{ border-top-style: dashed; }} .legend i.l.ice {{ border-top: 3.5px dotted #1f6fc5; }}
.legend i.d {{ width: 10px; height: 10px; border-radius: 50%; background: var(--route); }}
.legend i.d.issue {{ background: #ef8f00; }} .legend i.d.heritage {{ background: #8d5a2b; border-radius: 1px; transform: rotate(45deg); width: 8px; height: 8px; }}
.legend i.d.sign {{ background: #fff; border: 2px solid #114232; }}
svg.profile .ground {{ fill: #e9edf0; }} svg.profile .line {{ fill: none; stroke: var(--ink); stroke-width: 1.6; }}
svg.profile .ice {{ stroke: #1f6fc5; stroke-width: 3; }}
svg.profile .grid {{ stroke: #e3e7ea; }} svg.profile .tick {{ stroke: var(--muted); }}
svg.profile text {{ font: 500 11px "Barlow Semi Condensed", sans-serif; fill: var(--muted); }}
svg.profile .km {{ text-anchor: middle; }} svg.profile .z, svg.profile .unit {{ text-anchor: end; }}
svg.profile .placemark {{ stroke: var(--route); stroke-dasharray: 2 3; opacity: .6; }}
svg.profile .placename {{ fill: var(--route); font-weight: 600; paint-order: stroke; stroke: #fff; stroke-width: 3px; stroke-linejoin: round; }}
table.list {{ width: 100%; border-collapse: collapse; }}
table.list td, table.list th {{ padding: 5px 6px; border-top: 1px solid var(--line); vertical-align: top; text-align: left; }}
table.list th {{ font: 600 12px "Barlow Semi Condensed", sans-serif; color: var(--muted); border-top: 0; }}
table.list tr {{ break-inside: avoid; }}
td.km, td.when {{ white-space: nowrap; color: var(--muted); width: 1%; }}
td.num {{ white-space: nowrap; text-align: right; font-variant-numeric: tabular-nums; width: 1%; }}
td.where {{ white-space: nowrap; color: var(--muted); width: 1%; }}
table.issues span.issue {{ display: inline-block; min-width: 20px; height: 20px; border-radius: 10px; background: #ef8f00; color: #fff; font: 700 12px/20px "Barlow Semi Condensed", sans-serif; text-align: center; }}
table.issues span.issue.notice {{ background: #c62828; }}
.info {{ font-size: 12.5px; margin-top: 2px; }}
.photos {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }}
.photos figure {{ margin: 0; break-inside: avoid; }} .photos img {{ width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 3px; }}
.photos figcaption {{ font-size: 11.5px; color: var(--muted); }}
p {{ margin: 0 0 7px; }}
h3.way {{ font: 600 14px "Barlow Semi Condensed", sans-serif; color: var(--route); margin: 10px 0 3px; break-after: avoid; }}
footer {{ margin-top: 16px; padding-top: 6px; border-top: 1px solid var(--line); font-size: 11.5px; color: var(--muted); }}
@media print {{
  body {{ background: none; }} .tools {{ display: none; }}
  .page {{ width: auto; margin: 0; padding: 0; box-shadow: none; }}
  header.band {{ margin: 0 0 12px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  .notice, .grade, .legend i, table.issues span.issue {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
</style></head>
<body><div class="tools"><button onclick="print()">Print, or save as PDF</button></div><div class="page">{body}</div></body></html>
"""
