"""Issues and notices on a route: kept as positions along its stretches, so that they go where the route's line goes
(ADR-004), and behind a flag."""
import datetime as dt

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from test_mcp import call, rpc

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.api.auth import Author
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Invalid
from stiflyt2.ops import along as on_route
from stiflyt2.ops import anchor, issues, undo

AUTHOR = Author("Test Editor", "viewer")
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event)").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
            owner.execute("SET ROLE stiflyt2_owner")
            owner.execute("DELETE FROM ops.entry")
            owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark[0],))
            owner.execute("DELETE FROM ops.event WHERE seq > %s", (mark[1],))
            owner.execute("DELETE FROM ops.saved WHERE who = 'Test Editor'")
        while (latest := routes_undo.last(c)) and latest["txid"] > start:
            with c.transaction():
                routes_undo.undo(c, "Test Editor")


def act(conn, what):
    """Do one thing as one action, the way a request does."""
    with conn.transaction():
        return what()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def along(conn, route, n=0, at=0.5, east=8):
    """A stretch of a route and a point beside it (`east` metres off the line), as lon, lat."""
    edge = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq OFFSET %s LIMIT 1",
                        (route, n)).fetchone()[0]
    lon, lat = conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, %s), %s, 0), 4326) AS p
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s) m""", (at, east, edge)).fetchone()
    return edge, [lon, lat]


def off_line(conn, edge, lonlat):
    """How far a point is from a stretch's line as it is now, in metres."""
    return conn.execute("""SELECT ST_Distance(r.geom, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833))
                           FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s""",
                        (*lonlat, edge)).fetchone()[0]


def test_an_issue_is_at_a_spot_along_parts_of_a_route_or_of_the_whole_route(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    long_m = on_route.Route(conn, route).metres
    edge, beside = along(conn, route)
    tree = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="A birch across the path",
                                            spot={"point": beside}))
    assert (tree["about"], tree["state"], tree["open"], tree["adrift"]) == ("spot", "reported", True, None)
    assert [r["id"] for r in tree["routes"]] == [route] and tree["area"] == "bre" and tree["route"]["id"] == route
    # Clicked 8 m off the line: it is on the line, and says how far along the route it is.
    assert off_line(conn, edge, [tree["lon"], tree["lat"]]) < 0.5 and 0 < tree["at_m"] < long_m
    # Kept as a position along the stretch, and nothing of the line.
    kept = conn.execute("SELECT along, edge_ids::text[] FROM ops.entry WHERE id = %s", (tree["id"],)).fetchone()
    assert kept[0][0]["edge"] == edge and 0 <= kept[0][0]["at"] <= 1 and kept[1] == [edge]

    # Parts: from one bend to another, by km from the start or by two clicks; one issue may have several.
    clear = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Willow", about="part",
                                             parts=[{"from_m": 200, "to_m": 500}, {"from_m": long_m - 300, "to_m": long_m - 100}]))
    assert [(p["from_m"], p["metres"]) for p in clear["parts"]] == [(200, 300), (round(long_m - 300), 200)]
    assert len(clear["lines"]) == 2 and all(len(line) >= 2 for line in clear["lines"])
    _, start = along(conn, route, at=0.2, east=3)
    _, end = along(conn, route, at=0.6, east=-3)
    clicked = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="wet", text="Bog", about="part",
                                               parts=[{"from_point": end, "to_point": start}]))
    assert clicked["parts"][0]["from_m"] < clicked["parts"][0]["to_m"]       # in the route's order, whichever was first

    today = dt.date.today()
    closed = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="closed", text="Works at the dam",
                                              about="route", valid_from=today))
    assert closed["active"] and closed["notice"] and closed["valid_until"] is None and closed["lon"] is not None
    later = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="warning", text="Blasting in June",
                                             about="route", valid_from=today + dt.timedelta(days=30)))
    assert not later["active"] and later["open"]
    # Dates are for notices only; an issue has none.
    assert act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="bridge", text="Bridge out", spot={"m": 100},
                                           valid_from=today))["valid_from"] is None
    wrongs = [
        ("end before it begins", {"kind": "closed", "about": "route", "valid_from": today, "valid_until": today - dt.timedelta(days=1)}),
        ("within 50 m", {"kind": "other", "spot": {"point": [beside[0] + 0.01, beside[1]]}}),
        ("must have a length", {"kind": "other", "about": "part", "parts": [{"from_m": 300, "to_m": 300}]}),
        ("km long", {"kind": "other", "about": "part", "parts": [{"from_m": 0, "to_m": long_m + 900}]}),
        ("which part", {"kind": "other", "about": "part"})]
    for said, how in wrongs:
        with pytest.raises(Invalid, match=said):
            act(conn, lambda how=how: issues.report(conn, AUTHOR, route=route, text="x", **how))

    # The area and the route list them: open notices in force first, then the newest.
    listed = issues.listing(conn, "bre")
    assert [e["text"] for e in listed][:2] == ["Works at the dam", "Bridge out"]
    assert {e["id"] for e in issues.listing(conn, route=route)} == {e["id"] for e in listed}
    assert issues.listing(conn, "jot") == []
    # What parts would be, while they are chosen: nothing is kept.
    assert issues.parts(conn, route, [{"from_m": 100, "to_m": 400}])["parts"] == [{"from_m": 100, "to_m": 400, "metres": 300}]

    # Done, it is no longer open; its history says who said what.
    act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"state": "done", "text": "A birch across the path, cut"}))
    told = issues.one(conn, tree["id"])
    assert not told["open"] and [h["type"] for h in told["history"]] == ["entry.reported", "entry.changed"]
    assert told["history"][1]["payload"]["state"] == ["reported", "done"]
    # Its parts said anew, and a spot made the whole route.
    moved = act(conn, lambda: issues.change(conn, AUTHOR, clear["id"], {"parts": [{"from_m": 50, "to_m": 150}]}))
    assert [p["from_m"] for p in moved["parts"]] == [50] and moved["history"][-1]["payload"]["moved"]
    assert act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"about": "route"}))["at_m"] is None
    with pytest.raises(Invalid, match="the parts"):
        act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"about": "part"}))
    # A notice whose end has passed has lapsed, and is not in force.
    act(conn, lambda: issues.change(conn, AUTHOR, closed["id"], {"valid_from": today - dt.timedelta(days=9),
                                                                 "valid_until": today - dt.timedelta(days=2)}))
    lapsed = issues.one(conn, closed["id"])
    assert (lapsed["active"], lapsed["lapsed"]) == (False, True)


