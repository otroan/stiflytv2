"""The front door: the areas with routes in use, and one area with its routes and what they add up to."""
import re

import psycopg
import pytest
from conftest import role_dsn, scripts_of
from fastapi.testclient import TestClient
from test_height import Terrain

from stiflyt2 import height
from stiflyt2.api.app import create_app
from stiflyt2.domain import overview, routes

AUTHOR = "Test Editor"
V1 = "/api/v1"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def of(found, area):
    return next((a for a in found if a["area"] == area), {"routes": 0, "route_m": 0})


def test_an_area_is_its_routes_in_use_and_what_they_add_up_to(conn):
    before, was = of(overview.areas(conn), "bre"), overview.area(conn, "bre")["statistics"]
    a = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    b = routes.make(conn, candidate(conn, "bre14"), AUTHOR)        # all of it is a stretch of bre18 too
    da, db = routes.detail(conn, a), routes.detail(conn, b)
    la, lb = da["length"]["on_foot_m"], db["length"]["on_foot_m"]

    now = of(overview.areas(conn), "bre")
    assert now["routes"] - before["routes"] == 2 and now["route_m"] - before["route_m"] == pytest.approx(la + lb, abs=3)

    found = overview.area(conn, "bre")
    listed = {r["code"]: r for r in found["routes"]}
    assert found["area"] == "bre" and [r["code"] for r in found["routes"]].index("bre14") < [r["code"] for r in found["routes"]].index("bre18")
    for d in (da, db):
        r = listed[d["code"]]
        assert r["id"] == str(d["id"]) and r["name"] == (d["name"] or d["generated_name"]) and r["shape"] == d["shape"]
        assert (r["on_foot_m"], r["water_m"], r["glacier_m"]) == (d["length"]["on_foot_m"], d["length"]["water_m"], d["length"]["glacier_m"])
    S = found["statistics"]
    assert S["routes"] - was["routes"] == 2 and S["routes"] == len(found["routes"]) == sum(S["gradings"].values())
    assert S["route_m"] - was["route_m"] == pytest.approx(la + lb, abs=3)
    assert S["ground_m"] - was["ground_m"] == pytest.approx(la, abs=3)                     # the shared stretch once
    assert S["marked_m"] + S["unmarked_m"] + S["unknown_m"] == pytest.approx(S["ground_m"], abs=3)
    # The places the routes pass, each once though both routes pass it.
    passed = conn.execute("""
        SELECT count(DISTINCT p.id) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision v ON v.id = e.current_revision_id JOIN core.node n ON n.id IN (v.a_node, v.b_node)
        JOIN core.place p ON p.id = n.place_id AND p.status = 'confirmed' WHERE l.route_id = ANY(%s::uuid[])""", ([a, b],)).fetchone()[0]
    assert sum(S["places"].values()) - sum(was["places"].values()) == passed

    # No heights yet: nothing is said of climb, time or grading, and nothing is made up.
    r = listed["bre18"]
    assert (r["up_m"], r["high_m"], r["minutes"], r["grading"], r["grading_from"]) == (None, None, None, None, None)

    # With heights, a route says what it climbs each way, and its length and climb give a grading where none is set.
    ground = Terrain()
    with ground.client() as http:
        height.fill(height.lacking(conn, route_id=a, again=True), lambda got: height.keep(conn, got), http=http)
    high = height.of_route(conn, a)
    found = overview.area(conn, "bre")
    r = next(x for x in found["routes"] if x["code"] == "bre18")
    assert (r["up_m"], r["down_m"], r["low_m"], r["high_m"]) == (high["up_m"], high["down_m"], high["low_m"], high["high_m"])
    assert (r["minutes"], r["minutes_back"]) == (high["there"]["minutes"], high["back"]["minutes"])
    assert r["grading"] == height.harder(high["there"]["at_least"], high["back"]["at_least"]) and r["grading_from"] == "length and climb"
    assert found["statistics"]["gradings"].get(r["grading"], 0) >= 1
    assert found["statistics"]["highest"]["metres"] >= high["high_m"] and found["statistics"]["lowest"]["metres"] <= high["low_m"]
    # A grading a person has set stands.
    routes.update(conn, a, routes.detail(conn, a)["version"], AUTHOR, grading="B")
    r = next(x for x in overview.area(conn, "bre")["routes"] if x["code"] == "bre18")
    assert (r["grading"], r["grading_from"]) == ("B", "set")

    # A route that is discontinued, or kept back, is not in use: it is not here, and its ground is not counted.
    routes.unpublish(conn, b, routes.detail(conn, b)["version"], AUTHOR, "not walked yet")
    routes.discontinue(conn, a, routes.detail(conn, a)["version"], AUTHOR, "the bridge is gone")
    after = overview.area(conn, "bre")
    assert not {"bre14", "bre18"} & {r["code"] for r in after["routes"]}
    assert (after["statistics"]["routes"], after["statistics"]["ground_m"]) == (was["routes"], was["ground_m"])
    assert of(overview.areas(conn), "bre")["routes"] == before["routes"]
    # On the map each stretch says which of its routes are in use.
    said = {r["code"]: r["in_use"] for f in routes.collection(conn)["features"] for r in f["properties"]["routes"]}
    assert (said["bre14"], said["bre18"]) == (False, False)
    assert overview.area(conn, "xyz") == {"area": "xyz", "routes": [], "statistics": {
        "routes": 0, "route_m": 0, "ground_m": 0, "marked_m": 0, "unmarked_m": 0, "unknown_m": 0, "glacier_m": 0, "water_m": 0,
        "highest": None, "lowest": None, "gradings": {}, "places": {}}}


