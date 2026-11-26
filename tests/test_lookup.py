"""Look-ups: what carries a number, what lies at a point, the mapped way, shared lines and the odd things."""
import uuid

import psycopg
import pytest
from conftest import role_dsn
from fastapi.testclient import TestClient

from stiflyt2.api.app import create_app
from stiflyt2.domain import lookup, routes

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def lonlat(conn, x, y):
    return tuple(conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                              (x, y)).fetchone())


def ends_of(conn, route):
    return [lonlat(conn, *conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s",
                                       (e["id"],)).fetchone()) for e in routes.detail(conn, route)["ends"]]


def test_everything_that_carries_a_route_number_is_told_segment_by_segment(conn):
    found = lookup.turrutebasen(conn, " BRE18 ")
    (only,) = found["candidates"]
    assert (only["label"], only["shape"], only["yours"]) == ("bre18", "line", None)
    assert len(found["segments"]) == only["segments"] and abs(found["length_m"] - only["length_m"]) <= len(found["segments"])
    for s in found["segments"]:
        assert "bre18" in s["codes"] and s["candidate"] == "bre18" and s["yours"] == []
        assert 4 < s["lon"] < 9 and 60 < s["lat"] < 63 and isinstance(s["marked"], bool)
    assert {s["follows"] for s in found["segments"]} <= {*lookup.FOLLOWS.values(), None}

    route = routes.make(conn, only["id"], AUTHOR)
    made = lookup.turrutebasen(conn, "bre18")
    assert made["candidates"][0]["yours"] == {"id": route, "code": "bre18"}
    assert made["candidates"][0]["set_aside"] is False      # yours: not something a person has put aside
    assert all({"id": route, "code": "bre18"} in s["yours"] for s in made["segments"])
    with pytest.raises(lookup.NotFound, match="nothing in the loaded Turrutebasen carries the number bre999"):
        lookup.turrutebasen(conn, "bre999")


def test_what_lies_around_a_point_is_told_nearest_first(conn):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    segment, x, y = conn.execute("""
        SELECT t.id, ST_X(m.p), ST_Y(m.p) FROM source_index.route r
        JOIN source_index.route_segment rs ON rs.route_id = r.id
        JOIN source_index.trail_segment t ON t.id = rs.segment_id,
             LATERAL (SELECT ST_LineInterpolatePoint(t.geom, 0.5) AS p) m
        WHERE r.label = 'bre18' ORDER BY t.length_m DESC LIMIT 1""").fetchone()
    found = lookup.here(conn, *lonlat(conn, x, y), 100)
    assert found["turrutebasen"][0]["id"] == segment and found["turrutebasen"][0]["metres"] == 0
    stretch = found["stretches"][0]
    assert stretch["metres"] == 0 and stretch["routes"] == [{"id": route, "code": "bre18"}]
    for kind in ("names", "mapped", "turrutebasen", "stretches", "junctions", "places"):
        metres = [f["metres"] for f in found[kind]]
        assert metres == sorted(metres) and all(m <= 100 for m in metres) and len(metres) <= lookup.HERE_MOST

    # At an end of the route: its junction, and how many stretches meet in it.
    end = routes.detail(conn, route)["ends"][0]
    at = lookup.here(conn, *ends_of(conn, route)[0], 20)
    assert (at["junctions"][0]["id"], at["junctions"][0]["metres"]) == (end["id"], 0)
    assert at["junctions"][0]["stretches"] >= 1
    with pytest.raises(lookup.Invalid, match="look between 1 and 2000 m"):
        lookup.here(conn, 7.0, 61.5, 5000)


