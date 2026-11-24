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


WORK = ("area_load", "marked_load", "bare_load", "junction_load", "ice_load")


def finish(conn, pbf: Path, md5: str, made_at=None, began: float | None = None) -> dict:
    """The rest of a build, once the stretches are in walk.edge_next: marked routes, glaciers, huts."""
    began = began or time.monotonic()

    def said(what: str) -> None:
        log.info("%s (%.0f s)", what, time.monotonic() - began)

    for table in (*WORK, "hut_next"):
        conn.execute(f"DROP TABLE IF EXISTS walk.{table}")
    conn.execute("CREATE TABLE walk.hut_next (LIKE walk.hut INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")

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

    counts = conn.execute("""
        SELECT jsonb_build_object(
            'stretches', count(*), 'km', round(sum(length_m) / 1000),
            'marked_km', round(sum(length_m) FILTER (WHERE trail) / 1000),
            'dnt_km', round(sum(length_m) FILTER (WHERE dnt) / 1000),
            'put_in_km', round(sum(length_m) FILTER (WHERE origin IN ('turrutebasen', 'ours')) / 1000),
            'links', count(*) FILTER (WHERE origin = 'link'),
            'ice_km', round(sum(length_m) FILTER (WHERE ice) / 1000),
            'ferry_km', round(sum(length_m) FILTER (WHERE kind = 'ferry') / 1000),
            'huts', (SELECT count(*) FROM walk.hut_next))
        FROM walk.edge_next""").fetchone()[0]
    conn.execute("ALTER TABLE walk.edge_next ADD PRIMARY KEY (id)")
    conn.execute("ALTER TABLE walk.hut_next ADD PRIMARY KEY (osm_id)")
    conn.execute("CREATE INDEX ON walk.hut_next USING gist (geom)")
    conn.execute("ANALYZE walk.hut_next")
    took = time.monotonic() - began
    # In place of the ones in use, in one go.
    conn.execute("DROP TABLE walk.edge, walk.hut")
    conn.execute("ALTER TABLE walk.edge_next RENAME TO edge")
    conn.execute("ALTER TABLE walk.hut_next RENAME TO hut")
    for table in WORK:
        conn.execute(f"DROP TABLE walk.{table}")
    conn.execute("""INSERT INTO walk.built (extract_md5, extract_at, took_s, counts, built_at)
                    VALUES (%s, %s, %s, %s, clock_timestamp())
                    ON CONFLICT (one) DO UPDATE SET extract_md5 = EXCLUDED.extract_md5, extract_at = EXCLUDED.extract_at,
                        built_at = EXCLUDED.built_at, took_s = EXCLUDED.took_s, counts = EXCLUDED.counts""",
                 (md5, made_at, took, Jsonb(counts)))
    conn.execute("GRANT SELECT ON walk.edge, walk.hut TO stiflyt2_api")
    conn.commit()
    return counts | {"took_s": round(took)}


def built(conn) -> dict | None:
    """What the network in use was built from, and what is in it; None if it has not been built."""
    row = conn.execute("SELECT extract_md5, extract_at, built_at, took_s, counts FROM walk.built").fetchone()
    if row is None:
        return None
    return {"extract_md5": row[0], "extract_at": row[1], "built_at": row[2], "took_s": row[3], "counts": row[4]}
