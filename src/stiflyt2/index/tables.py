"""What source_index holds, as SQL over the active snapshots in source_raw.

One group per source. A group's tables are built together and replaced
together, so they always agree with each other. Nothing here is authoritative:
every table can be dropped and rebuilt.

Column names are English; the source's own attribute names are kept inside
`attrs` and in values.
"""
from __future__ import annotations

from dataclasses import dataclass

from stiflyt2.seasons import WINTER_CODE

BUILD = "source_index_build"

# Sign distances need true lengths: planar length in EPSG:25833 is off by
# about 0.2 % in western Norway.
LENGTH = "ST_Length(ST_Transform({g}, 4326)::geography)::real"
# Single-part multi-geometries are stored as the part itself.
SIMPLE = "CASE WHEN ST_NumGeometries({g}) = 1 THEN ST_GeometryN({g}, 1) ELSE {g} END"


def active(source: str, layer: str, *, scopes: bool = False, prefer: str = "") -> str:
    """The features of a source layer in its active snapshot.

    scopes: the source is loaded as several scopes (municipalities, boxes) that
    may hold the same feature; each feature is taken once, newest version first,
    or first by `prefer` (SQL to order by, over a and fv) where the scopes do not
    hold the same of it.
    """
    once, order = "", ""
    if scopes:
        once, order = "DISTINCT ON (fv.external_id)", f"ORDER BY fv.external_id, {prefer + ', ' if prefer else ''}fv.id DESC"
    return f"""(SELECT {once} fv.id AS version_id, fv.external_id, fv.properties AS p, fv.geom
                FROM import_control.active_snapshot a
                JOIN source_raw.snapshot_member m ON m.snapshot_id = a.snapshot_id
                JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                WHERE a.source_key = '{source}' AND fv.layer = '{layer}' {order})"""


@dataclass(frozen=True)
class Group:
    source: str             # the source it is built from, or a name of its own
    version: int            # raise when the SQL below changes, to force a rebuild
    tables: tuple[str, ...]
    statements: tuple[str, ...]
    # Sources whose snapshots the group depends on, when it is derived from
    # other groups' tables instead of straight from one source.
    depends: tuple[str, ...] = ()


# A route in Turrutebasen is a code (rutenummer) on a set of segments. A code
# can be used for stretches far apart (jot91: two, 16 km from each other), so a
# route candidate is a code and one group of segments lying together. Its id is
# derived from those and is stable across rebuilds.
#
# The maintainer is not part of what a route is. Responsibility belongs to the
# stretch: it can change along a route and can be shared, so it is kept per
# segment, as a list.
SPLIT_M = 1000
# DNT's winter routes among the ski trails: the summer code with a v (jot11v, har116v; `seasons`). The other ski
# trails (prepared tracks, mostly the municipalities') are not routes of DNT's.

