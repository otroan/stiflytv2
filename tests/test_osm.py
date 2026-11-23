"""The OpenStreetMap adapter against a canned Overpass server: no network, made-up ways."""
import json

import httpx
import psycopg
import pytest

from stiflyt2.config import settings
from stiflyt2.importer import core
from stiflyt2.importer.adapters import osm
from stiflyt2.index import build

BOX = (85_000, 6_850_000, 110_000, 6_875_000)   # one tile


class Overpass:
    """Answers every query with the same ways, and a new time of the data each time."""

    def __init__(self):
        self.calls, self.busy = 0, 0
        self.ways = [
            {"type": "way", "id": 101, "tags": {"highway": "path", "name": "Stien", "sac_scale": "mountain_hiking"},
             "geometry": [{"lat": 61.70, "lon": 7.50}, {"lat": 61.71, "lon": 7.51}, {"lat": 61.72, "lon": 7.51}]},
            {"type": "way", "id": 102, "tags": {"highway": "track"},
             "geometry": [{"lat": 61.70, "lon": 7.50}, {"lat": 61.69, "lon": 7.49}]},
            {"type": "way", "id": 103, "tags": {"highway": "tertiary", "ref": "55"},
             "geometry": [{"lat": 61.69, "lon": 7.49}, {"lat": 61.68, "lon": 7.47}]},
            {"type": "way", "id": 104, "tags": {"highway": "path"}, "geometry": [{"lat": 61.7, "lon": 7.5}]},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and b"highway" in request.content
        if self.busy:
            self.busy -= 1
            return httpx.Response(429, text="rate limited")
        self.calls += 1
        return httpx.Response(200, json={"version": 0.6, "osm3s": {"timestamp_osm_base": f"t{self.calls}"},
                                         "elements": self.ways})


@pytest.fixture
def overpass(small, monkeypatch):
    server, real = Overpass(), httpx.Client
    monkeypatch.setattr(osm.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    monkeypatch.setattr(osm, "PAUSE_S", 0)
    monkeypatch.setattr(osm, "RETRIES", (0, 0))
    return server


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def test_ways_are_stored_by_their_osm_id_with_their_tags(overpass):
    r = osm.run("test", BOX)
    assert r.counts == {"way": 3}       # the way with a single point has no line
    ways = dict(q("""SELECT fv.external_id, fv.properties FROM source_raw.snapshot_member m
                     JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                     WHERE m.snapshot_id = %s""", r.snapshot_id))
    assert ways["101"] == {"highway": "path", "name": "Stien", "sac_scale": "mountain_hiking"}
    assert set(ways) == {"101", "102", "103"}
    assert q("""SELECT DISTINCT ST_SRID(fv.geom), GeometryType(fv.geom) FROM source_raw.snapshot_member m
                JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                WHERE m.snapshot_id = %s""", r.snapshot_id) == [(25833, "LINESTRING")]
    # Registered as it is licensed: to be shown with attribution, not exported until someone decides.
    assert q("SELECT licence, attribution, export_policy FROM source_raw.source WHERE key = 'osm'") == [
        ("ODbL 1.0", "© OpenStreetMap contributors", "unresolved")]

    # The same ways again, in an answer made at another time: the same snapshot.
    assert osm.run("test", BOX).reused
    # A tag changed: one way updated. The service being busy once is waited out.
    overpass.ways[1]["tags"]["tracktype"] = "grade3"
    overpass.busy = 1
    r2 = osm.run("test", BOX)
    assert q("""SELECT i.kind, i.external_id FROM import_control.diff_item i
                JOIN import_control.diff d ON d.id = i.diff_id WHERE d.to_snapshot = %s""",
             r2.snapshot_id) == [("updated", "102")]

    # In the index: what it is, and whether it is something walked.
    build.build(only="osm")
    assert q("SELECT id, kind, name, sac_scale, on_foot, length_m > 1000 FROM source_index.osm_way ORDER BY id") == [
        ("101", "path", "Stien", "mountain_hiking", True, True), ("102", "track", None, None, True, True),
        ("103", "tertiary", None, None, False, True)]
    # It is not among the lines that routes are laid on.
    from stiflyt2.domain import routes

    with psycopg.connect(settings.import_dsn) as conn:
        assert "osm" not in routes.mapped_lines(conn)


def test_an_answer_that_is_not_one_is_refused(overpass):
    overpass.ways = []
    with pytest.raises(core.ImportRejected, match="no ways found"):
        osm.run("empty", BOX)
    with pytest.raises(core.ImportRejected, match="not an Overpass answer"):
        osm.ways(json.dumps({"error": "nope"}).encode())


def test_a_service_that_stays_busy_is_not_reached_and_what_is_loaded_stays(overpass):
    first = osm.run("busy", BOX)
    overpass.busy = 10
    with pytest.raises(core.NotReached, match="Overpass answers 429 after 3 tries"):
        osm.run("busy", BOX)
    assert q("SELECT snapshot_id::text FROM import_control.active_snapshot WHERE scope = 'bbox:busy'") == [
        (first.snapshot_id,)]


def test_openstreetmap_is_asked_for_once_a_week_not_every_night(overpass, monkeypatch):
    from stiflyt2.importer import refresh

    monkeypatch.setattr(refresh, "SOURCES", ("osm",))
    osm.run("test", BOX)
    asked = overpass.calls
    first = [c for c in refresh.run() if c.scope == "bbox:test"]
    assert [(c.source_key, c.outcome) for c in first] == [("osm", "unchanged")] and overpass.calls > asked
    # The next night it is left alone; asked for by name, it is checked all the same.
    asked = overpass.calls
    assert refresh.run() == [] and overpass.calls == asked
    assert [c.outcome for c in refresh.run("osm") if c.scope == "bbox:test"] == ["unchanged"]
    assert overpass.calls > asked


def test_a_busy_night_asks_no_more_and_the_next_night_asks_again(overpass, monkeypatch):
    from stiflyt2.importer import refresh

    monkeypatch.setattr(refresh, "SOURCES", ("osm",))
    for name in ("night1", "night2"):
        osm.run(name, BOX)
    overpass.busy = 100
    night = refresh.run()
    asked = 100 - overpass.busy
    assert asked == 3       # one box, three tries: then nothing more is asked of it that night
    mine = {c.scope: c for c in night if c.scope in ("bbox:night1", "bbox:night2")}
    assert {c.outcome for c in night} == {"unreached"} and set(mine) == {"bbox:night1", "bbox:night2"}
    first, *rest = night
    assert first.detail.startswith("Overpass answers 429") and all(c.detail.startswith("not asked tonight") for c in rest)
    # Nothing was refused as faulty, and the next night (the service free again) each is asked again.
    overpass.busy = 0
    assert {c.scope: c.outcome for c in refresh.run() if c.scope in mine} == {"bbox:night1": "unchanged", "bbox:night2": "unchanged"}