def test_an_issue_goes_where_the_line_goes_when_it_is_mended_or_divided_and_is_adrift_when_its_route_goes(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    edge, beside = along(conn, route, at=0.8)
    tree = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Birch", spot={"point": beside}))
    _, a = along(conn, route, at=0.3)
    clear = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Willow", about="part",
                                             parts=[{"from_point": a, "to_point": beside}]))
    was_m = clear["parts"][0]["metres"]

    # The stretch's line is drawn again, 25 m to the side in the middle: the spot and the part follow the new line.
    _, middle = along(conn, route, at=0.5, east=25)
    act(conn, lambda: routes.redraw(conn, edge, AUTHOR, [middle]))
    spot = issues.one(conn, tree["id"])
    assert off_line(conn, edge, [spot["lon"], spot["lat"]]) < 0.5
    part = issues.one(conn, clear["id"])
    assert part["adrift"] is None and part["parts"][0]["metres"] != was_m and conn.execute(
        "SELECT along -> 0 -> 'from' ->> 'edge' FROM ops.entry WHERE id = %s", (clear["id"],)).fetchone()[0] == edge

    # The stretch is divided: each position is found again on the part it lies on.
    measured = part["parts"][0]["metres"]
    _, cut = along(conn, route, at=0.55, east=0)
    act(conn, lambda: routes.split(conn, edge, AUTHOR, *cut))
    parts = anchor.stands_for(conn, "edge", [edge])[edge]
    assert len(parts) == 2
    spot = issues.one(conn, tree["id"])
    assert spot["adrift"] is None and min(off_line(conn, p, [spot["lon"], spot["lat"]]) for p in parts) < 0.5
    part = issues.one(conn, clear["id"])
    assert part["adrift"] is None and abs(part["parts"][0]["metres"] - measured) <= 2
    # Something more is said of it: it is kept under the parts from then on.
    act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"state": "planned"}))
    assert conn.execute("SELECT along -> 0 ->> 'edge' FROM ops.entry WHERE id = %s", (tree["id"],)).fetchone()[0] in parts

    # The route is withdrawn: what was said along it is adrift, and still listed under its area.
    act(conn, lambda: routes.retire(conn, route, AUTHOR))
    assert issues.one(conn, tree["id"])["adrift"] == "it lies on no route in use now"
    assert tree["id"] in {e["id"] for e in issues.listing(conn, "bre")}