TURRUTEBASEN = Group(
    source="turrutebasen",
    version=7,
    tables=("route_segment", "trail_segment", "route", "info_point",
            "winter_route_segment", "winter_segment", "winter_route"),
    statements=(
        f"""CREATE TEMP TABLE seg ON COMMIT DROP AS
            SELECT s.external_id AS id, s.version_id, s.p,
                   {SIMPLE.format(g="s.geom")} AS geom, {LENGTH.format(g="s.geom")} AS length_m
            FROM {active("turrutebasen", "fotrute")} s""",
        "CREATE INDEX ON seg (id)",
        f"""CREATE TEMP TABLE info ON COMMIT DROP AS
            SELECT * FROM (
                SELECT lower(btrim(p->>'rutenummer')) AS code,
                       coalesce(btrim(p->>'vedlikeholdsansvarlig'), '') AS maintainer,
                       p->>'fotrute_fk' AS segment_id,
                       nullif(btrim(p->>'rutenavn'), '') AS name,
                       nullif(p->>'gradering', '') AS grading,
                       nullif(p->>'spesialfotrutetype', '') AS special_type,
                       (p->>'rutetype')::int AS route_type,
                       (p->>'rutebetydning')::int AS importance
                FROM {active("turrutebasen", "fotruteinfo")} i) x
            WHERE code <> '' AND segment_id IN (SELECT id FROM seg)""",
        # Segments of one code and maintainer that lie more than SPLIT_M from
        # the rest are another route that happens to share the code.
        f"""CREATE TEMP TABLE part ON COMMIT DROP AS
            SELECT code, segment_id,
                   ST_ClusterDBSCAN(geom, eps := {SPLIT_M}, minpoints := 1)
                       OVER (PARTITION BY code) AS piece
            FROM (SELECT DISTINCT i.code, i.segment_id, s.geom
                  FROM info i JOIN seg s ON s.id = i.segment_id) x""",
        # A route that was not split keeps the id it always had. A split one is
        # identified by its first segment as well, which survives a rebuild.
        """CREATE TEMP TABLE route_key ON COMMIT DROP AS
            SELECT code, piece,
                   substr(md5(code || CASE WHEN count(*) OVER (PARTITION BY code) > 1
                                           THEN chr(31) || first_segment ELSE '' END), 1, 12)
                       AS route_id
            FROM (SELECT code, piece, min(segment_id) AS first_segment
                  FROM part GROUP BY 1, 2) x""",
        """CREATE TEMP TABLE route_info ON COMMIT DROP AS
            SELECT k.route_id, i.*
            FROM info i
            JOIN part p USING (code, segment_id)
            JOIN route_key k USING (code, piece)""",
        # Turrutebasen says who maintains a segment per route on it, not per segment.
        f"""CREATE TABLE {BUILD}.route_segment AS
            SELECT route_id, segment_id,
                   coalesce(array_agg(DISTINCT maintainer) FILTER (WHERE maintainer <> ''), '{{}}')
                       AS maintainers
            FROM route_info GROUP BY 1, 2""",
        f"""CREATE TABLE {BUILD}.trail_segment AS
            SELECT s.id, s.version_id, s.geom, s.length_m,
                   (s.p->>'merking') = 'JA' AS marked,
                   (s.p->>'oppdateringsdato')::timestamptz AS updated,
                   r.route_ids, r.codes, coalesce(r.maintainers, '{{}}') AS maintainers,
                   s.p AS attrs
            FROM seg s
            LEFT JOIN (SELECT segment_id, array_agg(DISTINCT route_id) AS route_ids,
                              array_agg(DISTINCT code) AS codes,
                              array_agg(DISTINCT maintainer) FILTER (WHERE maintainer <> '')
                                  AS maintainers
                       FROM route_info GROUP BY 1) r ON r.segment_id = s.id""",
        # The label is unique: the code itself where one candidate carries it,
        # else the code with a part number, west to east (jot91/1, jot91/2). No
        # real code contains a slash, and letters are taken: Turrutebasen uses
        # a, b, c for real alternatives between the same two places.
        f"""CREATE TABLE {BUILD}.route AS
            SELECT id, code,
                   CASE WHEN n = 1 THEN code ELSE code || '/' || rank END AS label,
                   array_to_string(maintainers, '; ') AS maintainer, maintainers,
                   area_prefix, name, names, grading, special_type, route_type,
                   importance, segment_count, length_m, geom
            FROM (SELECT i.route_id AS id, i.code, i.maintainers,
                         substring(i.code from '^([a-z]{{3}})[0-9]') AS area_prefix,
                         i.name, i.names, i.grading, i.special_type, i.route_type, i.importance,
                         g.segment_count, g.length_m, g.geom,
                         count(*) OVER (PARTITION BY i.code) AS n,
                         row_number() OVER (
                             PARTITION BY i.code
                             ORDER BY round(ST_X(ST_Centroid(g.geom))),
                                      round(ST_Y(ST_Centroid(g.geom))), i.route_id) AS rank
                  FROM (SELECT route_id, code,
                               coalesce(array_agg(DISTINCT maintainer)
                                            FILTER (WHERE maintainer <> ''), '{{}}') AS maintainers,
                               mode() WITHIN GROUP (ORDER BY name)
                                   FILTER (WHERE name IS NOT NULL) AS name,
                               array_agg(DISTINCT name) FILTER (WHERE name IS NOT NULL) AS names,
                               mode() WITHIN GROUP (ORDER BY grading)
                                   FILTER (WHERE grading IS NOT NULL) AS grading,
                               mode() WITHIN GROUP (ORDER BY special_type)
                                   FILTER (WHERE special_type IS NOT NULL) AS special_type,
                               mode() WITHIN GROUP (ORDER BY route_type) AS route_type,
                               min(importance) AS importance
                        FROM route_info GROUP BY 1, 2) i
                  JOIN (SELECT rs.route_id, count(*)::int AS segment_count,
                               sum(t.length_m)::real AS length_m,
                               ST_Collect(t.geom ORDER BY t.id) AS geom
                        FROM {BUILD}.route_segment rs
                        JOIN {BUILD}.trail_segment t ON t.id = rs.segment_id
                        GROUP BY 1) g ON g.route_id = i.route_id) x""",
        f"""CREATE TABLE {BUILD}.info_point AS
            SELECT s.external_id AS id, s.version_id, s.geom,
                   (s.p->>'tilrettelegging')::int AS facility,
                   nullif(s.p->>'informasjon', '') AS info,
                   nullif(s.p->>'vedlikeholdsansvarlig', '') AS maintainer, s.p AS attrs
            FROM {active("turrutebasen", "ruteinfopunkt")} s""",
        f"ALTER TABLE {BUILD}.route_segment ADD PRIMARY KEY (route_id, segment_id)",
        f"CREATE INDEX ON {BUILD}.route_segment (segment_id)",
        f"ALTER TABLE {BUILD}.trail_segment ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.trail_segment USING gist (geom)",
        f"CREATE INDEX ON {BUILD}.trail_segment USING gin (codes)",
        f"ALTER TABLE {BUILD}.route ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.route (code text_pattern_ops)",
        f"CREATE INDEX ON {BUILD}.route (label text_pattern_ops)",
        f"CREATE INDEX ON {BUILD}.route (area_prefix)",
        f"CREATE INDEX ON {BUILD}.route USING gin (name gin_trgm_ops)",
        f"CREATE INDEX ON {BUILD}.route USING gist (geom)",
        f"ALTER TABLE {BUILD}.info_point ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.info_point USING gist (geom)",
        # DNT's winter routes, from the ski trails: tables of their own, so that nothing made for the summer (the
        # candidates, the corrections, the planner) meets a line drawn over a frozen lake. A route is a code and a
        # group of segments lying together, as in the summer; its marking is mostly SM, staked for the season.
        f"""CREATE TEMP TABLE wseg ON COMMIT DROP AS
            SELECT s.external_id AS id, s.version_id, s.p,
                   {SIMPLE.format(g="s.geom")} AS geom, {LENGTH.format(g="s.geom")} AS length_m
            FROM {active("turrutebasen", "skiloype")} s""",
        "CREATE INDEX ON wseg (id)",
        f"""CREATE TEMP TABLE winfo ON COMMIT DROP AS
            SELECT * FROM (
                SELECT lower(btrim(p->>'rutenummer')) AS code,
                       coalesce(btrim(p->>'vedlikeholdsansvarlig'), '') AS maintainer,
                       p->>'skiloype_fk' AS segment_id,
                       nullif(btrim(p->>'rutenavn'), '') AS name
                FROM {active("turrutebasen", "skiloypeinfo")} i) x
            WHERE code ~ '{WINTER_CODE}' AND segment_id IN (SELECT id FROM wseg)""",
        f"""CREATE TEMP TABLE wpart ON COMMIT DROP AS
            SELECT code, segment_id,
                   ST_ClusterDBSCAN(geom, eps := {SPLIT_M}, minpoints := 1) OVER (PARTITION BY code) AS piece
            FROM (SELECT DISTINCT i.code, i.segment_id, s.geom FROM winfo i JOIN wseg s ON s.id = i.segment_id) x""",
        """CREATE TEMP TABLE wroute_key ON COMMIT DROP AS
            SELECT code, piece,
                   substr(md5(code || CASE WHEN count(*) OVER (PARTITION BY code) > 1
                                           THEN chr(31) || first_segment ELSE '' END), 1, 12) AS route_id
            FROM (SELECT code, piece, min(segment_id) AS first_segment FROM wpart GROUP BY 1, 2) x""",
        """CREATE TEMP TABLE wroute_info ON COMMIT DROP AS
            SELECT k.route_id, i.* FROM winfo i JOIN wpart p USING (code, segment_id) JOIN wroute_key k USING (code, piece)""",
        f"""CREATE TABLE {BUILD}.winter_route_segment AS
            SELECT route_id, segment_id,
                   coalesce(array_agg(DISTINCT maintainer) FILTER (WHERE maintainer <> ''), '{{}}') AS maintainers
            FROM wroute_info GROUP BY 1, 2""",
        f"""CREATE TABLE {BUILD}.winter_segment AS
            SELECT s.id, s.version_id, s.geom, s.length_m,
                   nullif(s.p->>'merking', '') AS marking, (s.p->>'merking') = 'SM' AS seasonal,
                   nullif(s.p->>'preparering', '') AS preparation,
                   (s.p->>'oppdateringsdato')::timestamptz AS updated,
                   (s.p->>'datafangstdato')::timestamptz AS captured,
                   r.route_ids, r.codes, coalesce(r.maintainers, '{{}}') AS maintainers, s.p AS attrs
            FROM wseg s
            JOIN (SELECT segment_id, array_agg(DISTINCT route_id) AS route_ids, array_agg(DISTINCT code) AS codes,
                         array_agg(DISTINCT maintainer) FILTER (WHERE maintainer <> '') AS maintainers
                  FROM wroute_info GROUP BY 1) r ON r.segment_id = s.id""",
        f"""CREATE TABLE {BUILD}.winter_route AS
            SELECT id, code, CASE WHEN n = 1 THEN code ELSE code || '/' || rank END AS label,
                   array_to_string(maintainers, '; ') AS maintainer, maintainers, area_prefix, name,
                   segment_count, length_m, captured_from, captured_to, geom
            FROM (SELECT i.route_id AS id, i.code, i.maintainers,
                         substring(i.code from '^([a-z]{{3}})[0-9]') AS area_prefix, i.name,
                         g.segment_count, g.length_m, g.captured_from, g.captured_to, g.geom,
                         count(*) OVER (PARTITION BY i.code) AS n,
                         row_number() OVER (PARTITION BY i.code ORDER BY round(ST_X(ST_Centroid(g.geom))),
                                            round(ST_Y(ST_Centroid(g.geom))), i.route_id) AS rank
                  FROM (SELECT route_id, code,
                               coalesce(array_agg(DISTINCT maintainer) FILTER (WHERE maintainer <> ''), '{{}}') AS maintainers,
                               mode() WITHIN GROUP (ORDER BY name) FILTER (WHERE name IS NOT NULL) AS name
                        FROM wroute_info GROUP BY 1, 2) i
                  JOIN (SELECT rs.route_id, count(*)::int AS segment_count, sum(t.length_m)::real AS length_m,
                               min(t.captured) AS captured_from, max(t.captured) AS captured_to,
                               ST_Collect(t.geom ORDER BY t.id) AS geom
                        FROM {BUILD}.winter_route_segment rs JOIN {BUILD}.winter_segment t ON t.id = rs.segment_id
                        GROUP BY 1) g ON g.route_id = i.route_id) x""",
        f"ALTER TABLE {BUILD}.winter_route_segment ADD PRIMARY KEY (route_id, segment_id)",
        f"ALTER TABLE {BUILD}.winter_segment ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.winter_segment USING gist (geom)",
        f"ALTER TABLE {BUILD}.winter_route ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.winter_route (code text_pattern_ops)",
        f"CREATE INDEX ON {BUILD}.winter_route (area_prefix)",
        f"CREATE INDEX ON {BUILD}.winter_route USING gist (geom)",
    ),
)

