"""Owned places: adopting a proposed anchor and editing the place (ADR-002)."""
import psycopg
import pytest
from conftest import TEST_DB, role_dsn

from stiflyt2.domain import places, proposals
from stiflyt2.index import build

AUTHOR = "Test Editor"
ARENTZBU = "place:799507"
SPRONGDALSHYTTA = "place:27269"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_adopting_an_anchor_makes_a_place_with_connected_access_points(conn):
    spots = one(conn, "SELECT spots FROM source_index.anchor WHERE id = %s", ARENTZBU)
    earlier = (places.changeset_status(conn, AUTHOR) or {"changes": 0})["changes"]
    place = places.detail(conn, places.adopt(conn, ARENTZBU, AUTHOR))
    assert (place["name"], place["kind"], place["adopted_from"]) == ("Arentzbu", "hut", ARENTZBU)

    # One access point per spot where routes end; the nearest to the hut is primary.
    points = place["access_points"]
    assert len(points) == spots and [p["is_primary"] for p in points] == [True] + [False] * (spots - 1)
    assert points[0]["from_reference_m"] == min(p["from_reference_m"] for p in points)
    assert {"bre18", "bre19", "bre20"} <= {code for p in points for code in p["routes"]}
    # Every other access point is joined to the primary one, so the place is connected.
    assert len(place["connectors"]) == spots - 1
    assert all(c["origin"] == "derived" and c["length_m"] > 0 for c in place["connectors"])

    # Recorded: where it came from, as values, and the event in the author's changeset.
    assert conn.execute("""SELECT source_key, external_id, snapshot_id IS NOT NULL
                           FROM core.source_reference WHERE subject_type = 'place'
                             AND subject_id = %s""", (place["id"],)).fetchall() == [
        ("stedsnavn", "799507", True)]
    assert [(e["type"], e["author"]) for e in place["history"]] == [("place.adopted", AUTHOR)]
    assert places.changeset_status(conn, AUTHOR)["changes"] == earlier + 1

    with pytest.raises(places.Conflict, match="already been adopted"):
        places.adopt(conn, ARENTZBU, AUTHOR)
    with pytest.raises(places.NotFound):
        places.adopt(conn, "place:0", AUTHOR)


def test_a_place_can_be_edited_and_every_change_is_an_event(conn):
    place_id = places.adopt(conn, ARENTZBU, AUTHOR)
    place = places.detail(conn, place_id)

    places.update(conn, place_id, place["version"], AUTHOR, name="Arentzbu turisthytte",
                  kind="other", lon=place["lon"] + 0.001, lat=place["lat"])
    with pytest.raises(places.Conflict, match="changed since"):  # an outdated version
        places.update(conn, place_id, place["version"], AUTHOR, name="Stale")
    with pytest.raises(places.Invalid):
        places.update(conn, place_id, place["version"] + 1, AUTHOR, name="  ")

    # A new access point is connected at once; it can be made primary, and the
    # old primary can then be taken out of the place.
    places.add_access_point(conn, place_id, place["lon"] + 0.002, place["lat"], AUTHOR)
    place = places.detail(conn, place_id)
    added = place["access_points"][-1]
    old_primary = place["primary_node_id"]
    assert not added["is_primary"] and len(place["connectors"]) == len(place["access_points"]) - 1
    with pytest.raises(places.Invalid, match="another primary"):
        places.remove_access_point(conn, place_id, str(old_primary), AUTHOR)
    places.update(conn, place_id, place["version"], AUTHOR, primary_node_id=str(added["id"]))
    places.remove_access_point(conn, place_id, str(old_primary), AUTHOR)

    place = places.detail(conn, place_id)
    # The others were joined to the access point that was taken out. They are
    # joined to the new primary instead: a place is never left in pieces.
    assert len(place["access_points"]) >= 2
    assert len(place["connectors"]) == len(place["access_points"]) - 1
    assert place["name"] == "Arentzbu turisthytte" and place["kind"] == "other"
    assert place["primary_node_id"] == added["id"]
    assert old_primary not in [p["id"] for p in place["access_points"]]
    # The removed node is still in the graph, just no longer part of the place.
    assert conn.execute("SELECT place_id, status FROM core.node WHERE id = %s",
                        (old_primary,)).fetchone() == (None, "active")
    assert [e["type"] for e in reversed(place["history"])] == [
        "place.adopted", "place.renamed", "place.kind_changed", "place.moved",
        "place.access_point_added", "place.primary_changed", "place.access_point_removed"]
    assert 40 < place["history"][3]["payload"]["metres"] < 70  # 0.001 degrees east at 61.6 N

    # Withdrawing the place makes its anchor a proposal again.
    places.retire(conn, place_id, AUTHOR)
    with pytest.raises(places.NotFound):
        places.detail(conn, place_id)
    assert places.adopt(conn, ARENTZBU, AUTHOR) != place_id


