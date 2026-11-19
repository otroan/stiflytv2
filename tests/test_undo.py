"""Undo: whatever the latest change was, the rows are back as they were."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import places, proposals, routes, undo

AUTHOR = "Test Editor"
STATE = {
    "route": "SELECT id, code, name, status, reviewed, maintainers, grading FROM core.route "
             "WHERE status <> 'undone' ORDER BY id",
    "route_leg": "SELECT route_id, seq, edge_id, reversed, maintainers FROM core.route_leg ORDER BY 1, 2",
    "node": "SELECT id, ST_AsText(geom), place_id, status FROM core.node WHERE status <> 'undone' ORDER BY id",
    "edge": "SELECT id, current_revision_id, status FROM core.edge WHERE status <> 'undone' ORDER BY id",
    "place": "SELECT id, name, kind, ST_AsText(geom), primary_node_id, status, reviewed FROM core.place "
             "WHERE status <> 'undone' ORDER BY id",
    "succession": "SELECT subject_type, old_id, new_id, how FROM core.succession ORDER BY 1, 2, 3",
    "alias": "SELECT subject_type, subject_id, kind, value FROM core.alias ORDER BY 1, 2, 3, 4",
}


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        yield c
        while (latest := undo.last(c)) and latest["txid"] > start:
            with c.transaction():
                undo.undo(c, AUTHOR)


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def state(conn):
    return {name: conn.execute(sql).fetchall() for name, sql in STATE.items()}


def act(conn, what):
    """Do one thing as one action, the way a request does."""
    with conn.transaction():
        return what()


def test_every_kind_of_change_is_taken_back_exactly(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    made = routes.detail(conn, route)
    assert undo.last(conn)["what"] == "bre18 made yours"
    longest, lon, lat, mid_lon, mid_lat = conn.execute("""
        SELECT e.id::text, ST_X(m.p), ST_Y(m.p), ST_X(m.q), ST_Y(m.q)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 30, 20), 4326) AS p,
                             ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS q) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route,)).fetchone()
    inner = next(n for n in made["nodes"] if n["id"] not in {e["id"] for e in made["ends"]})
    near_a, near_b = conn.execute("""
        SELECT r.a_node::text, r.b_node::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) LIMIT 1""", (route,)).fetchone()
    end = made["ends"][0]

    def version():
        return routes.detail(conn, route)["version"]

    changes = {
        "a line pulled": lambda: routes.bend(conn, longest, AUTHOR, (mid_lon, mid_lat), (lon, lat)),
        "a junction moved": lambda: routes.move_node(conn, inner["id"], AUTHOR, inner["lon"] + 0.0005, inner["lat"]),
        "two junctions joined": lambda: routes.merge_node(conn, near_a, near_b, AUTHOR),
        "a stretch divided": lambda: routes.split(conn, longest, AUTHOR, mid_lon, mid_lat),
        "a stretch taken out of bre18": lambda: routes.remove_leg(conn, route, version(), longest, AUTHOR),
        "what a stretch is changed": lambda: routes.set_stretch(conn, longest, AUTHOR, kind="glacier"),
        "who maintains bre18 changed": lambda: routes.update(conn, route, version(), AUTHOR,
                                                             maintainers=["Luster Turlag"], everywhere=True),
        "bre18 said to be done": lambda: routes.review(conn, route, version(), AUTHOR),
        "bre18 withdrawn": lambda: routes.retire(conn, route, AUTHOR),
        "a spot made a place": lambda: places.at_node(conn, end["id"], AUTHOR, "Enden"),
        "a route laid out": lambda: routes.lay_out(conn, AUTHOR, [(end["lon"], end["lat"]), (lon, lat)], "bre18y"),
    }
    for told, change in changes.items():
        before = state(conn)
        act(conn, change)
        assert state(conn) != before, told
        offered = undo.last(conn)
        if told not in ("a spot made a place", "a route laid out"):
            assert told in offered["what"], (told, offered["what"])
        after = state(conn)
        done = act(conn, lambda: undo.undo(conn, AUTHOR))
        assert state(conn) == before, told
        assert done["next"]["what"] == "bre18 made yours"
        # And brought back exactly, and taken back once more.
        assert done["again"]["what"] == offered["what"] and done["again"]["in_the_way"] is None
        again = act(conn, lambda: undo.redo(conn, AUTHOR))
        assert state(conn) == after, told
        assert (again["redone"], again["again"], again["next"]["txid"]) == (offered["what"], None, offered["txid"])
        act(conn, lambda: undo.undo(conn, AUTHOR))
        assert state(conn) == before, told
    # The route is usable as before: changing it again works, with a revision number that is free.
    act(conn, lambda: routes.bend(conn, longest, AUTHOR, (mid_lon, mid_lat), (lon, lat)))
    assert conn.execute("""SELECT r.rev_no > 2 FROM core.edge e JOIN core.edge_revision r
                           ON r.id = e.current_revision_id WHERE e.id = %s""", (longest,)).fetchone()[0]
    # The history keeps what was done, and that it was taken back.
    types = [h["type"] for h in routes.detail(conn, route)["history"]]
    assert types.count("undone") >= 4 and "route.reviewed" in types

    # Undone one after the other, back to before the route was made.
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert undo.last(conn)["what"] == "bre18 made yours"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert conn.execute("SELECT status FROM core.route WHERE id = %s", (route,)).fetchone() == ("undone",)
    assert conn.execute("SELECT count(*) FROM core.route_leg WHERE route_id = %s", (route,)).fetchone() == (0,)
    # The candidate is free to be made yours again, and gets stretches of its own again.
    assert routes.owner_of(conn, candidate(conn, "bre18")) is None
    again = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    assert routes.detail(conn, again)["shape"] == "line"