def test_the_mapped_way_between_points_is_found_without_making_anything(conn):
    metres, ax, ay, bx, by = conn.execute("""
        SELECT length_m, ST_X(ST_StartPoint(geom)), ST_Y(ST_StartPoint(geom)),
               ST_X(ST_EndPoint(geom)), ST_Y(ST_EndPoint(geom))
        FROM source_index.path WHERE GeometryType(geom) = 'LINESTRING' ORDER BY length_m DESC LIMIT 1""").fetchone()
    a, b = lonlat(conn, ax, ay), lonlat(conn, bx, by)
    events = conn.execute("SELECT count(*) FROM history.event").fetchone()[0]
    way = lookup.mapped_way(conn, [a, b])
    assert 0 < way["length_m"] <= metres + 1       # the path itself, unless something shorter joins its ends
    assert abs(way["on_paths_m"] + way["on_roads_m"] - way["length_m"]) <= 2
    assert way["legs"] == [{"from": 1, "to": 2, "length_m": way["legs"][0]["length_m"]}]
    assert way["geometry"]["type"] == "LineString" and "route" not in way
    assert conn.execute("SELECT count(*) FROM history.event").fetchone()[0] == events

    # Held against a route of yours: how much of the way that route already follows.
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    against = lookup.mapped_way(conn, [a, b, a], route)
    assert against["route"]["code"] == "bre18" and 0 <= against["route"]["share"] <= 1
    assert len(against["legs"]) == 2 and abs(against["length_m"] - 2 * way["length_m"]) <= 2

    nowhere = lonlat(conn, ax + 30_000, ay + 30_000)
    with pytest.raises(lookup.Invalid, match="no mapped path or road lies within 150 m of point 2"):
        lookup.mapped_way(conn, [a, nowhere])
    with pytest.raises(lookup.NotFound):
        lookup.mapped_way(conn, [a, b], str(uuid.uuid4()))
    with pytest.raises(lookup.Invalid, match="at least two points"):
        lookup.mapped_way(conn, [a])


def test_a_line_that_goes_out_and_the_same_way_back_is_found():
    out = [(0.0, 0.0), (300.0, 0.0), (300.0, 200.0)]
    spike = [*out, (302.0, 0.0), (600.0, 0.0)]                 # up a side path for 200 m and down again beside it
    ((at, metres),) = lookup._doubles_back(spike)
    assert abs(at[0] - 300) <= 12 and abs(at[1] - 200) <= 12 and 150 <= metres <= 210
    assert lookup._doubles_back(out) == []                     # a corner is not a way back
    hairpin = [(0.0, 0.0), (300.0, 0.0), (40.0, 150.0)]        # a turn up a slope: the legs draw apart
    assert lookup._doubles_back(hairpin) == []
    # A tight hairpin, as by Mansberg bru on jot60: back beside itself for 80 m, 16 m away. Not the same way.
    assert lookup._doubles_back([(140.0, 4.0), (0.0, 4.0), (0.0, -12.0), (80.0, -13.0), (120.0, -70.0)]) == []
    assert lookup._doubles_back([(0.0, 0.0), (1000.0, 0.0)]) == []


def test_routes_on_one_line_and_the_other_odd_things_of_an_area(conn, monkeypatch):
    before = lookup.checks(conn, "bre")
    assert set(before["counts"]) == {"same line", "short", "code", "twin", "maintainer", "out and back", "glacier",
                                     "gap at a junction", "place name"}
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    assert lookup.same_line(conn, "bre") == [p for p in lookup.same_line(conn, "bre") if p["of"] == "turrutebasen"]

    # A second route laid between the same ends, with a number that is the first one's misspelt.
    twin = routes.lay_out(conn, AUTHOR, ends_of(conn, route), "bre018")
    both = lookup.shared(conn, route, twin)
    assert both["same_line"] and both["a"]["alone_m"] == both["b"]["alone_m"] == 0
    assert both["shared_m"] == both["a"]["length_m"] and both["shared_stretches"] >= 1
    version = routes.detail(conn, twin)["version"]
    routes.update(conn, twin, version, AUTHOR, maintainers=["Test turlag"])
    found = lookup.checks(conn, "BRE")
    said = {f["kind"]: f for f in found["checks"] if any(r["id"] == twin for r in f["routes"])}
    assert set(said) == {"same line", "code", "maintainer"}
    assert said["same line"]["text"].startswith("bre18 and bre018 are the same line for ")
    assert [r["code"] for r in said["same line"]["routes"]] == ["bre18", "bre018"]
    assert said["code"]["text"] == "bre018 looks like a misspelling of bre18, which is a route too"
    assert said["maintainer"]["text"] == "bre018 is said to be maintained by Test turlag"
    assert 4 < said["code"]["lon"] < 9 and 60 < said["code"]["lat"] < 63
    assert found["counts"]["same line"] == before["counts"]["same line"] + 1

    # A route along a part of another is what routes do: not the same line.
    other = routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    assert not lookup.shared(conn, route, other)["same_line"]
    with pytest.raises(lookup.NotFound):
        lookup.shared(conn, route, str(uuid.uuid4()))

    # Short: yours, and candidates that wait. Not those a person has set aside.
    monkeypatch.setattr(lookup, "SHORT_M", 10**7)
    short = [f for f in lookup.checks(conn, "bre")["checks"] if f["kind"] == "short"]
    assert {"bre18", "bre018", "bre19"} <= {f["routes"][0]["code"] for f in short if f["routes"][0]["yours"]}
    waiting = {f["routes"][0]["code"] for f in short if not f["routes"][0]["yours"]}
    assert waiting and not waiting & {"bre18", "bre19"}
    assert all("in Turrutebasen" in f["text"] for f in short if not f["routes"][0]["yours"])