def test_rebuilding_the_index_leaves_owned_data_untouched(loaded):
    def owned(c):
        return [c.execute(f"SELECT coalesce(md5(string_agg(t::text, '' ORDER BY t::text)), '') "
                          f"FROM {table} t").fetchone()[0]
                for table in ("core.place", "core.node", "core.edge", "core.edge_revision",
                              "core.source_reference", "history.event")]

    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        place_id = places.adopt(c, SPRONGDALSHYTTA, AUTHOR)
        c.commit()
        before = owned(c)
        try:
            with psycopg.connect(role_dsn("stiflyt2_import")) as importer:
                for table in ("anchor", "route_end", "route_shape", "place"):
                    importer.execute(f"DROP TABLE source_index.{table}")
            assert places.detail(c, place_id)["name"] == "Sprongdalshytta"  # no index needed
            build.build(force=True)
            assert owned(c) == before
            # The event log cannot be rewritten, not even by the owner of the tables.
            with psycopg.connect(f"dbname={TEST_DB}") as owner:
                owner.execute("SET ROLE stiflyt2_owner")
                with pytest.raises(psycopg.Error) as refused:
                    owner.execute("UPDATE history.event SET author = 'someone else'")
                assert refused.value.sqlstate == "23001"
        finally:
            c.rollback()
            places.retire(c, place_id, AUTHOR)
            c.commit()


def test_places_are_made_by_default_and_follow_their_proposals(conn, monkeypatch):
    named = one(conn, "SELECT count(*) FROM source_index.anchor WHERE named")
    first = places.make_default(conn)
    # Proposals whose place a person withdrew earlier (other tests do) are left alone.
    made, left = first["made"], first["left withdrawn"]
    assert made >= 2 and made + left == named
    assert conn.execute("""SELECT count(*), bool_or(reviewed) FROM core.place
                           WHERE status = 'confirmed'""").fetchone() == (made, False)
    rest = {"made": 0, "made again": 0, "withdrawn": 0, "left withdrawn": left}
    assert places.make_default(conn) == {**rest, "kept": made}  # nothing has changed

    def place_of(anchor):
        return str(one(conn, """SELECT p.id FROM core.source_reference sr
                                JOIN core.place p ON p.id = sr.subject_id AND p.status = 'confirmed'
                                WHERE sr.subject_type = 'place' AND sr.detail->>'anchor_id' = %s""",
                       anchor))

    looked_at, untouched = [a for (a,) in conn.execute(
        """SELECT sr.detail->>'anchor_id' FROM core.source_reference sr
           JOIN core.place p ON p.id = sr.subject_id AND p.status = 'confirmed'
           WHERE sr.subject_type = 'place' ORDER BY 1 LIMIT 2""")]
    # A person finds one place right. Then both proposals change upstream.
    reviewed = place_of(looked_at)
    places.review(conn, reviewed, places.detail(conn, reviewed)["version"], AUTHOR)
    real = places.fingerprints
    monkeypatch.setattr(places, "fingerprints", lambda c, anchor_id=None: {
        a: f + "-changed" if a in (looked_at, untouched) else f
        for a, f in real(c, anchor_id).items()})
    before = place_of(untouched)
    # The unreviewed place follows its proposal; the reviewed one is left alone.
    assert places.make_default(conn) == {**rest, "made again": 1, "kept": made - 1}
    assert place_of(looked_at) == reviewed and place_of(untouched) != before
    assert places.detail(conn, reviewed)["reviewed"] is True

    # A place a person withdraws is not made again behind their back.
    places.retire(conn, place_of(untouched), AUTHOR)
    assert places.make_default(conn) == {**rest, "kept": made - 1, "left withdrawn": left + 1}
    # Everything made by default is in the automatic author's own, closed, changesets.
    assert conn.execute("SELECT count(*) FROM core.changeset WHERE author = %s AND state = 'open'",
                        (places.AUTOMATIC,)).fetchone() == (0,)


