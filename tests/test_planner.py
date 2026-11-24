"""The route planner: a network of every way one may walk, cut where ways meet, and a way over it that prefers
DNT's routes as much as is asked for. Built here from a small made-up extract beside Turrutebasen's fixture."""
import json
from xml.sax.saxutils import quoteattr

import httpx
import psycopg
import pytest
from conftest import HTTP_CLIENT, TEST_DB, role_dsn
from fastapi.testclient import TestClient
from test_mcp import call, rpc

from stiflyt2 import access, height, terrain
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain.places import Invalid
from stiflyt2.ops import planner, walknet

V1 = "/api/v1"
TRAIL = "ce4908aa-d039-413e-996d-96b4440a1dd0"        # bre20, 2.1 km, along which a path is mapped
BARE = "6ce2c484-2173-4fa7-8978-24f9e42946f0"         # bre18, 14 km, with nothing mapped along it
C, D, E, F = (7.45, 61.6668), (7.47, 61.68), (7.49, 61.69), (7.47, 61.70)
LONELY = (7.37, 61.68)          # a path that joins nothing, a kilometre from the rest


def extract(path, ways, points=()):
    """An extract as OpenStreetMap XML, which pyosmium and GDAL read as they read PBF. Ways that share a point
    share its node."""
    nodes = {}
    for _, line in ways:
        for p in line:
            nodes.setdefault(p, len(nodes) + 1)
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="test">']
    out += [f'  <node id="{n}" lon="{lon}" lat="{lat}" version="1"/>' for (lon, lat), n in nodes.items()]
    for i, ((lon, lat), tags) in enumerate(points, 1):
        out.append(f'  <node id="{900_000 + i}" lon="{lon}" lat="{lat}" version="1">'
                   + "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items()) + "</node>")
    for i, (tags, line) in enumerate(ways, 1):
        out.append(f'  <way id="{i}" version="1">' + "".join(f'<nd ref="{nodes[p]}"/>' for p in line)
                   + "".join(f"<tag k={quoteattr(k)} v={quoteattr(v)}/>" for k, v in tags.items()) + "</way>")
    out.append("</osm>\n")
    path.write_text("\n".join(out))
    return path


def test_what_may_be_walked_is_said_by_the_tags():
    def kind(**tags):
        return walknet.kind_of(tags.get)

    assert kind(highway="path") == ("path", False, False, False)
    assert kind(highway="track", access="private") == ("track", True, False, False)
    assert kind(highway="track", access="private", foot="yes") == ("track", False, False, False)
    assert kind(highway="primary", tunnel="yes") == ("primary", False, True, False)
    assert kind(highway="secondary", foot="use_sidepath")[3] is True
    assert kind(route="ferry") == ("ferry", False, False, False)
    for no in ({"highway": "motorway"}, {"highway": "trunk", "motorroad": "yes"}, {"highway": "path", "foot": "no"},
               {"highway": "proposed"}, {"highway": "platform"}, {"waterway": "river"}, {"route": "ferry", "foot": "no"}):
        assert walknet.kind_of(no.get) is None, no


@pytest.fixture
def network(loaded, tmp_path):
    """The network built from a small extract: a path along bre20 and one shorter beside it, a motorway and a
    path closed on foot between the same two ends, a road on to C, a ferry to D, and from there a path over a
    glacier to E and one round it, twice as long."""
    with psycopg.connect(dbname=TEST_DB) as owner:
        owner.execute("SET ROLE stiflyt2_owner")
        along = [tuple(round(v, 7) for v in c) for c in owner.execute(
            "SELECT ST_AsGeoJSON(ST_Transform(geom, 4326), 7)::json->'coordinates' FROM source_index.trail_segment "
            "WHERE id = %s", (TRAIL,)).fetchone()[0]]
        mid = owner.execute("SELECT ST_AsGeoJSON(ST_Transform(ST_LineInterpolatePoint(geom, 0.5), 4326), 7)::json"
                            "->'coordinates' FROM source_index.trail_segment WHERE id = %s", (BARE,)).fetchone()[0]
        a, b = along[0], along[-1]
        aside = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.003)       # north of bre20, which bends south
        ice = [(7.472, 61.678), (7.488, 61.678), (7.488, 61.692), (7.472, 61.692), (7.472, 61.678)]
        ways = [({"highway": "path", "name": "Langs ruta"}, along),
                ({"highway": "path", "name": "Snarveien"}, [a, aside, b]),
                ({"highway": "motorway"}, [a, b]),
                ({"highway": "path", "foot": "no"}, [a, (aside[0], aside[1] + 0.004), b]),
                ({"highway": "tertiary", "ref": "Fv55"}, [b, C]),
                ({"route": "ferry", "name": "Testferja"}, [C, D]),
                ({"highway": "path"}, [D, E]),
                ({"highway": "path", "name": "Rundt breen"}, [D, F, E]),
                ({"natural": "glacier", "name": "Testbreen"}, ice),
                ({"highway": "path", "name": "Ensom sti"}, [LONELY, (LONELY[0] - 0.002, LONELY[1] + 0.001)])]
        huts = [((C[0] - 0.002, C[1] + 0.001), {"tourism": "alpine_hut", "name": "Testhytta", "operator": "DNT Oslo og Omegn"}),
                (tuple(mid), {"tourism": "wilderness_hut", "name": "Midtbu"})]
        pbf = extract(tmp_path / "test-latest.osm", ways, huts)
        counts = walknet.build(owner, pbf, "abc123")
        yield {"a": a, "b": b, "along": along, "counts": counts, "bare": owner.execute(
            "SELECT ST_AsGeoJSON(ST_Transform(geom, 4326), 7)::json->'coordinates' FROM source_index.trail_segment "
            "WHERE id = %s", (BARE,)).fetchone()[0]}


