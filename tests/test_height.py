"""How high the ground is along a route: asked of the height service once per line, and worked out per route."""
import json

import httpx
import psycopg
import pytest
from conftest import HTTP_CLIENT, role_dsn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stiflyt2 import height
from stiflyt2.api.app import create_app
from stiflyt2.cli import app as cli
from stiflyt2.domain import export, routes

AUTHOR = "Test Editor"
V1 = "/api/v1"


class Terrain:
    """Kartverket's height service over a tilted plane, with ice north of a line."""

    def __init__(self, ice_north_of: float = 1e9, river: tuple[float, float] = (0, 0)) -> None:
        self.questions, self.down, self.ice_north_of, self.river = 0, False, ice_north_of, river

    @staticmethod
    def z(x: float, y: float) -> float:
        return round(1000 + 0.1 * (x - 80000) + 0.04 * (y - 6850000), 2)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.questions += 1
        if self.down:
            return httpx.Response(503, text="no")
        asked = json.loads(request.url.params["punkter"])
        assert len(asked) <= 50 and request.url.params["koordsys"] == "25833"
        return httpx.Response(200, json={"koordsys": 25833, "punkter": [
            {"x": x, "y": y, "z": self.z(x, y), "datakilde": "dtm1",
             "terreng": "Elv" if self.river[0] <= y <= self.river[1] else "SnøIsbre" if y > self.ice_north_of
             else "ÅpentOmråde"} for x, y in asked]})

    def client(self, timeout_s: float = 30.0) -> httpx.Client:
        return HTTP_CLIENT(transport=httpx.MockTransport(self))


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_a_rise_counts_from_where_it_turns_and_only_once_it_is_something():
    assert height.climb([100, 110, 120, 115, 130]) == (35, 5)
    # A line wandering a metre up and down a slope climbs nothing; a steady rise with such steps climbs its rise.
    assert height.climb([100, 101, 100, 101, 100, 101, 100]) == (0, 0)
    up, down = height.climb([100 + i + (i % 2) for i in range(41)])
    assert (up, down) == (40, 0)
    # Gained less lost is the last height less the first, whatever is left over at the end.
    for z in ([500, 501.5], [500, 520, 519], [500, 480, 481, 470.5, 471], [7, 7, 7]):
        up, down = height.climb(z)
        assert up - down == pytest.approx(z[-1] - z[0]) and up >= 0 and down >= 0
    assert height.climb([]) == (0, 0) == height.climb([812.0])


def test_time_is_naismiths_rule_and_length_and_climb_give_a_grading_at_least():
    assert height.minutes(4828, 0) == 60 and height.minutes(4828, 600) == 120 and height.minutes(0, 0) == 0
    # The first grade whose limits it stays under, for length and for climb; black has none.
    assert [height.at_least(m, up) for m, up in ((4_900, 290), (4_900, 300), (9_900, 100), (10_000, 100),
                                                 (3_000, 999), (3_000, 1_000), (25_000, 0))] == list("GBBRRSS")
    assert height.one_way(9_656, 300, 20) == {"up_m": 300, "down_m": 20, "steep_down_m": 0, "minutes": 150, "at_least": "B"}
    # Going down steeply takes ten minutes more for every 300 m lost, and counts half as climb towards the grading.
    assert height.minutes(4828, 0, 600) == 80 and height.at_least(3_000, 100, 380) == "G" and height.at_least(3_000, 100, 400) == "B"
    assert height.one_way(3_000, 500, 900, 300)["at_least"] == "R" and height.one_way(3_000, 500, 900, 100)["at_least"] == "B"
    assert height.harder("B", "S") == "S" and height.harder("G", "G") == "G" and height.harder("R", None) is None


