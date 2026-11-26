"""Repairs proposed for a person to approve: each is tried first, and nothing is kept until approved."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import places, proposals, routes

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def about(conn, route):
    return [p for p in proposals.proposals(conn, "bre") if p["action"].get("route") == route]


def test_a_short_extra_end_is_proposed_taken_out_and_a_long_one_is_not(conn):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    assert about(conn, route) == []     # one line already: nothing to propose

    def spur(metres):
        fork = next(n["id"] for n in routes.detail(conn, route)["nodes"]
                    if n["id"] not in {e["id"] for e in routes.detail(conn, route)["ends"]})
        end = routes._node_at(conn, conn.execute(
            "SELECT ST_Translate(geom, %s, 0) FROM core.node WHERE id = %s", (metres, fork)).fetchone()[0])
        edge = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(a.geom, b.geom) FROM core.node a,
                                                   core.node b WHERE a.id = %(a)s AND b.id = %(b)s)""",
                                {"a": fork, "b": end}, fork, end, "trail", "drawn", True, True, season="summer")
        routes._add_legs(conn, route, [edge])
        return edge

    with conn.transaction(force_rollback=True):
        spur(400)
        assert routes.detail(conn, route)["shape"] == "branched"
        assert about(conn, route) == []     # 400 m is not a slip of the pen: left for a person

    edge = spur(40)
    events = conn.execute("SELECT count(*) FROM history.event").fetchone()[0]
    offered = about(conn, route)
    assert [(p["kind"], p["text"], p["metres"]) for p in offered] == [
        ("twig", "Take the 40 m end out of bre18", 40)]
    # Working the proposal out tried it, and kept nothing.
    assert routes.detail(conn, route)["shape"] == "branched"
    assert conn.execute("SELECT count(*) FROM history.event").fetchone()[0] == events

    assert proposals.approve(conn, AUTHOR, offered[0]["action"]) == route
    after = routes.detail(conn, route)
    assert after["shape"] == "line" and after["reviewed"] is False     # repaired is not the same as done
    assert conn.execute("SELECT status FROM core.edge WHERE id = %s", (edge,)).fetchone() == ("active",)
    assert about(conn, route) == []
    with pytest.raises(places.Conflict, match="no longer applies"):
        proposals.approve(conn, AUTHOR, offered[0]["action"])


