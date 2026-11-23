"""source_index: built from the active snapshots, searchable, and rebuildable from nothing."""
from dataclasses import replace

import psycopg
import pytest
from conftest import FIXTURES, later_delivery, role_dsn

from stiflyt2.config import settings
from stiflyt2.importer.adapters import stedsnavn
from stiflyt2.index import build, query, tables

TABLES = ("route_segment", "trail_segment", "route", "info_point", "path", "path_box", "road", "road_area", "osm_way", "osm_box", "place", "heritage",
          "parcel", "anchor", "route_end", "route_shape")
SOURCES = {"turrutebasen", "fkb_wfs", "vegnett", "osm", "stedsnavn", "kulturminner", "teig", "anchors"}
ARENTZBU = 799507


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def places(text, **kw):
    with psycopg.connect(settings.import_dsn) as conn:
        return query.search_places(conn, text, **kw)


def routes(text):
    with psycopg.connect(settings.import_dsn) as conn:
        return query.search_routes(conn, text)


def contents() -> dict:
    """Row count and a checksum of every row, per index table."""
    return {t: q(f"""SELECT count(*), md5(coalesce(string_agg(x::text, '' ORDER BY x::text), ''))
                     FROM source_index.{t} x""")[0] for t in TABLES}


def test_index_holds_the_active_snapshots(loaded):
    counts = {t: n for t, (n, _) in contents().items()}
    assert counts["trail_segment"] == 11 and counts["info_point"] == 4
    assert counts["heritage"] == 6 and counts["parcel"] == 5 and counts["path"] >= 4
    assert counts["road"] == 23     # 25 road links, less the two that stand for one carriageway
    # Who maintains a segment is kept per route on it, as Turrutebasen has it.
    assert q("SELECT count(*), count(*) FILTER (WHERE maintainers <> '{}') FROM source_index.route_segment"
             ) == [(counts["route_segment"], counts["route_segment"])]

    # A route candidate is a code with its maintainer, over all segments carrying it.
    assert q("""SELECT name, segment_count, area_prefix, length_m BETWEEN 15000 AND 16000,
                       length_m < ST_Length(geom)
                FROM source_index.route WHERE code = 'bre18'""") == [
        ("Sprongdalshytta - Arentzbu", 4, "bre", True, True)]  # true length, shorter than planar
    assert q("SELECT codes FROM source_index.trail_segment WHERE id = %s",
             "19fa211c-e4e1-4634-aa5d-299803f37d3f") == [(["bre19", "bre21", "bre22", "bre62"],)]
    assert q("SELECT count(*) = count(DISTINCT label) FROM source_index.route") == [(True,)]
    assert q("SELECT DISTINCT GeometryType(geom) FROM source_index.trail_segment") == [
        ("LINESTRING",)]
    assert q("SELECT units->0->>'gnr', units->0->>'bnr' FROM source_index.parcel WHERE id = %s",
             "230985469") == [("178", "1")]
    assert {s: state for s, state, *_ in build.status()} == dict.fromkeys(SOURCES, "current")


def test_places_and_routes_can_be_searched(loaded):
    assert places("Arentzbu")[0]["place_id"] == ARENTZBU
    assert places("arentsbu")[0]["name"] == "Arentzbu"  # misspelt
    assert places("Sprongdalseggi")[0]["place_id"] == 131486  # a historical spelling
    # Among equally good matches, the closest to a given point comes first.
    hut = places("Arentzbu")[0]
    near = places("Sprong", near=(hut["east"], hut["north"]))
    assert [r["distance_m"] for r in near] == sorted(r["distance_m"] for r in near)

    assert [r["code"] for r in routes("bre1")] == ["bre14", "bre17", "bre18", "bre19"]
    assert routes("arentzbu - fast")[0]["code"] == "bre19"


def test_index_can_be_dropped_and_rebuilt(loaded):
    before = contents()
    with psycopg.connect(settings.import_dsn) as conn:
        for table in (*TABLES, "index_state"):
            conn.execute(f"DROP TABLE source_index.{table}")
    assert {b.source for b in build.build()} == SOURCES
    assert contents() == before