def test_a_batch_of_approved_proposals_is_one_action_and_undo_is_last_in_first_out(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre19"), AUTHOR))
    before = state(conn)
    legs = [str(e) for (e,) in conn.execute(
        "SELECT edge_id FROM core.route_leg WHERE route_id = %s ORDER BY seq", (route,))]

    def several():      # as approving several proposals in one request does
        for edge in legs[:2]:
            with conn.transaction():
                routes.remove_leg(conn, route, routes.detail(conn, route)["version"], edge, AUTHOR)

    act(conn, several)
    latest = undo.last(conn)
    with pytest.raises(places.Conflict, match="something else has been changed"):
        undo.undo(conn, AUTHOR, latest["txid"] - 1)
    act(conn, lambda: undo.undo(conn, AUTHOR, latest["txid"]))
    assert state(conn) == before
    assert proposals.proposals is not None


def test_a_route_is_put_back_as_it_was_while_what_was_done_elsewhere_stays(conn):
    """jot88: its changes taken back, although jot53 had been changed in between and lay on top."""
    def txid():
        return undo.last(conn)["txid"]

    a = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    made = txid()
    b = act(conn, lambda: routes.make(conn, candidate(conn, "bre14"), AUTHOR))     # shares one stretch with bre18
    first = routes.detail(conn, a)
    shared, alone = (conn.execute("""
        SELECT l.edge_id::text FROM core.route_leg l WHERE l.route_id = %s AND (EXISTS (
            SELECT 1 FROM core.route_leg o WHERE o.edge_id = l.edge_id AND o.route_id = %s)) = %s
        ORDER BY l.seq LIMIT 1""", (a, b, both)).fetchone()[0] for both in (True, False))

    # Two changes to bre18, with changes to bre14 in between and after.
    act(conn, lambda: routes.update(conn, a, routes.detail(conn, a)["version"], AUTHOR, name="Mended"))
    renamed = txid()
    act(conn, lambda: routes.unpublish(conn, b, routes.detail(conn, b)["version"], AUTHOR, "Kept for its history"))
    act(conn, lambda: routes.set_stretch(conn, alone, AUTHOR, marked=False))
    act(conn, lambda: routes.update(conn, b, routes.detail(conn, b)["version"], AUTHOR, name="Elsewhere"))
    top = txid()

    offered = undo.of_route(conn, a)
    assert [(c["what"], c["with"], c["in_the_way"]) for c in offered] == [
        ("what a stretch is changed", 1, None), ("bre18 renamed", 2, None), ("bre18 made yours", 3, None)]
    assert offered[1]["txid"] == renamed and offered[2]["txid"] == made
    with pytest.raises(undo.Conflict, match="not among those that can be taken back for this route"):
        undo.back(conn, AUTHOR, a, top)       # a change to bre14, not to this route

    told = act(conn, lambda: undo.back(conn, AUTHOR, a, renamed))
    assert told["undone"] == ["what a stretch is changed", "bre18 renamed"]
    back = routes.detail(conn, a)
    assert (back["name"], back["length"]) == (first["name"], first["length"])
    # What was done to bre14 stays, and is still what Undo at the top takes back next.
    other = routes.detail(conn, b)
    assert (other["name"], other["unpublished"]) == ("Elsewhere", {"why": "Kept for its history"})
    assert told["next"]["txid"] == top and [c["what"] for c in undo.of_route(conn, a)] == ["bre18 made yours"]
    assert [h["type"] for h in back["history"][:2]] == ["undone", "route.renamed"]

    # A change to bre18's line that also rewrote bre14 (their shared stretch divided), and then a change
    # to bre14 on top of that: taking the first back would lose the second. Nothing is done, and it says why.
    lon, lat = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
                               FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                               WHERE e.id = %s) x""", (shared,)).fetchone()
    act(conn, lambda: routes.split(conn, shared, AUTHOR, lon, lat))
    divided = txid()
    half = routes.detail(conn, b)
    assert half["length"]["legs"] == 2
    leg = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq LIMIT 1", (b,)).fetchone()[0]
    act(conn, lambda: routes.remove_leg(conn, b, half["version"], leg, AUTHOR))
    assert undo.of_route(conn, a)[0]["in_the_way"] == "a stretch taken out of bre14"
    before = state(conn)
    with pytest.raises(undo.Conflict, match="a stretch taken out of bre14 came after, and touches the same"):
        act(conn, lambda: undo.back(conn, AUTHOR, a, divided))
    assert state(conn) == before
    # With that taken back first, in order, the route can be put back.
    act(conn, lambda: undo.undo(conn, AUTHOR))
    act(conn, lambda: undo.back(conn, AUTHOR, a, divided))
    assert routes.detail(conn, a)["length"] == first["length"] and routes.detail(conn, b)["length"]["legs"] == 1


def test_undo_takes_back_the_askers_own_latest_change_made_the_way_they_ask(conn):
    """Ola in the map, an assistant acting as Ola, and Kari, all at once: each takes back their own."""
    from stiflyt2.api.auth import Author

    ola, assistant, kari = Author("Ola", "viewer"), Author("Ola", "mcp"), Author("Kari", "viewer")
    a = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), ola))
    b = act(conn, lambda: routes.make(conn, candidate(conn, "bre21"), assistant))
    c = act(conn, lambda: routes.make(conn, candidate(conn, "bre14"), kari))       # shares a stretch with bre18
    # (Not a dict by who: the two Oles are the same name.)
    by_ole, by_assistant, by_kari = (undo.last(conn, undo.whose(who))["txid"] for who in (ola, assistant, kari))
    assert len({by_ole, by_assistant, by_kari}) == 3 and undo.last(conn)["txid"] == by_kari
    assert conn.execute("SELECT DISTINCT through FROM history.event WHERE txid = %s", (by_assistant,)).fetchall() == [("mcp",)]
    assert conn.execute("SELECT DISTINCT through FROM core.undo_log WHERE txid = %s AND author IS NOT NULL",
                        (by_kari,)).fetchall() == [("viewer",)]

    def named(route, name, who):
        act(conn, lambda: routes.update(conn, route, routes.detail(conn, route)["version"], who, name=name))

    named(a, "Ola in the map", ola)
    named(b, "The assistant", assistant)
    named(c, "Kari", kari)
    # Each sees their own latest as what Undo would take back; and takes that back, the others' staying.
    assert [undo.last(conn, undo.whose(w))["what"] for w in (ola, assistant, kari)] == [
        "bre18 renamed", "bre21 renamed", "bre14 renamed"]
    told = act(conn, lambda: undo.undo(conn, ola))
    assert told["undone"] == "bre18 renamed" and told["next"]["txid"] == by_ole
    assert [routes.detail(conn, r)["name"] for r in (b, c)] == ["The assistant", "Kari"]
    assert routes.detail(conn, a)["name"] != "Ola in the map"
    assert act(conn, lambda: undo.undo(conn, assistant))["undone"] == "bre21 renamed"
    assert routes.detail(conn, c)["name"] == "Kari"
    with pytest.raises(undo.Conflict, match="there is nothing of yours to undo"):
        undo.undo(conn, Author("Per", "viewer"))
    with pytest.raises(undo.Conflict, match="something else has been changed since"):
        undo.undo(conn, kari, by_kari)       # the page showed an older change than her latest

    # Ola divides the stretch his route shares with Kari's; Kari then changes hers. Taking his back would
    # lose hers: nothing is done, and he is told what came after.
    shared = conn.execute("""SELECT l.edge_id::text FROM core.route_leg l JOIN core.route_leg o USING (edge_id)
                             WHERE l.route_id = %s AND o.route_id = %s""", (a, c)).fetchone()[0]
    lon, lat = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p
                               FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                               WHERE e.id = %s) x""", (shared,)).fetchone()
    act(conn, lambda: routes.split(conn, shared, ola, lon, lat))
    leg = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq LIMIT 1", (c,)).fetchone()[0]
    act(conn, lambda: routes.remove_leg(conn, c, routes.detail(conn, c)["version"], leg, kari))
    assert undo.last(conn, undo.whose(ola))["in_the_way"] == "a stretch taken out of bre14"
    before = state(conn)
    with pytest.raises(undo.Conflict, match="a stretch taken out of bre14 came after.*it would be lost"):
        act(conn, lambda: undo.undo(conn, ola))
    assert state(conn) == before
    # Kari takes hers back, and then he can.
    assert act(conn, lambda: undo.undo(conn, kari))["undone"] == "a stretch taken out of bre14"
    assert act(conn, lambda: undo.undo(conn, ola))["undone"] == "a stretch divided"

    # A route withdrawn: the row is changed before anything says whose change it is. It is hers all the same.
    act(conn, lambda: routes.retire(conn, c, kari))
    assert conn.execute("SELECT count(*) FROM core.undo_log WHERE txid = %s AND author IS NULL",
                        (undo.last(conn)["txid"],)).fetchone()[0] >= 1
    mine = undo.last(conn, undo.whose(kari))
    assert (mine["what"], mine["author"], mine["through"]) == ("bre14 withdrawn", "Kari", "viewer")
    assert undo.last(conn, undo.whose(ola))["what"] != "bre14 withdrawn"
    assert act(conn, lambda: undo.undo(conn, kari))["undone"] == "bre14 withdrawn"
    assert routes.detail(conn, c)["code"] == "bre14"