def test_pieces_lying_close_are_proposed_joined_and_a_loop_taken_out(conn, monkeypatch):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    before = routes.detail(conn, route)
    # Taking a stretch out of the middle leaves two pieces, as far apart as the stretch was long.
    middle, metres = conn.execute("""
        SELECT e.id::text, ST_Length(r.geom) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s AND NOT r.a_node = ANY(%s::uuid[]) AND NOT r.b_node = ANY(%s::uuid[])
        ORDER BY ST_Length(r.geom) LIMIT 1""",
        (route, *[[e["id"] for e in routes.detail(conn, route)["ends"]]] * 2)).fetchone()
    routes.remove_leg(conn, route, routes.detail(conn, route)["version"], middle, AUTHOR)
    apart = routes.detail(conn, route)
    assert apart["shape"] == "pieces" and apart["gaps"][0]["metres"] <= metres

    # The stretch that was taken out still runs between the two ends: the proposal is to carry
    # the route on along it, not to draw a straight link beside it.
    # However far apart they lie: the stretch is there, and nothing is drawn.
    monkeypatch.setattr(proposals, "GAP_M", apart["gaps"][0]["metres"] - 1)
    with conn.transaction(force_rollback=True):
        offered = about(conn, route)
        assert [p["kind"] for p in offered] == ["gap"]
        assert "joined along the stretch of" in offered[0]["text"]
        proposals.approve(conn, AUTHOR, offered[0]["action"])
        along = routes.detail(conn, route)
        assert along["shape"] == "line" and along["length"]["on_foot_m"] == before["length"]["on_foot_m"]
        assert conn.execute("""SELECT count(*) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                               JOIN core.edge_revision r ON r.id = e.current_revision_id
                               WHERE l.route_id = %s AND r.kind = 'connector'""", (route,)).fetchone() == (0,)

    # With no stretch between the ends (here: the segment is gone from Turrutebasen's index too),
    # they are joined by a straight link on foot.
    conn.execute("UPDATE core.edge SET status = 'retired' WHERE id = %s", (middle,))
    monkeypatch.setattr(proposals, "_between", lambda *a: None)
    assert about(conn, route) == []     # too far apart to join by a straight link unseen
    monkeypatch.setattr(proposals, "GAP_M", apart["gaps"][0]["metres"] + 1)
    offered = about(conn, route)
    assert [p["kind"] for p in offered] == ["gap"] and "joined on foot across" in offered[0]["text"]
    proposals.approve(conn, AUTHOR, offered[0]["action"])
    joined = routes.detail(conn, route)
    assert joined["shape"] == "line" and joined["length"]["legs"] == 4

    # A few metres that begin and end at the same junction.
    end = joined["ends"][0]["id"]
    loop = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ARRAY[n.geom, ST_Translate(n.geom, 2, 0), n.geom])
                                             FROM core.node n WHERE n.id = %(n)s)""", {"n": end},
                            end, end, "trail", "adopted", True, True, season="summer")
    routes._add_legs(conn, route, [loop])
    offered = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "loop"]
    assert [p["action"] for p in offered] == [{"kind": "loop", "edge": loop}]
    assert "begin and end at the same junction out of bre18" in offered[0]["text"]
    proposals.approve(conn, AUTHOR, offered[0]["action"])
    assert routes.detail(conn, route)["length"]["legs"] == 4
    assert not [p for p in proposals.proposals(conn, "bre") if p["kind"] == "loop"]


def test_a_route_one_stretch_short_of_the_meeting_point_and_a_hut_at_its_end(conn):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    made = routes.detail(conn, route)
    # An end beside a hut that is no place yet: the proposal is to make the hut a place there.
    named = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "place" and "bre18" in p["label"]]
    beside = [p for p in named if "named after the hut" in p["text"]]
    assert beside and beside[0]["text"].startswith("Make ") and " m away" in beside[0]["text"]
    # The other end has no hut: it is named after what does lie there.
    assert all(p["text"].startswith("Make ") and ": named after the " in p["text"] for p in named)
    proposals.approve(conn, AUTHOR, beside[0]["action"])
    at = next(e for e in routes.detail(conn, route)["ends"] if e["id"] == beside[0]["action"]["node"])
    assert at["place"] and not [p for p in proposals.proposals(conn, "bre")
                                if p["kind"] == "place" and p["action"] == beside[0]["action"]]

    # The route taken back one stretch from that end: it now ends at another arrival point of
    # the place, and the stretch on to where the place's routes meet is still there.
    last, inner = conn.execute("""
        SELECT e.id::text, CASE WHEN r.a_node = %(n)s THEN r.b_node ELSE r.a_node END::text
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %(r)s AND %(n)s IN (r.a_node, r.b_node)""", {"r": route, "n": at["id"]}).fetchone()
    conn.execute("UPDATE core.node SET place_id = (SELECT place_id FROM core.node WHERE id = %s) WHERE id = %s",
                 (at["id"], inner))
    routes.remove_leg(conn, route, routes.detail(conn, route)["version"], last, AUTHOR)
    offered = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "reach"]
    assert [p["action"] for p in offered] == [{"kind": "reach", "route": route, "edge": last}]
    assert offered[0]["text"].startswith(f"Carry bre18 on to where the routes of {at['place']} meet")
    proposals.approve(conn, AUTHOR, offered[0]["action"])
    again = routes.detail(conn, route)
    assert again["length"]["legs"] == made["length"]["legs"] and again["shape"] == "line"
    assert not [p for p in proposals.proposals(conn, "bre") if p["kind"] == "reach"]


def test_an_arrival_point_that_routes_only_pass_is_proposed_taken_out_of_the_place(conn):
    """Left from when a place was made from where Turrutebasen's routes ended: the mess at Gjendesheim."""
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    made = routes.detail(conn, route)
    place = places.at_node(conn, made["ends"][0]["id"], AUTHOR, "Enden")
    # A junction in the middle of a stretch, where the route passes and nothing ends, made an arrival point.
    longest, lon, lat = conn.execute("""
        SELECT e.id::text, ST_X(m.p), ST_Y(m.p) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route,)).fetchone()
    passed = routes.split(conn, longest, AUTHOR, lon, lat)
    places.take_in(conn, place, passed, AUTHOR)

    offered = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "passed"]
    assert [(p["label"], p["action"]["node"]) for p in offered] == [("Enden", passed)]
    assert offered[0]["text"].endswith("out of the place: bre18 only passes it")
    proposals.approve(conn, AUTHOR, offered[0]["action"])
    assert conn.execute("SELECT place_id FROM core.node WHERE id = %s", (passed,)).fetchone() == (None,)
    assert not [p for p in proposals.proposals(conn, "bre") if p["kind"] == "passed"]
    # The point where the route ends is what the place is for: never proposed.
    assert places.detail(conn, place)["access_points"][0]["is_primary"]


def test_where_a_route_steps_onto_the_ice_a_place_is_proposed_that_a_sign_can_point_to(conn, monkeypatch):
    """jot64 over Fannaråkbreen: the edge of the glacier on each side is a place, named after the glacier."""
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    made = routes.detail(conn, route)
    ends = {e["id"] for e in made["ends"]}
    # A stretch in the middle of the route, neither end of it an end of the route, said to be glacier.
    ice, a, b = conn.execute("""
        SELECT e.id::text, r.a_node::text, r.b_node::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s AND NOT r.a_node = ANY(%s::uuid[]) AND NOT r.b_node = ANY(%s::uuid[])
        ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route, list(ends), list(ends))).fetchone()
    assert places.glacier_edge(conn, a) is None                 # no ice there yet
    routes.set_stretch(conn, ice, AUTHOR, kind="glacier")
    # The fixtures have no glacier in Stedsnavn: let whatever is named nearest the stretch stand in for one.
    kind, name = conn.execute("""
        SELECT s.type, s.name FROM source_index.place s, core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = %s AND NOT s.alternative ORDER BY s.geom <-> r.geom LIMIT 1""", (ice,)).fetchone()
    assert places.glacier_edge(conn, a) is None                 # nothing called a glacier lies near: a person names it
    monkeypatch.setattr(places, "GLACIER_TYPES", (kind,))
    monkeypatch.setattr(places, "GLACIER_NAMED_M", 50_000)

    one, other = places.glacier_edge(conn, a), places.glacier_edge(conn, b)
    assert one["kind"] == other["kind"] == "glacier_edge" and one["name"] != other["name"]
    sides = {one["name"].removeprefix(f"{name} brekant "), other["name"].removeprefix(f"{name} brekant ")}
    assert sides in ({"nord", "sør"}, {"øst", "vest"})         # the two edges of one crossing, told apart
    offered = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "place" and "onto the ice" in p["text"]]
    assert {p["action"]["node"] for p in offered} == {a, b}
    assert offered[0]["text"].startswith(f"Make {name} brekant ") and offered[0]["text"].endswith(
        "a place where bre18 steps onto the ice: somewhere a sign can point to")

    # Approved, it is a place of its own kind; named by hand, it is the edge of a glacier all the same.
    proposals.approve(conn, AUTHOR, next(p["action"] for p in offered if p["action"]["node"] == a))
    made_a = places.detail(conn, conn.execute("SELECT place_id::text FROM core.node WHERE id = %s", (a,)).fetchone()[0])
    assert (made_a["name"], made_a["kind"]) == (one["name"], "glacier_edge")
    by_hand = places.detail(conn, places.at_node(conn, b, AUTHOR, "Brekanten"))
    assert (by_hand["name"], by_hand["kind"]) == ("Brekanten", "glacier_edge")
    assert not [p for p in proposals.proposals(conn, "bre") if p["kind"] == "place" and "onto the ice" in p["text"]]