def test_the_look_ups_are_answered_over_http(loaded):
    with TestClient(create_app()) as client:
        told = client.get("/api/v1/turrutebasen/bre18").json()
        assert told["code"] == "bre18" and told["segments"]
        assert client.get("/api/v1/turrutebasen/bre999").status_code == 404
        s = told["segments"][0]
        here = client.get("/api/v1/here", params={"lon": s["lon"], "lat": s["lat"], "radius_m": 50})
        assert here.status_code == 200 and here.json()["turrutebasen"][0]["id"] == s["id"]
        assert client.get("/api/v1/here", params={"lon": s["lon"], "lat": s["lat"], "radius_m": 0}).status_code == 422
        far = client.post("/api/v1/mapped-way", json={"points": [[s["lon"], s["lat"]], [s["lon"] + 1, s["lat"] + 1]]})
        assert far.status_code == 422 and "no mapped path or road lies within" in far.json()["detail"]
        assert client.get("/api/v1/areas/bre/checks").json()["area"] == "bre"
        missing = client.get("/api/v1/same-line", params={"a": str(uuid.uuid4()), "b": str(uuid.uuid4())})
        assert missing.status_code == 404


def test_a_hole_at_a_junction_and_two_places_with_one_name_are_found(conn):
    from stiflyt2.domain import places

    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    made = routes.detail(conn, route)
    assert [f for f in lookup.checks(conn, "bre")["checks"] if f["kind"] in ("gap at a junction", "place name")] == []

    # A stretch whose line stops three metres short of the junction it hangs in.
    end, other = made["ends"]
    far = routes._node_at(conn, conn.execute(
        "SELECT ST_Translate(geom, 0, 300) FROM core.node WHERE id = %s", (end["id"],)).fetchone()[0])
    short = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ST_Translate(a.geom, 0, 3), b.geom) FROM core.node a,
                                                core.node b WHERE a.id = %(a)s AND b.id = %(b)s)""",
                             {"a": end["id"], "b": far}, end["id"], far, "trail", "drawn", True, True, season="summer")
    routes._add_legs(conn, route, [short])
    hole, = [f for f in lookup.checks(conn, "bre")["checks"] if f["kind"] == "gap at a junction"]
    assert hole["text"] == "A stretch of bre18 stops 3.0 m short of the junction it hangs in: the line has a hole there"
    assert hole["routes"][0]["code"] == "bre18" and abs(hole["lat"] - end["lat"]) < 0.001

    # The same name given to two places, by hand.
    places.at_node(conn, far, AUTHOR, "Dobbeltbu")
    places.at_node(conn, other["id"], AUTHOR, "Dobbeltbu")
    twice, = [f for f in lookup.checks(conn, "bre")["checks"] if f["kind"] == "place name"]
    assert twice["text"].startswith("Two places are called Dobbeltbu, ") and "m apart" in twice["text"]
