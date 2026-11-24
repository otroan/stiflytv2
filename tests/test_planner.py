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

from stiflyt2 import access, height
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import planner, walknet

V1 = "/api/v1"
TRAIL = "ce4908aa-d039-413e-996d-96b4440a1dd0"        # bre20, 2.1 km, along which a path is mapped
BARE = "6ce2c484-2173-4fa7-8978-24f9e42946f0"         # bre18, 14 km, with nothing mapped along it
C, D, E, F = (7.45, 61.6668), (7.47, 61.68), (7.49, 61.69), (7.47, 61.70)


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
                ({"natural": "glacier", "name": "Testbreen"}, ice)]
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
                         "AND name <> 'Rundt breen' ORDER BY name").fetchall()
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
    with pytest.raises(NotFound, match="no way on foot was found"):
        planner.plan(conn, [a, E], planner.Options(ice=True, ferries=False))
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
    with pytest.raises(NotFound, match="no path or road within 3 km"):
        planner.plan(conn, [a, (10.0, 60.0)])


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
            assert client.post(f"{V1}/planner/plan", json={"points": [network["a"], [10.0, 60.0]]}).status_code == 404
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'planner'")
