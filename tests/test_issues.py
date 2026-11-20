"""Issues and notices on a route: kept with what they are about while the routes are mended (ADR-004), behind a flag."""
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


def middle(conn, route, n=0, at=0.5):
    """A stretch of a route and a point along it, a few metres off the line."""
    edge = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq OFFSET %s LIMIT 1",
                        (route, n)).fetchone()[0]
    lon, lat = conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, %s), 8, 0), 4326) AS p
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s) m""", (at, edge)).fetchone()
    return edge, lon, lat


def test_an_issue_is_at_a_spot_on_a_route_along_a_stretch_or_of_the_whole_route(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    edge, lon, lat = middle(conn, route)
    tree = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="A birch across the path",
                                            lon=lon, lat=lat))
    assert (tree["about"], tree["state"], tree["open"], tree["adrift"]) == ("spot", "reported", True, None)
    assert [r["id"] for r in tree["routes"]] == [route] and tree["area"] == "bre"
    # Clicked 8 m off the line: the spot is put on it.
    off = conn.execute("""SELECT ST_Distance(e.geom, r.geom) FROM ops.entry e, core.edge_revision r JOIN core.edge x
                           ON x.current_revision_id = r.id WHERE e.id = %s AND x.id = %s""", (tree["id"], edge)).fetchone()[0]
    assert off < 0.01

    wet = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="wet", text="Boards wanted", about="stretch",
                                           lon=lon, lat=lat))
    assert len(wet["lines"]) == 1 and len(wet["lines"][0]) > 2
    today = dt.date.today()
    closed = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="closed", text="Works at the dam",
                                              about="route", valid_from=today))
    assert closed["active"] and closed["notice"] and closed["valid_until"] is None and closed["lon"] is not None
    later = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="warning", text="Blasting in June",
                                             about="route", valid_from=today + dt.timedelta(days=30)))
    assert not later["active"] and later["open"]
    # Dates are for notices only; an issue has none.
    assert act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="bridge", text="Bridge out", lon=lon, lat=lat,
                                           valid_from=today))["valid_from"] is None
    with pytest.raises(Invalid, match="end before it begins"):
        act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="closed", text="x", about="route",
                                        valid_from=today, valid_until=today - dt.timedelta(days=1)))
    with pytest.raises(Invalid, match="within 50 m"):
        act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="other", text="Far off", lon=lon + 0.01, lat=lat))

    # The area and the route list them: open notices in force first, then the newest.
    listed = issues.listing(conn, "bre")
    assert [e["text"] for e in listed][:2] == ["Works at the dam", "Bridge out"]
    assert {e["id"] for e in issues.listing(conn, route=route)} == {e["id"] for e in listed}
    assert issues.listing(conn, "jot") == []

    # Done, it is no longer open; its history says who said what.
    act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"state": "done", "text": "A birch across the path, cut"}))
    told = issues.one(conn, tree["id"])
    assert not told["open"] and [h["type"] for h in told["history"]] == ["entry.reported", "entry.changed"]
    assert told["history"][1]["payload"]["state"] == ["reported", "done"]
    # A notice whose end has passed has lapsed, and is not in force.
    act(conn, lambda: issues.change(conn, AUTHOR, closed["id"], {"valid_from": today - dt.timedelta(days=9),
                                                                 "valid_until": today - dt.timedelta(days=2)}))
    lapsed = issues.one(conn, closed["id"])
    assert (lapsed["active"], lapsed["lapsed"]) == (False, True)


def test_an_issue_stays_with_its_spot_when_the_stretch_is_divided_and_is_adrift_when_its_route_goes(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    edge, lon, lat = middle(conn, route, at=0.8)
    tree = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Birch", lon=lon, lat=lat))
    wet = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="wet", text="Bog", about="stretch", lon=lon, lat=lat))
    _, mid_lon, mid_lat = middle(conn, route, at=0.5)
    act(conn, lambda: routes.split(conn, edge, AUTHOR, mid_lon, mid_lat))
    parts = anchor.stands_for(conn, "edge", [edge])[edge]
    assert len(parts) == 2
    # The spot is on the part it lies on, the stretch issue along both parts.
    spot = issues.one(conn, tree["id"])
    assert spot["adrift"] is None and [r["id"] for r in spot["routes"]] == [route]
    on = conn.execute("""SELECT x.id::text FROM core.edge x JOIN core.edge_revision r ON r.id = x.current_revision_id
                         WHERE x.id = ANY(%s::uuid[]) ORDER BY ST_Distance(r.geom, (SELECT geom FROM ops.entry WHERE id = %s)) LIMIT 1""",
                      (parts, tree["id"])).fetchone()[0]
    assert len(issues.one(conn, wet["id"])["lines"]) == 2
    # Something more is said of it: it is kept under the part from then on.
    act(conn, lambda: issues.change(conn, AUTHOR, tree["id"], {"state": "planned"}))
    assert conn.execute("SELECT edge_ids::text[] FROM ops.entry WHERE id = %s", (tree["id"],)).fetchone()[0] == [on]

    # The route is withdrawn: what was said of it is adrift, and still listed under its area.
    act(conn, lambda: routes.retire(conn, route, AUTHOR))
    assert issues.one(conn, tree["id"])["adrift"] == "it lies on no route in use now"
    assert tree["id"] in {e["id"] for e in issues.listing(conn, "bre")}


def test_an_issue_can_be_taken_back_and_taken_away(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    _, lon, lat = middle(conn, route)
    made = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="sign", text="Post rotten", lon=lon, lat=lat))
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
    _, lon, lat = middle(conn, route)
    ask = {"route": route, "kind": "marking", "text": "Paint worn off", "lon": lon, "lat": lat}
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/issues", json=ask, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `issues`")
            assert client.get(f"{V1}/issues", params={"area": "bre"}).status_code == 403
            assert "route_issues" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

            access.set_state(owner, "issues", "all", "test")
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
            assert client.post(f"{V1}/issues", json={**ask, "kind": "nonsense"}, headers=PAGE).status_code == 422
            assert client.patch(f"{V1}/issues/not-one", json={"state": "done"}, headers=PAGE).status_code == 404

            # For an assistant: listed, and dry unless told otherwise.
            assert {"route_issues", "report_issue", "change_issue"} <= {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}
            said, refused = call(client, "route_issues", area="bre")
            assert not refused and [e["id"] for e in said["issues"]] == [entry] and "lines" not in said["issues"][0]
            said, refused = call(client, "report_issue", route="bre18", kind="bridge", text="Bridge gone", at=[lon, lat])
            assert not refused and said["text"] == "Bridge gone" and len(issues.listing(conn, "bre")) == 1
            assert client.delete(f"{V1}/issues/{entry}", headers=PAGE).status_code == 204
            assert client.get(f"{V1}/issues", params={"area": "bre"}).json() == []
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'issues'")
