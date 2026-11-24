"""The network the route planner walks on: every way in Norway one may walk, joined where they meet.

The groundwork of the feature `planner` (`ops/planner.py`). Made from
OpenStreetMap's extract of Norway, the one fetched weekly from Geofabrik
(`importer/adapters/osm.py`), and rebuilt when a new one has come:

1. Every way one may walk on is read: paths, tracks, footways, steps and
   roads of every kind but motorways and motor roads, and ferries. What
   says it is closed on foot is left out.
2. A way is cut where it meets another (at a node they share), so that the
   network's stretches run from junction to junction, as pgRouting wants
   them. Bridges and tunnels share no node with what they cross, and so do
   not join it.
3. A stretch that runs along a marked route in Turrutebasen, or one of
   ours, is said to; and whether DNT looks after the route.
4. Where a marked route has nothing in OpenStreetMap along it, the route's
   own line is put in, joined to the network at its ends.
5. Glaciers and huts are taken from the extract too: a stretch mostly on a
   glacier is said to be, and a hut is where a day may end.

New tables are made beside the ones in use and put in their place in one
go, so that the planner never sees half a network. What is from
OpenStreetMap is a produced work under ODbL, and is credited where it is
shown.
"""
from __future__ import annotations

import logging
import struct
import subprocess
import tempfile
import time
from array import array
from pathlib import Path

import numpy as np
from psycopg.types.json import Jsonb

from stiflyt2 import terrain

log = logging.getLogger(__name__)

# What one may walk on, by OpenStreetMap's highway=. Everything else (motorways, what is planned or being
# built, platforms, race tracks) is not walked.
PATHS = frozenset({"path", "footway", "track", "steps", "bridleway", "pedestrian", "cycleway", "via_ferrata"})
ROADS = frozenset({"living_street", "residential", "service", "unclassified", "road", "tertiary", "tertiary_link",
                   "secondary", "secondary_link", "primary", "primary_link", "trunk", "trunk_link"})
WALKED = PATHS | ROADS
OPEN = frozenset({"yes", "designated", "permissive"})       # foot= that says a way is open on foot
CLOSED = frozenset({"no", "private"})                       # foot= that says it is not
SHUNNED = frozenset({"discouraged", "use_sidepath"})        # foot= that says it may be walked, but better not
# A vertex that is no OpenStreetMap node gets a number from where it lies, to the metre: two lines that end at
# the same spot meet there. Far above every node OpenStreetMap has.
VERTEX = 100_000_000_000_000
NEAR_M = 20             # a stretch this near a marked route is along it, at enough of its points
BARE_M = 25             # a point of a marked route with no stretch this near has nothing in OpenStreetMap
BARE_EVERY_M = 50       # a marked route is looked at this often along it
TIE_M = 60              # the end of a marked route put in is joined to a junction this near
DNT = r"^DNT|turistforening|turlag|utferdslag"      # who of Turrutebasen's maintainers is DNT


def kind_of(get) -> tuple[str, bool, bool, bool] | None:
    """What a way is to walk on, from its tags (`get` is the tags' get): its kind, and whether it is
    private, a tunnel, or a way one should rather not walk. None where it may not be walked."""
    highway, foot = get("highway"), get("foot")
    if highway is None:
        if get("route") == "ferry" and foot not in CLOSED:
            return "ferry", False, False, False
        return None
    if highway not in WALKED or foot in CLOSED:
        return None
    if get("motorroad") == "yes" and foot not in OPEN:
        return None
    private = get("access") in ("no", "private") and foot not in OPEN
    tunnel = get("tunnel") in ("yes", "building_passage") and highway in ROADS
    return highway, private, tunnel, foot in SHUNNED


def _ways(pbf: Path):
    """Every way one may walk on: what it is, and its nodes, one after another (`refs`, from `starts`)."""
    import osmium

    meta: list[tuple] = []
    refs, starts = array("q"), array("q", [0])
    ways = osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.KeyFilter("highway", "route"))
    for w in ways:
        tags = w.tags
        what = kind_of(tags.get)
        if what is None or len(w.nodes) < 2:
            continue
        refs.extend(n.ref for n in w.nodes)
        starts.append(len(refs))
        meta.append((w.id, *what, tags.get("name") or tags.get("ref"), tags.get("sac_scale"),
                     tags.get("trail_visibility")))
    return meta, np.frombuffer(refs, dtype=np.int64), np.frombuffer(starts, dtype=np.int64)