def test_an_issue_can_be_taken_back_and_taken_away(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    made = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="sign", text="Post rotten", spot={"m": 300}))
    assert undo.last(conn, AUTHOR)["what"] == "an issue reported"
    act(conn, lambda: issues.take_away(conn, AUTHOR, made["id"]))
    assert issues.listing(conn, "bre") == [] and undo.last(conn, AUTHOR)["what"] == "an issue taken away"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert [e["text"] for e in issues.listing(conn, "bre")] == ["Post rotten"]
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert issues.listing(conn, "bre") == []


def test_issues_are_a_feature_of_their_own_in_the_page_and_for_an_assistant(conn, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    _, beside = along(conn, route)
    ask = {"route": route, "kind": "marking", "text": "Paint worn off", "spot": {"point": beside}}
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/issues", json=ask, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `issues`")
            assert client.get(f"{V1}/issues", params={"area": "bre"}).status_code == 403
            assert "route_issues" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

            access.set_state(owner, "issues", "all", "test")
            # One who may neither change the routes nor decide about signs looks, and reports nothing.
            as_reader = {**PAGE, "X-Stiflyt-As": "reader"}
            no = client.post(f"{V1}/issues", json=ask, headers=as_reader)
            assert (no.status_code, no.json()["detail"]) == (
                403, "you may look at issues; reporting and changing them needs the editor or operations role")
            assert client.get(f"{V1}/issues", params={"area": "bre"}, headers=as_reader).status_code == 200
            made = client.post(f"{V1}/issues", json=ask, headers=PAGE)
            assert made.status_code == 201 and made.json()["text"] == "Paint worn off"
            # Made with nothing else waiting: saved as made, and Undo still takes it back.
            assert client.get(f"{V1}/signs/work", headers=PAGE).json()["changes"] == []
            assert client.get(f"{V1}/issues/undo", headers=PAGE).json()["last"]["what"] == "an issue reported"
            entry = made.json()["id"]
            changed = client.patch(f"{V1}/issues/{entry}", json={"state": "planned"}, headers=PAGE)
            assert changed.status_code == 200 and changed.json()["state"] == "planned"
            assert [e["id"] for e in client.get(f"{V1}/issues", params={"area": "bre"}).json()] == [entry]
            assert client.get(f"{V1}/issues/{entry}").json()["history"][-1]["type"] == "entry.changed"
            chosen = client.post(f"{V1}/issues/parts", json={"route": route, "parts": [{"from_m": 100, "to_m": 300}]}, headers=PAGE)
            assert chosen.status_code == 200 and chosen.json()["parts"][0]["metres"] == 200
            assert client.post(f"{V1}/issues", json={**ask, "kind": "nonsense"}, headers=PAGE).status_code == 422
            assert client.patch(f"{V1}/issues/not-one", json={"state": "done"}, headers=PAGE).status_code == 404
            assert client.patch(f"{V1}/issues/{entry}", json={"state": "done"}, headers=as_reader).status_code == 403
            assert client.delete(f"{V1}/issues/{entry}", headers=as_reader).status_code == 403

            # For an assistant: listed, and dry unless told otherwise.
            assert {"route_issues", "report_issue", "change_issue"} <= {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
            said, refused = call(client, "route_issues", area="bre")
            assert not refused and [e["id"] for e in said["issues"]] == [entry] and "lines" not in said["issues"][0]
            said, refused = call(client, "report_issue", route="bre18", kind="clearing", text="Willow", about="part",
                                 parts=[{"from_km": 0.1, "to_km": 0.4}])
            assert not refused and said["parts"][0]["metres"] == 300 and len(issues.listing(conn, "bre")) == 1
            assert client.delete(f"{V1}/issues/{entry}", headers=PAGE).status_code == 204
            assert client.get(f"{V1}/issues", params={"area": "bre"}).json() == []
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'issues'")
