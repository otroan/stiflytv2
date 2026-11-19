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
    with pytest.raises(Invalid, match="no way over your routes leads on foot from Hut H to r1, r3: begin at Hut .*, or leave where it begins empty"):
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
    assert said.strip() == "['routes', 'signs', 'trips']"
