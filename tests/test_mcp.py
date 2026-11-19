"""The MCP server: an assistant looks and changes through the same functions as the map, as the person it acts for."""
import json

import psycopg
import pytest
from conftest import TEST_DB
from fastapi.testclient import TestClient

from stiflyt2 import access
from stiflyt2.api import auth, mcp
from stiflyt2.api.app import create_app
from stiflyt2.config import settings

KARI, PER = "kari@example.org", "per@example.org"


@pytest.fixture
def client(loaded, monkeypatch):
    """Run locally, as the one editor of the settings."""
    monkeypatch.setattr(settings, "editor", "Test Editor")
    with TestClient(create_app()) as c:
        yield c


def rpc(client, method, params=None, headers=None, mid=1):
    answer = client.post("/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}},
                         headers=headers or {})
    assert answer.status_code == 200, answer.text
    return answer.json()


def call(client, tool, headers=None, /, **arguments):
    """A tool's answer as (what it said, whether it refused)."""
    got = rpc(client, "tools/call", {"name": tool, "arguments": arguments}, headers)["result"]
    text = got["content"][0]["text"]
    try:
        return json.loads(text), got["isError"]
    except ValueError:
        return text, got["isError"]


def test_it_speaks_the_protocol_a_client_expects(client):
    hello = rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert hello["protocolVersion"] == "2025-03-26" and hello["serverInfo"]["name"] == "stiflyt2"
    assert hello["capabilities"] == {"tools": {"listChanged": False}}
    assert "dry_run" in hello["instructions"] and "report_gap" in hello["instructions"]
    # A version it does not know: it answers with the newest it does.
    assert rpc(client, "initialize", {"protocolVersion": "2099-01-01"})["result"]["protocolVersion"] == mcp.VERSIONS[0]
    # Notifications are taken and not answered; there is no stream to listen on.
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert client.get("/mcp").status_code == 405
    assert rpc(client, "ping")["result"] == {}
    assert rpc(client, "resources/list")["error"]["code"] == -32601
    assert client.post("/mcp", content=b"{not json").json()["error"]["code"] == -32700
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    # Several at once, as older clients send them.
    both = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                     {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).json()
    assert [a["id"] for a in both] == [1, 2]

    tools = {t["name"]: t for t in rpc(client, "tools/list")["result"]["tools"]}
    assert {"find", "route", "areas", "area", "checks", "turrutebasen", "what_is_here", "mapped_way", "plan_way",
            "move_end", "send_via", "lay_out_again", "set_stretch", "set_state", "undo_last", "redo_last", "put_back",
            "report_gap"} <= set(tools)
    for t in tools.values():
        assert t["description"] and t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False
    assert tools["find"]["annotations"]["readOnlyHint"] is True and tools["move_end"]["annotations"]["readOnlyHint"] is False
    assert tools["move_end"]["inputSchema"]["properties"]["dry_run"]["default"] is True
    assert rpc(client, "tools/call", {"name": "no_such_tool", "arguments": {}})["error"]["code"] == -32602


def test_an_assistant_looks_things_up(client):
    found, refused = call(client, "find", text="bre18")
    assert not refused and found["turrutebasen"][0]["label"] == "bre18" and found["ours"] == []
    told, _ = call(client, "turrutebasen", code="bre18")
    assert told["candidates"][0]["label"] == "bre18" and told["segments"]
    s = told["segments"][0]
    here, _ = call(client, "what_is_here", lon=s["lon"], lat=s["lat"], radius_m=50)
    assert here["turrutebasen"][0]["id"] == s["id"]
    every, _ = call(client, "areas")
    assert "bre" in {a["area"] for a in every["areas"]}
    one, _ = call(client, "area", area="bre")
    assert one["area"] == "bre" and "all" in one and "map_data" not in one
    assert call(client, "checks", area="bre")[0]["area"] == "bre"
    # What it cannot find, or was asked wrongly, it says in words; and that is kept with the feedback.
    said, refused = call(client, "route", route="bre999")
    assert refused and said.startswith("no route of ours is called bre999")
    said, refused = call(client, "what_is_here", lon=7.5, lat=61.5, colour="red")
    assert refused and said.startswith("the arguments were not understood: colour")
    kept = client.get("/api/v1/feedback").json()["groups"]
    assert {"tool route", "tool what_is_here"} <= {g["what"] for g in kept if g["through"] == ["mcp"]}