# Paths and tractor roads to snap to.
FKB_WFS = Group(
    source="fkb_wfs",
    version=2,
    tables=("path", "path_box"),
    statements=(
        f"""CREATE TABLE {BUILD}.path AS
            SELECT s.external_id AS id, s.version_id,
                   {SIMPLE.format(g="s.geom")} AS geom,
                   {LENGTH.format(g="s.geom")} AS length_m,
                   s.p->>'typeveg' AS kind, s.p->>'kommunenummer' AS municipality_no
            FROM {active("fkb_wfs", "veglenke", scopes=True)} s""",
        f"ALTER TABLE {BUILD}.path ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.path USING gist (geom)",
        # Where this source has been fetched for, and when: what an area's map data rests on.
        f"""CREATE TABLE {BUILD}.path_box AS
            SELECT a.scope, s.fetched_at,
                   ST_MakeEnvelope((s.manifest->'bbox'->>0)::float8, (s.manifest->'bbox'->>1)::float8,
                                   (s.manifest->'bbox'->>2)::float8, (s.manifest->'bbox'->>3)::float8,
                                   25833) AS geom
            FROM import_control.active_snapshot a JOIN source_raw.snapshot s ON s.id = a.snapshot_id
            WHERE a.source_key = 'fkb_wfs'""",
        f"CREATE INDEX ON {BUILD}.path_box USING gist (geom)",
    ),
)

