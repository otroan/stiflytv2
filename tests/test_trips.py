"""Trips for work: every stretch that is asked for is walked, with little walked twice, in days that end at huts."""
import subprocess
import sys

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from test_mcp import call, rpc

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain import places, routes
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.domain.routes import _Paths
from stiflyt2.ops import trips

AUTHOR = "Test Editor"
V1 = "/api/v1"


def stretch(name, a, b, km, up=0.0, down=0.0, codes=("r1",)):
    """A stretch of a made-up network: 15 minutes a kilometre, and six for every 60 m climbed."""
    return trips.Stretch(name, a, b, km * 1000, km * 15 + up / 10, km * 15 + down / 10, up, down, tuple(codes),
                         ((0.0, 0.0), (0.0, 0.01)))


def walked(told):
    return [(d["from"]["place"], d["to"]["place"], round(d["metres"] / 1000, 1)) for d in told["days"]]


#   Hut A ---4--- x ---6--- Hut B ---8--- y ---5--- Hut C          and a spur  x ---3--- top
NET = {s.id: s for s in (
    stretch("ax", "A", "x", 4), stretch("xb", "x", "B", 6), stretch("by", "B", "y", 8, up=600, down=100),
    stretch("yc", "y", "C", 5), stretch("xt", "x", "top", 3, up=400, codes=("r2",)))}
PLACES = {"A": {"name": "Hut A", "kind": "hut"}, "B": {"name": "Hut B", "kind": "hut"}, "C": {"name": "Hut C", "kind": "hut"},
          "P": {"name": "The car park", "kind": "parking"}}


def test_a_line_of_huts_is_walked_once_and_a_spur_out_and_back():
    # The line alone: nothing is walked twice, from one end to the other, in the days the hours give.
    line = {e: {"r1"} for e in ("ax", "xb", "by", "yc")}
    told = trips.plan(NET, PLACES, line, hours=5)
    assert told["total"]["metres"] == 23_000 == told["total"]["new_m"] and told["total"]["twice_m"] == 0
    assert walked(told) in ([("Hut A", "Hut B", 10.0), ("Hut B", "Hut C", 13.0)], [("Hut C", "Hut B", 13.0), ("Hut B", "Hut A", 10.0)])
    assert told["covered"] == ["r1"] and told["left_out"] == [] and not any(d["long"] for d in told["days"])
    # Up one way is down the other: the day is as long as the way it is walked makes it.
    ab, bc = sorted(told["days"], key=lambda d: d["metres"])
    assert ab["minutes"] == 150 and bc["minutes"] in (13 * 15 + 60, 13 * 15 + 10) and {bc["up_m"], bc["down_m"]} == {600, 100}

    # With the spur: it has one end only, so it is walked out and back, and that is the only thing walked twice.
    both = {**line, "xt": {"r2"}}
    told = trips.plan(NET, PLACES, both, start="A", hours=7)
    assert told["days"][0]["from"]["place"] == "Hut A" and told["days"][-1]["to"]["place"] == "Hut C"
    assert (told["total"]["new_m"], told["total"]["twice_m"]) == (26_000, 3_000) and told["covered"] == ["r1", "r2"]
    assert told["days"][0]["routes"] == ["r1", "r2"] and [d["to"]["place"] for d in told["days"]] == ["Hut B", "Hut C"]

    # Shorter days: there is no hut between B and C, so that day is long, and is said to be.
    short = trips.plan(NET, PLACES, line, start="A", hours=3)
    assert [(d["to"]["place"], d["long"], d["no_hut"]) for d in short["days"]] == [("Hut B", False, False), ("Hut C", True, False)]
    # Far too far for a day, and no hut on the way: the day ends at a junction, and says there is no hut there.
    far = {**NET, "by": stretch("by", "B", "y", 30), "yc": stretch("yc", "y", "C", 30)}
    two = trips.plan(far, PLACES, {"by": {"r1"}, "yc": {"r1"}}, start="B", hours=8)
    assert [(d["to"]["place"], d["no_hut"], d["long"]) for d in two["days"]] == [(None, True, False), ("Hut C", False, False)]
    # With a tent a day may end anywhere: no day is long, and there are more of them.
    tent = trips.plan(NET, PLACES, line, start="A", hours=3, tent=True)
    assert len(tent["days"]) == 3 and not any(d["long"] for d in tent["days"]) and tent["days"][-1]["from"]["place"] is None
    # And all of it in one day, where the hours are there for it.
    assert walked(trips.plan(NET, PLACES, line, start="A", hours=7)) == [("Hut A", "Hut C", 23.0)]
    # Back to where it began: all of it is walked twice on a line.
    assert trips.plan(NET, PLACES, line, start="A", back_to_start=True)["total"]["twice_m"] == 23_000