def test_only_what_changed_is_rebuilt(loaded, tmp_path):
    assert build.build() == []
    stedsnavn.run(later_delivery(
        FIXTURES / "stedsnavn.zip", tmp_path,
        "UPDATE skrivemate SET komplettskrivemate = 'Arentzbu turisthytte' "
        "WHERE komplettskrivemate = 'Arentzbu'"))
    # The anchors are worked out from routes and place names, so they follow.
    assert [s for s, state, *_ in build.status() if state == "STALE"] == ["stedsnavn", "anchors"]
    assert [b.source for b in build.build()] == ["stedsnavn", "anchors"]
    assert places("Arentzbu turisthytte")[0]["place_id"] == ARENTZBU


def test_failed_build_leaves_the_index_as_it_was(loaded, monkeypatch):
    before = contents()
    broken = replace(tables.FKB_WFS, version=99,
                     statements=(*tables.FKB_WFS.statements, "SELECT 1 / 0"))
    monkeypatch.setattr(build, "GROUPS", (broken,))
    with pytest.raises(psycopg.errors.DivisionByZero):
        build.build()
    assert contents() == before


def test_api_role_reads_the_index(loaded):
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        assert conn.execute("SELECT count(*) FROM source_index.place").fetchone()[0] > 0
        assert conn.execute("SELECT count(*) FROM source_index.index_state").fetchone()[0] == 8


def test_route_ends_are_gathered_at_anchors(loaded):
    # bre18 runs between two huts. Each end belongs to the hut it stops near.
    assert q("""SELECT a.name, a.kind, a.named, round(e.offset_m) BETWEEN 1 AND 100
                FROM source_index.route r
                JOIN source_index.route_end e ON e.route_id = r.id
                JOIN source_index.anchor a ON a.id = e.anchor_id
                WHERE r.code = 'bre18' ORDER BY a.name""") == [
        ("Arentzbu", "hut", True, True), ("Sprongdalshytta", "hut", True, True)]
    assert q("""SELECT s.shape, s.pieces, s.ends FROM source_index.route_shape s
                JOIN source_index.route r ON r.id = s.route_id WHERE r.code = 'bre18'""") == [
        ("line", 1, 2)]

    # Arentzbu is one anchor for every route that ends there. The three routes
    # that are complete in the fixture arrive at one shared spot.
    (anchor_id, routes), = q("SELECT id, routes FROM source_index.anchor WHERE name = 'Arentzbu'")
    assert anchor_id == f"place:{ARENTZBU}" and routes >= 3
    arriving = dict(q("""SELECT r.code, e.spot FROM source_index.route_end e
                         JOIN source_index.route r ON r.id = e.route_id
                         WHERE e.anchor_id = %s""", anchor_id))
    assert arriving["bre18"] == arriving["bre19"] == arriving["bre20"]

    # Every end belongs to exactly one anchor, and every route has a verdict.
    assert q("""SELECT count(*) FROM source_index.route_end e
                LEFT JOIN source_index.anchor a ON a.id = e.anchor_id WHERE a.id IS NULL""") == [(0,)]
    assert q("""SELECT (SELECT count(*) FROM source_index.route_shape)
                     = (SELECT count(*) FROM source_index.route)""") == [(True,)]


