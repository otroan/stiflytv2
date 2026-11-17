"""Ids that hold: what came instead of a thing, and what it was called before."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import identity, places, routes, undo

AUTHOR = "Test Editor"


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


def act(conn, what):
    with conn.transaction():
        return what()


def version(conn, route):
    return routes.detail(conn, route)["version"]


def test_a_joined_junction_leads_to_the_one_it_was_joined_to_until_that_is_undone(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    a, b = conn.execute("""
        SELECT r.a_node::text, r.b_node::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) LIMIT 1""", (route,)).fetchone()
    assert identity.current(conn, "node", a)["state"] == "current"
    act(conn, lambda: routes.merge_node(conn, a, b, AUTHOR))
    assert identity.current(conn, "node", a) == {
        "type": "node", "id": a, "state": "replaced", "now": [b], "how": "joined"}
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert identity.current(conn, "node", a)["state"] == "current"
    assert conn.execute("SELECT count(*) FROM core.succession WHERE old_id = %s", (a,)).fetchone() == (0,)


def test_a_divided_stretch_leads_to_all_its_parts_and_a_person_has_to_choose(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))

    def longest():
        return conn.execute("""
            SELECT e.id::text, ST_X(m.p), ST_Y(m.p) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
            JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p) m
            WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route,)).fetchone()

    whole, lon, lat = longest()
    act(conn, lambda: routes.split(conn, whole, AUTHOR, lon, lat))
    once = identity.current(conn, "edge", whole)
    assert once["state"] == "divided" and once["how"] == "divided" and len(once["now"]) == 2
    # One of the halves divided again: the first stretch now stands for three.
    half, lon, lat = longest()
    assert half in once["now"]
    act(conn, lambda: routes.split(conn, half, AUTHOR, lon, lat))
    twice = identity.current(conn, "edge", whole)
    assert len(twice["now"]) == 3 and half not in twice["now"]
    assert identity.current(conn, "edge", half)["state"] == "divided"


def test_a_route_taken_into_another_is_found_by_its_id_and_by_its_code(conn):
    one = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    other = act(conn, lambda: routes.make(conn, candidate(conn, "bre19"), AUTHOR))
    act(conn, lambda: routes.add_piece(conn, one, version(conn, one), candidate(conn, "bre19"), AUTHOR))
    assert identity.current(conn, "route", other) == {
        "type": "route", "id": other, "state": "replaced", "now": [one], "how": "absorbed"}
    found = identity.lookup(conn, "BRE19")
    assert [(f["id"], f["label"], f["known_as"]) for f in found] == [(one, "bre18", "earlier")]
    # Withdrawn with nothing instead: the id is known, and stands for nothing.
    act(conn, lambda: routes.retire(conn, one, AUTHOR))
    assert identity.current(conn, "route", one)["state"] == "gone"
    assert identity.current(conn, "route", other)["state"] == "gone"
    with pytest.raises(places.NotFound):
        identity.current(conn, "route", "not-an-id")


def test_a_route_and_a_place_are_found_by_what_they_were_called_before(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    act(conn, lambda: routes.update(conn, route, version(conn, route), AUTHOR, name="Over fjellet"))
    act(conn, lambda: routes.update(conn, route, version(conn, route), AUTHOR, code="bre118", name="Over heia"))
    assert [(f["id"], f["label"], f["known_as"]) for f in identity.lookup(conn, "bre118")] == [(route, "bre118", "now")]
    for old, kind in (("bre18", "code"), ("over fjellet", "name")):
        found = identity.lookup(conn, old)
        assert [(f["id"], f["label"], f["known_as"], f["kind"]) for f in found] == [(route, "bre118", "earlier", kind)]
    assert [(e["kind"], e["value"]) for e in routes.detail(conn, route)["earlier"]] == [
        ("name", "Over fjellet"), ("code", "bre18")]
    # The old code given to another route: that route is what the code means now, the first is still found.
    again = act(conn, lambda: routes.lay_out(conn, AUTHOR, [
        (e["lon"], e["lat"]) for e in routes.detail(conn, route)["ends"]], "bre18"))
    assert [(f["id"], f["known_as"]) for f in identity.lookup(conn, "bre18")] == [
        (again, "now"), (route, "earlier")]

    end = routes.detail(conn, route)["ends"][0]
    place = act(conn, lambda: places.at_node(conn, end["id"], AUTHOR, "Enden"))
    act(conn, lambda: places.update(conn, place, places.detail(conn, place)["version"], AUTHOR, name="Endehytta"))
    assert [(f["type"], f["id"], f["label"], f["known_as"]) for f in identity.lookup(conn, "enden")] == [
        ("place", place, "Endehytta", "earlier")]
    assert [e["value"] for e in places.detail(conn, place)["earlier"]] == ["Enden"]
    # Renaming taken back: the place was never called anything else.
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert identity.lookup(conn, "Endehytta") == [] and places.detail(conn, place)["earlier"] == []