def test_each_day_says_how_it_is_walked_and_where_it_goes_back_the_same_way():
    """The spur up to the top has one end only: it is walked out and back, and the instructions say so."""
    both = {**{e: {"r1"} for e in ("ax", "xb", "by", "yc")}, "xt": {"r2"}}
    told = trips.plan(NET, PLACES, both, start="A", hours=7)
    first = [s["says"] for s in told["days"][0]["steps"]]
    assert first == ["Along r1 to the junction with r2", "Along r2 to the end of r2",
                     "Back the same way along r2 to the junction with r1", "Along r1 to Hut B"]
    assert [s["says"] for s in told["days"][1]["steps"]] == ["Along r1 to Hut C"]
    up, back = told["days"][0]["steps"][1:3]
    assert (up["metres"], up["again"], back["metres"], back["again"], back["back"]) == (3000, False, 3000, True, True)
    assert told["days"][0]["back_m"] == told["total"]["back_m"] == 3000 and told["total"]["off_route_m"] == 0
    # Each instruction says which part of the day's line it is, and the parts follow one another.
    day = told["days"][0]
    assert day["steps"][0]["at"][0] == 0 and day["steps"][-1]["at"][1] == len(day["coords"]) - 1
    assert all(a["at"][1] == b["at"][0] for a, b in zip(day["steps"], day["steps"][1:], strict=False))


def test_between_routes_it_may_go_along_a_road_and_says_so():
    """Two routes that no route joins, with a road between their ends: the road is walked from the one to the other."""
    road = _Paths([(("vegnett", "Fjellvegen"), [(0.0, 0.0), (4000.0, 0.0)], [])])
    made = trips._links(road, [("A", (0.0, 30.0)), ("B", (4000.0, -20.0)), ("Z", (2000.0, 900.0))])
    assert {(s.kind, s.a, s.b, round(s.metres)) for s in made.values() if s.kind == "hop"} == {
        ("hop", "A", "t:A", 30), ("hop", "B", "t:B", 20)}                         # Z lies too far off to be joined
    assert [round(s.metres) for s in made.values() if s.kind == "road" and s.metres > 1] == [4000]
    # (Lines are given here in metres; the planner is told of them in lon, lat. It needs no more than lengths.)
    net = {s.id: s for s in (stretch("pa", "P", "A", 2), stretch("bq", "B", "Q", 2, codes=("r2",)))} | made
    near = {"P": {"name": "Hut P", "kind": "hut"}, "Q": {"name": "Hut Q", "kind": "hut"}}
    told = trips.plan(net, near, {"pa": {"r1"}, "bq": {"r2"}}, start="P", hours=10)
    assert told["left_out"] == [] and told["covered"] == ["r1", "r2"]
    assert [s["says"] for s in told["days"][0]["steps"]] == [
        "Along r1 to the end of r1", "Along Fjellvegen to the end of r2", "Along r2 to Hut Q"]
    assert told["total"]["off_route_m"] == 4030 and told["total"]["twice_m"] == 4050     # (the hop back onto r2 goes with r2)
    assert told["credits"] == ["Roads: NVDB Vegnett Pluss, Kartverket, CC BY 4.0"]
    # The same along a path from OpenStreetMap: named so, and credited.
    osm = trips._links(_Paths([(("osm", "w1"), [(0.0, 0.0), (4000.0, 0.0)], [])]), [("A", (0.0, 30.0)), ("B", (4000.0, -20.0))])
    told = trips.plan({**{k: v for k, v in net.items() if k in ("pa", "bq")}, **osm}, near, {"pa": {"r1"}, "bq": {"r2"}}, start="P", hours=10)
    assert told["days"][0]["steps"][1]["says"] == "Along an OpenStreetMap path to the end of r2"
    assert told["credits"] == ["Paths: © OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)"]
    # Without the road, the second route cannot be reached from the first, and is left out.
    alone = trips.plan({k: v for k, v in net.items() if k in ("pa", "bq")}, near, {"pa": {"r1"}, "bq": {"r2"}}, start="P")
    assert alone["left_out"] == ["r2"]
    # A mapped path that lies along a route is that route, mapped: it counts for so much more that the route is walked.
    along = _Paths([(("route", "pa"), [(0.0, -2000.0), (0.0, 0.0)], [])])
    twin = _Paths([(("fkb_wfs", "p1"), [(5.0, -2000.0), (5.0, 0.0)], []), (("vegnett", "Fjellvegen"), [(0.0, 0.0), (4000.0, 0.0)], [])])
    kept = trips._links(twin, [("A", (0.0, 30.0)), ("P", (0.0, -2000.0)), ("B", (4000.0, -20.0))], along)
    assert {(x.kind, x.twin) for x in kept.values() if x.metres > 100} == {("path", True), ("road", False)}
    side = trips.plan({**{k: v for k, v in net.items() if k in ("pa", "bq")}, **kept}, near, {"bq": {"r2"}}, start="P")
    assert "Along r1" in side["days"][0]["steps"][0]["says"]