@pytest.fixture
def conn(network):
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def test_the_ways_are_cut_where_they_meet_and_what_may_not_be_walked_is_left_out(network, conn):
    counts = network["counts"]
    assert counts["huts"] == 2 and counts["ferry_km"] >= 1
    kinds = dict(conn.execute("SELECT kind, count(*) FROM walk.edge GROUP BY 1").fetchall())
    assert "motorway" not in kinds and kinds["ferry"] == 1 and kinds["tertiary"] == 1
    # No path closed on foot: two paths between A and B, each one stretch, from the same node to the same node.
    paths = conn.execute("SELECT name, source, target, trail, dnt FROM walk.edge WHERE kind = 'path' AND name IS NOT NULL "
                         "AND name NOT IN ('Rundt breen', 'Ensom sti') ORDER BY name").fetchall()
    assert [(n, t, d) for n, _, _, t, d in paths] == [("Langs ruta", True, True), ("Snarveien", False, False)]
    assert {paths[0][1], paths[0][2]} == {paths[1][1], paths[1][2]}
    # bre18 has nothing mapped along it: its own line is put in, and DNT looks after it.
    put = conn.execute("SELECT origin, kind, dnt, round(length_m) FROM walk.edge WHERE origin = 'turrutebasen'").fetchall()
    assert ("turrutebasen", "trail", True, 14012) in put
    # Over the glacier, and the huts: one of DNT's.
    assert conn.execute("SELECT bool_and(ice) FROM walk.edge WHERE kind = 'path' AND name IS NULL AND origin = 'osm'").fetchone()[0]
    assert not conn.execute("SELECT ice FROM walk.edge WHERE name = 'Rundt breen'").fetchone()[0]
    assert conn.execute("SELECT name, kind, dnt FROM walk.hut ORDER BY name").fetchall() == [
        ("Midtbu", "wilderness_hut", False), ("Testhytta", "alpine_hut", True)]
    with psycopg.connect(dbname=TEST_DB) as owner:
        assert walknet.built(owner)["extract_md5"] == "abc123"


def test_dnts_routes_are_preferred_as_much_as_asked(network, conn):
    a, b = network["a"], network["b"]
    shortest = planner.plan(conn, [a, b], planner.Options(prefer=0))
    along = planner.plan(conn, [a, b], planner.Options(prefer=1))
    assert [s["class"] for s in shortest["steps"]] == ["path"] and shortest["steps"][0]["name"] == "Snarveien"
    assert [s["class"] for s in along["steps"]] == ["dnt"] and along["total"]["metres"] > shortest["total"]["metres"]
    assert along["steps"][0]["says"] == "Along DNT's marked route bre20"
    # Never along the motorway, though it is the shortest of all.
    assert shortest["total"]["metres"] > 1.0 * conn.execute(
        "SELECT ST_Length(ST_MakeLine(ST_MakePoint(%s, %s), ST_MakePoint(%s, %s))::geography)", (*a, *b)).fetchone()[0]
    # The line is one, from the first point to the last.
    assert along["coords"][0] == pytest.approx(list(a), abs=1e-5) and along["coords"][-1] == pytest.approx(list(b), abs=1e-5)
    assert along["credits"] == [planner.CREDITS["osm"]]
    # Both ends on the same stretch: the part of it between them, either way.
    on = network["along"]
    for one, two in ((on[5], on[20]), (on[20], on[5])):
        part = planner.plan(conn, [one, two], planner.Options(prefer=1))
        assert [s["class"] for s in part["steps"]] == ["dnt"] and part["off"] == []
        between = on[5:21] if one == on[5] else on[20:4:-1]
        assert len(part["coords"]) == len(between) and all(c == pytest.approx(list(p), abs=1e-6)
                                                           for c, p in zip(part["coords"], between, strict=True))


