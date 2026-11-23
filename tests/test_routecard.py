"""The route card: a route on one page, of the parts asked for; a part that is a feature of its own only for one
who has it. Owners from the made-up register of the landowner tests, never the real one."""
import re

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from test_landowners import PERSON_NUMBER, Register

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.ops import issues, landowners, routecard

AUTHOR = "Test Editor"
V1 = "/api/v1"


@pytest.fixture
def card(loaded, monkeypatch):
    """bre18 made a route of ours with an issue on it, a client of the page, and both taken back after."""
    monkeypatch.setattr(settings, "editor", AUTHOR)
    made = Register()
    monkeypatch.setattr(landowners, "register", lambda: made)
    landowners._owners.clear()
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as conn, \
            psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        start = conn.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = owner.execute("SELECT coalesce(max(id), 0) FROM ops.owner_lookup").fetchone()[0]
        try:
            with conn.transaction():
                route = str(routes.make(conn, conn.execute("SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0], AUTHOR))
            with conn.transaction():
                issues.report(conn, AUTHOR, route=route, kind="clearing", text="Birch <across> the path", about="route")
            yield client, owner, route
        finally:
            for key in ("issues", "landowners"):
                owner.execute("DELETE FROM access.feature WHERE key = %s", (key,))
            owner.execute("DELETE FROM ops.entry")
            owner.execute("DELETE FROM ops.owner_lookup WHERE id > %s", (mark,))
            while (latest := routes_undo.last(conn)) and latest["txid"] > start:
                with conn.transaction():
                    routes_undo.undo(conn, AUTHOR)


def headings(page: str) -> list[str]:
    return re.findall(r"<h2>([^<]+)</h2>", page)


def test_a_card_has_the_facts_and_the_parts_chosen_by_default(card):
    client, _, route = card
    got = client.get(f"{V1}/routecard/{route}")
    assert got.status_code == 200 and got.headers["content-type"].startswith("text/html")
    page = got.text
    assert "<title>bre18" in page and "Route card" in page and "<dt>Length</dt>" in page and "© Kartverket" in page
    assert "cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/" in page          # the map, of Kartverket's tiles
    # Issues and reports are features of their own: the editor here has neither, so they are not on it.
    assert headings(page) == ["Height profile", "Description", "Section by section", "Heritage sites near it"]
    assert client.get(f"{V1}/routecard/{route}", params={"with": "map"}).text.count("<h2>") == 0
    assert headings(client.get(f"{V1}/routecard/{route}", params={"with": "walk,signs,description"}).text) == [
        "Description", "Section by section", "Signs along it"]
    assert client.get(f"{V1}/routecard/not-a-route").status_code == 404


def test_issues_are_on_it_for_one_who_may_see_them_and_owners_for_one_who_may_ask(card):
    client, owner, route = card
    assert "Birch" not in client.get(f"{V1}/routecard/{route}", params={"with": "issues"}).text
    access.set_state(owner, "issues", "all", "test")
    page = client.get(f"{V1}/routecard/{route}", params={"with": "issues"}).text
    assert headings(page) == ["Open issues and notices"]
    assert "Birch &lt;across&gt; the path" in page and "the whole route" in page        # said as text, not as markup

    # Who owns the ground: not without the feature, and then only by asking the register, which is kept.
    assert "Who owns the ground" not in client.get(f"{V1}/routecard/{route}", params={"with": "owners"}).text
    access.set_state(owner, "landowners", "all", "test")
    page = client.get(f"{V1}/routecard/{route}", params={"with": "owners"}).text
    assert headings(page) == ["Who owns the ground"] and "personal data" in page and PERSON_NUMBER not in page


def test_the_parts_are_in_the_cards_own_order_and_one_may_only_have_what_one_has():
    assert routecard.chosen("signs,map,nonsense") == ["map", "signs"]
    assert routecard.chosen(None) == [k for k, (_, default) in routecard.PARTS.items() if default]
    assert routecard.chosen("issues,owners,map", lambda feature: feature == "issues") == ["map", "issues"]


def test_with_none_of_our_own_the_card_gives_utnos_description_as_utnos(card, monkeypatch):
    from stiflyt2 import utno

    client, owner, route = card
    owner.execute("UPDATE core.route SET utno_id = 135600 WHERE id = %s", (route,))
    monkeypatch.setattr(utno, "fetch", lambda number, http=None: {"number": number, "code": "bre18", "there": "Steep up to the pass.",
                                                                   "back": "", "notes": ""})
    monkeypatch.setattr(utno, "described", lambda found: found["there"])
    page = client.get(f"{V1}/routecard/{route}", params={"with": "description"}).text
    assert "From UT.no (ut.no/rutebeskrivelse/135600)" in page and "Steep up to the pass." in page
    monkeypatch.setattr(utno, "fetch", lambda number, http=None: (_ for _ in ()).throw(utno.Unavailable("down")))
    assert "None is written yet." in client.get(f"{V1}/routecard/{route}", params={"with": "description"}).text


def test_a_route_is_described_each_way_and_the_card_gives_both_its_own_way_first(card):
    from stiflyt2.domain import routes as domain_routes

    client, _, route = card
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as conn:
        r = domain_routes.detail(conn, route)
        assert set(r["walked"]) == {"from", "to"}
        with conn.transaction():
            domain_routes.update(conn, route, r["version"], AUTHOR, description="Up the valley.", description_back="Down the valley.")
        r = domain_routes.detail(conn, route)
        assert (r["description"], r["description_back"]) == ("Up the valley.", "Down the valley.")
        said = conn.execute("""SELECT payload FROM history.event WHERE subject_id = %s AND type = 'route.described'
                               ORDER BY seq DESC LIMIT 2""", (route,)).fetchall()
        assert {p.get("way") for (p,) in said} == {"back", None}
    page = client.get(f"{V1}/routecard/{route}", params={"with": "description"}).text
    assert page.index("Up the valley.") < page.index("Down the valley.") and page.count('<h3 class="way">') == 2
    # Taken back with the change it was made in, both ways at once.
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as conn:
        with conn.transaction():
            routes_undo.undo(conn, AUTHOR)
        r = domain_routes.detail(conn, route)
        assert (r["description"], r["description_back"]) == (None, None)