def test_lines_that_meet_within_a_few_metres_are_joined():
    """A path that ends 6 m from a road, on its middle, and one that ends 6 m from the road's end."""
    road = (("vegnett", "r"), [(0.0, 0.0), (1000.0, 0.0)])
    side = (("osm", "a"), [(500.0, 6.0), (500.0, 400.0)])
    on = (("fkb_wfs", "b"), [(1006.0, 0.0), (1400.0, 0.0)])
    far = (("osm", "c"), [(0.0, 50.0), (0.0, 400.0)])
    joined = trips._joined([road, side, on, far], 10)
    cuts = {ref: sorted((round(f, 3), x, y) for f, x, y in c) for ref, _, c in joined}
    assert cuts[("vegnett", "r")] == [(0.5, 500.0, 6.0), (1.0, 1006.0, 0.0)] and cuts[("osm", "c")] == []
    net = _Paths(joined)
    a, b = net.attach((500.0, 390.0), 20), net.attach((1390.0, 0.0), 20)
    assert net.parts(a, b) is not None and net.parts(a, net.attach((0.0, 390.0), 20)) is None


def test_routes_apart_are_joined_by_the_way_between_them_and_what_cannot_be_reached_is_left_out():
    # Only the two ends are asked for: the way between them is walked to get from the one to the other, once.
    ends = {"ax": {"r1"}, "yc": {"r3"}}
    told = trips.plan(NET, PLACES, ends, hours=10)
    assert told["total"]["new_m"] == 9_000 and told["total"]["twice_m"] == 14_000 and told["covered"] == ["r1", "r3"]
    # A stretch on an island: left out, and said so; the rest is planned.
    island = {**NET, "far": stretch("far", "q", "w", 2, codes=("r9",))}
    told = trips.plan(island, PLACES, {**ends, "far": {"r9"}}, hours=10)
    assert told["left_out"] == ["r9"] and told["covered"] == ["r1", "r3"]
    # Begun at a hut that none of it can be reached from: said so, with where it would begin if left to it.
    apart = {**island, "qh": stretch("qh", "q", "H", 1, codes=("r9",))}
    with pytest.raises(Invalid, match="no way on foot leads from Hut H to r1, r3: begin at Hut .*, or leave where it begins empty"):
        trips.plan(apart, {**PLACES, "H": {"name": "Hut H", "kind": "hut"}}, ends, start="H", hours=10)
    with pytest.raises(Invalid, match="none of that can be walked"):
        trips.plan(NET, PLACES, {"not there": {"r1"}})
    with pytest.raises(Invalid, match="no stretch that can be walked leads to where the trip is to begin"):
        trips.plan(NET, PLACES, ends, start="nowhere")
    # Not at a hut at either end: the trip begins and ends where one can park or sleep, and says what that adds.
    car = {**NET, "pa": stretch("pa", "P", "x", 1)}
    middle = trips.plan(car, PLACES, {"xt": {"r2"}}, hours=10)
    assert middle["days"][0]["from"]["place"] == "The car park" and middle["days"][-1]["to"]["place"] == "The car park"
    assert (middle["total"]["new_m"], middle["total"]["twice_m"]) == (3_000, 5_000)


