"""What UT.no says about a route: read from the route's page there when a person asks, and taken only on their word."""
import json

import httpx
import pytest
from conftest import HTTP_CLIENT
from fastapi.testclient import TestClient

from stiflyt2 import utno
from stiflyt2.api.app import create_app
from stiflyt2.domain.places import Invalid, NotFound

V1 = "/api/v1"
ROUTE = {"__typename": "Route", "id": 13394424, "code": "sun35", "status": "PUBLIC", "type": "foot",
         "name": "Fra Barstadvik til Molladalen", "placeA": "Barstadvik", "placeVia": None, "placeB": "Molladalen",
         "descriptionAb": "<p>Fra parkeringsplassen følger du skilt.  Stien er tydelig &amp; tørr.</p>"
                          "<p>Se <a href=\"https://example.org/kart\">kartet</a>.<br>Husk kjetting:</p>"
                          "<ul><li>til høyre: klyving</li><li>til venstre: over haugen</li></ul>",
         "descriptionBa": "<p>Fra Molladalen følger du rød-merkede stolper nedover dalen.</p>",
         "gradingAb": "TOUGH", "gradingBa": "TOUGH", "notes": None, "distance": 3388,
         "updatedAt": "2026-10-12T11:07:52.711Z"}


class UtNo:
    """UT.no's route pages, as far as they are read: the route as data inside the page."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.routes = {ROUTE["id"]: ROUTE}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(str(request.url))
        assert request.url.host == "ut.no" and request.url.path.startswith("/rutebeskrivelse/")
        number = int(request.url.path.rsplit("/", 1)[1])
        if number == 1:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/rutebeskrivelse/1"})
        if number == 2:
            return httpx.Response(200, text="<html><body>Another page altogether</body></html>")
        if number not in self.routes:
            return httpx.Response(404, text="<html>Fant ikke siden</html>")
        data = json.dumps({"props": {"pageProps": {"route": self.routes[number]}}, "page": "/rutebeskrivelse/[...slug]"})
        return httpx.Response(200, text='<html><body><div id="__next"></div><script id="__NEXT_DATA__" '
                                        f'type="application/json">{data}</script></body></html>')

    def elsewhere(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "ut.no":
            return httpx.Response(200, text="<html>not UT.no</html>")
        return self(request)

    def client(self) -> httpx.Client:
        return HTTP_CLIENT(transport=httpx.MockTransport(self.elsewhere), follow_redirects=True)


@pytest.fixture
def site(monkeypatch):
    site = UtNo()
    monkeypatch.setattr(utno, "client", site.client)
    monkeypatch.setattr(utno, "APART_S", 0)
    return site


def test_utnos_two_descriptions_go_under_the_way_a_route_of_ours_is_walked():
    page = {"from": "Turtagrø", "to": "Stølsdalen", "there": "Up from the hotel.", "back": "Down to the hotel."}
    assert utno.ways(page, "Turtagrø hotell", "Stølsdalen") == {"there": "Up from the hotel.", "back": "Down to the hotel.", "sure": True, "turned": False}
    # Ours is walked the other way: UT.no's back is our way, and its way our back.
    assert utno.ways(page, "Stølsdalen", "Turtagrø hotell") == {"there": "Down to the hotel.", "back": "Up from the hotel.", "sure": True, "turned": True}
    assert utno.ways({**page, "from": "Sota Sæter"}, "Sota sæter", None)["turned"] is False
    # Spelt a little otherwise, or which of two it is: still the same place.
    assert utno.ways({**page, "from": "Juvasshytta", "to": "Spiterstulen"}, "Spiterstulen", "Juvvasshytta")["turned"] is True
    assert utno.ways({**page, "from": "Dørålseter", "to": "Grimsdalshytta"}, "Øvre Dørålseter", None)["turned"] is False
    # Names that say nothing of it, or fit both ways (a round trip): UT.no's own order, and said to be unsure.
    assert utno.ways(page, "Fondsbu", "Olavsbu")["sure"] is False
    assert utno.ways({**page, "to": "Turtagrø"}, "Turtagrø hotell", "Turtagrø")["sure"] is False


def test_a_route_is_searched_for_by_the_names_of_its_ends_in_a_web_search():
    from urllib.parse import unquote

    said = unquote(utno.search("Memurubu – Gjendesheim", "jot2"))
    assert said.startswith("https://www.google.com/search?q=") and said.endswith('site:ut.no rutebeskrivelse "Memurubu" "Gjendesheim"')
    assert unquote(utno.search(None, "jot2")).endswith("site:ut.no rutebeskrivelse jot2")


def test_the_page_is_named_by_its_address_or_its_number_and_nothing_else_is_fetched():
    for said in ("https://ut.no/rutebeskrivelse/13394424", "https://www.ut.no/rutebeskrivelse/13394424/barstadvik-molladalen",
                 "ut.no/rutebeskrivelse/13394424?utm=x", " 13394424 ", 13394424,
                 "https://ut.no/kart/rutebeskrivelse/13394424/fra-turtagr-til-stlsdalen"):
        assert utno.number(said) == 13394424
    for said in ("https://elsewhere.example/rutebeskrivelse/13394424", "https://ut.no.elsewhere.example/rutebeskrivelse/1",
                 "https://ut.no/turforslag/115", "file:///etc/passwd", "jot64", "", "-4"):
        with pytest.raises(Invalid, match="address of the route's page"):
            utno.number(said)


def test_a_description_kept_as_html_becomes_text():
    assert utno.words(ROUTE["descriptionAb"]) == (
        "Fra parkeringsplassen følger du skilt. Stien er tydelig & tørr.\n\n"
        "Se kartet (https://example.org/kart).\nHusk kjetting:\n\n"
        "- til høyre: klyving\n- til venstre: over haugen")
    assert utno.words(None) == "" == utno.words("<p> </p>")
    assert utno.words('<a href="https://ut.no/x">https://ut.no/x</a> og <a href="/hytte/1">hytta</a>') == "https://ut.no/x og hytta"


def test_what_the_page_says_is_read_out_of_it(site):
    found = utno.fetch(13394424)
    assert site.asked == ["https://ut.no/rutebeskrivelse/13394424"]
    assert (found["code"], found["from"], found["via"], found["to"]) == ("sun35", "Barstadvik", None, "Molladalen")
    assert (found["length_m"], found["grading_there"], found["changed"], found["public"]) == (3388, "TOUGH", "2026-10-12", True)
    assert found["back"] == "Fra Molladalen følger du rød-merkede stolper nedover dalen." and found["notes"] == ""
    # The two directions as one text, each under the way it is walked; one alone, or the same twice, as it is.
    text = utno.described(found)
    assert text.startswith("Barstadvik – Molladalen\nFra parkeringsplassen") and "\n\nMolladalen – Barstadvik\nFra Molladalen" in text
    assert utno.described({**found, "back": ""}) == found["there"] == utno.described({**found, "back": found["there"]})

    with pytest.raises(NotFound, match="no route description with the number 77"):
        utno.fetch(77)
    with pytest.raises(utno.Unavailable, match="did not give the page"):       # sent on to somewhere that is not UT.no
        utno.fetch(1)
    with pytest.raises(utno.Unavailable, match="not put together as it used to be"):
        utno.fetch(2)
    site.routes[5] = ROUTE                                                      # the page of another route than asked for
    with pytest.raises(utno.Unavailable, match="not put together"):
        utno.fetch(5)


def test_a_person_fetches_the_description_and_takes_it(loaded, site, monkeypatch):
    from stiflyt2.config import settings

    monkeypatch.setattr(settings, "editor", "Test Editor")
    with TestClient(create_app()) as client:
        found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "T-19"}).json()
        at = f"{V1}/owned/routes/{route['id']}"
        try:
            assert route["utno_id"] is None
            # Which page is the route's is not known until a person says so.
            unknown = client.get(f"{at}/utno")
            assert unknown.status_code == 422 and "which page on UT.no is T-19" in unknown.json()["detail"]
            assert client.get(f"{at}/utno", params={"address": "https://elsewhere.example/x"}).status_code == 422
            assert client.get(f"{at}/utno", params={"address": "77"}).status_code == 404
            assert client.get(f"{at}/utno", params={"address": "2"}).status_code == 502
            assert site.asked == ["https://ut.no/rutebeskrivelse/77", "https://ut.no/rutebeskrivelse/2"]

            says = client.get(f"{at}/utno", params={"address": "https://ut.no/rutebeskrivelse/13394424"}).json()
            assert says["number"] == 13394424 and says["text"].startswith("Barstadvik – Molladalen\n")
            assert says["same_code"] is False and says["ours"] == {
                "code": "T-19", "length_m": route["length"]["on_foot_m"], "has_description": False, "utno_id": None,
                "walked": route["walked"]}
            # Its two descriptions, each under one of our two ways; whether the names of the ends said which is which.
            assert {says["this_way"], says["other_way"]} == {says["there"], says["back"]} and isinstance(says["ways_sure"], bool)
            assert client.get(at).json()["utno_id"] is None                     # looking changes nothing

            # Taken on the person's word: the text, and which page it was.
            taken = client.patch(at, json={"version": route["version"], "description": says["text"],
                                           "utno_id": says["number"]}).json()
            assert taken["utno_id"] == 13394424 and taken["description"] == says["text"]
            told = {e["type"]: e["payload"] for e in taken["history"]}
            assert told["route.on_utno"] == {"from": None, "to": 13394424}
            assert told["route.described"]["taken_from"] == "ut.no" and told["route.described"]["from"] is None
            # Now the route knows its page; and it is handed on with the route.
            assert client.get(f"{at}/utno").json()["ours"]["utno_id"] == 13394424
            handed = client.get(f"{V1}/routes.geojson").json()["features"]
            assert next(f for f in handed if f["id"] == route["id"])["properties"]["utno_id"] == 13394424
            # A description written by hand afterwards is not said to be UT.no's; and the page can be forgotten.
            own = client.patch(at, json={"version": taken["version"], "description": "Our own words.", "utno_id": 0}).json()
            assert own["utno_id"] is None and "taken_from" not in own["history"][0]["payload"] | own["history"][1]["payload"]

            # An assistant reads the same, and is told plainly when UT.no has nothing.
            def tool(name, **arguments):
                got = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                "params": {"name": name, "arguments": arguments}}).json()["result"]
                return got["content"][0]["text"], got["isError"]

            text, refused = tool("on_utno", route="T-19", address="13394424")
            assert not refused and json.loads(text)["code"] == "sun35"
            text, refused = tool("on_utno", route="T-19")
            assert refused and "which page on UT.no" in text
            text, refused = tool("on_utno", route="T-19", address="2")
            assert refused and "not put together" in text
        finally:
            assert client.delete(at).status_code == 204
        assert client.post(f"{V1}/changeset/close").status_code == 204