def test_what_is_gained_and_lost_on_steep_ground_is_told_apart():
    # 300 m level, 300 m up at 30 %, 300 m down at 10 %, 300 m down at 40 %: only the two steep parts count.
    points, z = [], 1000.0
    for at in range(0, 1201, 10):
        points.append((float(at), z))
        z += 0 if at < 300 else 3 if at < 600 else -1 if at < 900 else -4
    up, down = height.steep(points)
    assert up == pytest.approx(90, abs=16) and down == pytest.approx(120, abs=21)
    # A line stepping two metres up and down every ten metres is not steep ground.
    assert height.steep([(float(at), 500.0 + 2 * (at // 10 % 2)) for at in range(0, 1000, 10)]) == (0, 0)
    assert height.steep([]) == (0, 0) == height.steep([(0.0, 10.0)])


def test_a_route_gets_its_heights_once_per_line_and_says_what_it_climbs(conn, monkeypatch):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    north = conn.execute("""SELECT ST_Y(ST_Centroid(ST_Collect(r.geom))) FROM core.route_leg l
                            JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
                            WHERE l.route_id = %s""", (route["id"],)).fetchone()[0]
    ground = Terrain(ice_north_of=north)
    todo = height.lacking(conn, route_id=route["id"], again=True)
    assert len(todo) == route["length"]["legs"] and all(len(t["points"]) >= 2 for t in todo)
    # No two points asked about are further apart than the step, and the corners of the line are among them.
    for t in todo:
        assert max(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                   for (ax, ay), (bx, by) in zip(t["points"], t["points"][1:], strict=False)) <= height.STEP_M + 0.02

    with ground.client() as http:
        went = height.fill(todo, lambda got: height.keep(conn, got), http=http)
    assert went == {"lines": len(todo), "points": sum(len(t["points"]) for t in todo), "left": 0, "stopped": None}
    assert ground.questions >= len(todo) and height.lacking(conn, route_id=route["id"]) == []

    high = height.of_route(conn, route["id"], samples=True)
    assert high["known_m"] == high["on_foot_m"] == route["length"]["on_foot_m"] and high["source"] == "dtm1"
    assert [high["from_node"], high["to_node"]] == [e["id"] for e in route["ends"]]
    first, last = high["samples"][0], high["samples"][-1]
    assert first[0] == 0 and last[0] == pytest.approx(high["on_foot_m"], abs=2)
    assert [s[0] for s in high["samples"]] == sorted(s[0] for s in high["samples"])       # walked one way, end to end
    assert (first[3], first[4]) == pytest.approx((route["ends"][0]["lon"], route["ends"][0]["lat"]), abs=2e-4)
    assert (last[3], last[4]) == pytest.approx((route["ends"][1]["lon"], route["ends"][1]["lat"]), abs=2e-4)
    assert high["up_m"] - high["down_m"] == pytest.approx(last[1] - first[1], abs=2)
    assert high["low_m"] <= min(first[1], last[1]) and high["high_m"] >= max(first[1], last[1])
    assert high["up_m"] > 0 and high["down_m"] > 0                                         # it is not a straight line
    assert 0 < high["ice_m"] < high["on_foot_m"] and {s[2] for s in high["samples"]} == {0, 1}
    assert {e["place"] for e in route["ends"] if e["place"]} <= {p["name"] for p in high["places"]}
    # Each way: what is climbed there is what is lost back, and the time follows the climb.
    there, back = high["there"], high["back"]
    assert (there["up_m"], there["down_m"]) == (high["up_m"], high["down_m"]) == (back["down_m"], back["up_m"])
    assert there["steep_down_m"] == 0 == back["steep_down_m"]              # the test's ground slopes gently
    assert there["minutes"] == height.minutes(high["on_foot_m"], high["up_m"])
    assert back["minutes"] - there["minutes"] == pytest.approx((high["down_m"] - high["up_m"]) * height.MIN_PER_M_UP, abs=1)
    assert there["at_least"] in "RS" and back["at_least"] in "RS"           # 15.5 km is red by its length alone

    # The route says it of itself, and it is handed on with the route.
    said = routes.detail(conn, route["id"])
    told = said["height"]
    assert told == {k: v for k, v in high.items() if k not in ("samples", "places")}
    # Nobody has set a grading: the one its numbers give stands in, the harder of the two ways, and says so.
    assert said["grading"] is None and said["grading_by_numbers"] == height.harder(there["at_least"], back["at_least"])
    assert routes.grading_of(said) == (said["grading_by_numbers"], "length and climb")
    assert routes.grading_of({**said, "grading": "I"}) == (said["grading_by_numbers"], "length and climb")
    assert routes.grading_of({**said, "grading": "B"}) == ("B", "set")
    assert routes.grading_of({**said, "grading": "I", "grading_by_numbers": None}) == ("I", "set")
    assert routes.grading_of({**said, "grading_by_numbers": None}) == (None, None)
    handed = next(f for f in export.routes_geojson(conn, "bre")["features"] if f["id"] == str(route["id"]))["properties"]
    assert (handed["ascent_m"], handed["descent_m"], handed["lowest_m"], handed["highest_m"]) == (
        high["up_m"], high["down_m"], high["low_m"], high["high_m"])
    assert (handed["minutes"], handed["minutes_back"], handed["grading_at_least"]) == (
        there["minutes"], back["minutes"], there["at_least"])
    assert (handed["grading"], handed["grading_from"]) == (said["grading_by_numbers"], "length and climb")

    # Saying what a stretch is does not move its line: the heights stay. Moving the line asks again for that one.
    edge, lon, lat = conn.execute("""
        SELECT e.id::text, ST_X(m.p), ST_Y(m.p) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 40, 0), 4326) AS p) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route["id"],)).fetchone()
    routes.set_stretch(conn, edge, AUTHOR, marked=False)
    assert height.lacking(conn, route_id=route["id"]) == []
    routes.redraw(conn, edge, AUTHOR, [(lon, lat)])
    assert len(height.lacking(conn, route_id=route["id"])) == 1
    part = height.of_route(conn, route["id"], samples=True)
    assert 0 < part["known_m"] < part["on_foot_m"] and any(s[1] is None for s in part["samples"])
    assert part["there"] is None and part["back"] is None                  # no time for a route known by halves
    handed = next(f for f in export.routes_geojson(conn, "bre")["features"] if f["id"] == str(route["id"]))["properties"]
    assert handed["ascent_m"] is None and handed["highest_m"] is None and handed["minutes"] is None   # nor handed on
    assert (handed["grading"], handed["grading_from"]) == (None, None)

    # Water is not walked, and is not asked about.
    routes.set_stretch(conn, edge, AUTHOR, kind="water")
    assert height.lacking(conn, route_id=route["id"]) == []
    assert height.of_route(conn, route["id"])["on_foot_m"] == height.of_route(conn, route["id"])["known_m"]


def test_kartverket_is_asked_for_the_grading_the_numbers_give_once_the_route_is_done(conn):
    from stiflyt2.domain import corrections

    def asked():
        return [c for c in corrections.corrections(conn, "bre") if c["rutenummer"] == ["bre18"] and "gradering" in c["now"]]

    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    with Terrain().client() as http:
        height.fill(height.lacking(conn, route_id=route["id"], again=True), lambda got: height.keep(conn, got), http=http)
    by_numbers = routes.detail(conn, route["id"])["grading_by_numbers"]
    theirs = conn.execute("SELECT grading FROM source_index.route WHERE label = 'bre18'").fetchone()[0]
    assert by_numbers in ("R", "S") and route["grading"] in (None, "I") and asked() == []     # nobody has looked at it yet
    routes.review(conn, route["id"], routes.detail(conn, route["id"])["version"], AUTHOR)
    if by_numbers == theirs:
        assert asked() == []
    else:
        assert [(c["change"], c["was"], c["now"]) for c in asked()] == [
            ("route attribute", {"gradering": theirs}, {"gradering": by_numbers})]
        assert asked()[0]["text"].endswith(f"to {by_numbers}, by its length and climb")
    # A grading set by a person is what is asked for, whatever the numbers say.
    routes.update(conn, route["id"], routes.detail(conn, route["id"])["version"], AUTHOR, grading="G")
    assert [(c["now"], c["text"].endswith("to G")) for c in asked()] == ([] if theirs == "G" else [({"gradering": "G"}, True)])


def test_the_service_must_answer_what_it_was_asked(monkeypatch):
    def elsewhere(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.url.params["punkter"])
        return httpx.Response(200, json={"punkter": [{"x": x + 5, "y": y, "z": 1.0} for x, y in asked]})

    monkeypatch.setattr(height.time, "sleep", lambda s: None)
    with HTTP_CLIENT(transport=httpx.MockTransport(elsewhere)) as http, pytest.raises(height.Unavailable, match="other"):
        height.ask(http, [(80000.0, 6850000.0)])
    down = Terrain()
    down.down = True
    with down.client() as http, pytest.raises(height.Unavailable, match="503"):
        height.ask(http, [(80000.0, 6850000.0)], tries=3)
    assert down.questions == 3


def test_the_page_gets_the_profile_and_what_is_missing_is_asked_for_while_it_waits(loaded, monkeypatch):
    from stiflyt2.config import settings

    ground = Terrain()
    monkeypatch.setattr(settings, "editor", AUTHOR)
    monkeypatch.setattr(height, "client", ground.client)
    monkeypatch.setattr(height.time, "sleep", lambda s: None)
    with TestClient(create_app()) as client:
        found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "T-19"}).json()
        try:
            assert route["height"]["known_m"] == 0 and route["height"]["up_m"] is None

            # The service is down: the page is told so, and gets what there is.
            ground.down = True
            none = client.get(f"{V1}/owned/routes/{route['id']}/height")
            assert none.status_code == 200 and none.json()["known_m"] == 0
            assert none.json()["waiting"]["stretches"] == route["length"]["legs"] and "503" in none.json()["waiting"]["why"]

            ground.down = False
            high = client.get(f"{V1}/owned/routes/{route['id']}/height").json()
            assert high["waiting"] is None and high["known_m"] == high["on_foot_m"] == route["length"]["on_foot_m"]
            assert len(high["samples"]) > 10 and high["up_m"] is not None
            asked = ground.questions
            assert client.get(f"{V1}/owned/routes/{route['id']}/height").json()["up_m"] == high["up_m"]
            assert ground.questions == asked                                    # kept: not asked twice
            assert client.get(f"{V1}/owned/routes/{route['id']}").json()["height"]["up_m"] == high["up_m"]
            assert client.get(f"{V1}/owned/routes/00000000-0000-0000-0000-000000000000/height").status_code == 404

            # From the command line: what has heights, and nothing left to ask about for this area.
            said = CliRunner().invoke(cli, ["height"])
            assert said.exit_code == 0 and "stretches have heights" in said.output
            filled = CliRunner().invoke(cli, ["height", "fill", "T-1"])
            assert filled.exit_code == 0 and "Every stretch has heights." in filled.output
        finally:
            assert client.delete(f"{V1}/owned/routes/{route['id']}").status_code == 204
        assert client.post(f"{V1}/changeset/close").status_code == 204


def test_a_route_in_pieces_is_walked_one_piece_after_the_other_and_climbs_nothing_between_them(conn):
    # The order and the way round of the pieces: what leaves the least to cross from one to the next.
    at = {"south edge": (0, 0), "Spiterstulen": (10, -1), "north edge": (2, 2), "Juvvasshytta": (4, 4)}
    down = {"order": [("d1", False), ("d2", True)], "ends": ["south edge", "Spiterstulen"], "start": "south edge",
            "nodes": {"south edge", "summit", "Spiterstulen"}, "west": (0, 0)}
    up = {"order": [("u1", False)], "ends": ["north edge", "Juvvasshytta"], "start": "north edge",
          "nodes": {"north edge", "Juvvasshytta"}, "west": (2, 2)}
    walk = routes._walked_through([down, up], at)
    assert [p["start"] for p in walk] == ["Juvvasshytta", "south edge"]       # not: down to the hut, and up to the ice again
    assert walk[0]["order"] == [("u1", True)] and walk[0]["ends"] == ["Juvvasshytta", "north edge"] and walk[1] is down
    assert routes._walked_through([down], at) == [down]

    # A route with a stretch taken out of its middle is two pieces. Each is climbed for itself.
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    legs = [e for (e,) in conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq", (route,))]
    with Terrain().client() as http:
        height.fill(height.lacking(conn, route_id=route, again=True), lambda got: height.keep(conn, got), http=http)
    whole = height.of_route(conn, route, samples=True)
    assert all(s[1] is not None for s in whole["samples"])
    routes.remove_leg(conn, route, routes.detail(conn, route)["version"], legs[len(legs) // 2], AUTHOR)
    after = routes.detail(conn, route)
    assert after["shape"] == "pieces" and len(after["gaps"]) == 1
    apart = height.of_route(conn, route, samples=True)
    breaks = [i for i, s in enumerate(apart["samples"]) if s[1] is None]
    assert len(breaks) == 1 and 0 < breaks[0] < len(apart["samples"]) - 1         # the profile has a break where the pieces part
    first, second = apart["samples"][:breaks[0]], apart["samples"][breaks[0] + 1:]
    rises = (first[-1][1] - first[0][1]) + (second[-1][1] - second[0][1])
    assert apart["up_m"] - apart["down_m"] == pytest.approx(rises, abs=6)          # nothing is climbed from the one to the other
    assert apart["known_m"] == apart["on_foot_m"] < whole["on_foot_m"] and apart["there"] is not None
    # The second piece is begun where the first stopped, as near as the gap allows.
    stop, start = first[-1], second[0]
    gap = after["gaps"][0]["metres"]
    assert conn.execute("""SELECT ST_Distance(ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833),
                                              ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833))""",
                        (stop[3], stop[4], start[3], start[4])).fetchone()[0] == pytest.approx(gap, abs=15)