def _nodes(pbf: Path, ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Where each of the nodes is (longitude, latitude), in the order of `ids`. NaN for one not found."""
    import osmium

    got, lon, lat = array("q"), array("d"), array("d")
    nodes = osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(int(i) for i in ids))
    for n in nodes:
        at = n.location
        if at.valid():
            got.append(n.id)
            lon.append(at.lon)
            lat.append(at.lat)
    x, y = np.full(len(ids), np.nan), np.full(len(ids), np.nan)
    where = np.searchsorted(ids, np.frombuffer(got, dtype=np.int64))
    x[where], y[where] = np.frombuffer(lon), np.frombuffer(lat)
    return x, y


def _line(x: np.ndarray, y: np.ndarray) -> str:
    """A line as PostGIS takes it in a COPY: extended WKB, in hex, in longitude and latitude."""
    head = struct.pack("<BIII", 1, 0x20000002, 4326, len(x))
    return (head + np.column_stack((x, y)).astype("<f8").tobytes()).hex()


def stretches(meta: list[tuple], refs: np.ndarray, starts: np.ndarray, at: np.ndarray, uses: np.ndarray,
               lon: np.ndarray, lat: np.ndarray):
    """The ways cut where they meet: one row per stretch, as walk.edge is loaded.

    `at` is each of `refs` as the number of its node among the nodes there are, `uses` how often each node
    is used, and `lon`, `lat` where each node lies. A node is a junction where two ways meet, or one way
    meets itself, and at the ends of every way.
    """
    junction = uses >= 2
    junction[at[starts[:-1]]] = True            # the first node of every way
    junction[at[starts[1:] - 1]] = True         # and the last
    cuts = np.flatnonzero(junction[at])
    way = np.repeat(np.arange(len(meta)), np.diff(starts))
    a, b = cuts[:-1], cuts[1:]
    same = way[a] == way[b]
    n = 0
    for i, j in zip(a[same].tolist(), b[same].tolist(), strict=True):
        source, target = int(refs[i]), int(refs[j])
        nodes = at[i:j + 1]
        x, y = lon[nodes], lat[nodes]
        if source == target or np.isnan(x).any():
            continue                            # a ring with nothing joined to it, or a node not in the extract
        n += 1
        osm_id, kind, private, tunnel, shunned, name, sac, visibility = meta[way[i]]
        yield (n, source, target, osm_id, kind, name, sac, visibility, private, tunnel, shunned, _line(x, y))


LOAD = """CREATE UNLOGGED TABLE walk.edge_load (
    id bigint, source bigint, target bigint, osm_id bigint, kind text, name text, sac text, visibility text,
    private boolean, tunnel boolean, discouraged boolean, g geometry(LineString, 4326))"""

MADE = """INSERT INTO walk.edge_next (id, source, target, origin, osm_id, kind, name, sac, visibility, private, tunnel,
                                  discouraged, length_m, geom, x1, y1, x2, y2)
    SELECT id, source, target, 'osm', osm_id, kind, name, sac, visibility, private, tunnel, discouraged,
           ST_Length(g::geography), t, ST_X(ST_StartPoint(t)), ST_Y(ST_StartPoint(t)),
           ST_X(ST_EndPoint(t)), ST_Y(ST_EndPoint(t))
    FROM walk.edge_load, LATERAL ST_Transform(g, 25833) t"""

# The marked routes: Turrutebasen's, and our own stretches of routes in use (not those over water).
MARKED = f"""CREATE UNLOGGED TABLE walk.marked_load AS
    SELECT row_number() OVER () AS gid, * FROM (
        SELECT 'turrutebasen' AS origin, s.geom,
               EXISTS (SELECT 1 FROM unnest(s.maintainers) m WHERE m ~* '{DNT}') AS dnt,
               (SELECT string_agg(DISTINCT c, ', ') FROM unnest(s.codes) c) AS name
        FROM source_index.trail_segment s
        UNION ALL
        SELECT 'ours', r.geom, true, (SELECT string_agg(DISTINCT t.code, ', ') FROM core.route_leg l
                                      JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                                      WHERE l.edge_id = e.id)
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.status = 'active' AND r.kind <> 'water'
          AND EXISTS (SELECT 1 FROM core.route_leg l JOIN core.route t ON t.id = l.route_id AND t.status = 'active'
                      WHERE l.edge_id = e.id)) m"""

# A stretch is along a marked route where two of three points along it are near one.
ALONG = f"""WITH near AS (
        SELECT DISTINCT e.id FROM walk.edge_next e JOIN walk.marked_load m ON ST_DWithin(e.geom, m.geom, {NEAR_M})),
    said AS (
        SELECT n.id,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM walk.marked_load m WHERE ST_DWithin(m.geom, p, {NEAR_M}))) AS near_any,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM walk.marked_load m WHERE m.dnt AND ST_DWithin(m.geom, p, {NEAR_M})))
                   AS near_dnt
        FROM near n JOIN walk.edge_next e USING (id),
             LATERAL unnest('{{0.2,0.5,0.8}}'::float8[]) f, LATERAL ST_LineInterpolatePoint(e.geom, f) p
        GROUP BY n.id)
    UPDATE walk.edge_next e SET trail = true, dnt = s.near_dnt >= 2
    FROM said s WHERE s.id = e.id AND s.near_any >= 2"""

# Where a marked route has no stretch along it for more than one look in five, its own line is put in.
BARE = f"""CREATE UNLOGGED TABLE walk.bare_load AS
    SELECT m.* FROM walk.marked_load m,
         LATERAL (SELECT count(*) AS n,
                         count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM walk.edge_next e
                                                            WHERE ST_DWithin(e.geom, d.geom, {BARE_M}))) AS bare
                  FROM ST_DumpPoints(ST_LineInterpolatePoints(
                           m.geom, least(0.5, {BARE_EVERY_M} / greatest(ST_Length(m.geom), 1)))) d) k
    WHERE ST_Length(m.geom) > 1 AND k.bare * 5 > k.n"""

# Each end of a line put in is a vertex of its own, from where it lies, so that lines that end at the
# same spot meet; and is joined to the nearest junction of the network, if one is near.
VERTEX_OF = f"({VERTEX} + (round(ST_X({{p}}))::bigint + 500000) * 10000000 + round(ST_Y({{p}}))::bigint)"
PUT_IN = f"""
    INSERT INTO walk.edge_next (id, source, target, origin, kind, name, trail, dnt, length_m, geom, x1, y1, x2, y2)
    SELECT (SELECT max(id) FROM walk.edge_next) + row_number() OVER (),
           {VERTEX_OF.format(p="ST_StartPoint(b.geom)")}, {VERTEX_OF.format(p="ST_EndPoint(b.geom)")},
           b.origin, 'trail', b.name, true, b.dnt, ST_Length(ST_Transform(b.geom, 4326)::geography), b.geom,
           ST_X(ST_StartPoint(b.geom)), ST_Y(ST_StartPoint(b.geom)), ST_X(ST_EndPoint(b.geom)), ST_Y(ST_EndPoint(b.geom))
    FROM walk.bare_load b;

    CREATE UNLOGGED TABLE walk.junction_load AS
        SELECT DISTINCT ON (id) id, p FROM (
            SELECT source AS id, ST_StartPoint(geom) AS p FROM walk.edge_next WHERE origin = 'osm'
            UNION ALL SELECT target, ST_EndPoint(geom) FROM walk.edge_next WHERE origin = 'osm') v;
    CREATE INDEX ON walk.junction_load USING gist (p);
    ANALYZE walk.junction_load;

    INSERT INTO walk.edge_next (id, source, target, origin, kind, length_m, geom, x1, y1, x2, y2)
    SELECT (SELECT max(id) FROM walk.edge_next) + row_number() OVER (), e.v, j.id, 'link', 'link',
           ST_Length(ST_Transform(l, 4326)::geography), l, ST_X(e.p), ST_Y(e.p), ST_X(j.p), ST_Y(j.p)
    FROM (SELECT DISTINCT ON (v) v, p FROM (
              SELECT source AS v, ST_StartPoint(geom) AS p FROM walk.edge_next WHERE origin IN ('turrutebasen', 'ours')
              UNION ALL
              SELECT target, ST_EndPoint(geom) FROM walk.edge_next WHERE origin IN ('turrutebasen', 'ours')) x) e
    CROSS JOIN LATERAL (SELECT id, p FROM walk.junction_load j WHERE ST_DWithin(j.p, e.p, {TIE_M})
                        ORDER BY j.p <-> e.p LIMIT 1) j
    CROSS JOIN LATERAL ST_MakeLine(e.p, j.p) l"""

ICE = """
    CREATE UNLOGGED TABLE walk.ice_load AS
        SELECT ST_Subdivide(ST_CollectionExtract(ST_MakeValid(geom), 3), 128) AS geom FROM walk.area_load WHERE "natural" = 'glacier';
    CREATE INDEX ON walk.ice_load USING gist (geom);
    ANALYZE walk.ice_load;
    UPDATE walk.edge_next e SET ice = true
    FROM (SELECT e.id FROM walk.edge_next e JOIN walk.ice_load i ON ST_Intersects(e.geom, i.geom)
          GROUP BY e.id, e.geom HAVING sum(ST_Length(ST_Intersection(e.geom, i.geom))) > 0.5 * ST_Length(e.geom)) x
    WHERE x.id = e.id"""

HUTS = f"""
    INSERT INTO walk.hut_next (osm_id, name, kind, operator, dnt, geom)
    SELECT DISTINCT ON (osm_id) osm_id, name, tourism, operator, coalesce(operator ~* '{DNT}', false),
           ST_PointOnSurface(geom)
    FROM (SELECT osm_id, name, tourism, operator, geom FROM walk.area_load) h
    WHERE tourism IN ('alpine_hut', 'wilderness_hut')
    ORDER BY osm_id"""

# Which piece of the network each stretch is in: pgRouting's connected components, by the junction it starts at.
PIECES = """
    CREATE UNLOGGED TABLE walk.piece_load AS
        SELECT node, component FROM pgr_connectedComponents('SELECT id, source, target, 1::float8 AS cost FROM walk.edge_next');
    CREATE INDEX ON walk.piece_load (node);"""
# The pieces, and how high each stretch lies (where the height model is here): the stretches written anew with
# them, rather than every row updated (which would leave the table twice its size).
PIECES_AND_HEIGHTS = """
    CREATE TABLE walk.edge_done (LIKE walk.edge INCLUDING DEFAULTS INCLUDING CONSTRAINTS);
    INSERT INTO walk.edge_done (id, source, target, origin, osm_id, kind, name, sac, visibility, private, tunnel,
                                discouraged, trail, dnt, ice, length_m, geom, x1, y1, x2, y2, piece, piece_nodes, z, high)
    SELECT e.id, e.source, e.target, e.origin, e.osm_id, e.kind, e.name, e.sac, e.visibility, e.private, e.tunnel,
           e.discouraged, e.trail, e.dnt, e.ice, e.length_m, e.geom, e.x1, e.y1, e.x2, e.y2, c.component, s.n, h.z, h.high
    FROM walk.edge_next e
    LEFT JOIN walk.piece_load c ON c.node = e.source
    LEFT JOIN (SELECT component, count(*)::int AS n FROM walk.piece_load GROUP BY 1) s USING (component)
    LEFT JOIN walk.z_load h ON h.id = e.id;
    DROP TABLE walk.edge_next;
    ALTER TABLE walk.edge_done RENAME TO edge_next;
    CREATE INDEX ON walk.edge_next USING gist (geom);
    ANALYZE walk.edge_next"""

AREAS = Path(__file__).with_name("osmconf-areas.ini")
HUT = "tourism IN ('alpine_hut', 'wilderness_hut')"


def _areas(pbf: Path, work: Path):
    """The glaciers and huts in the extract: (osm id, name, natural, tourism, operator, GeoJSON in longitude
    and latitude, as GeoJSON is).

    Read with GDAL, which puts areas together from their ways and relations.
    """
    import json
    import os

    for layer, where in (("points", HUT), ("multipolygons", f"\"natural\" = 'glacier' OR {HUT}")):
        dest = work / f"{layer}.geojsons"
        dest.unlink(missing_ok=True)
        p = subprocess.run(
            ["ogr2ogr", "-f", "GeoJSONSeq", str(dest), str(pbf), layer, "-where", where,
             "-lco", "COORDINATE_PRECISION=7", "--config", "OSM_CONFIG_FILE", str(AREAS),
             "--config", "CPL_TMPDIR", str(work)],
            capture_output=True, text=True, check=False, env={**os.environ, "OSM_MAX_TMPFILE_SIZE": "1024"})
        if p.returncode != 0:
            raise RuntimeError(f"the extract could not be read: {p.stderr.strip()[-300:]}")
        with dest.open() as f:
            for row in f:
                feature = json.loads(row.strip().lstrip("\x1e"))
                said = feature["properties"]
                if not feature.get("geometry"):
                    continue
                if said.get("osm_way_id"):
                    osm_id = f"w{said['osm_way_id']}"
                else:
                    osm_id = ("n" if layer == "points" else "r") + str(said["osm_id"])
                yield (osm_id, said.get("name"), said.get("natural"), said.get("tourism"), said.get("operator"),
                       json.dumps(feature["geometry"]))
        dest.unlink()


def build(conn, pbf: Path, md5: str, made_at=None) -> dict:
    """Build the network from the extract, and put it in place of the one in use. Returns the counts.

    `conn` acts as the database's owner, and is committed.
    """
    began = time.monotonic()

    def said(what: str) -> None:
        log.info("%s (%.0f s)", what, time.monotonic() - began)

    meta, refs, starts = _ways(pbf)
    said(f"{len(meta)} ways, {len(refs)} nodes along them")
    ids, at, uses = np.unique(refs, return_inverse=True, return_counts=True)
    lon, lat = _nodes(pbf, ids)
    said(f"{int(np.isfinite(lon).sum())} of {len(ids)} nodes placed")

    for table in ("edge_load", "edge_next"):
        conn.execute(f"DROP TABLE IF EXISTS walk.{table}")
    conn.execute(LOAD)
    conn.execute("CREATE TABLE walk.edge_next (LIKE walk.edge INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    with conn.cursor() as cur, cur.copy("COPY walk.edge_load FROM STDIN") as copy:
        for row in stretches(meta, refs, starts, at, uses, lon, lat):
            copy.write_row(row)
    del meta, refs, starts, ids, at, uses, lon, lat
    conn.execute(MADE)
    conn.execute("DROP TABLE walk.edge_load")
    conn.execute("CREATE INDEX ON walk.edge_next USING gist (geom)")
    conn.execute("ANALYZE walk.edge_next")
    conn.commit()
    said("stretches in")
    return finish(conn, pbf, md5, made_at, began)


WORK = ("area_load", "marked_load", "bare_load", "junction_load", "ice_load", "piece_load", "z_load", "field_load")

# Across country (`walk.field`): a lattice over land, a point every LATTICE_M, each joined to its eight neighbours
# where the ground between allows: not over water or a river, nor anywhere steeper than STEEPEST between two looks
# at the ground along it, every 50 m (70 m on the diagonal).
LATTICE_M = 500
LOOKS = 10
STEEPEST = 0.7              # 35 degrees
SEA_M = 0.5                 # ground no higher than this is the sea
MASK_M = 100                # water, rivers, bogs and glaciers are told apart on a grid this fine
WATER, RIVER, GLACIER, BOG = 1, 2, 3, 4
FIELD_ID = 1_000_000_000_000                # the lattice's stretches, numbered above the network's
FIELD_VERTEX = 200_000_000_000_000          # and its points, above every other vertex
WAYS = ((1, 0), (0, 1), (1, 1), (-1, 1))    # each point to its neighbours east, north, north-east and north-west


def tree_line(north):
    """How high the trees go, near enough: 1,100 m in the south, 300 m in Finnmark."""
    return 1100 - (np.asarray(north) / 111_000 - 60) * 80


def high_of(z, north):
    """How far up in the mountains a height is: 0 more than 300 m under the tree line, 1 more than 300 m over."""
    return np.clip((np.asarray(z) - tree_line(north) + 300) / 600, 0, 1)


def _masks(pbf: Path, work: Path):
    """Water, rivers, glaciers and bogs from the extract, on a grid of MASK_M over the height model's tiles: the
    grid, and its west and north edge."""
    import os

    from PIL import Image

    gpkg = work / "masks.gpkg"
    env = {**os.environ, "OSM_MAX_TMPFILE_SIZE": "1024"}
    for layer, where, name in (("multipolygons", "\"natural\" IN ('water', 'wetland', 'glacier')", "areas"),
                               ("lines", "waterway = 'river'", "rivers")):
        subprocess.run(["ogr2ogr", "-f", "GPKG", *(["-update"] if gpkg.exists() else []), str(gpkg), str(pbf), layer,
                        "-where", where, "-nln", name, "-t_srs", "EPSG:25833", "-nlt", "PROMOTE_TO_MULTI",
                        "--config", "OSM_CONFIG_FILE", str(AREAS), "--config", "CPL_TMPDIR", str(work)],
                       check=True, capture_output=True, env=env)
    west, south, east, north = terrain.extent()
    tif = work / "mask.tif"
    # Burnt one over the other, what matters most last: a lake in a bog is a lake.
    for k, (value, layer, where, more) in enumerate(((BOG, "areas", "\"natural\" = 'wetland'", []),
                                                      (GLACIER, "areas", "\"natural\" = 'glacier'", []),
                                                      (RIVER, "rivers", None, ["-at"]),
                                                      (WATER, "areas", "\"natural\" = 'water'", []))):
        new = ["-te", *map(str, (west, south, east, north)), "-tr", str(MASK_M), str(MASK_M), "-ot", "Byte",
               "-init", "0", "-co", "COMPRESS=DEFLATE"] if k == 0 else []
        subprocess.run(["gdal_rasterize", "-q", "-burn", str(value), "-l", layer, *more,
                        *(["-where", where] if where else []), *new, str(gpkg), str(tif)], check=True, capture_output=True)
    Image.MAX_IMAGE_PIXELS = None
    return np.asarray(Image.open(tif)), west, north


def _looks(x1, y1, x2, y2, mask, west, north):
    """The ground along lines, LOOKS + 1 times each: heights (NaN off the tiles) and what the mask says."""
    t = np.linspace(0, 1, LOOKS + 1)
    x = x1[:, None] + (x2 - x1)[:, None] * t
    y = y1[:, None] + (y2 - y1)[:, None] * t
    z = terrain.heights_of(x.ravel(), y.ravel()).reshape(x.shape)
    row = np.clip(((north - y) / MASK_M).astype(np.int64), 0, mask.shape[0] - 1)
    col = np.clip(((x - west) / MASK_M).astype(np.int64), 0, mask.shape[1] - 1)
    return z, mask[row, col]


def _walkable(x1, y1, x2, y2, mask, west, north):
    """Which of the lines can be walked across country, and for those: metres, up, down, height, how high, bog,
    glacier."""
    z, what = _looks(x1, y1, x2, y2, mask, west, north)
    length = np.hypot(x2 - x1, y2 - y1)
    dz = np.diff(z, axis=1)
    ok = (~np.isnan(z).any(axis=1) & (np.nan_to_num(z, nan=0.0) > SEA_M).all(axis=1)
          & ~np.isin(what, (WATER, RIVER)).any(axis=1)
          & (np.abs(np.nan_to_num(dz)) <= STEEPEST * np.maximum(length, 1)[:, None] / LOOKS).all(axis=1))
    up = np.clip(np.nan_to_num(dz), 0, None).sum(axis=1)
    down = np.clip(-np.nan_to_num(dz), 0, None).sum(axis=1)
    mean = np.where(np.isnan(z), 0, z).sum(axis=1) / np.maximum((~np.isnan(z)).sum(axis=1), 1)
    return ok, length, up, down, mean, high_of(mean, (y1 + y2) / 2), (what == BOG).mean(axis=1) >= 0.5, \
        (what == GLACIER).mean(axis=1) > 0.5


def _field(copy, mask, west, north, vertices, said) -> int:
    """The lattice and its ties to the network, written to walk.field_load. Returns how many stretches."""
    w, s, e, n = terrain.extent()
    i0, j0 = w // LATTICE_M, s // LATTICE_M
    ni, nj = (e - w) // LATTICE_M + 1, (n - s) // LATTICE_M + 1
    I, J = np.meshgrid(np.arange(ni) + i0, np.arange(nj) + j0, indexing="ij")
    z = terrain.heights_of((I * LATTICE_M).ravel(), (J * LATTICE_M).ravel()).reshape(I.shape)
    row = np.clip(((north - J * LATTICE_M) / MASK_M).astype(np.int64), 0, mask.shape[0] - 1)
    col = np.clip(((I * LATTICE_M - west) / MASK_M).astype(np.int64), 0, mask.shape[1] - 1)
    standing = ~np.isnan(z) & (np.nan_to_num(z) > SEA_M) & ~np.isin(mask[row, col], (WATER, RIVER))
    said(f"{int(standing.sum())} points of the lattice on land")

    def vertex(i, j):
        return FIELD_VERTEX + (i + 1000) * 100_000 + j

    made = 0

    def put(src, dst, x1, y1, x2, y2, kind):
        nonlocal made
        ok, length, up, down, mean, high, wet, ice = _walkable(x1, y1, x2, y2, mask, west, north)
        for k in np.flatnonzero(ok):
            made += 1
            copy.write_row((FIELD_ID + made, int(src[k]), int(dst[k]), kind, float(length[k]), float(up[k]),
                            float(down[k]), float(mean[k]), float(high[k]), bool(wet[k]), bool(ice[k]),
                            float(x1[k]), float(y1[k]), float(x2[k]), float(y2[k])))

    band = terrain.TILE_M // LATTICE_M          # a row of tiles at a time: the height model's tiles stay read
    for b in range(0, nj, band):
        for di, dj in WAYS:
            a_i, a_j = np.nonzero(standing[max(0, -di):ni - max(0, di), b:min(b + band, nj - dj)])
            a_i, a_j = a_i + max(0, -di), a_j + b
            there = standing[a_i + di, a_j + dj]
            a_i, a_j = a_i[there], a_j[there]
            gi, gj = a_i + i0, a_j + j0
            put(vertex(gi, gj), vertex(gi + di, gj + dj), (gi * LATTICE_M).astype(float), (gj * LATTICE_M).astype(float),
                ((gi + di) * LATTICE_M).astype(float), ((gj + dj) * LATTICE_M).astype(float), "field")
    said(f"{made} stretches across country")
    # Each point of the lattice is tied to the nearest junction of the network in its square.
    vid, vx, vy = vertices
    vi, vj = np.rint(vx / LATTICE_M).astype(np.int64) - i0, np.rint(vy / LATTICE_M).astype(np.int64) - j0
    inside = (vi >= 0) & (vi < ni) & (vj >= 0) & (vj < nj)
    vid, vx, vy, vi, vj = vid[inside], vx[inside], vy[inside], vi[inside], vj[inside]
    on = standing[vi, vj]
    vid, vx, vy, vi, vj = vid[on], vx[on], vy[on], vi[on], vj[on]
    far = np.hypot(vx - (vi + i0) * LATTICE_M, vy - (vj + j0) * LATTICE_M)
    key = vi * nj + vj
    order = np.lexsort((far, key))
    first = order[np.r_[True, np.diff(key[order]) != 0]] if len(order) else order
    fields = made
    for part in np.array_split(first, max(1, len(first) // 200_000)):
        gi, gj = vi[part] + i0, vj[part] + j0
        put(vertex(gi, gj), vid[part], (gi * LATTICE_M).astype(float), (gj * LATTICE_M).astype(float),
            vx[part], vy[part], "tie")
    said(f"{made - fields} ties to the network")
    return made


def _heights_and_vertices(conn, said):
    """How high each stretch of the network lies (into walk.z_load), and the network's vertices with where they
    are, for the lattice to be tied to."""
    conn.execute("CREATE UNLOGGED TABLE walk.z_load (id bigint, z real, high real)")
    eids, zs, highs, ids, xs, ys = [], [], [], [], [], []
    with conn.cursor(name="stretches") as cur:
        cur.execute("""SELECT id, source, target, x1, y1, x2, y2, ST_X(m), ST_Y(m)
                       FROM walk.edge_next, LATERAL ST_LineInterpolatePoint(geom, 0.5) m
                       ORDER BY floor(x1 / 100000), floor(y1 / 100000)""")
        while rows := cur.fetchmany(200_000):
            a = np.array([r[3:] for r in rows], dtype=np.float64)
            z = terrain.heights_of(np.concatenate([a[:, 0], a[:, 4], a[:, 2]]),
                                   np.concatenate([a[:, 1], a[:, 5], a[:, 3]])).reshape(3, -1)
            known = ~np.isnan(z).all(axis=0)
            mean = np.full(len(a), np.nan)
            mean[known] = np.nanmean(z[:, known], axis=0)
            eids.append(np.array([r[0] for r in rows], dtype=np.int64)[known])
            zs.append(mean[known])
            highs.append(high_of(mean[known], a[known, 5]))
            ids += [np.array([r[1] for r in rows], dtype=np.int64), np.array([r[2] for r in rows], dtype=np.int64)]
            xs += [a[:, 0], a[:, 2]]
            ys += [a[:, 1], a[:, 3]]
    # Written once the stretches have been read: a connection does one thing at a time.
    with conn.cursor() as cur, cur.copy("COPY walk.z_load (id, z, high) FROM STDIN") as copy:
        for e, z, h in zip(np.concatenate(eids).tolist(), np.concatenate(zs).tolist(), np.concatenate(highs).tolist(),
                           strict=True):
            copy.write_row((e, z, h))
    vid, at = np.unique(np.concatenate(ids), return_index=True)
    said(f"heights of the stretches; {len(vid)} vertices")
    return vid, np.concatenate(xs)[at], np.concatenate(ys)[at]


FIELD_IN = """
    INSERT INTO walk.field_next (id, source, target, kind, length_m, up_m, down_m, z, high, wet, ice, geom, x1, y1, x2, y2)
    SELECT id, source, target, kind, length_m, up_m, down_m, z, high, wet, ice,
           ST_SetSRID(ST_MakeLine(ST_MakePoint(x1, y1), ST_MakePoint(x2, y2)), 25833), x1, y1, x2, y2
    FROM walk.field_load"""


def finish(conn, pbf: Path, md5: str, made_at=None, began: float | None = None) -> dict:
    """The rest of a build, once the stretches are in walk.edge_next: marked routes, glaciers, huts."""
    began = began or time.monotonic()

    def said(what: str) -> None:
        log.info("%s (%.0f s)", what, time.monotonic() - began)

    for table in (*WORK, "hut_next", "field_next", "edge_done"):
        conn.execute(f"DROP TABLE IF EXISTS walk.{table}")
    conn.execute("CREATE TABLE walk.hut_next (LIKE walk.hut INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
    conn.execute("CREATE TABLE walk.field_next (LIKE walk.field INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")

    conn.execute("""CREATE UNLOGGED TABLE walk.area_load (osm_id text, name text, "natural" text, tourism text,
                                                       operator text, gj text)""")
    with (tempfile.TemporaryDirectory(dir=pbf.parent) as work, conn.cursor() as cur,
          cur.copy('COPY walk.area_load (osm_id, name, "natural", tourism, operator, gj) FROM STDIN') as copy):
        for row in _areas(pbf, Path(work)):
            copy.write_row(row)
    conn.execute("ALTER TABLE walk.area_load ADD COLUMN geom geometry")
    conn.execute("UPDATE walk.area_load SET geom = ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(gj), 4326), 25833)")
    said("glaciers and huts read")

    conn.execute(MARKED)
    conn.execute("CREATE INDEX ON walk.marked_load USING gist (geom)")
    conn.execute("ANALYZE walk.marked_load")
    conn.execute(ALONG)
    said("stretches along marked routes")
    conn.execute(BARE)
    conn.execute(PUT_IN)
    said("marked routes with nothing along them put in")
    conn.execute(ICE)
    conn.execute(HUTS)
    said("glaciers and huts")
    conn.execute(PIECES)
    said("the pieces of the network")
    conn.execute("""CREATE UNLOGGED TABLE walk.field_load (id bigint, source bigint, target bigint, kind text,
        length_m real, up_m real, down_m real, z real, high real, wet boolean, ice boolean,
        x1 float8, y1 float8, x2 float8, y2 float8)""")
    if terrain.ready():
        vertices = _heights_and_vertices(conn, said)
        with tempfile.TemporaryDirectory(dir=pbf.parent) as work:
            mask, west, north = _masks(pbf, Path(work))
        said("water, rivers, bogs and glaciers on a grid")
        with conn.cursor() as cur, cur.copy("COPY walk.field_load FROM STDIN") as copy:
            _field(copy, mask, west, north, vertices, said)
        del mask, vertices
        conn.execute(FIELD_IN)
        conn.execute("ALTER TABLE walk.field_next ADD PRIMARY KEY (id)")
        conn.execute("CREATE INDEX ON walk.field_next USING gist (geom)")
        conn.execute("ANALYZE walk.field_next")
        said("the lattice across country in")
    else:
        conn.execute("CREATE UNLOGGED TABLE walk.z_load (id bigint, z real, high real)")
        log.info("no height model here (`stiflyt2 terrain fetch`): no heights of the stretches, nor across country")
    conn.execute(PIECES_AND_HEIGHTS)
    said("pieces and heights of the stretches")

    counts = conn.execute("""
        SELECT jsonb_build_object(
            'stretches', count(*), 'km', round(sum(length_m) / 1000),
            'marked_km', round(sum(length_m) FILTER (WHERE trail) / 1000),
            'dnt_km', round(sum(length_m) FILTER (WHERE dnt) / 1000),
            'put_in_km', round(sum(length_m) FILTER (WHERE origin IN ('turrutebasen', 'ours')) / 1000),
            'links', count(*) FILTER (WHERE origin = 'link'),
            'ice_km', round(sum(length_m) FILTER (WHERE ice) / 1000),
            'ferry_km', round(sum(length_m) FILTER (WHERE kind = 'ferry') / 1000),
            'huts', (SELECT count(*) FROM walk.hut_next),
            'pieces', (SELECT count(DISTINCT component) FROM walk.piece_load),
            'in_the_greatest', (SELECT max(piece_nodes) FROM walk.edge_next),
            'with_heights', count(*) FILTER (WHERE z IS NOT NULL),
            'across_country', (SELECT count(*) FROM walk.field_next WHERE kind = 'field'),
            'ties', (SELECT count(*) FROM walk.field_next WHERE kind = 'tie'))
        FROM walk.edge_next""").fetchone()[0]
    conn.execute("ALTER TABLE walk.edge_next ADD PRIMARY KEY (id)")
    conn.execute("ALTER TABLE walk.hut_next ADD PRIMARY KEY (osm_id)")
    conn.execute("CREATE INDEX ON walk.hut_next USING gist (geom)")
    conn.execute("ANALYZE walk.hut_next")
    took = time.monotonic() - began
    # In place of the ones in use, in one go.
    conn.execute("DROP TABLE walk.edge, walk.hut, walk.field")
    conn.execute("ALTER TABLE walk.field_next RENAME TO field")
    conn.execute("ALTER TABLE walk.edge_next RENAME TO edge")
    conn.execute("ALTER TABLE walk.hut_next RENAME TO hut")
    for table in WORK:
        conn.execute(f"DROP TABLE walk.{table}")
    conn.execute("""INSERT INTO walk.built (extract_md5, extract_at, took_s, counts, built_at)
                    VALUES (%s, %s, %s, %s, clock_timestamp())
                    ON CONFLICT (one) DO UPDATE SET extract_md5 = EXCLUDED.extract_md5, extract_at = EXCLUDED.extract_at,
                        built_at = EXCLUDED.built_at, took_s = EXCLUDED.took_s, counts = EXCLUDED.counts""",
                 (md5, made_at, took, Jsonb(counts)))
    conn.execute("GRANT SELECT ON walk.edge, walk.hut, walk.field TO stiflyt2_api")
    conn.commit()
    return counts | {"took_s": round(took)}


def built(conn) -> dict | None:
    """What the network in use was built from, and what is in it; None if it has not been built."""
    row = conn.execute("SELECT extract_md5, extract_at, built_at, took_s, counts FROM walk.built").fetchone()
    if row is None:
        return None
    return {"extract_md5": row[0], "extract_at": row[1], "built_at": row[2], "took_s": row[3], "counts": row[4]}
