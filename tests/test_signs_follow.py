"""What is decided about a sign stays with the sign while the routes are mended, and can be taken back (ADR-004)."""
import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient

from stiflyt2.api.auth import Author
from stiflyt2.domain import places, routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Conflict, NotFound
from stiflyt2.ops import anchor, signs, undo

AUTHOR = Author("Test Editor", "viewer")
OPS = ("sign", "sign_post", "sign_site", "sign_point")


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("""SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event),
                                   (SELECT coalesce(max(id), 0) FROM ops.sign_order)""").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:     # what the test kept about signs
            owner.execute("SET ROLE stiflyt2_owner")
            owner.execute("SELECT set_config('stiflyt2.undoing', 'on', false)")
            for table in OPS:
                owner.execute(f"""DELETE FROM ops.{table} WHERE id IN (
                                      SELECT key FROM ops.undo_log WHERE id > %s AND tbl = %s AND op = 'I')""", (mark[0], table))
            owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark[0],))
            owner.execute("DELETE FROM ops.event WHERE seq > %s", (mark[1],))
            owner.execute("DELETE FROM ops.sign_order WHERE id > %s", (mark[2],))
        while (latest := routes_undo.last(c)) and latest["txid"] > start:      # and what it did to the routes
            with c.transaction():
                routes_undo.undo(c, "Test Editor")


def act(conn, what):
    """Do one thing as one action, the way a request does."""
    with conn.transaction():
        return what()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def every(found):
    return [(site, post, sign) for site in found["sites"] for post in site["posts"] for sign in post["signs"]]


def kept(conn):
    return conn.execute("SELECT count(*) FROM ops.sign").fetchone()[0]


@pytest.fixture
def adrift(conn):
    """What is adrift because of this test: other tests leave an order behind whose route they have withdrawn."""
    before = {entry["id"] for entry in signs.adrift(conn)}
    return lambda: [entry for entry in signs.adrift(conn) if entry["id"] not in before]


def test_a_decision_follows_its_sign_when_its_stretch_is_divided_and_its_route_renumbered(conn, adrift):
    before = kept(conn)
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        act(conn, lambda hut=hut: places.adopt(conn, hut, AUTHOR))
    _, post, sign = next(x for x in every(signs.listing(conn, "bre")) if x[2]["to"]["type"] == "place")
    key = sign["key"]
    node, edge = key.split("|")[:2]
    act(conn, lambda: signs.set_sign(conn, key, AUTHOR, direction="høyre", text="Til hytta"))
    act(conn, lambda: signs.mark(conn, [key], AUTHOR))
    act(conn, lambda: signs.set_post(conn, post["key"], AUTHOR, name_sign=True, name_text="Her"))
    assert kept(conn) == before + 1 and adrift() == []

    # The stretch the sign points along is divided: the part that leaves the sign's junction is its stretch now.
    lon, lat = conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s) m""", (edge,)).fetchone()
    act(conn, lambda: routes.split(conn, edge, AUTHOR, lon, lat))
    parts = anchor.stands_for(conn, "edge", [edge])[edge]
    assert len(parts) == 2 and anchor.one(parts) is None
    same = next(g for _, _, g in every(signs.listing(conn, "bre")) if g["text"] == "Til hytta")
    assert same["key"] != key and same["key"].split("|")[0] == node and same["key"].split("|")[1] in parts
    assert (same["direction"], same["to_order"], sorted(same["by_hand"])) == ("høyre", True, ["direction", "text"])
    assert adrift() == []
    # Something more is said of it: about the same row, which is now kept under the stretch as it is.
    act(conn, lambda: signs.set_sign(conn, same["key"], AUTHOR, colour="grønn"))
    assert kept(conn) == before + 1
    assert conn.execute("SELECT first_edge::text, colour, direction FROM ops.sign WHERE text = 'Til hytta'").fetchone() == (
        same["key"].split("|")[1], "grønn", "høyre")
    assert conn.execute("SELECT count(*) FROM ops.event WHERE type = 'sign.followed'").fetchone()[0] >= 1

    # An order keeps the key a sign had then. The sign is found by it still, after its stretch is divided again.
    act(conn, lambda: signs.take_out_order(conn, "bre", AUTHOR))
    ordered = next(g for _, _, g in every(signs.listing(conn, "bre")) if g["text"] == "Til hytta")
    assert ordered["ordered"] and not ordered["ordered"]["up"] and not ordered["to_order"]
    half = ordered["key"].split("|")[1]
    lon, lat = conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s) m""", (half,)).fetchone()
    act(conn, lambda: routes.split(conn, half, AUTHOR, lon, lat))
    again = next(g for _, _, g in every(signs.listing(conn, "bre")) if g["text"] == "Til hytta")
    assert again["key"] != ordered["key"] and again["ordered"]["order"] == ordered["ordered"]["order"]
    assert act(conn, lambda: signs.mark_up(conn, [again["key"]], AUTHOR)) == 1
    assert next(g for _, _, g in every(signs.listing(conn, "bre")) if g["text"] == "Til hytta")["ordered"]["up"] is True
    # The name sign on its post is where it was.
    top = next(p for _, p, _ in every(signs.listing(conn, "bre")) if p["key"] == post["key"])["name_sign"]
    assert top and top["text"] == "Her"

    # A route that gets another code is the same route to whatever was told apart by it.
    detail = routes.detail(conn, route)
    act(conn, lambda: routes.update(conn, route, detail["version"], AUTHOR, code="bre918"))
    assert (anchor.code_now(conn, "bre18"), anchor.code_now(conn, "BRE918"), anchor.code_now(conn, "bre7777")) == (
        "bre918", "bre918", None)


