"""What an area is (its routes by code, and routes added to it from another), and what a person works with."""
import io

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from PIL import Image

from stiflyt2.api.app import create_app
from stiflyt2.api.auth import Author
from stiflyt2.config import settings
from stiflyt2.domain import overview, routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Invalid, NotFound
from stiflyt2.ops import areas, issues, photos, reports, undo

AUTHOR = Author("Test Editor", "viewer")
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}


@pytest.fixture
def conn(loaded):
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event)").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
            owner.execute("SET ROLE stiflyt2_owner")
            for table in ("area_route", "report_part", "report", "photo", "entry"):
                owner.execute(f"DELETE FROM ops.{table}")
            owner.execute("DELETE FROM access.profile")
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


def test_a_route_of_another_area_added_to_an_area_is_in_its_lists(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    code = conn.execute("SELECT code FROM core.route WHERE id = %s", (route,)).fetchone()[0]
    act(conn, lambda: areas.add(conn, AUTHOR, "sun", code))
    assert areas.added(conn) == {"sun": [route]} and areas.listing(conn) == {"sun": [{"id": route, "code": code}]}
    assert {a["area"]: a["added"] for a in overview.areas(conn)}["sun"] == [route]
    assert [r["code"] for r in overview.area(conn, "sun")["routes"]] == [code]
    # What happens on it is the area's too: issues, reports, photos.
    issue = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="clearing", text="Birch", spot={"m": 100}))
    assert [e["id"] for e in issues.listing(conn, "sun")] == [issue["id"]]
    made = act(conn, lambda: reports.report(conn, AUTHOR, kind="inspection", done_on="2026-06-01", covers=[{"route": route}]))
    assert [r["id"] for r in reports.listing(conn, "sun")] == [made["id"]] and [s["id"] for s in reports.status(conn, "sun")] == [route]
    buf = io.BytesIO()
    Image.new("RGB", (100, 80), (1, 1, 1)).save(buf, "JPEG")
    lon, lat = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
                               FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
                               WHERE l.route_id = %s ORDER BY l.seq LIMIT 1) m""", (route,)).fetchone()
    shot = act(conn, lambda: photos.add(conn, AUTHOR, buf.getvalue(), lon=lon, lat=lat))
    assert shot["area"] == "bre" and [p["id"] for p in photos.listing(conn, "sun")] == [shot["id"]]

    for wrong, said in ((lambda: areas.add(conn, AUTHOR, "bre", code), "of bre already"), (lambda: areas.add(conn, AUTHOR, "sun", code), "added to sun already"),
                        (lambda: areas.add(conn, AUTHOR, "su", code), "three letters")):
        with pytest.raises(Invalid, match=said):
            act(conn, wrong)
    with pytest.raises(NotFound):
        act(conn, lambda: areas.add(conn, AUTHOR, "sun", "nowhere9"))

    # Taken out, it is the area's no more; Undo puts it back.
    act(conn, lambda: areas.take_out(conn, AUTHOR, "sun", route))
    assert areas.added(conn) == {} and issues.listing(conn, "sun") == [] and undo.last(conn, AUTHOR)["what"] == "a route taken out of an area"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert areas.added(conn) == {"sun": [route]}


def test_a_person_says_what_they_work_with_and_an_editor_what_an_area_is(conn, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    code = conn.execute("SELECT code FROM core.route WHERE id = %s", (route,)).fetchone()[0]
    with TestClient(create_app()) as client:
        assert client.get(f"{V1}/auth/me", headers=PAGE).json()["profile"] == {"areas": [], "maintainer": None}
        said = client.put(f"{V1}/profile", json={"areas": ["bre", "jot", "BRE"], "maintainer": "DNT Oslo og omegn"}, headers=PAGE)
        assert said.status_code == 200 and said.json() == {"areas": ["bre", "jot"], "maintainer": "DNT Oslo og omegn"}
        assert client.get(f"{V1}/auth/me", headers=PAGE).json()["profile"]["areas"] == ["bre", "jot"]
        assert client.put(f"{V1}/profile", json={"areas": ["breh"]}, headers=PAGE).status_code == 422
        added = client.post(f"{V1}/areas/sun/added", json={"code": code}, headers=PAGE)
        assert added.status_code == 201 and added.json()["added"] == [{"id": route, "code": code}]
        assert client.get(f"{V1}/areas/added").json() == {"sun": [{"id": route, "code": code}]}
        assert client.delete(f"{V1}/areas/sun/added/{route}", headers=PAGE).json()["added"] == []