def test_a_code_used_for_stretches_far_apart_becomes_separate_candidates(loaded, tmp_path, small):
    from stiflyt2.importer.adapters import turrutebasen

    # Unsplit routes are labelled with their code and keep their id.
    (before_id, label), = q("SELECT id, label FROM source_index.route WHERE code = 'bre18'")
    assert label == "bre18"

    # Upstream tags a segment at the far end of bre19 as bre18 too: 10 km from
    # the rest of bre18, so it is another route sharing the code.
    far = "7883e674-3f55-4805-bf36-9352e87cc3b0"
    turrutebasen.run(later_delivery(
        FIXTURES / "turrutebasen.zip", tmp_path,
        f"UPDATE fotruteinfo_tabell SET rutenummer = 'bre18' "
        f"WHERE fotrute_fk = '{far}' AND rutenummer = 'bre49'"))
    build.build()
    found = q("""SELECT label, code, segment_count, id FROM source_index.route
                 WHERE code = 'bre18' ORDER BY segment_count DESC""")
    # Parts are numbered, not lettered: a, b, c are real alternatives in Turrutebasen.
    assert sorted(r[0] for r in found) == ["bre18/1", "bre18/2"]
    assert [(r[1], r[2]) for r in found] == [("bre18", 4), ("bre18", 1)]
    assert before_id not in [r[3] for r in found] and found[0][3] != found[1][3]
    assert q("SELECT route_id FROM source_index.route_segment WHERE segment_id = %s AND route_id = %s",
             far, found[1][3]) == [(found[1][3],)]
    # Searching for the shared code finds both; searching for a label finds that one.
    assert [r["label"] for r in routes("bre18")] == ["bre18/1", "bre18/2"]
    assert routes("bre18/2")[0]["label"] == "bre18/2"


def test_a_route_is_not_split_by_who_maintains_its_stretches(loaded, tmp_path, small):
    from stiflyt2.importer.adapters import turrutebasen

    (before,), = q("SELECT id FROM source_index.route WHERE code = 'bre18'")
    assert q("SELECT maintainers FROM source_index.route WHERE code = 'bre18'") == [
        (["DNT | DNT Oslo og omegn"],)]
    # One stretch of bre18 is the responsibility of another association.
    stretch = "74a241da-554e-492a-8e16-226732854ece"
    turrutebasen.run(later_delivery(
        FIXTURES / "turrutebasen.zip", tmp_path,
        f"UPDATE fotruteinfo_tabell SET vedlikeholdsansvarlig = 'DNT | Luster Turlag' "
        f"WHERE fotrute_fk = '{stretch}' AND rutenummer = 'bre18'"))
    build.build()
    # Still one route, with the same id, and two maintainers.
    assert q("SELECT id, label, segment_count, maintainers FROM source_index.route WHERE code = 'bre18'") == [
        (before, "bre18", 4, ["DNT | DNT Oslo og omegn", "DNT | Luster Turlag"])]
    # The stretch itself is shared: bre20 runs over it too, under the first association.
    assert q("SELECT maintainers FROM source_index.trail_segment WHERE id = %s", stretch) == [
        (["DNT | DNT Oslo og omegn", "DNT | Luster Turlag"],)]


def test_an_area_says_what_map_data_it_rests_on(loaded):
    from stiflyt2.domain import mapdata

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        found = mapdata.status(conn, "bre")
        # Paths were fetched for a box round the fixture's routes; roads for Luster, which they run through.
        assert 0 < found["paths"]["share"] <= 1 and found["paths"]["fetched"] is not None
        assert {"no": "4644", "name": "Luster", "loaded": True} in found["roads"]["municipalities"]
        assert found["roads"]["fetched"] is not None and 0 <= found["osm"]["share"] <= 1
        # The property map too, per municipality: the fixture's is Luster's.
        assert {"no": "4644", "name": "Luster", "loaded": True} in found["properties"]["municipalities"]
        assert found["properties"]["fetched"] is not None
        x0, y0, x1, y1 = mapdata.box(conn, "bre")
        assert x0 % 1000 == y0 % 1000 == 0 and x1 - x0 > 4000 and y1 - y0 > 4000
        assert mapdata.box(conn, "xyz") is None
        # An area nothing has been fetched for says so.
        nothing = {"share": 0.0, "fetched": None, "checked": None}
        assert mapdata.status(conn, "xyz") == {"paths": nothing, "osm": nothing, "roads": {
            "municipalities": [], "fetched": None, "checked": None}, "properties": {"municipalities": [], "fetched": None, "checked": None}}
        assert found["paths"]["checked"] is not None