def test_a_ferry_a_glacier_and_a_point_off_the_way(network, conn):
    a = network["a"]
    # Without the ferry, E cannot be reached on foot: the leg is drawn straight, and says why.
    cut = planner.plan(conn, [a, E], planner.Options(ice=True, ferries=False))
    assert [s["class"] for s in cut["steps"]] == ["off"] and "no way on foot was found" in cut["legs"][0]["unfound"]
    assert cut["legs"][0]["metres"] == cut["legs"][0]["crow_m"] and not cut["legs"][0]["straight"]
    told = planner.plan(conn, [a, C, E], planner.Options(ice=True))
    classes = [s["class"] for s in told["steps"]]
    assert "ferry" in classes and "road" in classes and told["steps"][-1]["ice"]
    assert told["steps"][classes.index("road")]["says"] == "Road, Fv55" and told["steps"][classes.index("road")].get("via") == 1
    assert told["total"]["by"]["ferry"] > 1000 and told["total"]["walked_m"] < told["total"]["metres"]
    # Without the glacier, round it.
    kept = planner.plan(conn, [a, C, E], planner.Options(ice=False))
    assert not any(s["ice"] for s in kept["steps"]) and kept["steps"][-1]["name"] == "Rundt breen"
    assert kept["total"]["metres"] > told["total"]["metres"] + 1500
    # Clicked a little beside a path: it begins on the path, and says how far off it was.
    beside = planner.plan(conn, [[a[0] - 0.001, a[1]], network["b"]], planner.Options(prefer=1))
    assert beside["steps"][0]["class"] == "off" and 30 < beside["off"][0]["off_m"] < 80
    for wrong in ([a], [a] * 13):
        with pytest.raises(Invalid):
            planner.plan(conn, wrong)
    far = planner.plan(conn, [a, (10.0, 60.0)])
    assert "no path or road within 10 km" in far["legs"][0]["unfound"] and far["steps"][0]["class"] == "off"
    with pytest.raises(Invalid, match="for each leg"):
        planner.plan(conn, [a, network["b"]], straight=[True, False])


def test_a_leg_is_drawn_straight_where_the_person_says_so(network, conn):
    """Where nothing is mapped: a sketch over the ground, between the two points as clicked."""
    a, b = network["a"], network["b"]
    told = planner.plan(conn, [a, b, C], straight=[True, False])
    assert [s["class"] for s in told["steps"]][:1] == ["off"] and told["steps"][0]["says"] == "Straight, with no path (a sketch)"
    assert told["steps"][0].get("via") == 1 and told["coords"][0] == list(a) and told["coords"][1] == list(b)
    first, second = told["legs"]
    assert first["straight"] and first["unfound"] is None and first["metres"] == first["crow_m"]
    assert not second["straight"] and second["metres"] >= second["crow_m"] and not second["roundabout"]
    assert told["coords"][second["at"][0]] == list(b) and second["at"][1] == len(told["coords"]) - 1


class Heights:
    """Kartverket's height service, made up: the ground rises 1 m for every 5 m north."""

    def __init__(self):
        self.asked = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked += 1
        points = json.loads(request.url.params["punkter"])
        return httpx.Response(200, json={"punkter": [{"x": x, "y": y, "z": (y - 6_840_000) / 5, "terreng": "Fjell",
                                                      "datakilde": "test"} for x, y in points]})

    def client(self):
        return HTTP_CLIENT(transport=httpx.MockTransport(self))