# Roads, for routes that follow one. A link stands for the road as a whole or for
# one carriageway of it; the carriageway-only links (KB) would double the road.
VEGNETT = Group(
    source="vegnett",
    version=2,
    tables=("road", "road_area"),
    statements=(
        f"""CREATE TABLE {BUILD}.road AS
            SELECT s.external_id AS id, s.version_id,
                   {SIMPLE.format(g="s.geom")} AS geom,
                   {LENGTH.format(g="s.geom")} AS length_m,
                   s.p->>'typeveg' AS kind, s.p->>'vegkategori' AS category,
                   s.p->>'adressenavn' AS name, s.p->>'kommunenummer' AS municipality_no
            FROM {active("vegnett", "veglenke", scopes=True)} s
            WHERE s.p->>'detaljnivå' IS DISTINCT FROM 'KB'""",
        f"ALTER TABLE {BUILD}.road ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.road USING gist (geom)",
        # The municipalities roads have been fetched for, and when.
        f"""CREATE TABLE {BUILD}.road_area AS
            SELECT replace(a.scope, 'kommune:', '') AS municipality_no, s.fetched_at
            FROM import_control.active_snapshot a JOIN source_raw.snapshot s ON s.id = a.snapshot_id
            WHERE a.source_key = 'vegnett'""",
    ),
)

