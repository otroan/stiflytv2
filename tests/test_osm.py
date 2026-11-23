"""The OpenStreetMap adapter against a canned Geofabrik: no network, a made-up extract cut by GDAL."""
import hashlib
import itertools
from xml.sax.saxutils import quoteattr

import httpx
import psycopg
import pytest
from conftest import TEST_DB

from stiflyt2.config import settings
from stiflyt2.importer import core
from stiflyt2.importer.adapters import osm
from stiflyt2.index import build

BOX = (85_000, 6_850_000, 110_000, 6_875_000)   # in degrees about 61.56-61.81 north, 7.12-7.64 east
_made = itertools.count()


class Geofabrik:
    """Serves an extract (OpenStreetMap XML, which GDAL reads as it reads PBF) and its checksum.

    Each one serves a file of its own, so that what one test has downloaded is not another's.
    """

    URL = "https://download.geofabrik.de/test/test-latest.osm"

    def __init__(self):
        self.downloads, self.asked, self.down, self.spoilt, self.made = 0, 0, False, 0, next(_made)
        self.ways = [
            {"id": 101, "tags": {"highway": "path", "name": "Stien", "sac_scale": "mountain_hiking",
                                 "source": "survey", "note": "kept, though GDAL leaves notes out by itself"},
             "line": [(7.50, 61.70), (7.51, 61.71), (7.51, 61.72)]},
            {"id": 102, "tags": {"highway": "track"}, "line": [(7.50, 61.70), (7.49, 61.69)]},
            {"id": 103, "tags": {"highway": "tertiary", "ref": "55"}, "line": [(7.49, 61.69), (7.47, 61.68)]},
            {"id": 104, "tags": {"highway": "path"}, "line": [(7.5, 61.7)]},                       # no line
            {"id": 105, "tags": {"highway": "path"}, "line": [(8.00, 62.20), (8.01, 62.21)]},      # outside the box
            {"id": 106, "tags": {"highway": "footway"}, "line": [(7.10, 61.65), (7.30, 61.65)]},   # over its edge
            {"id": 107, "tags": {"waterway": "stream"}, "line": [(7.40, 61.60), (7.41, 61.61)]},   # not a highway
        ]

    def body(self) -> bytes:
        nodes, out = {}, []
        for w in self.ways:
            for point in w["line"]:
                nodes.setdefault(point, len(nodes) + 1)
        out.append(f'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="test {self.made}">')
        out += [f'  <node id="{n}" lon="{lon}" lat="{lat}" version="1"/>' for (lon, lat), n in nodes.items()]
        for w in self.ways:
            out.append(f'  <way id="{w["id"]}" version="1">')
            out += [f'    <nd ref="{nodes[point]}"/>' for point in w["line"]]
            out += [f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in w["tags"].items()]
            out.append("  </way>")
        out.append("</osm>\n")
        return "\n".join(out).encode()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked += 1
        if self.down:
            return httpx.Response(503, text="down for maintenance")
        body = self.body()
        if str(request.url) == f"{self.URL}.md5":
            return httpx.Response(200, text=f"{hashlib.md5(body).hexdigest()}  test-latest.osm\n")
        assert str(request.url) == self.URL
        self.downloads += 1
        if self.spoilt:
            self.spoilt -= 1
            body = body[:-20]           # cut short, or made anew while it was fetched
        return httpx.Response(200, content=body, headers={"Last-Modified": "Sun, 22 Nov 2026 00:41:55 GMT"})