def test_a_junction_or_dead_end_becomes_a_place_under_a_name_given_by_hand(conn):
    # Such an anchor has only the nearest place name, which may not be what the spot is called.
    anchor, nearest = conn.execute(
        "SELECT id, name FROM source_index.anchor WHERE NOT named ORDER BY id LIMIT 1").fetchone()
    place = places.detail(conn, places.adopt(conn, anchor, AUTHOR, "  Valdresflya vandrerhjem "))
    assert (place["name"], place["kind"], place["reviewed"]) == ("Valdresflya vandrerhjem", "other", True)
    assert place["name"] != nearest and len(place["access_points"]) >= 1


def test_a_link_that_a_route_walks_is_not_retired_with_the_access_point(conn):
    """Routes join loose ends with a short link of the same kind as a place's own links."""
    from stiflyt2.domain import routes

    place_id = places.adopt(conn, ARENTZBU, AUTHOR)
    place = places.detail(conn, place_id)
    node = places.add_access_point(conn, place_id, place["lon"] + 0.002, place["lat"], AUTHOR)
    primary = conn.execute("SELECT primary_node_id FROM core.place WHERE id = %s", (place_id,)).fetchone()[0]
    route = str(conn.execute("INSERT INTO core.route (code, code_provisional, reviewed) "
                             "VALUES ('link-test', false, false) RETURNING id").fetchone()[0])
    walked = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(a.geom, b.geom) FROM core.node a, core.node b
                                               WHERE a.id = %(a)s AND b.id = %(b)s)""",
                              {"a": str(node), "b": str(primary)}, str(node), str(primary),
                              "connector", "drawn", None, None)
    routes._add_legs(conn, route, [walked])
    places.remove_access_point(conn, place_id, str(node), AUTHOR)
    assert conn.execute("SELECT status FROM core.edge WHERE id = %s", (walked,)).fetchone() == ("active",)


def test_a_route_end_beside_a_hut_becomes_a_place_named_after_the_hut(conn):
    """For a hut no route ended at before: the index proposes no anchor there."""
    hut = conn.execute("""SELECT s.name, ST_Translate(s.geom, 40, 0) FROM source_index.place s
                          WHERE s.place_id = %s AND NOT s.alternative LIMIT 1""", (int(ARENTZBU.split(":")[1]),)).fetchone()
    node = str(conn.execute("INSERT INTO core.node (geom) VALUES (%s) RETURNING id", (hut[1],)).fetchone()[0])
    assert places.name_near(conn, node)["name"] == hut[0]
    place = places.detail(conn, places.at_node(conn, node, AUTHOR))
    assert (place["name"], place["kind"], place["reviewed"]) == (hut[0], "hut", True)
    assert conn.execute("SELECT place_id::text, (SELECT primary_node_id::text FROM core.place WHERE id = n.place_id) "
                        "FROM core.node n WHERE n.id = %s", (node,)).fetchone() == (str(place["id"]), node)
    with pytest.raises(places.Conflict, match="already an arrival point"):
        places.at_node(conn, node, AUTHOR)
    # Far from any hut a name has to be given.
    far = str(conn.execute("INSERT INTO core.node (geom) VALUES (ST_Translate(%s::geometry, 5000, 5000)) "
                           "RETURNING id", (hut[1],)).fetchone()[0])
    with pytest.raises(places.Invalid, match="needs a name"):
        places.at_node(conn, far, AUTHOR)
    assert places.detail(conn, places.at_node(conn, far, AUTHOR, "Keisarpasset"))["kind"] == "other"


def test_an_end_is_named_after_what_lies_there_when_no_hut_does(conn):
    """A summit, a farm, a bridge; else the road it ends on. One place per name: a second end is an arrival point."""
    kinds = [k[0] for k in places.NAMED_AFTER]
    # A named thing with no hut and no other such thing around it, and a spot 40 m from it.
    name, stedsnavn_id, word, spot = conn.execute("""
        SELECT s.name, s.place_id, k.word, ST_Translate(s.geom, 40, 0)
        FROM source_index.place s JOIN unnest(%(kinds)s::text[], %(words)s::text[]) AS k(type, word) ON k.type = s.type
        WHERE NOT s.alternative AND NOT EXISTS (
            SELECT 1 FROM source_index.place o WHERE o.place_id <> s.place_id AND ST_DWithin(o.geom, s.geom, 500)
              AND (o.type = ANY(%(kinds)s) OR o.type = 'turisthytte'))
          AND NOT EXISTS (SELECT 1 FROM core.place p WHERE lower(p.name) = lower(s.name))
        ORDER BY s.place_id, s.name LIMIT 1""", {"kinds": kinds, "words": [k[2] for k in places.NAMED_AFTER]}).fetchone()
    node = str(conn.execute("INSERT INTO core.node (geom) VALUES (%s) RETURNING id", (spot,)).fetchone()[0])
    after = places.suggest(conn, node)
    assert (after["name"], after["kind"], after["what"], after["metres"]) == (name, "other", f"the {word}", 40)
    place = places.detail(conn, places.at_node(conn, node, AUTHOR))
    assert (place["name"], place["kind"]) == (name, "other")
    assert conn.execute("""SELECT external_id FROM core.source_reference WHERE subject_type = 'place'
                           AND subject_id = %s AND source_key = 'stedsnavn'""", (place["id"],)).fetchone() == (
        str(stedsnavn_id),)

    # Another end beside the same thing: not a second place of that name, but one more way in to the first.
    other = str(conn.execute("INSERT INTO core.node (geom) VALUES (ST_Translate(%s::geometry, 0, 30)) RETURNING id",
                             (spot,)).fetchone()[0])
    assert places._named(conn, places.suggest(conn, other)["name"], stedsnavn_id) == str(place["id"])
    with pytest.raises(places.Conflict, match=f"already a place called {name}"):
        places.at_node(conn, other, AUTHOR)
    proposals.approve(conn, AUTHOR, {"kind": "arrival", "node": other, "place": str(place["id"])})
    assert one(conn, "SELECT place_id::text FROM core.node WHERE id = %s", other) == str(place["id"])
    assert one(conn, """SELECT count(*) FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                        WHERE e.status = 'active' AND r.kind = 'connector' AND %s::uuid IN (r.a_node, r.b_node)""",
               other) == 1
    with pytest.raises(places.Conflict, match="already an arrival point"):
        places.take_in(conn, str(place["id"]), other, AUTHOR)

    # On a road with a name and nothing else named there: the road gives the name.
    road, at = conn.execute("""
        SELECT r.name, ST_LineInterpolatePoint(ST_GeometryN(r.geom, 1), 0.5) FROM source_index.road r
        WHERE r.name IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM source_index.place o WHERE ST_DWithin(o.geom, r.geom, 600)
              AND (o.type = ANY(%(kinds)s) OR o.type = 'turisthytte'))
        ORDER BY r.id LIMIT 1""", {"kinds": kinds}).fetchone()
    end = str(conn.execute("INSERT INTO core.node (geom) VALUES (%s) RETURNING id", (at,)).fetchone()[0])
    named = places.suggest(conn, end)
    assert named["what"] == "the road" and named["name"].startswith(road) and named["stedsnavn_id"] is None


def test_two_places_that_are_one_are_made_one_and_what_lies_round_a_place_is_listed(conn):
    """A hut with an arrival point of its own that no route reaches, and a place made by hand where the route ends."""
    from stiflyt2.domain import identity, routes

    route = routes.make(conn, one(conn, "SELECT id FROM source_index.route WHERE label = 'bre18'"), AUTHOR)
    end = routes.detail(conn, route)["ends"][0]
    x, y = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s", (end["id"],)).fetchone()
    beside = routes._node_at(conn, f"SRID=25833;POINT({x + 120:.2f} {y:.2f})")
    hut = places.at_node(conn, beside, AUTHOR, "Hytta ved veien")

    # Round the hut: the route ends 120 m off, at no arrival point of it.
    found = places.around(conn, hut)
    there = next(e for e in found["ends"] if e["id"] == end["id"])
    assert (there["metres"], there["place"], there["routes"]) == (
        120, None, [{"id": route, "code": "bre18", "version": routes.detail(conn, route)["version"], "ends_here": True}])
    # Somebody makes a place of that end, by the same name: now the hut is there twice.
    by_hand = places.at_node(conn, end["id"], AUTHOR, "Hytta ved veien")
    found = places.around(conn, hut)
    assert [(o["metres"], o["same_name"], o["access_points"]) for o in found["places"] if o["id"] == by_hand] == [(120, True, 1)]
    assert next(e for e in found["ends"] if e["id"] == end["id"])["place"] == "Hytta ved veien"

    # Made one: the hut stays, and the junction where the route ends is an arrival point of it.
    places.merge(conn, hut, places.detail(conn, hut)["version"], by_hand, AUTHOR)
    now = places.detail(conn, hut)
    assert {str(a["id"]) for a in now["access_points"]} == {beside, end["id"]} and len(now["connectors"]) == 1
    assert str(next(a for a in now["access_points"] if a["is_primary"])["id"]) == beside
    # Each arrival point says which of your routes come there, as they are now.
    assert {str(a["id"]): a["routes"] for a in now["access_points"]} == {beside: [], end["id"]: ["bre18"]}
    assert now["history"][0]["type"] == "place.merged" and now["history"][0]["payload"]["taken_in"] == by_hand
    assert one(conn, "SELECT status FROM core.place WHERE id = %s", by_hand) == "retired"
    assert identity.current(conn, "place", by_hand)["now"] == [hut]
    assert routes.detail(conn, route)["ends"][0]["place"] == "Hytta ved veien"
    after = places.around(conn, hut)
    assert by_hand not in {o["id"] for o in after["places"]} and end["id"] not in {e["id"] for e in after["ends"]}
    # A person withdrew it into the other: the proposal it may have come from is not made a place again.
    assert conn.execute("SELECT payload FROM history.event WHERE subject_id = %s AND type = 'place.merged_into'",
                        (by_hand,)).fetchone()[0] == {"into": hut}

    with pytest.raises(places.Invalid, match="the same place"):
        places.merge(conn, hut, now["version"], hut, AUTHOR)
    with pytest.raises(places.NotFound):
        places.merge(conn, hut, now["version"], by_hand, AUTHOR)
    with pytest.raises(places.Conflict, match="changed since"):
        places.merge(conn, hut, now["version"] - 1, places.at_node(conn, routes.detail(conn, route)["ends"][1]["id"], AUTHOR, "Andre enden"), AUTHOR)