def test_a_page_is_its_scripts_and_no_two_of_them_say_the_same_name(loaded):
    """Scripts loaded one after the other share their names: one declared twice stops the second script whole."""
    with TestClient(create_app()) as client:
        for page, least in (("/", 6), ("/ruter", 3)):
            scripts = scripts_of(client, page)
            assert len(scripts) > least and "/static/common.js" in scripts
            said: dict[str, str] = {}
            for src, text in scripts.items():
                names = re.findall(r"^(?:async function|function|class)\s+([A-Za-z_$][\w$]*)", text, re.MULTILINE)
                for line in re.findall(r"^(?:const|let|var)\s+.*$", text, re.MULTILINE):
                    names += re.findall(r"(?:^(?:const|let|var)\s+|,\s*)([A-Za-z_$][\w$]*)\s*=[^=>]", line)
                for name in names:
                    assert name not in said or said[name] == src, f"{name} is declared in {said[name] or page} and in {src or page}"
                    said[name] = src
            # What each page needs of the shared ones is there.
            assert {"el", "get", "send", "svg", "go", "drawProfile", "BaseControl"} <= set(said)
        for styles in ("/static/front.css", "/static/profile.css"):
            assert client.get(styles).status_code == 200 and styles in client.get("/").text


def test_the_front_door_is_the_areas_and_the_routes_and_anybody_let_in_may_look(loaded):
    with TestClient(create_app()) as client:
        page = client.get("/").text
        code = "\n".join(scripts_of(client, "/").values())
        # The areas and the routes come first; the signs are a part to choose, and the editor is somewhere else.
        assert "owned/areas" in code and 'id="tab-routes"' in page and 'id="tab-signs"' in page and 'href="/ruter"' in page
        assert page.index('id="tab-routes"') < page.index('id="tab-signs"')
        # The height profile is one piece of code, for the front door and for the editor.
        for shared in ("/static/profile.js", "/static/profile.css"):
            assert client.get(shared).status_code == 200 and all(shared in client.get(at).text for at in ("/", "/ruter"))
        assert "function drawProfile" in client.get("/static/profile.js").text and "function drawProfile" not in client.get("/ruter").text
        # Nobody is named as editor on this server: looking is allowed.
        listed = client.get(f"{V1}/owned/areas")
        assert listed.status_code == 200 and all(set(a) == {"area", "routes", "route_m"} for a in listed.json())
        one = client.get(f"{V1}/owned/areas/bre")
        assert one.status_code == 200 and set(one.json()) == {"area", "routes", "statistics"}
        assert client.get(f"{V1}/owned/areas/BRE").json()["area"] == "bre"
        assert client.get(f"{V1}/owned/areas/breheimen").status_code == 422