def test_a_route_end_that_becomes_a_place_keeps_what_was_said_of_its_signs_and_its_site(conn, adrift):
    route = routes.detail(conn, act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR)))
    assert not any(e["place"] for e in route["ends"])           # it ends at no place, at both ends
    found = signs.listing(conn, "bre")
    _, _, sign = next(x for x in every(found) if x[2]["to"]["type"] == "node")
    end = sign["to"]["id"]
    there = next(s for s in found["sites"] if s["key"] == f"node:{end}")
    act(conn, lambda: signs.set_sign(conn, sign["key"], AUTHOR, text="Enden", distance_m=4000))
    act(conn, lambda: signs.set_site(conn, there["key"], AUTHOR, area="bre", name="Der stien slutter", no_signs=True))

    place = act(conn, lambda: places.at_node(conn, end, AUTHOR, "Endestasjonen"))
    now = signs.listing(conn, "bre")
    same = next(g for _, _, g in every(now) if g["text"] == "Enden")
    assert same["key"] != sign["key"] and same["to"] == {"type": "place", "id": str(place), "name": "Endestasjonen"}
    assert same["km"] == 4.0 and sorted(same["by_hand"]) == ["distance_m", "text"]
    moved = next(s for s in now["sites"] if s["key"] == f"place:{place}")
    assert (moved["name"], moved["name_by_hand"], moved["no_signs"]) == ("Der stien slutter", True, True)
    assert adrift() == []
    # Said again, it is said of the same rows.
    act(conn, lambda: signs.set_site(conn, moved["key"], AUTHOR, no_signs=False))
    act(conn, lambda: signs.set_sign(conn, same["key"], AUTHOR, direction="venstre"))
    assert conn.execute("SELECT at_type, at_id::text, status FROM ops.sign_site WHERE name = 'Der stien slutter'").fetchall() == [
        ("place", str(place), "proposed")]
    assert conn.execute("SELECT to_type, to_id::text, direction FROM ops.sign WHERE text = 'Enden'").fetchall() == [
        ("place", str(place), "venstre")]