def test_an_assistant_changes_a_route_dry_first_and_takes_its_own_changes_back(client):
    made, refused = call(client, "make_yours", turrutebasen="bre18")
    assert not refused and (made["route"], made["shape"], made["done"]) == ("bre18", "line", False)
    try:
        seen, _ = call(client, "route", route="bre18")
        assert "geometry" not in seen and len(seen["stretches"]) == seen["length"]["legs"]
        first = seen["stretches"][0]
        assert set(first) == {"stretch", "kind", "marked", "signed", "origin", "length_m", "from", "to", "also_on"}
        assert call(client, "route", route=made["id"], with_line=True)[0]["geometry"]["type"] in ("LineString", "MultiLineString")

        # Moving an end back along the route: looked at first, by default; then done.
        end, inner = seen["ends"][0], seen["stretches"][1]
        spot = inner["to"] if (end["lon"], end["lat"]) != tuple(inner["to"]) else inner["from"]
        dry, refused = call(client, "move_end", route="bre18", end=end["id"], lon=spot[0], lat=spot[1])
        assert not refused and dry["dropped"] >= 1 and "geometry" not in dry
        assert call(client, "route", route="bre18")[0]["length"] == seen["length"]
        done, _ = call(client, "move_end", route="bre18", end=end["id"], lon=spot[0], lat=spot[1], dry_run=False)
        assert done["length"]["legs"] < seen["length"]["legs"] and done["told"]["dropped"] == dry["dropped"]

        # What a stretch is; and what is said about the route.
        assert call(client, "set_stretch", stretch=inner["stretch"])[1]                    # nothing to change
        assert call(client, "set_stretch", stretch=inner["stretch"], marked=False)[0] == {"stretch": inner["stretch"], "marked": False}
        named, _ = call(client, "describe_route", route="bre18", name="Laid by an assistant")
        assert named["name"] == "Laid by an assistant"

        # Done is for a person to say.
        said, refused = call(client, "set_state", route="bre18", state="done")
        assert refused and "by a person, not by an assistant" in said
        assert call(client, "set_state", route="bre18", state="done", the_person_said_so=True)[0]["done"] is True
        kept, _ = call(client, "set_state", route="bre18", state="not published", why="For its history", the_person_said_so=True)
        assert kept["not_published"] == {"why": "For its history"}

        # It is all recorded as the person's, come in through an assistant; and the map's undo passes it by.
        history = client.get(f"/api/v1/owned/routes/{made['id']}").json()["history"]
        assert {h["author"] for h in history} == {"Test Editor"}
        assert client.get("/api/v1/undo", headers={"X-Stiflyt-Through": "viewer"}).json() is None or \
            client.get("/api/v1/undo", headers={"X-Stiflyt-Through": "viewer"}).json()["what"] != "bre18 said not to be published"
        would, _ = call(client, "undo_last")
        assert would["would_take_back"]["what"] == "bre18 said not to be published" and would["would_take_back"]["through"] == "mcp"
        assert call(client, "undo_last", dry_run=False)[0]["undone"] == "bre18 said not to be published"

        # The route put back to before the end was moved, in one go.
        listed, _ = call(client, "changes", route="bre18")
        moved = next(x for x in listed if x["what"] == "an end of bre18 moved")
        back, refused = call(client, "put_back", route="bre18", txid=moved["txid"])
        assert not refused and "an end of bre18 moved" in back["undone"]
        assert call(client, "route", route="bre18")[0]["length"] == seen["length"]

        gap, _ = call(client, "report_gap", wanted="Close the route for the winter", why_not="There is no such state", route="bre18")
        entry = next(g for g in client.get("/api/v1/feedback").json()["groups"] if g["id"] == gap["kept_as"])
        assert (entry["kind"], entry["what"], entry["through"], entry["about"][0]["called"]) == ("reported", "report_gap", ["mcp"], "bre18")
    finally:
        while call(client, "undo_last")[0]["would_take_back"] is not None:
            call(client, "undo_last", dry_run=False)


def test_who_the_assistant_acts_for_decides_what_it_may_do(loaded, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "session_secret", "a secret for the tests only")
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    monkeypatch.setattr(auth, "REMEMBER_S", 0)
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            access.add(owner, KARI, "Kari Nordmann", "editor", "test")
            access.add(owner, PER, "Per Hansen", "reader", "test")
            hers = {"Authorization": f"Bearer {access.new_token(owner, KARI, 'mcp', 'her assistant')}"}
            his = {"Authorization": f"Bearer {access.new_token(owner, PER, 'mcp', 'his assistant')}"}
            with TestClient(create_app(), base_url="https://testserver") as client:
                # Without a token: told so, in the way a program understands.
                nobody = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert nobody.status_code == 401 and nobody.headers["www-authenticate"].startswith("Bearer")
                # A reader's assistant looks; changing is refused in words.
                assert not call(client, "find", his, text="bre18")[1]
                said, refused = call(client, "make_yours", his, turrutebasen="bre18")
                assert refused and said == "you are signed in to look, not to change: ask for the editor role"
                # An editor's assistant changes, under her name.
                made, refused = call(client, "make_yours", hers, turrutebasen="bre18")
                assert not refused
                who = owner.execute("""SELECT DISTINCT e.author, e.through FROM history.event e
                                       WHERE e.subject_id = %s""", (made["id"],)).fetchall()
                assert who == [("Kari Nordmann", "mcp")]
                assert call(client, "undo_last", hers, dry_run=False)[0]["undone"] == "bre18 made yours"
        finally:
            owner.execute("DELETE FROM access.person WHERE email LIKE '%@example.org'")
