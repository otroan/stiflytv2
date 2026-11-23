"""Reports of inspections and work parties: where and when, and from them when each route was last done."""
import datetime as dt
import io

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from PIL import Image
from test_mcp import call, rpc

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.api.auth import Author
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Invalid
from stiflyt2.ops import along as on_route
from stiflyt2.ops import anchor, issues, photos, reports, undo

AUTHOR = Author("Test Editor", "viewer")
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}
TODAY = dt.date.today()


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event)").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
            owner.execute("SET ROLE stiflyt2_owner")
            for table in ("report_part", "report", "photo", "entry"):
                owner.execute(f"DELETE FROM ops.{table}")
            owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark[0],))
            owner.execute("DELETE FROM ops.event WHERE seq > %s", (mark[1],))
            owner.execute("DELETE FROM ops.saved WHERE who = 'Test Editor'")
        while (latest := routes_undo.last(c)) and latest["txid"] > start:
            with c.transaction():
                routes_undo.undo(c, "Test Editor")


def act(conn, what):
    with conn.transaction():
        return what()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def years_ago(n):
    return TODAY - dt.timedelta(days=round(365.25 * n))


def test_when_a_route_was_last_inspected_and_worked_on_is_worked_out_from_the_reports(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    other = act(conn, lambda: routes.make(conn, candidate(conn, "bre17"), AUTHOR))
    long_m = on_route.Route(conn, route).metres
    status = {s["id"]: s for s in reports.status(conn, "bre")}
    assert status[route]["inspection"] is None and status[route]["work"] is None
    # Never inspected comes first.
    assert reports.status(conn, "bre")[0]["inspection"] is None

    old = act(conn, lambda: reports.report(conn, AUTHOR, kind="inspection", done_on=years_ago(4), covers=[{"route": route}],
                                            condition="fair", text="Marking faint above the lake"))
    assert [c["route"]["id"] for c in old["covers"]] == [route] and old["covers"][0]["whole"]
    s = {x["id"]: x for x in reports.status(conn, "bre")}[route]["inspection"]
    assert (s["report"], s["condition"], s["all_of_it"], s["due"]) == (old["id"], "fair", True, True)

    # A work party on part of it, two years ago, and on all of another route the same day.
    work = act(conn, lambda: reports.report(conn, AUTHOR, kind="work", done_on=years_ago(2), persons=6, hours=42.5,
                                             done=["clearing", "marking"], link="https://dnt.sharepoint.com/sites/x/report.pdf",
                                             covers=[{"route": route, "parts": [{"from_m": 100, "to_m": long_m / 2}]}, {"route": other}]))
    assert [p["from_m"] for p in work["covers"][0]["parts"]] == [100] and work["hours"] == 42.5 and work["done"] == ["clearing", "marking"]
    s = {x["id"]: x for x in reports.status(conn, "bre")}
    assert (s[route]["work"]["all_of_it"], s[route]["work"]["due"], s[other]["work"]["all_of_it"]) == (False, False, True)
    # An inspection last month: no longer due.
    act(conn, lambda: reports.report(conn, AUTHOR, kind="inspection", done_on=TODAY - dt.timedelta(days=30), covers=[{"route": route}], condition="good"))
    assert {x["id"]: x for x in reports.status(conn, "bre")}[route]["inspection"]["due"] is False

    # Only the year known, as from an area contact's overview: told as the year, counted from its middle.
    year = act(conn, lambda: reports.report(conn, AUTHOR, kind="work", done_on=dt.date(2015, 3, 3), year_only=True, covers=[{"route": other}]))
    assert (year["year_only"], year["done_on"]) == (True, dt.date(2015, 7, 1))
    # The lines of what was covered, the latest last, for a map of how long since.
    lines = reports.coverage(conn, "bre", "work")
    assert lines and lines[-1]["done_on"] == years_ago(2) and lines[0]["done_on"] == dt.date(2015, 7, 1)
    # A route's reports are those that cover some of it: here also those of the route it shares stretches with.
    assert work["id"] in [r["id"] for r in reports.listing(conn, route=other)]
    assert [r["kind"] for r in reports.listing(conn, "bre")] == ["inspection", "work", "inspection", "work"]

    for wrong, said in (({"kind": "inspection", "done_on": TODAY + dt.timedelta(days=2), "covers": [{"route": route}]}, "still to come"),
                        ({"kind": "work", "done_on": TODAY, "covers": []}, "which routes"),
                        ({"kind": "work", "done_on": TODAY, "covers": [{"route": route}], "link": "sharepoint/x"}, "https://"),
                        ({"kind": "work", "done_on": TODAY, "covers": [{"route": route}], "done": ["painting"]}, "what was done")):
        with pytest.raises(Invalid, match=said):
            act(conn, lambda wrong=wrong: reports.report(conn, AUTHOR, **wrong))


def test_a_report_says_issues_are_done_follows_the_routes_and_can_be_taken_back(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    birch = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Birch", spot={"m": 200}))
    made = act(conn, lambda: reports.report(conn, AUTHOR, kind="work", done_on=TODAY, covers=[{"route": route}], closes=[birch["id"]]))
    assert issues.one(conn, birch["id"])["state"] == "done" and [c["id"] for c in reports.one(conn, made["id"])["closed"]] == [birch["id"]]
    # Photos of it: added with it, or said to be of it, as from the day they were taken.
    buf = io.BytesIO()
    Image.new("RGB", (200, 150), (9, 99, 9)).save(buf, "JPEG")
    shot = act(conn, lambda: photos.add(conn, AUTHOR, buf.getvalue(), report=made["id"]))
    assert [p["id"] for p in photos.listing(conn, report=made["id"])] == [shot["id"]] and shot["area"] == "bre"
    later = io.BytesIO()
    Image.new("RGB", (200, 150), (99, 9, 9)).save(later, "JPEG")
    loose = act(conn, lambda: photos.add(conn, AUTHOR, later.getvalue(), area="bre"))
    second = act(conn, lambda: reports.report(conn, AUTHOR, kind="note", done_on=TODAY, covers=[{"route": route}], photos=[loose["id"], shot["id"]]))
    # Only a photo of no report yet is said to be of this one.
    assert [p["id"] for p in photos.listing(conn, report=second["id"])] == [loose["id"]]
    act(conn, lambda: reports.take_away(conn, AUTHOR, second["id"]))

    # A stretch it covered is divided: both parts are still covered, and the route is still all done.
    edge = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq LIMIT 1", (route,)).fetchone()[0]
    lon, lat = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
                               FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s) m""", (edge,)).fetchone()
    act(conn, lambda: routes.split(conn, edge, AUTHOR, lon, lat))
    parts = anchor.stands_for(conn, "edge", [edge])[edge]
    assert set(parts) <= set(reports.one(conn, made["id"])["edges"])
    assert {x["id"]: x for x in reports.status(conn, "bre")}[route]["work"]["all_of_it"]

    # Said anew, then taken away; Undo brings it back.
    changed = act(conn, lambda: reports.change(conn, AUTHOR, made["id"], {"text": "Cleared and marked", "persons": 4}))
    assert (changed["text"], changed["persons"]) == ("Cleared and marked", 4)
    act(conn, lambda: reports.take_away(conn, AUTHOR, made["id"]))
    assert reports.listing(conn, "bre") == [] and undo.last(conn, AUTHOR)["what"] == "a report taken away"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert [r["id"] for r in reports.listing(conn, "bre")] == [made["id"]] and reports.one(conn, made["id"])["covers"]


def test_reports_are_a_feature_of_their_own_in_the_page_and_for_an_assistant(conn, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    ask = {"kind": "inspection", "done_on": str(TODAY), "covers": [{"route": route}], "condition": "good"}
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/reports", json=ask, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `reports`")
            assert "route_upkeep" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

            access.set_state(owner, "reports", "all", "test")
            made = client.post(f"{V1}/reports", json=ask, headers=PAGE)
            assert made.status_code == 201 and made.json()["condition"] == "good"
            told = {s["id"]: s for s in client.get(f"{V1}/reports/status", params={"area": "bre"}).json()}
            assert told[route]["inspection"]["report"] == made.json()["id"]
            assert client.get(f"{V1}/reports/coverage", params={"area": "bre", "kind": "inspection"}).json()
            assert client.patch(f"{V1}/reports/{made.json()['id']}", json={"text": "All well"}, headers=PAGE).json()["text"] == "All well"
            assert client.post(f"{V1}/reports", json={**ask, "kind": "party"}, headers=PAGE).status_code == 422
            said, refused = call(client, "route_upkeep", area="bre")
            assert not refused and said["due_every_years"] == {"inspection": 3, "work": 10}
            said, refused = call(client, "report_work", kind="work", done_on=str(TODAY), routes=[{"route": "bre18", "from_km": 0.1, "to_km": 0.6}], persons=3)
            assert not refused and said["covers"][0]["parts"][0]["metres"] == 500 and len(reports.listing(conn, "bre")) == 1
            assert client.delete(f"{V1}/reports/{made.json()['id']}", headers=PAGE).status_code == 204
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'reports'")