def test_what_no_longer_finds_its_sign_is_adrift_until_a_person_settles_it(conn, adrift):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        act(conn, lambda hut=hut: places.adopt(conn, hut, AUTHOR))
    site, _, sign = next(x for x in every(signs.listing(conn, "bre"))
                         if x[2]["to"]["type"] == "place" and x[0]["key"].startswith("place:"))
    act(conn, lambda: signs.set_sign(conn, sign["key"], AUTHOR, direction="høyre", to_order=True, why="worn"))
    assert adrift() == []

    # The route is given up: no sign is worked out there any more. What was decided is not lost, and not shown as a sign.
    act(conn, lambda: routes.discontinue(conn, route, routes.detail(conn, route)["version"], AUTHOR, "the bridge is gone"))
    assert not [x for x in every(signs.listing(conn, "bre")) if x[2]["key"] == sign["key"]]
    loose = adrift()
    assert len(loose) == 1 and (loose[0]["kind"], loose[0]["at"], loose[0]["to"]) == ("sign", site["name"], sign["to"]["name"])
    assert loose[0]["said"] == ["arrow høyre", "on the order (worn)"] and "no longer give" in loose[0]["why"]
    assert loose[0]["lon"] == pytest.approx(site["lon"], abs=0.01) and loose[0]["moves"] == []
    # It comes into use again: the decision is the sign's again, by itself.
    act(conn, lambda: routes.take_up_again(conn, route, routes.detail(conn, route)["version"], AUTHOR))
    assert adrift() == []
    assert next(g for _, _, g in every(signs.listing(conn, "bre")) if g["key"] == sign["key"])["direction"] == "høyre"

    # Kept as pointing to the place the post itself is at, which no sign does: adrift, and the sign it was
    # about is free to take it.
    act(conn, lambda: conn.execute("UPDATE ops.sign SET to_id = %s WHERE direction = 'høyre'", (site["key"].split(":")[1],)))
    loose = adrift()
    assert len(loose) == 1 and sign["key"] in [m["key"] for m in loose[0]["moves"]]
    with pytest.raises(Conflict):
        act(conn, lambda: undo.undo(conn, Author("Kari", "viewer")))            # nothing of hers
    assert act(conn, lambda: signs.move(conn, loose[0]["id"], sign["key"], AUTHOR))["key"] == sign["key"]
    assert adrift() == []
    back = next(g for _, _, g in every(signs.listing(conn, "bre")) if g["key"] == sign["key"])
    assert (back["direction"], back["to_order"]) == ("høyre", True)
    act(conn, lambda: undo.undo(conn, AUTHOR))                                  # the move is taken back
    loose = adrift()
    assert len(loose) == 1
    # Forgotten, it is gone; and that can be taken back too.
    was = kept(conn)
    forgotten = act(conn, lambda: signs.forget(conn, loose[0]["id"], AUTHOR))
    assert forgotten["said"][0] == "arrow høyre" and adrift() == [] and kept(conn) == was - 1
    with pytest.raises(NotFound):
        act(conn, lambda: signs.forget(conn, loose[0]["id"], AUTHOR))
    assert undo.last(conn, AUTHOR)["what"] == "what was decided about a sign that is gone, forgotten"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert kept(conn) == was and len(adrift()) == 1


