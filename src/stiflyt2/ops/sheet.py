"""The field sheet: one page per sign site, to print and take along when signs are put up or looked at.

As v1 had it: where the site is, on a map with its posts and the routes, and
what hangs on each post, with a box to tick for each sign. It is a page to
print (or to save as PDF from the print dialogue), with no script in it but
the print button: the map is Kartverket's own tiles laid side by side, with
the routes and the posts drawn over them.

It is written in Norwegian, like the list for whoever makes the signs: it
is read by the people who carry the signs up.
"""
from __future__ import annotations

import math
from datetime import date
from html import escape

from stiflyt2 import seasons
from stiflyt2.domain.places import NotFound
from stiflyt2.ops import signs

TILES = "https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png"
TILE = 256
MAP_W, MAP_H = 660, 430     # the map on the page, in CSS pixels
ZOOM, ZOOM_OUT = 16, 13     # as near as it is drawn, and as far out as it goes to get every post in
AROUND_M = 900              # routes this near the site are drawn
ARROW = {"venstre": "←", "høyre": "→", "rett fram": "↑"}


def _pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Where a position is on the Web Mercator map of the whole world, at a zoom."""
    n = TILE * 2 ** zoom
    y = (1 - math.asinh(math.tan(math.radians(max(-85.05, min(85.05, lat))))) / math.pi) / 2
    return (lon + 180) / 360 * n, y * n


def _map(site: dict, lines: list[list]) -> str:
    """The map of one site: tiles, the routes, and each post with its number."""
    posts = [p for p in site["posts"] if not p["taken_out"]]
    spots = [(p["lon"], p["lat"]) for p in posts] or [(site["lon"], site["lat"])]
    zoom = ZOOM
    while zoom > ZOOM_OUT:      # out, until every post is well inside the frame
        at = [_pixel(lon, lat, zoom) for lon, lat in spots]
        if (max(x for x, _ in at) - min(x for x, _ in at) <= MAP_W * 0.6
                and max(y for _, y in at) - min(y for _, y in at) <= MAP_H * 0.6):
            break
        zoom -= 1
    at = [_pixel(lon, lat, zoom) for lon, lat in spots]
    left = (min(x for x, _ in at) + max(x for x, _ in at)) / 2 - MAP_W / 2
    top = (min(y for _, y in at) + max(y for _, y in at)) / 2 - MAP_H / 2
    tiles = "".join(
        f'<img alt="" src="{TILES.format(z=zoom, x=tx, y=ty)}" style="left:{tx * TILE - left:.0f}px;top:{ty * TILE - top:.0f}px">'
        for tx in range(int(left // TILE), int((left + MAP_W) // TILE) + 1)
        for ty in range(int(top // TILE), int((top + MAP_H) // TILE) + 1))
    drawn = []
    for line in lines:
        points = " ".join(f"{x - left:.1f},{y - top:.1f}" for x, y in (_pixel(lon, lat, zoom) for lon, lat in line))
        drawn.append(f'<polyline points="{points}" class="casing"/><polyline points="{points}" class="route"/>')
    for n, (x, y) in enumerate(at, start=1):
        drawn.append(f'<rect x="{x - left - 9:.1f}" y="{y - top - 9:.1f}" width="18" height="18" rx="3" class="post"/>'
                     f'<text x="{x - left:.1f}" y="{y - top + 4.5:.1f}" class="no">{n if posts else ""}</text>')
    metres_per_px = 156543.034 * math.cos(math.radians(site["lat"])) / 2 ** zoom
    bar = next(m for m in (50, 100, 200, 500, 1000, 2000, 5000) if m / metres_per_px >= 60)
    drawn.append(f'<g transform="translate(14,{MAP_H - 16})"><rect x="-6" y="-16" width="{bar / metres_per_px + 12:.0f}" height="24" class="plate"/>'
                 f'<line x1="0" x2="{bar / metres_per_px:.0f}" y1="0" y2="0" class="bar"/>'
                 f'<text x="0" y="-5" class="scale">{bar if bar < 1000 else bar // 1000} {"m" if bar < 1000 else "km"}</text></g>'
                 f'<text x="{MAP_W - 12}" y="20" class="north">N ↑</text>')
    return (f'<div class="map" style="width:{MAP_W}px;height:{MAP_H}px">{tiles}'
            f'<svg viewBox="0 0 {MAP_W} {MAP_H}" width="{MAP_W}" height="{MAP_H}">{"".join(drawn)}</svg></div>')


def _routes_near(conn, site: dict) -> list[list]:
    """The lines of the summer's routes around a site, as longitude and latitude: the signs are the summer's, for now."""
    lines = []
    for (geometry,) in conn.execute("""
            SELECT ST_AsGeoJSON(ST_Transform(ST_Intersection(r.geom, ST_Buffer(c.at, %(around)s)), 4326), 6)::json
            FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833) AS at) c,
                 core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            WHERE e.status = 'active' AND ST_DWithin(r.geom, c.at, %(around)s)
              AND EXISTS (SELECT 1 FROM core.route_leg g JOIN core.route t ON t.id = g.route_id AND t.status = 'active'
                          AND t.season = %(summer)s AND t.discontinued_since IS NULL AND t.unpublished_why IS NULL
                          WHERE g.edge_id = e.id)""",
            {"lon": site["lon"], "lat": site["lat"], "around": AROUND_M, "summer": seasons.SUMMER}):
        if geometry["type"] == "LineString":
            lines.append(geometry["coordinates"])
        elif geometry["type"] == "MultiLineString":
            lines.extend(geometry["coordinates"])
    return [line for line in lines if len(line) > 1]


def _status(sign: dict) -> str:
    if sign["to_order"]:
        return "skal bestilles"
    before = sign["ordered"]
    if not before:
        return ""
    return f"bestilling {before['order']}" + (", oppe" if before["up"] else ", ikke oppe") + (
        ", endret siden" if before["changed"] else "")


def _site(conn, site: dict) -> str:
    said = escape
    label = site["name"] or ("Skiltsted på " if site["free"] else "Kryss ") + ", ".join(site["routes"]) + (
        f", ved {site['near']['name']}" if site["near"] else "")
    posts = [p for p in site["posts"] if not p["taken_out"]]
    tables = []
    for n, post in enumerate(posts, start=1):
        rows = []
        if post["name_sign"]:
            top = post["name_sign"]
            rows.append(f'<tr><td class="tick"></td><td class="text">{said(top["text"] or "")}</td><td></td><td></td>'
                        f'<td>trehvit</td><td>stedsnavn, øverst</td><td>{said(_status(top))}</td></tr>')
        for sign in post["signs"]:
            if sign["taken_out"]:
                continue
            rows.append(
                f'<tr><td class="tick"></td><td class="text">{said(sign["text"] or "(uten navn)")}</td>'
                f'<td class="km">{said(signs.km_text(sign["km"]))}</td>'
                f'<td class="arrow">{ARROW.get(sign["direction"], "")} {said(sign["direction"] or "")}</td>'
                f'<td>{said(sign["colour"])}</td><td>{said(", ".join(sign["routes"]))}</td><td>{said(_status(sign))}</td></tr>')
        tables.append(
            f'<h2>Stolpe {n}<span> {said(post["utm"])}{"" if not post["assumed"] else ", antatt plassering"}</span></h2>'
            '<table><thead><tr><th>Oppe</th><th>Tekst på skiltet</th><th>Km</th><th>Pil</th><th>Farge</th><th>Ruter</th>'
            f'<th>Status</th></tr></thead><tbody>{"".join(rows) or "<tr><td colspan=7>Ingen skilt.</td></tr>"}</tbody></table>')
    back = "<br>".join(said(line) for line in site["back_text"].splitlines())
    send = ", ".join(filter(None, (site["send_to_name"], site["send_to_address"])))
    return (f'<section class="sheet"><header><h1>{said(label)}</h1>'
            f'<p>{said(site["code"] + ". " if site["code"] else "")}Ruter: {said(", ".join(site["routes"]))}.'
            f'{" Deles med " + said(", ".join(site["other_areas"])) + "." if site["other_areas"] else ""}</p></header>'
            f'{_map(site, _routes_near(conn, site))}'
            f'<dl><dt>Baksidetekst</dt><dd>{back}</dd>'
            f'<dt>Posisjon</dt><dd>{site["lat"]:.5f}° N, {site["lon"]:.5f}° Ø</dd>'
            f'{f"<dt>Skilt sendes til</dt><dd>{said(send)}</dd>" if send else ""}</dl>'
            f'{"".join(tables)}'
            '<h2>Merknader</h2><div class="notes"></div></section>')


def page(conn, area: str, site: str | None = None, order: int | None = None) -> str:
    """The field sheets of an area as one page to print: of one site, of the sites an order has signs at, or of
    every site that is not said to have no signs."""
    found = signs.listing(conn, area)
    sites = [s for s in found["sites"] if not s["no_signs"]]
    what = f"Skiltsteder i {area}"
    if site:
        sites = [s for s in found["sites"] if s["key"] == site]
        if not sites:
            raise NotFound("no such sign site in that area")
        what = sites[0]["name"] or what
    elif order is not None:
        ordered = {row["key"] for row in signs.order(conn, order)["rows"]}
        sites = [s for s in sites if any(g["key"] in ordered for p in s["posts"] for g in [*p["signs"], *([p["name_sign"]] if p["name_sign"] else [])])]
        what = f"Bestilling {order}"
    body = "".join(_site(conn, s) for s in sites) or "<p>Ingen skiltsteder.</p>"
    return f"""<!doctype html>