# OpenStreetMap ways, to look at beside the rest. ODbL: not among the lines that snapping follows.
OSM = Group(
    source="osm",
    version=2,
    tables=("osm_way", "osm_box"),
    statements=(
        f"""CREATE TABLE {BUILD}.osm_way AS
            SELECT s.external_id AS id, s.version_id,
                   {SIMPLE.format(g="s.geom")} AS geom,
                   {LENGTH.format(g="s.geom")} AS length_m,
                   s.p->>'highway' AS kind, s.p->>'name' AS name,
                   s.p->>'sac_scale' AS sac_scale, s.p->>'trail_visibility' AS visibility,
                   s.p->>'surface' AS surface,
                   (s.p->>'highway') IN ('path', 'footway', 'track', 'steps', 'bridleway', 'pedestrian',
                                         'cycleway', 'via_ferrata') AS on_foot
            FROM {active("osm", "way", scopes=True)} s""",
        f"ALTER TABLE {BUILD}.osm_way ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.osm_way USING gist (geom)",
        # Where this source has been fetched for, and when: what an area's map data rests on.
        f"""CREATE TABLE {BUILD}.osm_box AS
            SELECT a.scope, s.fetched_at,
                   ST_MakeEnvelope((s.manifest->'bbox'->>0)::float8, (s.manifest->'bbox'->>1)::float8,
                                   (s.manifest->'bbox'->>2)::float8, (s.manifest->'bbox'->>3)::float8,
                                   25833) AS geom
            FROM import_control.active_snapshot a JOIN source_raw.snapshot s ON s.id = a.snapshot_id
            WHERE a.source_key = 'osm'""",
        f"CREATE INDEX ON {BUILD}.osm_box USING gist (geom)",
    ),
)

# One row per spelling of a place name, so that every approved and historical
# spelling can be searched. The point is somewhere on the place's geometry.
STEDSNAVN = Group(
    source="stedsnavn",
    version=1,
    tables=("place",),
    statements=(
        f"""CREATE TABLE {BUILD}.place AS
            SELECT s.external_id::bigint AS place_id, n.name, n.alternative,
                   navn.j->>'sprak' AS language, navn.j->>'status' AS name_status,
                   n.spelling_status,
                   s.p->>'navneobjekttype' AS type, s.p->>'navneobjektgruppe' AS type_group,
                   s.p->>'navneobjekthovedgruppe' AS main_group,
                   s.p->'kommuner'->0->>'navn' AS municipality,
                   s.p->'kommuner'->0->>'nr' AS municipality_no,
                   s.p->'kommuner'->0->>'fylke' AS county,
                   ST_PointOnSurface(s.geom) AS geom
            FROM {active("stedsnavn", "sted")} s
            CROSS JOIN LATERAL jsonb_array_elements(s.p->'navn') AS navn(j)
            CROSS JOIN LATERAL (
                SELECT sk->>'navn' AS name, sk->>'status' AS spelling_status, false AS alternative
                FROM jsonb_array_elements(navn.j->'skrivemater') sk
                UNION ALL
                SELECT sk->>'navn', sk->>'status', true
                FROM jsonb_array_elements(coalesce(navn.j->'andre_skrivemater', '[]')) sk) n
            WHERE n.name IS NOT NULL""",
        f"CREATE INDEX ON {BUILD}.place (place_id)",
        f"CREATE INDEX ON {BUILD}.place USING gin (name gin_trgm_ops)",
        f"CREATE INDEX ON {BUILD}.place (lower(name) text_pattern_ops)",
        f"CREATE INDEX ON {BUILD}.place USING gist (geom)",
    ),
)

