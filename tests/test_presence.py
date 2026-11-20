"""Who else is at work in the pages now: the feature `presence`."""
import psycopg
from conftest import TEST_DB
from fastapi.testclient import TestClient

from stiflyt2 import access, presence
from stiflyt2.api.app import create_app
from stiflyt2.config import settings

AUTHOR = "Test Editor"
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}


def test_the_others_whose_pages_said_where_they_are_lately_are_told_and_not_oneself():
    board = presence.Board()
    board.say("Ann", {"page": "editor", "area": "har", "route": "har13"}, now=1000)
    board.say("Bo", {"page": "signs", "area": "jot", "site": "Gjendesheim"}, now=1030)
    board.say("Cy", {"page": "routes"}, now=1000)
    assert [o["name"] for o in board.others("Cy", now=1060)] == ["Ann", "Bo"]
    assert board.others("Cy", now=1060)[0] == {"name": "Ann", "page": "editor", "area": "har", "route": "har13",
                                               "site": None, "seconds": 60}
    # A page that has not said where it is for a while has been closed.
    assert [o["name"] for o in board.others("Cy", now=1000 + presence.FRESH_S + 1)] == ["Bo"]
    assert "Ann" not in board.seen


def test_what_a_page_says_is_kept_short_and_plain_as_it_is_shown_to_others():
    board = presence.Board()
    board.say("Ann", {"page": "elsewhere", "area": "harx", "route": "<b>har13</b>", "site": "x" * 300}, now=0)
    said = board.seen["Ann"]
    assert (said["page"], said["area"], said["route"], len(said["site"])) == (None, "har", "bhar13/b", 60)


def test_only_those_who_have_the_feature_see_and_are_seen_and_only_from_the_page(monkeypatch):
    monkeypatch.setattr(settings, "editor", AUTHOR)
    monkeypatch.setattr(presence, "BOARD", presence.Board())
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/here", json={"page": "editor"}, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `presence`")
            assert presence.BOARD.seen == {}

            access.set_state(owner, "presence", "all", "test")
            assert "presence" in client.get(f"{V1}/auth/me").json()["features"]
            # An assistant or a script is not somebody at work in the pages.
            assert client.post(f"{V1}/here", json={"page": "editor"}).status_code == 403
            assert presence.BOARD.seen == {}

            assert client.post(f"{V1}/here", json={"page": "editor", "area": "bre"}, headers=PAGE).json() == []
            presence.BOARD.say("Another Editor", {"page": "signs", "area": "jot", "site": "Gjendesheim"})
            told = client.post(f"{V1}/here", json={"page": "editor", "area": "bre", "route": "bre18"}, headers=PAGE).json()
            assert [(o["name"], o["page"], o["site"], o["unsaved"]) for o in told] == [("Another Editor", "signs", "Gjendesheim", 0)]
            assert presence.BOARD.seen[AUTHOR]["route"] == "bre18"
            assert client.post(f"{V1}/here", json={"page": "editor", "area": "x" * 20}, headers=PAGE).status_code == 422
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'presence'")