def test_the_climb_the_time_and_days_that_end_at_huts(network, conn):
    bare = network["bare"]
    told = planner.plan(conn, [bare[0], bare[-1]])
    assert [s["class"] for s in told["steps"]] == ["dnt"] and told["steps"][0]["says"] == "Along DNT's marked route bre18"
    assert planner.CREDITS["turrutebasen"] in told["credits"]
    parts = [{"coords": told["coords"][s["at"][0]:s["at"][1] + 1], "ferry": s["class"] == "ferry"} for s in told["steps"]]
    service = Heights()
    got = planner.heights(conn, parts, hours=3, http=service.client())
    assert service.asked >= 1 and got["walked_m"] == pytest.approx(14012, rel=0.01)
    assert got["up_m"] > 1500 and got["minutes"] > height.minutes(14012, 0) + 150
    # A day of three hours ends at the open shelter half way, which is a little over; the rest is the second day.
    assert [d["to"] for d in got["days"]] == ["Midtbu", None] and got["days"][-1]["last"]
    assert got["days"][0]["hut"]["kind"] == "wilderness_hut"
    assert sum(d["metres"] for d in got["days"]) == pytest.approx(got["walked_m"], abs=2)
    assert len(got["profile"]["z"]) == len(got["profile"]["at_m"]) > 100
    # The same with no path: half as slow again on the flat, and said.
    rough = planner.heights(conn, [{**p, "rough": True} for p in parts], hours=3, http=service.client())
    assert rough["rough_m"] == got["walked_m"] and got["rough_m"] == 0 and rough["up_m"] == got["up_m"]
    assert rough["minutes"] - got["minutes"] == pytest.approx(0.5 * got["walked_m"] / 1000 / height.KM_PER_HOUR * 60, abs=2)


def test_the_planner_is_a_feature_of_its_own(network, monkeypatch):
    service = Heights()
    monkeypatch.setattr(height, "client", service.client)
    monkeypatch.setattr(settings, "editor", "Test Editor")
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        ask = {"points": [network["a"], network["b"]], "prefer": 1}
        try:
            no = client.post(f"{V1}/planner/plan", json=ask)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `planner`")
            assert "plan_walk" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
            access.set_state(owner, "planner", "all", "test")
            told = client.post(f"{V1}/planner/plan", json=ask).json()
            assert told["steps"][0]["class"] == "dnt"
            parts = [{"coords": told["coords"][s["at"][0]:s["at"][1] + 1]} for s in told["steps"]]
            up = client.post(f"{V1}/planner/heights", json={"parts": parts, "hours": 7}).json()
            assert up["days"][0]["last"] and up["up_m"] >= 0
            said, refused = call(client, "plan_walk", points=ask["points"], prefer=1)
            assert not refused and "coords" not in said and said["steps"][0]["says"].startswith("Along DNT's")
            assert said["heights"]["minutes"] > 0 and planner.CREDITS["height"] in said["credits"]
            assert client.post(f"{V1}/planner/plan", json={"points": [network["a"]]}).status_code == 422
            assert client.post(f"{V1}/planner/plan", json={**ask, "prefer": 2}).status_code == 422
            nowhere = client.post(f"{V1}/planner/plan", json={"points": [network["a"], [10.0, 60.0]]}).json()
            assert nowhere["legs"][0]["unfound"] and nowhere["steps"][0]["class"] == "off"
            assert client.post(f"{V1}/planner/plan", json={**ask, "straight": [True]}).json()["steps"][0]["class"] == "off"
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'planner'")


def tile(z_of):
    """A made-up tile of the height model, as Pillow writes it: 2000 by 2000 heights, row 0 at the north."""
    import numpy as np
    from PIL import Image

    rows, cols = np.mgrid[0:terrain.SIDE, 0:terrain.SIDE]
    return Image.fromarray(z_of(rows, cols).astype("float32"), mode="F")