_HERITAGE = " UNION ALL ".join(
    f"""SELECT '{layer}'::text AS kind, s.external_id AS id, s.version_id, s.geom,
               nullif(s.p->>'navn', '') AS name,
               coalesce(s.p->>'lokalitetskategori', s.p->>'enkeltminnekategori') AS category,
               coalesce(s.p->>'lokalitetsart', s.p->>'enkeltminneart') AS art,
               s.p->>'vernetype' AS protection, s.p->>'vernelov' AS law,
               s.p->>'lokalitetid' AS site_id, s.p->>'linkkulturminnesok' AS url,
               s.p->>'kommune' AS municipality_no,
               nullif(s.p->>'informasjon', '') AS info, (s.p->>'synlig')::int = 1 AS visible
        FROM {active("kulturminner", layer)} s"""
    for layer in ("lokalitet", "enkeltminne", "sikringssone")
)

KULTURMINNER = Group(
    source="kulturminner",
    version=2,
    tables=("heritage",),
    statements=(
        f"CREATE TABLE {BUILD}.heritage AS {_HERITAGE}",
        f"ALTER TABLE {BUILD}.heritage ADD PRIMARY KEY (kind, id)",
        f"CREATE INDEX ON {BUILD}.heritage USING gist (geom)",
    ),
)

# A municipality's delivery of the property map also has slivers of its neighbours' parcels along the border,
# under the same ids (found 2026-11-23: Luster's state commons, 337 km², came as a sliver from Lom). A parcel is
# taken as its own municipality delivers it; where that is not loaded, the largest piece there is.
TEIG_OWN = "(a.scope = 'kommune:' || (fv.properties->>'kommunenummer')) DESC, ST_Area(fv.geom) DESC"

TEIG = Group(
    source="teig",
    version=2,
    tables=("parcel",),
    statements=(
        f"""CREATE TABLE {BUILD}.parcel AS
            SELECT s.external_id AS id, s.version_id, s.geom,
                   s.p->>'kommunenummer' AS municipality_no,
                   s.p->>'matrikkelnummertekst' AS matrikkel,
                   (s.p->>'lagretberegnetareal')::double precision AS area_m2,
                   (s.p->>'tvist')::int = 1 AS disputed,
                   (SELECT jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                               'gnr', u->'gardsnummer', 'bnr', u->'bruksnummer',
                               'fnr', u->'festenummer', 'snr', u->'seksjonsnummer',
                               'type', u->'matrikkelenhetstype', 'name', u->'bruksnavn',
                               'id', u->'matrikkelenhetid')))
                    FROM jsonb_array_elements(s.p->'matrikkelenheter') u) AS units
            FROM {active("teig", "teig", scopes=True, prefer=TEIG_OWN)} s""",
        f"ALTER TABLE {BUILD}.parcel ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.parcel USING gist (geom)",
    ),
)

# Where routes end, and what they end at. All of it is a proposal worked out
# from the sources, for a person to confirm or correct in the owned layer.
#
# An end of a route is a segment end that no other segment of the same route
# touches (within 1 m, the tolerance v1 arrived at). An anchor is the place an
# end belongs to: a hut within 300 m (from Stedsnavn, or a Turrutebasen info
# point for a staffed, self-service or unstaffed hut), else a parking info
# point within 100 m, else the other route ends within 30 m of it.
_HUT_M, _PARKING_M, _JOIN_M, _SPOT_M = 300, 100, 30, 5
_INDEX = "source_index"

