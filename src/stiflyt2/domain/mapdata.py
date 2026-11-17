"""What map data an area has to lay its routes on: mapped paths, roads, and OpenStreetMap to compare with.

Paths and OpenStreetMap are fetched per bounding box, roads per municipality.
An area is a route code prefix; its extent is where Turrutebasen's routes with
that prefix lie.
"""
from __future__ import annotations

from psycopg.rows import dict_row

MARGIN_M = 2000       # map data is fetched this far beyond the area's routes
NEAR_M = 1000         # a municipality counts for an area when one of its place names lies this near a route


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"source_index.{table}",)).fetchone()[0]


def municipalities(conn, area: str) -> list[dict]:
    """The municipalities the area's routes run through, as Stedsnavn places them."""
    return _rows(conn, """
        SELECT p.municipality_no AS no, min(p.municipality) AS name
        FROM source_index.route r JOIN source_index.place p ON ST_DWithin(p.geom, r.geom, %(near)s)
        WHERE r.area_prefix = %(a)s AND p.municipality_no IS NOT NULL
        GROUP BY 1 ORDER BY 2""", {"a": area.lower(), "near": NEAR_M})


def box(conn, area: str) -> tuple[int, int, int, int] | None:
    """The bounding box to fetch for the area, in EPSG:25833, on whole kilometres."""
    found = conn.execute("""
        SELECT floor((ST_XMin(e) - %(m)s) / 1000) * 1000, floor((ST_YMin(e) - %(m)s) / 1000) * 1000,
               ceil((ST_XMax(e) + %(m)s) / 1000) * 1000, ceil((ST_YMax(e) + %(m)s) / 1000) * 1000
        FROM (SELECT ST_Extent(geom) AS e FROM source_index.route WHERE area_prefix = %(a)s) x
        WHERE e IS NOT NULL""", {"a": area.lower(), "m": MARGIN_M}).fetchone()
    return tuple(int(v) for v in found) if found else None


def _checked(conn, source: str, scopes: list[str]) -> object:
    """When these scopes of a source were last looked at for changes, by the refresh log."""
    return conn.execute(
        """SELECT max(coalesce(finished_at, started_at)) FROM import_control.check_log
           WHERE source_key = %s AND scope = ANY(%s) AND outcome IN ('unchanged', 'imported')""",
        (source, scopes)).fetchone()[0]


def status(conn, area: str) -> dict:
    """How much of the area each kind of map data covers, when it was fetched and last checked."""
    out = {}
    for name, table, source in (("paths", "path_box", "fkb_wfs"), ("osm", "osm_box", "osm")):
        if not _has(conn, table):
            out[name] = {"share": 0.0, "fetched": None, "checked": None}
            continue
        share, fetched = conn.execute(f"""
            SELECT coalesce(sum(ST_Length(ST_Intersection(r.geom, b.geom))) / nullif(sum(ST_Length(r.geom)), 0), 0),
                   (SELECT min(x.fetched_at) FROM source_index.{table} x
                    WHERE EXISTS (SELECT 1 FROM source_index.route y
                                  WHERE y.area_prefix = %(a)s AND ST_Intersects(y.geom, x.geom)))
            FROM source_index.route r, (SELECT ST_Union(geom) AS geom FROM source_index.{table}) b
            WHERE r.area_prefix = %(a)s""", {"a": area.lower()}).fetchone()
        scopes = [s for (s,) in conn.execute(f"""
            SELECT x.scope FROM source_index.{table} x WHERE EXISTS (
                SELECT 1 FROM source_index.route y WHERE y.area_prefix = %(a)s AND ST_Intersects(y.geom, x.geom))""",
            {"a": area.lower()})]
        out[name] = {"share": round(float(share or 0), 3), "fetched": fetched,
                     "checked": _checked(conn, source, scopes) or fetched}
    loaded = dict(conn.execute("SELECT municipality_no, fetched_at FROM source_index.road_area")
                  ) if _has(conn, "road_area") else {}
    needed = [{**m, "loaded": m["no"] in loaded} for m in municipalities(conn, area)]
    dates = [loaded[m["no"]] for m in needed if m["loaded"]]
    out["roads"] = {"municipalities": needed, "fetched": min(dates) if dates else None,
                    "checked": _checked(conn, "vegnett", [f"kommune:{m['no']}" for m in needed if m["loaded"]])
                    or (min(dates) if dates else None)}
    return out