<html lang="nb"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feltark: {escape(what)}</title>
<style>
  @page {{ size: A4; margin: 13mm; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 13px/1.4 "Helvetica Neue", Arial, sans-serif; color: #16242e; }}
  .tools {{ padding: 10px 16px; background: #f1f4f6; border-bottom: 1px solid #dfe4e8; }}
  .tools button {{ font: inherit; padding: 5px 12px; }}
  .sheet {{ width: {MAP_W}px; margin: 18px auto 40px; break-after: page; }}
  .sheet:last-of-type {{ break-after: auto; }}
  h1 {{ font-size: 22px; margin: 0; }} header p {{ margin: 2px 0 8px; color: #44525c; }}
  h2 {{ font-size: 14px; margin: 14px 0 4px; }} h2 span {{ font-weight: 400; color: #44525c; }}
  .map {{ position: relative; overflow: hidden; border: 1px solid #16242e; }}
  .map img {{ position: absolute; width: {TILE}px; height: {TILE}px; }}
  .map svg {{ position: absolute; inset: 0; }}
  .casing {{ fill: none; stroke: #fff; stroke-width: 6; stroke-linejoin: round; opacity: .8; }}
  .route {{ fill: none; stroke: #7a1e2c; stroke-width: 2.5; stroke-linejoin: round; }}
  .post {{ fill: #fff; stroke: #16242e; stroke-width: 2; }}
  .no {{ font: 700 12px Arial, sans-serif; text-anchor: middle; }}
  .plate {{ fill: #fff; opacity: .85; }} .bar {{ stroke: #16242e; stroke-width: 2; }}
  .scale {{ font: 11px Arial, sans-serif; }} .north {{ font: 700 13px Arial, sans-serif; text-anchor: end; paint-order: stroke; stroke: #fff; stroke-width: 3px; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 10px 0 0; }}
  dt {{ color: #44525c; }} dd {{ margin: 0; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border: 1px solid #9aa5ad; padding: 4px 6px; text-align: left; vertical-align: top; }}
  th {{ font-weight: 600; background: #f1f4f6; }}
  td.tick {{ width: 34px; }} td.text {{ font-weight: 600; }} td.km {{ white-space: nowrap; text-align: right; }} td.arrow {{ white-space: nowrap; }}
  .notes {{ height: 70px; border: 1px solid #9aa5ad; }}
  @media print {{ .tools {{ display: none; }} .sheet {{ margin: 0 auto; }} th {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
</style></head>
<body><div class="tools"><button onclick="print()">Skriv ut, eller lagre som PDF</button>
 {len(sites)} {"skiltsted" if len(sites) == 1 else "skiltsteder"}, ett per side. Skrevet ut {date.today():%d.%m.%Y}. Kart: © Kartverket.</div>
{body}</body></html>"""