ANCHORS = Group(
    source="anchors",
    depends=("turrutebasen", "stedsnavn"),
    version=2,
    tables=("anchor", "route_end", "route_shape"),
    statements=(
        f"""CREATE TEMP TABLE seg_end ON COMMIT DROP AS
            SELECT rs.route_id, e.pt
            FROM {_INDEX}.route_segment rs
            JOIN {_INDEX}.trail_segment t ON t.id = rs.segment_id
            CROSS JOIN LATERAL (VALUES (ST_StartPoint(t.geom)), (ST_EndPoint(t.geom))) e(pt)
            WHERE GeometryType(t.geom) = 'LINESTRING'""",
        """CREATE TEMP TABLE term ON COMMIT DROP AS
            SELECT route_id || ':' || row_number() OVER (
                       PARTITION BY route_id ORDER BY ST_X(geom), ST_Y(geom)) AS id, route_id, geom
            FROM (SELECT route_id, ST_Centroid(ST_Collect(pt)) AS geom
                  FROM (SELECT route_id, pt, ST_ClusterDBSCAN(pt, eps := 1, minpoints := 1)
                                                 OVER (PARTITION BY route_id) AS c
                        FROM seg_end) x
                  GROUP BY route_id, c HAVING count(*) = 1) y""",
        "CREATE INDEX ON term USING gist (geom)",
        f"""CREATE TEMP TABLE piece ON COMMIT DROP AS
            SELECT route_id, count(DISTINCT c)::int AS pieces
            FROM (SELECT rs.route_id, ST_ClusterDBSCAN(t.geom, eps := 1, minpoints := 1)
                                          OVER (PARTITION BY rs.route_id) AS c
                  FROM {_INDEX}.route_segment rs
                  JOIN {_INDEX}.trail_segment t ON t.id = rs.segment_id) x
            GROUP BY 1""",
        # Named places an end can belong to. A hut info point beside a hut that
        # Stedsnavn knows is that hut. Facility 42, 43, 44: staffed,
        # self-service and unstaffed hut; 22: parking. A hut info point is named
        # after the nearest place name; its own `opphav` field, which v1 used,
        # mostly says how it was captured ("Rett i kartet"), not what it is.
        f"""CREATE TEMP TABLE cand ON COMMIT DROP AS
            WITH hut AS (
                SELECT DISTINCT ON (place_id) 'place:' || place_id AS id, 'hut'::text AS kind,
                       name, geom
                FROM {_INDEX}.place WHERE type = 'turisthytte' AND NOT alternative
                ORDER BY place_id, (name_status = 'hovednavn') DESC, name),
            info AS (
                SELECT 'info:' || i.id AS id,
                       CASE WHEN i.facility = 22 THEN 'parking' ELSE 'hut' END AS kind,
                       CASE WHEN i.facility = 22 THEN left(i.info, 60)
                            ELSE coalesce(near.name, left(i.info, 60)) END AS name,
                       i.geom
                FROM {_INDEX}.info_point i
                LEFT JOIN LATERAL (
                    SELECT pl.name FROM {_INDEX}.place pl
                    WHERE NOT pl.alternative AND ST_DWithin(pl.geom, i.geom, 200)
                    ORDER BY pl.geom <-> i.geom LIMIT 1) near ON i.facility <> 22
                WHERE i.facility IN (22, 42, 43, 44))
            SELECT id, kind, name, geom FROM hut
            UNION ALL
            SELECT i.id, i.kind, i.name, i.geom FROM info i
            WHERE i.kind = 'parking'
               OR NOT EXISTS (SELECT 1 FROM hut h WHERE ST_DWithin(h.geom, i.geom, 150))""",
        "CREATE INDEX ON cand USING gist (geom)",
        f"""CREATE TEMP TABLE assigned ON COMMIT DROP AS
            SELECT t.id, t.route_id, t.geom, c.id AS anchor_id
            FROM term t
            LEFT JOIN LATERAL (
                SELECT c.id FROM cand c
                WHERE ST_DWithin(c.geom, t.geom, {_HUT_M})
                  AND (c.kind = 'hut' OR ST_DWithin(c.geom, t.geom, {_PARKING_M}))
                ORDER BY (c.kind <> 'hut'), c.geom <-> t.geom LIMIT 1) c ON true""",
        # Ends that belong to no named place: those lying together are one
        # meeting point. Its id is its position, so it survives a rebuild.
        f"""CREATE TEMP TABLE meeting ON COMMIT DROP AS
            SELECT id, 'pt:' || round(ST_X(centre)) || '_' || round(ST_Y(centre)) AS anchor_id, centre
            FROM (SELECT id, ST_Centroid(ST_Collect(geom) OVER (PARTITION BY c)) AS centre
                  FROM (SELECT id, geom, ST_ClusterDBSCAN(geom, eps := {_JOIN_M}, minpoints := 1)
                                             OVER () AS c
                        FROM assigned WHERE anchor_id IS NULL) x) y""",
        f"""CREATE TABLE {BUILD}.route_end AS
            SELECT a.id, a.route_id, coalesce(a.anchor_id, m.anchor_id) AS anchor_id, a.geom,
                   ST_Distance(a.geom, coalesce(c.geom, m.centre))::real AS offset_m,
                   ST_MakeLine(a.geom, coalesce(c.geom, m.centre)) AS link,
                   ST_ClusterDBSCAN(a.geom, eps := {_SPOT_M}, minpoints := 1)
                       OVER (PARTITION BY coalesce(a.anchor_id, m.anchor_id)) AS spot
            FROM assigned a
            LEFT JOIN cand c ON c.id = a.anchor_id
            LEFT JOIN meeting m ON m.id = a.id""",
        # A lone end that touches another route's line is a junction, not a dead end.
        f"""CREATE TABLE {BUILD}.anchor AS
            WITH e AS (
                SELECT anchor_id, count(*)::int AS ends, count(DISTINCT route_id)::int AS routes,
                       count(DISTINCT spot)::int AS spots, max(offset_m)::real AS spread_m,
                       ST_Centroid(ST_Collect(geom)) AS centre,
                       bool_or(EXISTS (
                           SELECT 1 FROM {_INDEX}.trail_segment s
                           WHERE ST_DWithin(s.geom, re.geom, 1)
                             AND NOT (re.route_id = ANY(s.route_ids)))) AS meets_other
                FROM {BUILD}.route_end re GROUP BY 1)
            SELECT e.anchor_id AS id,
                   coalesce(c.kind, CASE WHEN e.routes > 1 OR e.meets_other THEN 'junction'
                                         ELSE 'dead end' END) AS kind,
                   coalesce(c.name, near.name) AS name, c.id IS NOT NULL AS named,
                   coalesce(c.geom, e.centre) AS geom, e.routes, e.ends, e.spots, e.spread_m
            FROM e
            LEFT JOIN cand c ON c.id = e.anchor_id
            LEFT JOIN LATERAL (
                SELECT pl.name FROM {_INDEX}.place pl
                WHERE c.name IS NULL AND NOT pl.alternative AND ST_DWithin(pl.geom, e.centre, 300)
                ORDER BY pl.geom <-> e.centre LIMIT 1) near ON true""",
        f"""CREATE TABLE {BUILD}.route_shape AS
            SELECT r.id AS route_id, coalesce(p.pieces, 0) AS pieces, coalesce(n.ends, 0) AS ends,
                   CASE WHEN coalesce(p.pieces, 0) > 1 THEN 'pieces'
                        WHEN coalesce(n.ends, 0) = 2 THEN 'line'
                        WHEN coalesce(n.ends, 0) = 0 THEN 'loop'
                        ELSE 'branched' END AS shape
            FROM {_INDEX}.route r
            LEFT JOIN piece p ON p.route_id = r.id
            LEFT JOIN (SELECT route_id, count(*)::int AS ends FROM term GROUP BY 1) n
                   ON n.route_id = r.id""",
        f"ALTER TABLE {BUILD}.anchor ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.anchor USING gist (geom)",
        f"ALTER TABLE {BUILD}.route_end ADD PRIMARY KEY (id)",
        f"CREATE INDEX ON {BUILD}.route_end (route_id)",
        f"CREATE INDEX ON {BUILD}.route_end (anchor_id)",
        f"CREATE INDEX ON {BUILD}.route_end USING gist (geom)",
        f"CREATE INDEX ON {BUILD}.route_end USING gist (link)",
        f"ALTER TABLE {BUILD}.route_shape ADD PRIMARY KEY (route_id)",
    ),
)

GROUPS = (TURRUTEBASEN, FKB_WFS, VEGNETT, OSM, STEDSNAVN, KULTURMINNER, TEIG, ANCHORS)
