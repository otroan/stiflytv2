"""A route as it is walked, in facts: what a description can be written from, and what it cannot tell."""
import itertools
import json

import psycopg
import pytest
from conftest import role_dsn
from fastapi.testclient import TestClient
from test_height import Terrain

from stiflyt2 import height
from stiflyt2.api.app import create_app
from stiflyt2.domain import factsheet, routes

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


def test_the_steep_parts_of_a_profile_are_found_and_which_way_they_go():
    # Level for 500 m, up 150 m over the next 500 m (30 %), level again, then gently down.
    run = [[at, 1000 + max(0, min(at - 500, 500)) * 0.3 - max(0, at - 1500) * 0.05, 0, 0, 0, None]
           for at in range(0, 2501, 10)]
    steep = factsheet._steep(run)
    assert len(steep) == 1 and steep[0]["going"] == "up" and steep[0]["percent"] >= 20
    assert 300 <= steep[0]["from_m"] <= 500 and 1000 <= steep[0]["to_m"] <= 1200 and 140 <= steep[0]["climb_m"] <= 150
    assert factsheet._steepest(run) == {"percent": 30, "going": "up", "from_m": 500, "to_m": 700}
    assert factsheet._steep(list(reversed([[2500 - p[0], *p[1:]] for p in run])))[0]["going"] == "down"
    assert factsheet._steepest(run[:5]) is None and factsheet._steep(run[:30]) == []       # too short, and level


def test_a_few_metres_between_two_junctions_are_no_section_of_a_walk():
    def section(a, b, start, end):
        return {"from_m": a, "to_m": b, "length_m": b - a, "from": start, "to": end, "kind": "trail"}

    told = factsheet._whole_sections([
        section(0, 225, "Skogadalsbøen", "Skogadalsbøen"),             # between two arrival points of one place
        section(225, 251, "Skogadalsbøen", "the junction with jot26"),
        section(251, 9000, "the junction with jot26", "the junction with jot52"),
        section(9000, 9007, "the junction with jot52", "Fondsbu")])     # seven metres to the door
    assert [(s["from_m"], s["to_m"], s["length_m"], s["from"], s["to"]) for s in told] == [
        (0, 251, 251, "Skogadalsbøen", "the junction with jot26"), (251, 9007, 8756, "the junction with jot26", "Fondsbu")]
    alone = [section(0, 12, "A", "B")]
    assert factsheet._whole_sections(alone) == alone and factsheet._whole_sections([]) == []


def test_a_route_is_told_in_sections_with_what_is_passed_on_the_way(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    shares = routes.detail(conn, routes.make(conn, candidate(conn, "bre14"), AUTHOR))   # all of it lies on bre18
    south, north = conn.execute("""SELECT ST_YMin(b), ST_YMax(b) FROM (SELECT Box2D(ST_Collect(r.geom)) AS b
                                   FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                                   JOIN core.edge_revision r ON r.id = e.current_revision_id
                                   WHERE l.route_id = %s) x""", (route["id"],)).fetchone()
    middle = (south + north) / 2
    ground = Terrain(river=(middle - 15, middle + 15))
    with ground.client() as http:
        height.fill(height.lacking(conn, route_id=route["id"], again=True), lambda got: height.keep(conn, got), http=http)

    facts = factsheet.of_route(conn, route["id"])
    assert facts["route"]["code"] == "bre18" and facts["order"].startswith("walked from its first end")
    whole, sections = facts["whole"], facts["sections"]
    assert whole["on_foot_m"] == route["length"]["on_foot_m"] == whole["known_m"]
    assert whole["there"]["minutes"] == height.minutes(whole["on_foot_m"], whole["up_m"])
    # The sections follow one another from the first end to the last, and add up to the route.
    assert sections[0]["from_m"] == 0 and sections[-1]["to_m"] == whole["on_foot_m"]
    assert all(a["to_m"] == b["from_m"] and a["to"] == b["from"] for a, b in itertools.pairwise(sections))
    assert sum(s["length_m"] for s in sections) == pytest.approx(whole["on_foot_m"], abs=len(sections))
    assert sum(s["up_m"] for s in sections) == pytest.approx(whole["up_m"], abs=2 * len(sections))
    assert sum(s["minutes"] for s in sections) == pytest.approx(whole["there"]["minutes"], abs=2 * len(sections))
    # Where the other route runs with it is a section of its own, and where it joins and leaves is a point.
    with_it = [s for s in sections if "bre14" in s["also"]]
    assert with_it and sum(s["length_m"] for s in with_it) == pytest.approx(shares["length"]["on_foot_m"], abs=len(with_it))
    assert len(sections) > len(with_it)
    junctions = [p for p in facts["points"] if p["what"] == "junction"]
    assert any("bre14" in p.get("joins", []) + p.get("leaves", []) for p in junctions)
    assert "bre14" in facts["other_routes"]
    assert all(s["kind"] == "trail" and set(s["ground"]) <= {"open", "river"} for s in sections)
    # The river under the line is told where it is, and that no bridge is known.
    rivers = [p for p in facts["points"] if p["what"] == "river under the line"]
    assert rivers and all(10 <= p["over_m"] <= 400 and isinstance(p["crossing"], str) for p in rivers)
    assert [p["at_m"] for p in facts["points"]] == sorted(p["at_m"] for p in facts["points"])
    assert {"highest point", "lowest point"} <= {p["what"] for p in facts["points"]}
    # Names from Stedsnavn, each once, with how far along and on which side.
    assert facts["names"] and all(n["side"] in ("left", "right", "on the route") and 0 <= n["at_m"] <= whole["on_foot_m"]
                                  and n["off_m"] <= factsheet.WIDE_M for n in facts["names"])
    assert len({(n["name"], n["type"]) for n in facts["names"]}) == len(facts["names"])
    assert any("underfoot" in line for line in facts["not_known"])

    # Where a line has been moved and has no heights yet, the section is told without what the heights give.
    edge, lon, lat = conn.execute("""
        SELECT e.id::text, ST_X(m.p), ST_Y(m.p) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 40, 0), 4326) AS p) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route["id"],)).fetchone()
    routes.redraw(conn, edge, AUTHOR, [(lon, lat)])
    part = factsheet.of_route(conn, route["id"])
    assert part["whole"]["there"] is None and part["whole"]["known_m"] < part["whole"]["on_foot_m"]
    without = [s for s in part["sections"] if s["up_m"] is None]
    assert without and len(without) < len(part["sections"]) and all(s["minutes"] is None and s["ground"] is None for s in without)


def test_the_page_and_an_assistant_get_the_same_facts(loaded, monkeypatch):
    from stiflyt2.config import settings

    monkeypatch.setattr(settings, "editor", AUTHOR)
    with TestClient(create_app()) as client:
        found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "T-19"}).json()
        try:
            facts = client.get(f"{V1}/owned/routes/{route['id']}/facts").json()
            assert facts["route"]["code"] == "T-19" and facts["sections"][-1]["to_m"] == route["length"]["on_foot_m"]
            got = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "route_facts", "arguments": {"route": "T-19"}}}).json()["result"]
            assert not got["isError"] and json.loads(got["content"][0]["text"]) == facts
            assert client.get(f"{V1}/owned/routes/00000000-0000-0000-0000-000000000000/facts").status_code == 404
        finally:
            assert client.delete(f"{V1}/owned/routes/{route['id']}").status_code == 204
        assert client.post(f"{V1}/changeset/close").status_code == 204