@pytest.fixture
def geofabrik(small, monkeypatch):
    server, real = Geofabrik(), httpx.Client
    monkeypatch.setattr(osm.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    monkeypatch.setattr(osm, "URL", Geofabrik.URL)
    return server


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def test_ways_are_stored_by_their_osm_id_with_every_tag(geofabrik):
    r = osm.run("test", BOX)
    # In the box, a way over its edge whole, every tag; not a way with one point, outside, or not a highway.
    assert r.counts == {"way": 4}
    ways = dict(q("""SELECT fv.external_id, fv.properties FROM source_raw.snapshot_member m
                     JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                     WHERE m.snapshot_id = %s""", r.snapshot_id))
    assert set(ways) == {"101", "102", "103", "106"}
    assert ways["101"] == geofabrik.ways[0]["tags"]
    assert q("""SELECT DISTINCT ST_SRID(fv.geom), GeometryType(fv.geom) FROM source_raw.snapshot_member m
                JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                WHERE m.snapshot_id = %s""", r.snapshot_id) == [(25833, "LINESTRING")]
    edge = q("""SELECT ST_NPoints(fv.geom), round(ST_X(ST_Transform(ST_StartPoint(fv.geom), 4326))::numeric, 7)::float8
                FROM source_raw.snapshot_member m JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                WHERE m.snapshot_id = %s AND fv.external_id = '106'""", r.snapshot_id)
    assert edge == [(2, 7.1)]
    assert q("SELECT source_updated_at::date::text FROM source_raw.snapshot WHERE id = %s", r.snapshot_id) == [("2026-11-22",)]
    # Registered as it is licensed: to be shown with attribution, not exported until someone decides.
    assert q("SELECT licence, attribution, export_policy FROM source_raw.source WHERE key = 'osm'") == [
        ("ODbL 1.0", "© OpenStreetMap contributors", "unresolved")]

    # The same extract again: not fetched again, and the same snapshot.
    assert geofabrik.downloads == 1
    assert osm.run("test", BOX).reused and geofabrik.downloads == 1
    # A tag changed in a new extract: fetched, the one before removed, one way updated.
    geofabrik.ways[1]["tags"]["tracktype"] = "grade3"
    r2 = osm.run("test", BOX)
    assert geofabrik.downloads == 2 and len(list((settings.data_dir / "osm" / "geofabrik").glob("*-test-latest.osm"))) == 1
    assert q("""SELECT i.kind, i.external_id FROM import_control.diff_item i
                JOIN import_control.diff d ON d.id = i.diff_id WHERE d.to_snapshot = %s""",
             r2.snapshot_id) == [("updated", "102")]

    # In the index: what it is, and whether it is something walked.
    build.build(only="osm")
    assert q("""SELECT id, kind, name, sac_scale, on_foot, length_m > 1000 FROM source_index.osm_way
                WHERE id IN ('101', '102', '103', '106') ORDER BY id""") == [
        ("101", "path", "Stien", "mountain_hiking", True, True), ("102", "track", None, None, True, True),
        ("103", "tertiary", None, None, False, True), ("106", "footway", None, None, True, True)]
    # It is not among the lines that routes are laid on.
    from stiflyt2.domain import routes

    with psycopg.connect(settings.import_dsn) as conn:
        assert "osm" not in routes.mapped_lines(conn)


def test_a_box_with_no_ways_is_refused(geofabrik):
    geofabrik.ways = geofabrik.ways[4:5]        # only the one outside the box
    with pytest.raises(core.ImportRejected, match="no ways found"):
        osm.run("empty", BOX)


def test_geofabrik_not_answering_is_not_reached_and_what_is_loaded_stays(geofabrik):
    first = osm.run("down", BOX)
    geofabrik.ways[0]["tags"]["name"] = "Another"
    geofabrik.down = True
    with pytest.raises(core.NotReached, match="Geofabrik did not answer"):
        osm.run("down", BOX)
    assert q("SELECT snapshot_id::text FROM import_control.active_snapshot WHERE scope = 'bbox:down'") == [
        (first.snapshot_id,)]


def test_a_download_that_does_not_match_its_checksum_is_fetched_once_more(geofabrik):
    geofabrik.spoilt = 1
    osm.run("spoilt", BOX)
    assert geofabrik.downloads == 2
    geofabrik.ways[0]["tags"]["name"] = "Again"
    geofabrik.spoilt = 2
    with pytest.raises(core.NotReached, match="did not match its checksum twice"):
        osm.run("spoilt", BOX)
    assert not list((settings.data_dir / "osm" / "geofabrik").glob("*.part"))


def owner_sql(sql):
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as conn:
        conn.execute("SET ROLE stiflyt2_owner")
        conn.execute(sql)


def test_openstreetmap_is_checked_once_a_week_every_box_together(geofabrik, monkeypatch):
    from stiflyt2.importer import refresh

    monkeypatch.setattr(refresh, "SOURCES", ("osm",))
    for name in ("week1", "week2"):
        osm.run(name, BOX)
    mine = {"bbox:week1", "bbox:week2"}
    first = {c.scope: c.outcome for c in refresh.run() if c.scope in mine}
    assert first == {"bbox:week1": "unchanged", "bbox:week2": "unchanged"} and geofabrik.downloads == 1
    # The next night they are left alone; asked for by name, they are checked all the same.
    assert refresh.run() == []
    assert {c.scope for c in refresh.run("osm")} >= mine
    # A week on, one box is due: every box is checked with it, from the one download.
    owner_sql("UPDATE import_control.check_log SET started_at = now() - interval '8 days' WHERE scope = 'bbox:week1'")
    assert {c.scope for c in refresh.run()} >= mine and geofabrik.downloads == 1


def test_a_night_geofabrik_does_not_answer_asks_no_more_and_the_next_night_asks_again(geofabrik, monkeypatch):
    from stiflyt2.importer import refresh

    monkeypatch.setattr(refresh, "SOURCES", ("osm",))
    for name in ("night1", "night2"):
        osm.run(name, BOX)
    owner_sql("DELETE FROM import_control.check_log WHERE source_key = 'osm'")
    geofabrik.down, geofabrik.asked = True, 0
    night = refresh.run()
    assert geofabrik.asked == 1        # once, for the checksum: then nothing more that night
    mine = {c.scope: c for c in night if c.scope in ("bbox:night1", "bbox:night2")}
    assert {c.outcome for c in night} == {"unreached"} and set(mine) == {"bbox:night1", "bbox:night2"}
    first, *rest = night
    assert first.detail.startswith("Geofabrik did not answer") and all(c.detail.startswith("not asked tonight") for c in rest)
    # Nothing was refused as faulty, and the next night (Geofabrik up again) each is asked again.
    geofabrik.down = False
    assert {c.scope: c.outcome for c in refresh.run() if c.scope in mine} == {"bbox:night1": "unchanged", "bbox:night2": "unchanged"}