def test_what_was_taken_back_is_brought_back_until_something_else_is_changed(conn):
    """A row of changes that went wrong: all of them taken back in one go, and brought back one by one."""
    from stiflyt2.api.auth import Author

    ola, kari = Author("Ola Nordmann", "viewer"), Author("Kari Nordmann", "viewer")
    mine = undo.whose(ola)
    a = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), ola))

    def change(who=ola, **what):
        act(conn, lambda: routes.update(conn, a, routes.detail(conn, a)["version"], who, **what))

    states = [state(conn)]
    for what in ({"name": "One"}, {"grading": "B"}, {"name": "Three"}):
        change(**what)
        states.append(state(conn))
    listed = undo.recent(conn, mine)
    assert [(c["what"], c["with"], c["in_the_way"]) for c in listed] == [
        ("bre18 renamed", 1, None), ("the grading of bre18 changed", 2, None), ("bre18 renamed", 3, None),
        ("bre18 made yours", 4, None)]
    assert undo.recent(conn, undo.whose(kari)) == [] and undo.undone_last(conn, mine) is None
    # All of it is his item of work: he has not saved since he began. Others can see that he is at it.
    work = undo.item(conn, mine)
    assert [c["txid"] for c in work["changes"]] == [c["txid"] for c in listed] and work["in_the_way"] is None
    assert [r["code"] for r in work["routes"]] == ["bre18"]
    assert undo.item(conn, undo.whose(kari)) == {"changes": [], "in_the_way": None, "routes": []}
    assert [(o["changes"], [r["code"] for r in o["routes"]]) for o in undo.open_items(conn, undo.whose(kari))
            if o["author"] == "Ola Nordmann"] == [(4, ["bre18"])]
    assert not [o for o in undo.open_items(conn, mine) if o["author"] == "Ola Nordmann"]
    with pytest.raises(undo.Conflict, match="there is nothing to bring back"):
        undo.redo(conn, ola)
    with pytest.raises(undo.Conflict, match="not among your latest"):
        undo.back_to(conn, kari, listed[2]["txid"])

    # Back to before the first of the three, in one go.
    told = act(conn, lambda: undo.back_to(conn, ola, listed[2]["txid"]))
    assert told["undone"] == ["bre18 renamed", "the grading of bre18 changed", "bre18 renamed"]
    assert state(conn) == states[0] and told["next"]["txid"] == listed[3]["txid"]
    assert (told["again"]["txid"], told["again"]["what"]) == (listed[2]["txid"], "bre18 renamed")
    # Bringing back is for the one who took it back.
    with pytest.raises(undo.Conflict, match="there is nothing to bring back"):
        undo.redo(conn, kari)
    with pytest.raises(undo.Conflict, match="something else has been changed since"):
        undo.redo(conn, ola, listed[0]["txid"])
    # One by one they come back, in the order they were made.
    for i in (1, 2, 3):
        back = act(conn, lambda: undo.redo(conn, ola))
        assert state(conn) == states[i]
    assert back["again"] is None and back["next"]["txid"] == listed[0]["txid"]
    assert [c["what"] for c in undo.recent(conn, mine)] == [c["what"] for c in listed]
    assert [h["type"] for h in routes.detail(conn, a)["history"][:3]] == ["redone", "redone", "redone"]

    # One taken back, and something else changed: that stands in its place, and there is nothing to bring back.
    act(conn, lambda: undo.undo(conn, ola))
    assert undo.undone_last(conn, mine)["what"] == "bre18 renamed"
    change(name="Another")
    assert undo.undone_last(conn, mine) is None
    with pytest.raises(undo.Conflict, match="until you change something else"):
        undo.redo(conn, ola)

    # Taken back, and somebody else changes the same route before it is brought back: not over that.
    act(conn, lambda: undo.undo(conn, ola))
    change(kari, name="Hers")
    assert undo.undone_last(conn, mine)["in_the_way"] == "bre18 renamed came after it was taken back"
    before = state(conn)
    with pytest.raises(undo.Conflict, match="bre18 renamed came after it was taken back"):
        act(conn, lambda: undo.redo(conn, ola))
    assert state(conn) == before and routes.detail(conn, a)["name"] == "Hers"

    # She takes hers back, and he could bring his back again. But then the route itself is taken back, by
    # somebody else: what he took back was a change to a route that is no longer there.
    act(conn, lambda: undo.undo(conn, kari))
    assert undo.undone_last(conn, mine)["in_the_way"] is None
    while (latest := undo.last(conn)) and latest["txid"] >= listed[3]["txid"]:
        act(conn, lambda: undo.undo(conn, AUTHOR))
    assert undo.undone_last(conn, mine)["in_the_way"] == "the grading of bre18 changed was taken back after it"
    with pytest.raises(undo.Conflict, match="was taken back after it, and touches the same"):
        act(conn, lambda: undo.redo(conn, ola))