def test_what_is_decided_about_signs_can_be_taken_back_by_the_one_who_decided_it(conn):
    ola, assistant, kari = Author("Ola", "viewer"), Author("Ola", "mcp"), Author("Kari", "viewer")
    act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        act(conn, lambda hut=hut: places.adopt(conn, hut, AUTHOR))
    found = signs.listing(conn, "bre")
    (_, post, one), (_, _, two) = every(found)[:2]
    assert undo.last(conn, ola) is None

    act(conn, lambda: signs.set_post(conn, post["key"], ola, lon=post["lon"] + 0.0002, lat=post["lat"]))
    act(conn, lambda: signs.mark(conn, [one["key"], two["key"]], ola))
    act(conn, lambda: signs.set_sign(conn, one["key"], assistant, text="Skrevet av en assistent"))
    assert undo.last(conn, ola)["what"] == "2 signs put on the order"
    assert undo.last(conn, assistant)["what"] == "what was said of a sign" and undo.last(conn, kari) is None
    # The assistant's change is about one of the two signs, and came after: Ola's would lose it.
    with pytest.raises(Conflict, match="came after"):
        act(conn, lambda: undo.undo(conn, ola))
    assert act(conn, lambda: undo.undo(conn, assistant))["undone"] == "what was said of a sign"
    again = act(conn, lambda: undo.undo(conn, ola))
    assert again["undone"] == "2 signs put on the order" and again["next"]["what"] == "a post moved"
    now = {g["key"]: g for _, _, g in every(signs.listing(conn, "bre"))}
    assert not now[one["key"]]["to_order"] and not now[two["key"]]["to_order"] and now[one["key"]]["text"] == one["text"]
    assert act(conn, lambda: undo.undo(conn, ola))["next"] is None

    back = next(p for _, p, _ in every(signs.listing(conn, "bre")) if p["key"] == post["key"])
    assert back["assumed"] and back["lon"] == pytest.approx(post["lon"], abs=1e-6)
    with pytest.raises(Conflict, match="nothing of yours"):
        act(conn, lambda: undo.undo(conn, ola))
    assert conn.execute("SELECT count(*) FROM ops.event WHERE type = 'undone' AND who = 'Ola'").fetchone()[0] == 3

    # A sign site put on a route, and taken back: it is gone, with its spot.
    lon, lat = conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.route t ON t.id = l.route_id WHERE t.code = 'bre18' ORDER BY ST_Length(r.geom) DESC LIMIT 1) m""").fetchone()
    spot = act(conn, lambda: signs.add_site(conn, kari, "bre", lon, lat, "Ved brua"))
    assert spot in [s["key"] for s in signs.listing(conn, "bre")["sites"]]
    assert act(conn, lambda: undo.undo(conn, kari))["undone"] == "a sign site put on a route"
    assert spot not in [s["key"] for s in signs.listing(conn, "bre")["sites"]]

    # An order that has been taken out stands, and so does what was decided before it.
    act(conn, lambda: signs.mark(conn, [one["key"]], kari))
    act(conn, lambda: signs.take_out_order(conn, "bre", kari))
    assert undo.last(conn, kari) is None
    with pytest.raises(Conflict):
        act(conn, lambda: undo.undo(conn, kari))


def test_the_page_and_an_assistant_have_what_is_adrift_and_the_undo(loaded, monkeypatch):
    from stiflyt2.api import mcp
    from stiflyt2.api.app import create_app
    from stiflyt2.config import settings

    assert mcp.TOOLS["settle_adrift"].operations and mcp.TOOLS["undo_signs"].operations
    assert not mcp.TOOLS["signs_adrift"].changes
    v1 = "/api/v1"
    with TestClient(create_app()) as client:
        # Nobody is named as editor on this server: looking is allowed, deciding and taking back are not.
        assert isinstance(client.get(f"{v1}/signs/adrift").json(), list)
        assert client.get(f"{v1}/signs/undo").status_code == 403 and client.post(f"{v1}/signs/undo").status_code == 403
        assert client.post(f"{v1}/signs/adrift/00000000-0000-0000-0000-000000000000/forget").status_code == 403
        monkeypatch.setattr(settings, "editor", "Test Editor")
        assert client.post(f"{v1}/signs/adrift/00000000-0000-0000-0000-000000000000/forget").status_code == 404
        found = client.get(f"{v1}/routes", params={"q": "bre19"}).json()[0]
        route = client.post(f"{v1}/owned/routes", json={"candidate_id": found["id"], "code": "bre19"}).json()
        try:
            site = next(s for s in client.get(f"{v1}/signs", params={"area": "bre"}).json()["sites"] if "bre19" in s["routes"])
            sign = site["posts"][0]["signs"][0]
            was = client.get(f"{v1}/signs/undo").json()["last"]
            assert client.patch(f"{v1}/signs", json={"key": sign["key"], "direction": "høyre"}).status_code == 204
            last = client.get(f"{v1}/signs/undo").json()["last"]
            assert last["what"] == "what was said of a sign" and last["in_the_way"] is None
            assert client.post(f"{v1}/signs/undo", json={"txid": last["txid"] + 1}).status_code == 409    # not the latest
            done = client.post(f"{v1}/signs/undo", json={"txid": last["txid"]})
            assert done.status_code == 200 and done.json()["undone"] == "what was said of a sign"
            assert client.get(f"{v1}/signs/undo").json()["last"] == was
            after = next(s for s in client.get(f"{v1}/signs", params={"area": "bre"}).json()["sites"] if s["key"] == site["key"])
            assert "direction" not in next(g for g in after["posts"][0]["signs"] if g["key"] == sign["key"])["by_hand"]
            # An assistant: its own latest, dry unless told otherwise.
            would = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "undo_signs", "arguments": {}}}).json()["result"]
            assert not would["isError"] and '"would_take_back"' in would["content"][0]["text"]
            adrift = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                               "params": {"name": "signs_adrift", "arguments": {}}}).json()["result"]
            assert not adrift["isError"] and '"count":' in adrift["content"][0]["text"].replace(" ", "")
        finally:
            assert client.delete(f"{v1}/owned/routes/{route['id']}").status_code == 204
        assert client.post(f"{v1}/changeset/close").status_code == 204


def test_what_is_decided_about_signs_since_the_last_save_is_saved_or_discarded_as_a_whole(conn):
    ola, assistant, kari = Author("Ola Nordmann", "viewer"), Author("Ola Nordmann", "mcp"), Author("Kari Nordmann", "viewer")
    act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    for hut in ("place:799507", "place:27269"):
        act(conn, lambda hut=hut: places.adopt(conn, hut, AUTHOR))
    (_, _, one), (_, _, two) = every(signs.listing(conn, "bre"))[:2]
    assert undo.item(conn, ola) == {"changes": [], "in_the_way": None}
    with pytest.raises(Conflict, match="nothing that is not saved"):
        undo.discard(conn, ola)
    before = {g["key"]: (g["text"], g["to_order"]) for _, _, g in every(signs.listing(conn, "bre"))}
    act(conn, lambda: signs.set_sign(conn, one["key"], ola, text="Til hytta"))
    assert act(conn, lambda: undo.save(conn, ola)) == {"saved": 1} and undo.item(conn, ola)["changes"] == []
    saved = {g["key"]: (g["text"], g["to_order"]) for _, _, g in every(signs.listing(conn, "bre"))}
    assert saved != before
    act(conn, lambda: signs.set_sign(conn, two["key"], ola, text="Til vannet"))
    act(conn, lambda: signs.mark(conn, [one["key"], two["key"]], ola))
    assert [c["what"] for c in undo.item(conn, ola)["changes"]] == ["2 signs put on the order", "what was said of a sign"]
    assert undo.item(conn, kari)["changes"] == [] and undo.item(conn, assistant)["changes"] == []
    # Kari decides something about one of the two since: his cannot be discarded over hers.
    act(conn, lambda: signs.set_sign(conn, two["key"], kari, text="Karis tekst"))
    with pytest.raises(Conflict, match="came after.*cannot be discarded as a whole"):
        act(conn, lambda: undo.discard(conn, ola))
    act(conn, lambda: undo.undo(conn, kari))
    told = act(conn, lambda: undo.discard(conn, ola))
    assert told["undone"] == ["2 signs put on the order", "what was said of a sign"]
    assert {g["key"]: (g["text"], g["to_order"]) for _, _, g in every(signs.listing(conn, "bre"))} == saved
    assert undo.item(conn, ola)["changes"] == [] and told["next"]["what"] == "what was said of a sign"
    # An order taken out is the save: what went into it is not his to discard afterwards.
    act(conn, lambda: signs.mark(conn, [one["key"]], ola))
    act(conn, lambda: signs.take_out_order(conn, "bre", ola))
    assert undo.item(conn, ola)["changes"] == []