def test_a_loop_and_a_crossing_are_each_walked_once():
    #    A --- B         a square with one diagonal: the two ends of the diagonal have three ways each
    #    |  \\  |
    #    D --- C
    square = {s.id: s for s in (stretch("ab", "A", "B", 2), stretch("bc", "B", "C", 2), stretch("cd", "C", "D", 2),
                                stretch("da", "D", "A", 2), stretch("ac", "A", "C", 3))}
    told = trips.plan(square, PLACES, {e: {"r1"} for e in square}, hours=12)
    assert (told["total"]["new_m"], told["total"]["twice_m"]) == (11_000, 0)
    assert {told["days"][0]["from"]["place"], told["days"][-1]["to"]["place"]} == {"Hut A", "Hut C"}
    # Every stretch is there, once, and each begins where the one before ended.
    line = [c for d in told["days"] for c in d["coords"]]
    assert len(line) == 5 + 1 and told["days"][0]["routes"] == ["r1"]


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_a_route_of_yours_is_covered_from_the_hut_at_one_end_to_the_hut_at_the_other(conn):
    a = routes.make(conn, candidate(conn, "bre18"), AUTHOR)             # Arentzbu – Sprongdalshytta
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    long = routes.detail(conn, a)["length"]["on_foot_m"]

    told = trips.for_routes(conn, routes=["BRE18"], hours=10)
    assert told["asked"] == told["covered"] == ["bre18"] and told["left_out"] == [] and told["not_on_foot"] == []
    assert told["days"][0]["steps"][0]["says"].startswith("Along bre18 to ")
    # The same, kept to the routes: nothing off them is needed here, so it is the same trip.
    kept = trips.for_routes(conn, routes=["bre18"], hours=10, mapped=False)
    assert kept["total"]["metres"] == told["total"]["metres"] and kept["notes"] == []
    assert told["total"]["new_m"] == pytest.approx(long, abs=5) and told["total"]["twice_m"] == 0
    assert [(d["routes"], d["long"]) for d in told["days"]] == [(["bre18"], False)]
    assert {told["days"][0]["from"]["place"], told["days"][0]["to"]["place"]} == {"Arentzbu", "Sprongdalshytta"}
    # The day is a line on the map that hangs together: as long as the route, give or take the corners.
    line = told["days"][0]["coords"]
    assert len(line) > 20 and all(len(c) == 2 for c in line)
    assert conn.execute("SELECT ST_Length(ST_GeomFromGeoJSON(%s)::geography)",
                        (psycopg.types.json.Json({"type": "LineString", "coordinates": line}),)).fetchone()[0] == pytest.approx(long, rel=0.01)

    # It begins where it is said to, by the name of the place.
    assert trips.for_routes(conn, routes=["bre18"], start="sprongdalshytta")["days"][0]["from"]["place"] == "Sprongdalshytta"
    # A whole area: every route in use of it.
    assert "bre18" in trips.for_routes(conn, area="bre")["asked"]
    # A route that is given up is not walked.
    routes.discontinue(conn, a, routes.detail(conn, a)["version"], AUTHOR, "the bridge is gone")
    with pytest.raises(NotFound, match="no route of yours in use is called bre18"):
        trips.for_routes(conn, routes=["bre18"])
    for wrong, why in (({"routes": ["nowhere9"]}, NotFound), ({}, Invalid), ({"area": "xyz"}, NotFound)):
        with pytest.raises(why):
            trips.for_routes(conn, **wrong)
    with pytest.raises(Invalid, match="between 3 and 14 hours"):
        trips.for_routes(conn, area="bre", hours=30)


def test_the_planner_is_a_feature_of_its_own_and_is_refused_and_hidden_without_it(loaded, monkeypatch):
    """Nobody has it until it is given: not the route responsible either."""
    monkeypatch.setattr(settings, "editor", AUTHOR)
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        start = (client.get(f"{V1}/undo").json() or {"txid": 0})["txid"]
        try:
            ask = {"routes": ["bre18"]}
            assert client.get(f"{V1}/auth/me").json()["features"] == ["routes", "signs"]
            no = client.post(f"{V1}/trips/plan", json=ask)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `trips`")
            assert "plan_trip" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
            assert call(client, "plan_trip", routes=["bre18"]) == ("that is not switched on for you: ask to be given the feature `trips`", True)

            # Switched on for all who are let in: it is there, in the page's list of what one has and for an assistant.
            access.set_state(owner, "trips", "all", "test")
            assert client.get(f"{V1}/auth/me").json()["features"] == ["routes", "signs", "trips"]
            assert "plan_trip" in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
            found = client.get(f"{V1}/routes", params={"q": "bre18"}).json()[0]
            assert client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "bre18"}).status_code == 201
            told = client.post(f"{V1}/trips/plan", json=ask)
            assert told.status_code == 200 and told.json()["covered"] == ["bre18"] and len(told.json()["days"][0]["coords"]) > 20
            said, refused = call(client, "plan_trip", routes=["bre18"], hours=10)
            assert not refused and said["covered"] == ["bre18"] and "coords" not in said["days"][0] and len(said["days"][0]["ends_at"]) == 2
            assert client.post(f"{V1}/trips/plan", json={"routes": ["nowhere9"]}).status_code == 404
            assert client.post(f"{V1}/trips/plan", json={}).status_code == 422
            assert client.post(f"{V1}/trips/plan", json={"area": "bre", "hours": 40}).status_code == 422
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'trips'")
            while (latest := client.get(f"{V1}/undo").json()) and latest["txid"] > start:
                assert client.post(f"{V1}/undo", json={"txid": latest["txid"]}).status_code == 200


def test_the_command_line_knows_the_feature_though_it_loads_no_part_of_the_service():
    """Asked which features there are, the modules that are features are loaded: one can be given from the command line."""
    said = subprocess.run([sys.executable, "-c", "from stiflyt2 import features; print(sorted(features.load()))"],
                          capture_output=True, text=True, check=True).stdout
    assert said.strip() == "['issues', 'presence', 'routes', 'signs', 'trips']"
