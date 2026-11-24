"""The search on the front page: a route of ours by its code, its name, or a place it passes."""
import psycopg
import pytest
from conftest import role_dsn
from fastapi.testclient import TestClient

from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain import places, routes, search

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_a_route_is_found_by_its_code_its_name_or_a_place_it_passes(conn):
    a = routes.make(conn, candidate(conn, "bre18"), AUTHOR)             # Arentzbu – Sprongdalshytta
    routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    got = search.routes(conn, "bre1")
    assert [r["code"] for r in got][:2] == ["bre18", "bre19"] and got[0]["area"] == "bre"
    assert search.routes(conn, "BRE19")[0]["code"] == "bre19"           # the code itself first
    # By a place it passes, and by the name its ends give it.
    passing = search.routes(conn, "sprongdal")
    assert passing[0]["code"] == "bre18" and passing[0]["passes"] == "Sprongdalshytta"
    assert "Sprongdalshytta" in passing[0]["name"]
    # By a name given it.
    routes.update(conn, a, routes.detail(conn, a)["version"], AUTHOR, name="Langturen over breen")
    assert search.routes(conn, "langturen")[0]["code"] == "bre18"
    assert search.routes(conn, "nowhere at all") == [] and search.routes(conn, "b") == []     # one letter says too little


def test_the_search_is_asked_over_http(loaded, monkeypatch):
    monkeypatch.setattr(settings, "editor", AUTHOR)
    with TestClient(create_app()) as client:
        assert client.get("/api/v1/search/routes", params={"q": "nowhere at all"}).json() == []
        assert client.get("/api/v1/search/routes", params={"q": ""}).status_code == 422