def test_the_height_model_is_read_between_its_four_nearest_heights(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(terrain, "TILES", ["6801"])
    terrain._kept.clear()
    assert terrain.code_of(150_000, 6_850_000) == "6801" and terrain.code_of(-50_000, 6_450_000) == "64m1"
    assert terrain.corner("64m1") == (-100_000, 6_400_000) and not terrain.ready()
    terrain.folder().mkdir(parents=True)
    tile(lambda r, c: 1000 + c - 2 * r).save(terrain.folder() / "6801.tif")
    assert terrain.ready()
    # The middle of the cell in row 10, column 20; half way to the next column; a point in no tile.
    x, y = 100_000 + 20 * 50 + 25, 6_900_000 - 10 * 50 - 25
    assert terrain.heights([(x, y), (x + 25, y), (x, y - 25), (5e5, 7.5e6)]) == [
        pytest.approx(1000), pytest.approx(1000.5), pytest.approx(999), None]
    terrain._kept.clear()


def test_the_height_model_is_fetched_tile_by_tile_and_kept(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(terrain, "TILES", ["6801", "64m1"])
    monkeypatch.setattr(terrain.time, "sleep", lambda s: None)
    body = io.BytesIO()
    tile(lambda r, c: r * 0 + 500).save(body, format="TIFF")
    asked = []

    def wcs(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["bbox"])
        if request.url.params["bbox"].startswith("-100000"):
            return httpx.Response(200, text="<ServiceExceptionReport/>", headers={"content-type": "application/vnd.ogc.se_xml"})
        return httpx.Response(200, content=body.getvalue(), headers={"content-type": "image/tiff"})

    client = HTTP_CLIENT(transport=httpx.MockTransport(wcs))
    with pytest.raises(terrain.Unavailable, match="did not give tile 64m1"):
        terrain.fetch(client=client)
    assert asked[0] == "100000,6800000,200000,6900000" and (terrain.folder() / "6801.tif").is_file()
    monkeypatch.setattr(terrain, "TILES", ["6801"])
    assert terrain.fetch(client=client)["fetched"] == 0              # what is here is not fetched again
    terrain._kept.clear()
    assert terrain.heights([(150_000, 6_850_000)]) == [pytest.approx(500)]
    terrain._kept.clear()


def test_the_heights_of_a_walk_are_read_from_the_height_model_when_it_is_here(network, conn, tmp_path, monkeypatch):
    code = terrain.code_of(*conn.execute("SELECT ST_X(p), ST_Y(p) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833) p",
                                         network["a"]).fetchone())
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(terrain, "TILES", [code])
    terrain._kept.clear()
    terrain.folder().mkdir(parents=True)
    tile(lambda r, c: 2000 - r).save(terrain.folder() / f"{code}.tif")        # rising 1 m for every 50 m north
    told = planner.plan(conn, [network["a"], network["b"]], planner.Options(prefer=1))
    parts = [{"coords": told["coords"][s["at"][0]:s["at"][1] + 1]} for s in told["steps"]]
    got = planner.heights(conn, parts)
    assert got["credits"] == [terrain.CREDIT] and got["every_m"] == planner.STEP_M
    north = conn.execute("SELECT max(ST_Y(p)) - min(ST_Y(p)) FROM unnest(%s::float8[], %s::float8[]) u(x, y), "
                         "LATERAL ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 4326), 25833) p",
                         ([c[0] for c in told["coords"]], [c[1] for c in told["coords"]])).fetchone()[0]
    assert got["highest_m"] - got["lowest_m"] == pytest.approx(north / 50, abs=1.5)
    terrain._kept.clear()


def test_a_point_by_a_path_that_joins_nothing_is_joined_to_the_network(network, conn, monkeypatch):
    """The nearest path to the point is on a piece of its own: the leg begins on the great piece instead."""
    pieces = dict(conn.execute("SELECT name, piece_nodes FROM walk.edge WHERE name IN ('Ensom sti', 'Langs ruta')").fetchall())
    assert pieces["Ensom sti"] == 2 and pieces["Langs ruta"] > 10
    monkeypatch.setattr(planner, "GREAT", 10)
    told = planner.plan(conn, [LONELY, network["b"]], planner.Options(prefer=1))
    assert told["legs"][0]["unfound"] is None and told["steps"][0]["class"] == "off" and told["off"][0]["off_m"] > 400
    assert "Ensom sti" not in [s["name"] for s in told["steps"]]
    # Further off than a path is looked for, but not a path that leads anywhere: straight to that.
    monkeypatch.setattr(planner, "SNAP_M", 100)
    wide = planner.plan(conn, [(LONELY[0] - 0.01, LONELY[1]), network["b"]], planner.Options(prefer=1))
    assert wide["legs"][0]["unfound"] is None and wide["steps"][0]["class"] == "off" and wide["off"][0]["off_m"] > 400
    monkeypatch.setattr(planner, "SNAP_M", 10_000)
    # Both ends on the lonely path: it is walked.
    on = planner.plan(conn, [LONELY, (LONELY[0] - 0.002, LONELY[1] + 0.001)])
    assert [s["name"] for s in on["steps"]] == ["Ensom sti"]
