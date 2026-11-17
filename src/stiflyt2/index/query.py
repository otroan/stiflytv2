"""Read-only questions to source_index: search, route details and map tiles.

Every function takes an open connection, so the CLI (import role) and the API
(API role) share them.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg.rows import dict_row

WEB_MERCATOR_WIDTH = 40075016.686


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL", (f"source_index.{table}",)
                        ).fetchone()[0]


def search_places(conn, text: str, *, limit: int = 20,
                  near: tuple[float, float] | None = None, near_srid: int = 25833) -> list[dict]:
    """Places whose name matches: exact first, then names starting with the text, then similar ones.

    Every spelling is searched, also historical ones; a place is returned once,
    under the best-matching spelling. `near` puts the closest first among
    equally good matches; for names that are only similar, likeness counts
    before distance.
    """
    x, y = near or (None, None)
    return _rows(conn, """
        WITH hits AS (
            SELECT place_id, name, alternative, type, municipality, county, geom,
                   CASE WHEN lower(name) = lower(%(q)s) THEN 0
                        WHEN lower(name) LIKE lower(%(q)s) || '%%' THEN 1
                        ELSE 2 END AS rank,
                   similarity(name, %(q)s) AS sim
            FROM source_index.place
            WHERE name ILIKE '%%' || %(q)s || '%%' OR name %% %(q)s),
        best AS (
            SELECT DISTINCT ON (place_id) * FROM hits
            ORDER BY place_id, rank, alternative, sim DESC)
        SELECT place_id, name, type, municipality, county,
               round(ST_X(geom)) AS east, round(ST_Y(geom)) AS north,
               round(ST_X(ST_Transform(geom, 4326))::numeric, 6)::float8 AS lon,
               round(ST_Y(ST_Transform(geom, 4326))::numeric, 6)::float8 AS lat,
               round(ST_Distance(geom, ST_Transform(ST_SetSRID(
                   ST_MakePoint(%(x)s::float8, %(y)s::float8), %(srid)s::int), 25833)))
                   AS distance_m
        FROM best
        ORDER BY rank, (CASE WHEN rank = 2 THEN round(sim::numeric, 1) END) DESC NULLS LAST,
                 distance_m NULLS LAST, sim DESC, name
        LIMIT %(limit)s""", {"q": text, "x": x, "y": y, "srid": near_srid, "limit": limit})


def _routes(conn, source: str, where: str, order: str, params: dict) -> list[dict]:
    """Route candidates for a list, with the shape verdict where it has been worked out."""
    shaped = _has(conn, "route_shape")
    return _rows(conn, f"""
        SELECT r.id, r.code, r.label, r.name, r.maintainer, r.area_prefix, r.segment_count,
               round((r.length_m / 1000)::numeric, 1)::float8 AS length_km,
               {"sh.shape, sh.pieces, sh.ends" if shaped
                else "NULL::text AS shape, NULL::int AS pieces, NULL::int AS ends"}
        FROM {source}
        {"LEFT JOIN source_index.route_shape sh ON sh.route_id = r.id" if shaped else ""}
        WHERE {where} ORDER BY {order}""", params)


def search_routes(conn, text: str, *, limit: int = 20) -> list[dict]:
    """Route candidates by code (prefix, e.g. "bre") or by part of the name."""
    return _routes(
        conn, "source_index.route r",
        """r.code LIKE lower(%(q)s) || '%%' OR r.label = lower(%(q)s)
           OR r.name ILIKE '%%' || %(q)s || '%%'""",
        """r.label <> lower(%(q)s), r.code <> lower(%(q)s), r.code NOT LIKE lower(%(q)s) || '%%',
           length(r.code), r.label, r.maintainer LIMIT %(limit)s""", {"q": text, "limit": limit})


def area_routes(conn, prefix: str) -> list[dict]:
    """Every route candidate whose code starts with a three-letter area prefix, e.g. "bre"."""
    return _routes(conn, "source_index.route r", "r.area_prefix = lower(%(p)s)",
                   "length(r.code), r.label, r.maintainer", {"p": prefix})


def segment_routes(conn, segment_id: str) -> list[dict]:
    """The route candidates that use one trail segment."""
    return _routes(
        conn, "source_index.route_segment rs JOIN source_index.route r ON r.id = rs.route_id",
        "rs.segment_id = %(s)s", "length(r.code), r.label", {"s": segment_id})


_ANCHOR_COLUMNS = """a.id, a.kind, a.name, a.named, a.routes, a.ends, a.spots,
                     round(a.spread_m)::int AS spread_m,
                     round(ST_X(ST_Transform(a.geom, 4326))::numeric, 6)::float8 AS lon,
                     round(ST_Y(ST_Transform(a.geom, 4326))::numeric, 6)::float8 AS lat"""


def area_anchors(conn, prefix: str) -> list[dict]:
    """Huts and parking places where routes of an area end, the most scattered first."""
    if not _has(conn, "anchor"):
        return []
    return _rows(conn, f"""
        SELECT {_ANCHOR_COLUMNS} FROM source_index.anchor a
        WHERE a.named AND a.id IN (
            SELECT e.anchor_id FROM source_index.route_end e
            JOIN source_index.route r ON r.id = e.route_id WHERE r.area_prefix = lower(%(p)s))
        ORDER BY a.spots DESC, a.spread_m DESC, a.name""", {"p": prefix})


def anchor_detail(conn, anchor_id: str) -> dict | None:
    """One anchor and the route ends that belong to it, grouped by the spot they end at."""
    if not _has(conn, "anchor"):
        return None
    found = _rows(conn, f"SELECT {_ANCHOR_COLUMNS} FROM source_index.anchor a WHERE a.id = %(id)s",
                  {"id": anchor_id})
    if not found:
        return None
    anchor = found[0]
    anchor["route_ends"] = _rows(conn, """
        SELECT r.id AS route_id, r.code, r.label, r.name, e.spot, round(e.offset_m)::int AS offset_m
        FROM source_index.route_end e JOIN source_index.route r ON r.id = e.route_id
        WHERE e.anchor_id = %(id)s ORDER BY e.spot, length(r.code), r.label""", {"id": anchor_id})
    return anchor


def areas(conn) -> list[dict]:
    return _rows(conn, """
        SELECT area_prefix AS prefix, count(*)::int AS routes,
               round(sum(length_m) / 1000)::int AS km
        FROM source_index.route WHERE area_prefix IS NOT NULL GROUP BY 1 ORDER BY 1""", {})


def route_detail(conn, route_id: str) -> dict | None:
    """One route candidate with its geometry (WGS 84) and what lies along it."""
    found = _rows(conn, """
        SELECT r.id, r.code, r.label, r.name, r.names, r.maintainer, r.grading, r.special_type,
               r.segment_count, round(r.length_m)::int AS length_m,
               (SELECT count(*)::int FROM source_index.route_segment rs
                JOIN source_index.trail_segment t ON t.id = rs.segment_id
                WHERE rs.route_id = r.id AND t.marked IS NOT TRUE) AS unmarked_segments,
               (SELECT coalesce(round(sum(t.length_m)), 0)::int FROM source_index.route_segment rs
                JOIN source_index.trail_segment t ON t.id = rs.segment_id
                WHERE rs.route_id = r.id AND t.attrs->>'rutefolger' = 'VV') AS water_m,
               ST_AsGeoJSON(ST_Transform(r.geom, 4326), 6)::json AS geometry,
               ARRAY[ST_XMin(b.box), ST_YMin(b.box), ST_XMax(b.box), ST_YMax(b.box)] AS bbox
        FROM source_index.route r,
             LATERAL (SELECT Box2D(ST_Transform(r.geom, 4326)) AS box) b
        WHERE r.id = %(id)s""", {"id": route_id})
    if not found:
        return None
    route = found[0]
    # Who maintains it, stretch by stretch: a stretch shared by two counts for both.
    route["maintainers"] = _rows(conn, """
        SELECT m AS name, round(sum(t.length_m))::int AS length_m
        FROM source_index.route_segment rs
        JOIN source_index.trail_segment t ON t.id = rs.segment_id,
             unnest(CASE WHEN cardinality(t.maintainers) = 0 THEN ARRAY['not stated']
                         ELSE t.maintainers END) AS m
        WHERE rs.route_id = %(id)s GROUP BY 1 ORDER BY 2 DESC, 1""", {"id": route_id})
    route["near_path_share"] = None
    if _has(conn, "path"):
        # Share of the route lying within 20 m of a mapped path. None where no
        # paths are loaded around the route at all.
        route["near_path_share"] = conn.execute("""
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM source_index.path p
                                         WHERE ST_DWithin(p.geom, r.geom, 2000)) THEN NULL
                        ELSE coalesce(ST_Length(ST_Intersection(r.geom, (
                                 SELECT ST_Union(ST_Buffer(p.geom, 20)) FROM source_index.path p
                                 WHERE ST_DWithin(p.geom, r.geom, 20)))), 0)
                             / nullif(ST_Length(r.geom), 0) END
            FROM source_index.route r WHERE r.id = %s""", (route_id,)).fetchone()[0]
    route["shape"], route["ends"] = None, []
    if _has(conn, "route_end"):
        route["shape"] = _rows(conn, """SELECT shape, pieces, ends FROM source_index.route_shape
                                        WHERE route_id = %(id)s""", {"id": route_id})[0]
        route["ends"] = _rows(conn, f"""
            SELECT {_ANCHOR_COLUMNS}, round(e.offset_m)::int AS offset_m
            FROM source_index.route_end e JOIN source_index.anchor a ON a.id = e.anchor_id
            WHERE e.route_id = %(id)s ORDER BY e.id""", {"id": route_id})
    route["heritage"] = _rows(conn, """
        SELECT h.kind, h.id, h.name, h.category, h.protection, h.url,
               round(ST_Distance(h.geom, r.geom))::int AS distance_m
        FROM source_index.route r
        JOIN source_index.heritage h ON ST_DWithin(h.geom, r.geom, 50)
        WHERE r.id = %(id)s AND h.kind <> 'sikringssone'
        ORDER BY distance_m, h.kind, h.id LIMIT 200""", {"id": route_id}
    ) if _has(conn, "heritage") else []
    route["parcels"] = _rows(conn, """
        SELECT p.id, p.matrikkel, p.municipality_no,
               round(ST_Length(ST_Intersection(ST_MakeValid(p.geom), r.geom)))::int AS length_m
        FROM source_index.route r
        JOIN source_index.parcel p ON ST_Intersects(p.geom, r.geom)
        WHERE r.id = %(id)s ORDER BY length_m DESC, p.id LIMIT 200""", {"id": route_id}
    ) if _has(conn, "parcel") else []
    return route


def index_state(conn) -> list[dict]:
    return _rows(conn, """SELECT source, built_at, rows FROM source_index.index_state
                          ORDER BY source""", {})


@dataclass(frozen=True)
class TileLayer:
    table: str
    columns: str   # attributes carried in the tile, over alias t
    minzoom: int
    geom: str = "t.geom"
    where: str = "TRUE"
    # Below this zoom the lines are merged per value of overview_columns and
    # carry nothing else: a zoomed-out tile holds tens of thousands of segments.
    overview_below: int = 0
    overview_columns: str = ""


TILE_LAYERS = {
    "trails": TileLayer(
        "trail_segment",
        "t.id, t.marked, array_to_string(t.codes, ', ') AS codes, round(t.length_m)::int AS length_m,"
        " coalesce(t.attrs->>'rutefolger' = 'VV', false) AS water",
        7, overview_below=10, overview_columns="t.marked"),
    "paths": TileLayer("path", "t.id, t.kind, round(t.length_m)::int AS length_m", 11),
    "roads": TileLayer("road", "t.id, t.kind, t.category, t.name, round(t.length_m)::int AS length_m", 11),
    "osm": TileLayer("osm_way", "t.id, t.kind, t.name, t.sac_scale, t.visibility, t.surface, t.on_foot,"
                                " round(t.length_m)::int AS length_m", 11),
    "info_points": TileLayer("info_point", "t.id, t.facility, t.info, t.maintainer", 10),
    "heritage": TileLayer("heritage", "t.kind, t.id, t.name, t.category, t.protection, t.url", 12),
    "parcels": TileLayer("parcel", "t.id, t.matrikkel, t.municipality_no", 13),
    # Named anchors (huts, parking) are shown from far out, the rest close up.
    "anchors": TileLayer(
        "anchor", "t.id, t.kind, t.name, t.routes, t.ends, t.spots, round(t.spread_m)::int AS spread_m",
        8, where="t.named"),
    "meetings": TileLayer(
        "anchor", "t.id, t.kind, t.name, t.routes, t.ends, t.spots, round(t.spread_m)::int AS spread_m",
        11, where="NOT t.named"),
    "end_links": TileLayer("route_end", "t.id, t.anchor_id, round(t.offset_m)::int AS offset_m", 12,
                           geom="t.link", where="t.offset_m > 1"),
}


def tile(conn, layer: str, z: int, x: int, y: int) -> bytes:
    """A Mapbox vector tile of one index table. Empty below the layer's minimum zoom."""
    spec = TILE_LAYERS[layer]
    if z < spec.minzoom or not _has(conn, spec.table):
        return b""
    width = WEB_MERCATOR_WIDTH / 2**z
    bounds = """SELECT ST_TileEnvelope(%(z)s, %(x)s, %(y)s) AS tile,
                       ST_Transform(ST_Segmentize(
                           ST_TileEnvelope(%(z)s, %(x)s, %(y)s, margin => 0.02), %(step)s), 25833) AS area"""
    if z < spec.overview_below:
        # Segments shorter than about a screen pixel are left out.
        features = f"""SELECT ST_AsMVTGeom(ST_Simplify(ST_LineMerge(ST_Collect(
                                  ST_Transform(t.geom, 3857))), %(tolerance)s * 4),
                              b.tile, 4096, 64, true) AS geom, {spec.overview_columns}
                       FROM source_index.{spec.table} t, b
                       WHERE t.geom && b.area AND t.length_m > %(pixel)s
                       GROUP BY {spec.overview_columns}, b.tile"""
    else:
        features = f"""SELECT ST_AsMVTGeom(ST_Simplify(ST_Transform({spec.geom}, 3857), %(tolerance)s),
                              b.tile, 4096, 64, true) AS geom, {spec.columns}
                       FROM source_index.{spec.table} t, b
                       WHERE {spec.geom} && b.area AND {spec.where}"""
    row = conn.execute(
        f"""WITH b AS ({bounds}), f AS ({features})
            SELECT ST_AsMVT(f, %(layer)s, 4096, 'geom') FROM f WHERE geom IS NOT NULL""",
        {"z": z, "x": x, "y": y, "layer": layer, "step": width / 8, "tolerance": width / 4096,
         "pixel": width / 1024},
    ).fetchone()
    return bytes(row[0] or b"")