def test_an_item_of_work_is_saved_or_discarded_as_a_whole(conn):
    """What a person has changed since they last saved: said to stay, or all of it taken back in one go."""
    from stiflyt2.api.auth import Author

    per, assistant, mine = Author("Per Nordmann", "viewer"), Author("Per Nordmann", "mcp"), ("Per Nordmann", "viewer")
    a = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), per))

    def change(who=per, **what):
        act(conn, lambda: routes.update(conn, a, routes.detail(conn, a)["version"], who, **what))

    # Saved: the item is closed and named after what it changed, and the next change opens another.
    assert act(conn, lambda: undo.save(conn, per)) == {"saved": 1, "title": "bre18: 1 change"}
    assert conn.execute("SELECT title, state, through FROM core.changeset WHERE author = 'Per Nordmann' "
                        "ORDER BY created_at DESC LIMIT 1").fetchone() == ("bre18: 1 change", "closed", "viewer")
    assert undo.item(conn, mine)["changes"] == [] and act(conn, lambda: undo.save(conn, per)) == {"saved": 0}
    saved = state(conn)
    change(name="One")
    change(grading="B")
    work = undo.item(conn, mine)
    assert [c["what"] for c in work["changes"]] == ["the grading of bre18 changed", "bre18 renamed"]
    assert [r["code"] for r in work["routes"]] == ["bre18"] and work["in_the_way"] is None

    # An assistant acting as him has an item of its own. What it changes on top stands in the way of his discarding.
    change(assistant, name="By the assistant")
    assert [c["what"] for c in undo.item(conn, ("Per Nordmann", "mcp"))["changes"]] == ["bre18 renamed"]
    assert len(undo.item(conn, mine)["changes"]) == 2 and undo.item(conn, mine)["in_the_way"] == "bre18 renamed"
    with pytest.raises(undo.Conflict, match="bre18 renamed came after.*cannot be discarded as a whole"):
        act(conn, lambda: undo.discard(conn, per))
    act(conn, lambda: undo.undo(conn, assistant))

    # Discarded: all of it is taken back in one go, and the route is as it was saved.
    told = act(conn, lambda: undo.discard(conn, per))
    assert told["undone"] == ["the grading of bre18 changed", "bre18 renamed"] and state(conn) == saved
    assert undo.item(conn, mine)["changes"] == []
    with pytest.raises(undo.Conflict, match="nothing that is not saved"):
        undo.discard(conn, per)
    # What is brought back is in the item again, not saved; and saved, it is not his to discard any more.
    act(conn, lambda: undo.redo(conn, per))
    assert [c["what"] for c in undo.item(conn, mine)["changes"]] == ["bre18 renamed"]
    assert act(conn, lambda: undo.save(conn, per))["saved"] == 1 and undo.item(conn, mine)["changes"] == []
    assert routes.detail(conn, a)["name"] == "One"
