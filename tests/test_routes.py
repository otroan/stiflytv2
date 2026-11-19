"""Owned routes: making a candidate ours, sharing edges, joining pieces with a crossing (ADR-003)."""
import psycopg
import pytest
from conftest import FIXTURES, later_delivery, role_dsn

from stiflyt2.domain import places, proposals, routes
from stiflyt2.index import build

AUTHOR = "Test Editor"
FAR = "7883e674-3f55-4805-bf36-9352e87cc3b0"  # a segment at the far end of bre19


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_making_a_route_turns_its_segments_into_shared_edges(conn):
    route_id = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    assert [e["place"] for e in routes.detail(conn, route_id)["ends"]] == [None, None]
    # The huts become places afterwards. They take over the nodes the route
    # already ends at, so the route arrives at them all the same.
    huts = [places.adopt(conn, hut, AUTHOR) for hut in ("place:799507", "place:27269")]
    route = routes.detail(conn, route_id)
    assert (route["code"], route["code_provisional"], route["shape"]) == ("bre18", False, "line")
    assert route["length"]["legs"] == 4 and 15_000 < route["length"]["on_foot_m"] < 16_000
    assert route["length"]["water_m"] == 0
    # It runs between two places, joins their access points, and is named after them.
    assert sorted(e["place"] for e in route["ends"]) == ["Arentzbu", "Sprongdalshytta"]
    assert route["generated_name"] in ("Arentzbu – Sprongdalshytta", "Sprongdalshytta – Arentzbu")
    # Asked what it is called, where routes are chosen between: the name its ends give, until it is given one.
    assert routes.called(conn, [route_id, "00000000-0000-0000-0000-000000000000"]) == {route_id: route["generated_name"]}
    assert route["sources"][0]["code"] == "bre18" and route["gaps"] == []
    # Who maintains each stretch comes along, and adds up to the route's length.
    assert [m["name"] for m in route["maintainers"]] == ["DNT | DNT Oslo og omegn"]
    assert abs(route["maintainers"][0]["length_m"] - route["length"]["on_foot_m"]) < 3

    # bre20 uses two of the same segments: those edges are shared, not copied.
    edges_before = conn.execute("SELECT count(*) FROM core.edge").fetchone()[0]
    other = routes.detail(conn, routes.make(conn, candidate(conn, "bre20"), AUTHOR))
    shared = conn.execute("""SELECT count(*) FROM core.route_leg a JOIN core.route_leg b USING (edge_id)
                             WHERE a.route_id = %s AND b.route_id = %s""",
                          (route["id"], other["id"])).fetchone()[0]
    assert shared == 2
    assert conn.execute("SELECT count(*) FROM core.edge").fetchone()[0] == (
        edges_before + other["length"]["legs"] - shared)
    # Each edge carries what Turrutebasen says about marking, and where it came from.
    assert conn.execute("""SELECT bool_and(r.marked), count(sr.id) = count(*)
                           FROM core.route_leg l JOIN core.edge_revision r ON r.edge_id = l.edge_id
                           LEFT JOIN core.source_reference sr ON sr.subject_type = 'edge'
                                AND sr.subject_id = l.edge_id AND sr.source_key = 'turrutebasen'
                           WHERE l.route_id = %s""", (route["id"],)).fetchone() == (True, True)

    with pytest.raises(places.Conflict, match="already part of"):
        routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    with pytest.raises(places.Conflict, match="already used"):
        routes.make(conn, candidate(conn, "bre19"), AUTHOR, code="BRE18")

    # The code is ours: it can be changed, and a hand-written name overrides the generated one.
    routes.update(conn, route["id"], route["version"], AUTHOR, code="b-18", name="Over Sprongdalen")
    route = routes.detail(conn, route["id"])
    assert (route["code"], route["name"]) == ("b-18", "Over Sprongdalen")
    assert [e["type"] for e in reversed(route["history"])] == [
        "route.made", "route.code_changed", "route.renamed"]
    # Withdrawing a place leaves the nodes that routes run through in the graph.
    places.retire(conn, huts[0], AUTHOR)
    route = routes.detail(conn, route["id"])
    assert route["shape"] == "line" and sorted(str(e["place"]) for e in route["ends"]) == [
        "None", "Sprongdalshytta"]
    routes.retire(conn, route["id"], AUTHOR)
    assert routes.owner_of(conn, candidate(conn, "bre18")) is None


def test_pieces_are_joined_by_a_crossing_that_is_not_walked(loaded, tmp_path, small):
    from stiflyt2.importer.adapters import turrutebasen

    # Upstream uses the code bre18 for a second stretch, far from the first.
    turrutebasen.run(later_delivery(
        FIXTURES / "turrutebasen.zip", tmp_path,
        f"UPDATE fotruteinfo_tabell SET rutenummer = 'bre18' "
        f"WHERE fotrute_fk = '{FAR}' AND rutenummer = 'bre49'"))
    build.build()
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            parts = [r[0] for r in conn.execute(
                "SELECT id FROM source_index.route WHERE code = 'bre18' ORDER BY segment_count DESC")]
            route = routes.detail(conn, routes.make(conn, parts[0], AUTHOR))
            # A part label is only a temporary code until someone assigns a real one.
            assert route["code"].startswith("bre18/") and route["code_provisional"] is True
            walked = route["length"]["on_foot_m"]

            routes.add_piece(conn, route["id"], route["version"], parts[1], AUTHOR)
            route = routes.detail(conn, route["id"])
            assert (route["shape"], route["pieces"], len(route["gaps"])) == ("pieces", 2, 1)
            gap = route["gaps"][0]
            assert gap["metres"] > 1000

            # Joined over water: one route again, and the water is not walking distance.
            routes.add_crossing(conn, route["id"], route["version"], gap["from_node"],
                                gap["to_node"], "water", AUTHOR)
            route = routes.detail(conn, route["id"])
            assert (route["shape"], route["pieces"], route["gaps"]) == ("line", 1, [])
            assert abs(route["length"]["water_m"] - gap["metres"]) < 30
            assert walked < route["length"]["on_foot_m"] < walked + 2500  # only the second stretch
            assert conn.execute("""SELECT r.kind, r.marked, r.origin FROM core.edge_revision r
                                   JOIN core.route_leg l ON l.edge_id = r.edge_id
                                   WHERE l.route_id = %s AND r.kind <> 'trail'""",
                                (route["id"],)).fetchall() == [("water", False, "drawn")]

            # A glacier is walked and signed, but never marked.
            with pytest.raises(places.Invalid):
                routes.add_crossing(conn, route["id"], route["version"], gap["from_node"],
                                    gap["from_node"], "glacier", AUTHOR)
            ends = [e["id"] for e in route["ends"]]
            routes.add_crossing(conn, route["id"], route["version"], ends[0], ends[1], "glacier", AUTHOR)
            route = routes.detail(conn, route["id"])
            assert route["shape"] == "loop" and route["length"]["glacier_m"] > 0
            assert route["length"]["unmarked_m"] == route["length"]["glacier_m"]
            assert conn.execute("""SELECT r.marked, r.signed FROM core.edge_revision r
                                   WHERE r.kind = 'glacier'""").fetchall() == [(False, True)]
        finally:
            conn.rollback()


def test_a_segment_turrutebasen_records_as_water_is_not_walked(loaded, tmp_path, small):
    from stiflyt2.importer.adapters import turrutebasen

    boat = "ce4908aa-d039-413e-996d-96b4440a1dd0"  # 2.1 km, used by bre20 only
    turrutebasen.run(later_delivery(
        FIXTURES / "turrutebasen.zip", tmp_path,
        f"UPDATE fotrute_senterlinje SET rutefolger = 'VV', merking = 'NEI' WHERE lokalid = '{boat}'"))
    build.build()
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            whole = conn.execute("SELECT round(length_m) FROM source_index.route WHERE label = 'bre20'"
                                 ).fetchone()[0]
            route = routes.detail(conn, routes.make(conn, candidate(conn, "bre20"), AUTHOR))
            # Made yours, the boat stretch is a water crossing from the start.
            assert 2000 < route["length"]["water_m"] < 2200
            assert abs(route["length"]["on_foot_m"] + route["length"]["water_m"] - whole) < 3
            assert route["shape"] == "line" and route["length"]["unmarked_m"] == 0
            assert conn.execute("""SELECT r.kind, r.origin FROM core.edge_revision r
                                   JOIN core.source_reference sr ON sr.subject_type = 'edge'
                                        AND sr.subject_id = r.edge_id
                                   WHERE sr.external_id = %s""", (boat,)).fetchall() == [
                ("water", "adopted")]
        finally:
            conn.rollback()


def test_clean_candidates_are_made_yours_in_bulk_and_listed(conn):
    lines = conn.execute("""SELECT count(*) FROM source_index.route r
                            JOIN source_index.route_shape s ON s.route_id = r.id
                            WHERE r.area_prefix = 'bre' AND s.shape = 'line'""").fetchone()[0]
    from stiflyt2.domain import worklist

    before = worklist.worklist(conn, "bre")
    assert before["clean_candidates"] + before["withdrawn_candidates"] == lines
    assert before["your_routes"] == 0

    done = routes.make_default(conn, "bre")
    # Candidates whose route a person withdrew earlier (other tests do) are left alone.
    made, left = done["made"], done["left withdrawn"]
    assert made >= 3 and made + left == lines and done["kept"] == done["code taken"] == 0
    assert routes.make_default(conn, "bre")["kept"] == made  # nothing to do the second time
    # Made in bulk, so not looked at yet, by the automatic author.
    assert conn.execute("""SELECT count(*), bool_or(reviewed) FROM core.route
                           WHERE status = 'active'""").fetchone() == (made, False)

    after = worklist.worklist(conn, "bre")
    assert (after["clean_candidates"], after["withdrawn_candidates"]) == (0, left)
    assert (after["your_routes"], after["reviewed_routes"]) == (made, 0)
    # The list says what is wrong: here, routes that do not run between two places.
    assert {i["problem"] for i in after["items"]} <= {"shape", "network", "name", "geometry", "place",
                                                      "code", "end"}
    assert any(i["problem"] == "name" and i["about"] == "route" for i in after["items"])

    # The report adds up the same area: lengths over shared ground, who maintains it, the network.
    from stiflyt2.domain import report

    r = report.report(conn, "bre")
    assert (r["routes"]["yours"], r["routes"]["reviewed"]) == (made, 0)
    assert 0 < r["length"]["on_foot_m"] <= r["length"]["routes_on_foot_m"]
    assert sum(m["metres"] for m in r["maintainers"]) >= r["length"]["on_foot_m"] - len(r["maintainers"])
    assert r["network"]["parts"] >= 1 and 0 < r["network"]["largest_share"] <= 1
    assert sum(r["to_do"].values()) == len(r["items"]) == len(after["items"])
    # One list of every route of the area, yours or not, with what is the matter with each.
    assert len(r["all"]) == r["routes"]["candidates"]
    assert sum(row["kind"] == "yours" for row in r["all"]) == made
    assert all(row["problems"] for row in r["all"] if row["kind"] == "candidate")
    unnamed = next(i for i in r["items"] if i["about"] == "route" and i["problem"] == "name")
    assert unnamed["text"] in next(row for row in r["all"] if row["id"] == unnamed["id"])["problems"]
    text = report.as_text(r)
    assert f"{made} routes are yours" in text and "Network" in text

    # A person looks at one; a person withdraws another, which is then left alone.
    first, second = [str(r) for (r,) in conn.execute(
        "SELECT id FROM core.route WHERE status = 'active' ORDER BY code LIMIT 2")]
    routes.review(conn, first, routes.detail(conn, first)["version"], AUTHOR)
    routes.retire(conn, second, AUTHOR)
    assert routes.make_default(conn, "bre") == {"made": 0, "kept": made - 1,
                                                 "left withdrawn": left + 1, "code taken": 0}
    assert routes.detail(conn, first)["reviewed"] is True
    routes.review(conn, first, routes.detail(conn, first)["version"], AUTHOR, done=False)
    assert routes.detail(conn, first)["reviewed"] is False


def test_a_stretch_can_be_called_a_glacier_and_snapped_to_the_mapped_path(loaded, monkeypatch):
    import httpx
    from conftest import HTTP_CLIENT
    from test_fkb_wfs import Server

    from stiflyt2.importer.adapters import fkb_wfs

    stretch = "dab5141a-4bd5-4539-a3f4-72bffcf4c964"  # 189 m of bre18
    with psycopg.connect(role_dsn("stiflyt2_import")) as c:
        # A mapped path that runs 8 m beside the stretch, in two pieces that meet.
        beside = [f"{x} {y}" for x, y in c.execute(
            """SELECT round((ST_X(d.geom) + 8)::numeric, 2), round(ST_Y(d.geom)::numeric, 2)
               FROM source_index.trail_segment t, ST_DumpPoints(t.geom) d
               WHERE t.id = %s ORDER BY d.path""", (stretch,))]
        box = c.execute("""SELECT floor(ST_XMin(geom) - 500)::int, floor(ST_YMin(geom) - 500)::int,
                                  ceil(ST_XMax(geom) + 500)::int, ceil(ST_YMax(geom) + 500)::int
                           FROM source_index.trail_segment WHERE id = %s""", (stretch,)).fetchone()
    half = len(beside) // 2
    server, real = Server(), HTTP_CLIENT
    server.features = [(" ".join(beside[:half + 1]), "sti"), (" ".join(beside[half:]), "sti")]
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    fkb_wfs.run("beside", tuple(box))
    build.build()

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
            edge = str(conn.execute("""SELECT s.subject_id FROM core.source_reference s
                                       JOIN core.edge e ON e.id = s.subject_id AND e.status = 'active'
                                       WHERE s.subject_type = 'edge' AND s.external_id = %s""",
                                    (stretch,)).fetchone()[0])

            # Redrawn through one clicked point, following: both halves take the mapped path,
            # and the clicked point, 3 m off it, is taken to mean the path. A dry run changes nothing.
            lon, lat, away_lon, away_lat, a_node = conn.execute("""
                SELECT ST_X(m.p), ST_Y(m.p), ST_X(m.q), ST_Y(m.q), r.a_node::text
                FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
                     LATERAL (SELECT ST_LineInterpolatePoint(r.geom, 0.5) AS mid) x,
                     LATERAL (SELECT ST_Transform(ST_Translate(x.mid, 11, 0), 4326) AS p,
                                     ST_Transform(ST_Translate(x.mid, 150, 0), 4326) AS q) m
                WHERE e.id = %s""", (edge,)).fetchone()
            shown = routes.redraw(conn, edge, AUTHOR, [(lon, lat)], follow=True, dry_run=True)
            assert (shown["parts"], shown["followed"]) == (2, 2)
            assert abs(shown["length_m"] - shown["was_m"]) < 25
            assert len(shown["geometry"]["coordinates"]) > 4
            assert conn.execute("SELECT count(*) FROM core.edge_revision WHERE edge_id = %s",
                                (edge,)).fetchone() == (1,)
            # A point far from any mapped path is kept as clicked, with straight lines to it.
            free = routes.redraw(conn, edge, AUTHOR, [(away_lon, away_lat)], follow=True, dry_run=True)
            assert free["followed"] == 0 and len(free["geometry"]["coordinates"]) == 3
            # A junction moved to a spot near the mapped path lands on the path.
            with conn.transaction(force_rollback=True):
                at = conn.execute("""SELECT ST_X(p.g), ST_Y(p.g) FROM core.node n, LATERAL (
                                         SELECT ST_Transform(ST_Translate(n.geom, 14, 5), 4326) AS g) p
                                     WHERE n.id = %s""", (a_node,)).fetchone()
                assert routes.move_node(conn, a_node, AUTHOR, *at, snap_m=20)["snapped"] is True
                assert conn.execute("""SELECT min(ST_Distance(n.geom, m.geom)) < 0.01
                                       FROM core.node n, source_index.path m WHERE n.id = %s""",
                                    (a_node,)).fetchone()[0]

            # The line pulled 8 m, onto the mapped path: near the grab it follows the path, and
            # the ends stay. Pulled into terrain with no path, it runs straight to the spot.
            with conn.transaction(force_rollback=True):
                grab, onto, off = conn.execute("""
                    SELECT ARRAY[ST_X(m.g), ST_Y(m.g)], ARRAY[ST_X(m.p), ST_Y(m.p)], ARRAY[ST_X(m.q), ST_Y(m.q)]
                    FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id,
                         LATERAL (SELECT ST_LineInterpolatePoint(r.geom, 0.5) AS mid,
                                         ST_Azimuth(ST_LineInterpolatePoint(r.geom, 0.45),
                                                    ST_LineInterpolatePoint(r.geom, 0.55)) + pi() / 2 AS az) x,
                         LATERAL (SELECT ST_Transform(x.mid, 4326) AS g,
                                         ST_Transform(ST_Translate(x.mid, 8, 0), 4326) AS p,
                                         ST_Transform(ST_Translate(x.mid, 60 * sin(x.az), 60 * cos(x.az)),
                                                      4326) AS q) m
                    WHERE e.id = %s""", (edge,)).fetchone()
                pulled = routes.bend(conn, edge, AUTHOR, grab, onto)
                assert pulled["followed"] is True and 7 < pulled["pulled_m"] < 9
                on_path, same_ends = conn.execute("""
                    SELECT (SELECT min(ST_Distance(ST_LineInterpolatePoint(r.geom, 0.5), m.geom))
                            FROM source_index.path m) < 0.5,
                           ST_Distance(ST_StartPoint(r.geom), a.geom) + ST_Distance(ST_EndPoint(r.geom), b.geom) < 0.01
                    FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                    JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
                    WHERE e.id = %s""", (edge,)).fetchone()
                assert on_path and same_ends
                routes.restore(conn, edge, pulled["undo_rev"], AUTHOR)
                assert conn.execute("""SELECT ST_Equals(r.geom, o.geom) FROM core.edge e
                                       JOIN core.edge_revision r ON r.id = e.current_revision_id
                                       JOIN core.edge_revision o ON o.edge_id = e.id AND o.rev_no = 1
                                       WHERE e.id = %s""", (edge,)).fetchone()[0]
                away = routes.bend(conn, edge, AUTHOR, grab, off)
                assert away["followed"] is False and 59 < away["pulled_m"] < 61
                assert away["length_m"] > away["was_m"]

            # Where Turrutebasen says a segment follows a road or track, laying it on the mapped
            # line is proposed, with the line it would get. Approved, it is done as a snap.
            with conn.transaction(force_rollback=True):
                from stiflyt2.domain import proposals

                follows = conn.execute("SELECT attrs->>'rutefolger' FROM source_index.trail_segment "
                                       "WHERE id = %s", (stretch,)).fetchone()[0]
                monkeypatch.setattr(proposals, "ALONG_A_WAY", (follows,))
                offered = [p for p in proposals.proposals(conn, "bre") if p["action"] == {
                    "kind": "snap", "edge": edge}]
                assert len(offered) == 1 and offered[0]["moves_m"] == 8
                assert offered[0]["geometry"]["type"] == "LineString"
                assert conn.execute("SELECT count(*) FROM core.edge_revision WHERE edge_id = %s",
                                    (edge,)).fetchone() == (1,)
                proposals.approve(conn, AUTHOR, offered[0]["action"])
                assert conn.execute("""SELECT r.rev_no, r.origin FROM core.edge e
                                       JOIN core.edge_revision r ON r.id = e.current_revision_id
                                       WHERE e.id = %s""", (edge,)).fetchone() == (2, "edited")
                assert not [p for p in proposals.proposals(conn, "bre") if p["kind"] == "snap"]

            # Snapped: the line now follows the mapped path, 8 m to the side, between the same ends.
            preview = routes.snap_preview(conn, edge)
            assert abs(preview["length_m"] - preview["was_m"]) < 25
            assert conn.execute("SELECT count(*) FROM core.edge_revision WHERE edge_id = %s",
                                (edge,)).fetchone() == (1,)  # a preview changes nothing
            routes.snap(conn, edge, AUTHOR)
            rev_no, origin, offset, same_ends = conn.execute(
                """SELECT r.rev_no, r.origin,
                          ST_Distance(ST_LineInterpolatePoint(r.geom, 0.5), o.geom),
                          ST_Distance(ST_StartPoint(r.geom), a.geom) < 0.1
                          AND ST_Distance(ST_EndPoint(r.geom), b.geom) < 0.1
                   FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                   JOIN core.edge_revision o ON o.edge_id = e.id AND o.rev_no = 1
                   JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
                   WHERE e.id = %s""", (edge,)).fetchone()
            assert (rev_no, origin, same_ends) == (2, "edited", True) and 6 < offset < 10
            assert conn.execute("""SELECT count(*) FROM core.source_reference
                                   WHERE subject_id = %s AND ref_type = 'traced'""",
                                (edge,)).fetchone()[0] == 2  # both pieces of the mapped path
            assert routes.detail(conn, route["id"])["shape"] == "line"
            with pytest.raises(places.Conflict, match="already follows"):
                routes.snap(conn, edge, AUTHOR)

            # Called a glacier: walked and signed, never marked. It stays in the walking distance.
            walked = routes.detail(conn, route["id"])["length"]["on_foot_m"]
            routes.set_stretch(conn, edge, AUTHOR, kind="glacier")
            length = routes.detail(conn, route["id"])["length"]
            assert length["on_foot_m"] == walked and length["glacier_m"] == length["unmarked_m"] > 150
            assert conn.execute("""SELECT r.kind, r.marked, r.signed, r.rev_no FROM core.edge e
                                   JOIN core.edge_revision r ON r.id = e.current_revision_id
                                   WHERE e.id = %s""", (edge,)).fetchone() == ("glacier", False, True, 3)
            with pytest.raises(places.Invalid, match="no mapped path to follow"):
                routes.snap(conn, edge, AUTHOR)

            # Taken out of the route, the stretch is gone from it but stays in the graph.
            routes.remove_leg(conn, route["id"], routes.detail(conn, route["id"])["version"], edge, AUTHOR)
            assert routes.detail(conn, route["id"])["length"]["legs"] == route["length"]["legs"] - 1
            assert conn.execute("SELECT status FROM core.edge WHERE id = %s", (edge,)).fetchone() == (
                "active",)
        finally:
            conn.rollback()


def test_a_stretch_is_redrawn_a_junction_moved_and_a_route_carried_on(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    assert len(route["nodes"]) == 5 and route["shape"] == "line"
    edge, b, mid_lon, mid_lat, far_lon, far_lat = conn.execute("""
        SELECT e.id::text, r.b_node::text,
               ST_X(m.p), ST_Y(m.p), ST_X(m.q), ST_Y(m.q)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id,
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.3), 40, 0), 4326) AS p,
                             ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.7), 40, 0), 4326) AS q) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route["id"],)).fetchone()

    def line(edge_id):
        return conn.execute("""
            SELECT r.rev_no, r.origin, ST_NPoints(r.geom),
                   ST_Distance(ST_StartPoint(r.geom), a.geom) + ST_Distance(ST_EndPoint(r.geom), b.geom)
            FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
            JOIN core.node a ON a.id = r.a_node JOIN core.node b ON b.id = r.b_node
            WHERE e.id = %s""", (edge_id,)).fetchone()

    # Redrawn through two points given from the far end: the line is turned to run a to b.
    done = routes.redraw(conn, edge, AUTHOR, [(far_lon, far_lat), (mid_lon, mid_lat)])
    rev_no, origin, points, off_ends = line(edge)
    assert (rev_no, origin, points) == (2, "edited", 4) and off_ends < 0.01
    assert done["length_m"] != done["was_m"]
    assert conn.execute("""
        SELECT ST_LineLocatePoint(r.geom, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833))
               < ST_LineLocatePoint(r.geom, ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 4326), 25833))
        FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE e.id = %s""", (mid_lon, mid_lat, far_lon, far_lat, edge)).fetchone()[0]
    with pytest.raises(places.Invalid, match="far from the stretch"):
        routes.redraw(conn, edge, AUTHOR, [(mid_lon + 1, mid_lat)])

    # A junction moved 30 m: the stretches meeting there follow, and the route is still one line.
    lon, lat, meeting = conn.execute("""
        SELECT ST_X(p.g), ST_Y(p.g), (SELECT count(*) FROM core.edge e
                                      JOIN core.edge_revision r ON r.id = e.current_revision_id
                                      WHERE e.status = 'active' AND n.id IN (r.a_node, r.b_node))
        FROM core.node n, LATERAL (SELECT ST_Transform(ST_Translate(n.geom, 30, 0), 4326) AS g) p
        WHERE n.id = %s""", (b,)).fetchone()
    assert routes.move_node(conn, b, AUTHOR, lon, lat, dry_run=True) == {"outcome": "move"}
    moved = routes.move_node(conn, b, AUTHOR, lon, lat)
    assert (moved["moved_m"], moved["stretches"], moved["outcome"]) == (30.0, meeting, "move")
    # Moved back to where it was, as undo does.
    assert routes.move_node(conn, b, AUTHOR, *moved["from"])["moved_m"] == 30.0
    routes.move_node(conn, b, AUTHOR, lon, lat)
    assert line(edge)[0] == 5 and line(edge)[3] < 0.01
    assert routes.detail(conn, route["id"])["shape"] == "line"
    with pytest.raises(places.Invalid, match="at most 500 m"):
        routes.move_node(conn, b, AUTHOR, lon + 0.1, lat)

    # The earlier line of a stretch is given back, as undo does, unless its ends have moved since.
    with pytest.raises(places.Conflict, match="no longer fits"):
        routes.restore(conn, edge, 1, AUTHOR)
    routes.restore(conn, edge, 3, AUTHOR)
    assert line(edge)[0] == 6
    # A route keeps its candidate when a stretch is taken out of it, so it is not offered again.
    assert str(routes.owner_of(conn, candidate(conn, "bre18"))) == str(route["id"])

    # Carried on along a Turrutebasen segment that touches its end: the segment becomes a stretch.
    onward = conn.execute("""
        SELECT t.id FROM source_index.trail_segment t, core.node n
        WHERE n.id = ANY(%s::uuid[]) AND ST_DWithin(t.geom, n.geom, 1) AND NOT EXISTS (
            SELECT 1 FROM core.source_reference sr JOIN core.route_leg l ON l.edge_id = sr.subject_id
            WHERE sr.subject_type = 'edge' AND sr.external_id = t.id AND l.route_id = %s)
        ORDER BY t.id LIMIT 1""", ([e["id"] for e in route["ends"] if e["id"] != b],
                                   route["id"])).fetchone()[0]
    version = routes.detail(conn, route["id"])["version"]
    routes.add_stretch(conn, route["id"], version, AUTHOR, segment_id=onward)
    longer = routes.detail(conn, route["id"])
    # A change does not make a route done: only a person saying so does.
    assert longer["length"]["legs"] == route["length"]["legs"] + 1 and longer["reviewed"] is False
    with pytest.raises(places.Conflict, match="already part"):
        routes.add_stretch(conn, route["id"], longer["version"], AUTHOR, segment_id=onward)
    with pytest.raises(places.Invalid, match="does not touch"):
        routes.add_stretch(conn, route["id"], longer["version"], AUTHOR, segment_id=FAR)
    routes.remove_leg(conn, route["id"], longer["version"], routes._segment_edges(
        conn, AUTHOR, "t.id = %(id)s", onward)[0], AUTHOR)
    assert str(routes.owner_of(conn, candidate(conn, "bre18"))) == str(route["id"])

    # A junction dropped on another is joined to it. The stretch that ran between the two has
    # nowhere left to go and leaves the route; the others now meet at the junction that stays.
    before = routes.detail(conn, route["id"])
    gone, stays, metres = conn.execute("""
        SELECT r.b_node::text, r.a_node::text, ST_Length(r.geom)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) LIMIT 1""", (route["id"],)).fetchone()
    assert metres < 500
    target = conn.execute("""SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326))
                             FROM core.node WHERE id = %s""", (stays,)).fetchone()
    assert routes.move_node(conn, gone, AUTHOR, *target, snap_m=5, dry_run=True)["outcome"] == "join"
    joined = routes.move_node(conn, gone, AUTHOR, *target, snap_m=5)
    assert (joined["outcome"], joined["dropped"]) == ("join", 1)
    after = routes.detail(conn, route["id"])
    assert after["length"]["legs"] == before["length"]["legs"] - 1 and after["shape"] == "line"
    assert gone not in {n["id"] for n in after["nodes"]} and stays in {n["id"] for n in after["nodes"]}
    assert conn.execute("SELECT status FROM core.node WHERE id = %s", (gone,)).fetchone() == ("retired",)

    # A spur makes a third end. Each end is told with its twig, the stretches in to the fork,
    # so that the end that does not belong can be taken out in one go.
    fork = next(n["id"] for n in after["nodes"] if n["id"] not in {e["id"] for e in after["ends"]})
    spur = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(n.geom, ST_Translate(n.geom, 20, 20))
                                             FROM core.node n WHERE n.id = %(n)s)""", {"n": fork},
                            fork, routes._node_at(conn, conn.execute(
                                "SELECT ST_Translate(geom, 20, 20) FROM core.node WHERE id = %s",
                                (fork,)).fetchone()[0]), "trail", "drawn", True, True)
    routes._add_legs(conn, route["id"], [spur])
    forked = routes.detail(conn, route["id"])
    assert forked["shape"] == "branched" and len(forked["twigs"]) == 3
    assert forked["twigs"][0]["edges"] == [spur] and forked["twigs"][0]["metres"] == 28
    routes.remove_leg(conn, route["id"], forked["version"], spur, AUTHOR)
    assert routes.detail(conn, route["id"])["twigs"] == []

    # A short loop inside one arrival point does not make the route look branched.
    end = after["ends"][0]["id"]
    loop = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ARRAY[n.geom, ST_Translate(n.geom, 2, 0), n.geom])
                                             FROM core.node n WHERE n.id = %(n)s)""", {"n": end},
                            end, end, "trail", "adopted", True, True)
    routes._add_legs(conn, route["id"], [loop])
    looped = routes.detail(conn, route["id"])
    assert looped["shape"] == "line" and len(looped["ends"]) == 2


def test_who_maintains_belongs_to_the_route_and_a_stretch_holds_only_an_exception(conn):
    from stiflyt2.domain import corrections

    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    route = made["id"]
    # Taken from Turrutebasen as it has it for this route, not for every route on the segments.
    theirs = conn.execute("""SELECT DISTINCT rs.maintainers FROM source_index.route_segment rs
                             JOIN source_index.route r ON r.id = rs.route_id WHERE r.label = 'bre18'""").fetchall()
    assert [tuple(made["maintained_by"])] == [tuple(sorted(m)) for (m,) in theirs] and made["exceptions"] == []
    assert not [c for c in corrections.corrections(conn, "bre") if c["change"] == "route attribute"
                and c["rutenummer"] == ["bre18"]]

    def now():
        return routes.detail(conn, route)

    # Said once, for the whole route.
    routes.update(conn, route, now()["version"], AUTHOR, maintainers=["Luster Turlag", " DNT Oslo "],
                  grading="B")
    after = now()
    assert after["maintained_by"] == ["DNT Oslo", "Luster Turlag"] and after["grading"] == "B"
    assert {m["name"]: m["length_m"] for m in after["maintainers"]} == {
        "DNT Oslo": after["length"]["on_foot_m"], "Luster Turlag": after["length"]["on_foot_m"]}
    with pytest.raises(places.Invalid, match="a grading is one of"):
        routes.update(conn, route, after["version"], AUTHOR, grading="X")

    # One stretch is someone else's. Saying the same as the route takes the exception away again.
    edge, metres = conn.execute(f"""
        SELECT l.edge_id::text, round({routes._LEN})::int FROM core.route_leg l
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY l.seq LIMIT 1""", (route,)).fetchone()
    routes.set_leg(conn, route, now()["version"], edge, AUTHOR, ["Statskog"])
    apart = now()
    assert apart["exceptions"] == [{"edge_id": edge, "maintainers": ["Statskog"], "metres": metres}]
    assert {m["name"]: m["length_m"] for m in apart["maintainers"]}["Statskog"] == metres
    routes.set_leg(conn, route, apart["version"], edge, AUTHOR, ["Luster Turlag", "DNT Oslo"])
    assert now()["exceptions"] == []

    # A new word for the route leaves an exception alone, unless it is to hold everywhere.
    # The exception also stays when other stretches are taken out of the route or added.
    routes.set_leg(conn, route, now()["version"], edge, AUTHOR, ["Statskog"])
    last = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s "
                        "ORDER BY seq DESC LIMIT 1", (route,)).fetchone()[0]
    routes.remove_leg(conn, route, now()["version"], last, AUTHOR)
    assert [x["edge_id"] for x in now()["exceptions"]] == [edge]
    routes.add_stretch(conn, route, now()["version"], AUTHOR, edge_id=last)
    assert [x["edge_id"] for x in now()["exceptions"]] == [edge]
    routes.update(conn, route, now()["version"], AUTHOR, maintainers=["Luster Turlag"])
    assert len(now()["exceptions"]) == 1
    routes.update(conn, route, now()["version"], AUTHOR, maintainers=["Luster Turlag"], everywhere=True)
    assert now()["exceptions"] == [] and now()["maintained_by"] == ["Luster Turlag"]

    # For Kartverket this is one change for the route, on all its segments, and one for the grading.
    told = {c["text"]: c for c in corrections.corrections(conn, "bre") if c["change"] == "route attribute"
            and c["rutenummer"] == ["bre18"]}
    who = next(c for text, c in told.items() if "vedlikeholdsansvarlig" in text)
    assert who["now"] == {"vedlikeholdsansvarlig": ["Luster Turlag"]} and "on all its segments" in who["text"]
    assert len(who["lokalids"]) == made["length"]["legs"] and who["geometry"]["type"].endswith("LineString")
    assert any("gradering" in text and text.endswith("to B") for text in told)

    # A route that says nothing is given what Turrutebasen has.
    conn.execute("UPDATE core.route SET maintainers = '{}' WHERE id = %s", (route,))
    assert routes.fill_metadata(conn) >= 1
    assert now()["maintained_by"] == made["maintained_by"]


def test_a_route_is_laid_out_between_points_over_what_exists(conn):
    from stiflyt2.domain import worklist

    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    ends = [(e["lon"], e["lat"]) for e in made["ends"]]

    def stretches():
        return conn.execute("SELECT count(*) FROM core.edge").fetchone()[0]

    # Between the two ends of a route of yours the way is that route: nothing new, and nothing kept.
    had = stretches()
    way = routes.plan(conn, ends)
    assert (way["stretches"], way["new"], way["divided"], way["segments"]) == (4, 0, 0, [])
    assert abs(way["length_m"] - made["length"]["on_foot_m"]) <= 2 and len(way["stops"]) == 2
    assert way["geometry"]["type"] == "MultiLineString" and stretches() == had

    # Made a route of its own, it shares the stretches, and belongs to the area its code names.
    twin = routes.detail(conn, routes.lay_out(conn, AUTHOR, ends, "bre18b", "Samme vei"))
    assert (twin["shape"], twin["length"]["legs"], twin["name"], twin["reviewed"]) == (
        "line", 4, "Samme vei", False)
    assert str(twin["id"]) in worklist.routes_of(conn, "bre") and stretches() == had
    assert twin["history"][0]["type"] == "route.made"
    with pytest.raises(places.Conflict, match="already used"):
        routes.lay_out(conn, AUTHOR, ends, "BRE18B")
    with pytest.raises(places.Invalid, match="nothing is mapped between points 1 and 2, and they lie 10[0-9] km apart"):
        routes.plan(conn, [ends[0], (ends[0][0] + 2, ends[0][1])])

    # A Turrutebasen segment that is nobody's stretch yet can be walked: the plan names it by its
    # own id, and it becomes a stretch only when the route is made.
    loose = conn.execute("""
        SELECT t.id, ST_X(a.p), ST_Y(a.p), ST_X(b.p), ST_Y(b.p)
        FROM source_index.trail_segment t,
             LATERAL (SELECT ST_Transform(ST_StartPoint(t.geom), 4326) AS p) a,
             LATERAL (SELECT ST_Transform(ST_EndPoint(t.geom), 4326) AS p) b
        WHERE GeometryType(t.geom) = 'LINESTRING' AND t.length_m > 50 AND NOT EXISTS (
            SELECT 1 FROM core.source_reference sr WHERE sr.subject_type = 'edge' AND sr.external_id = t.id)
        ORDER BY t.id LIMIT 1""").fetchone()
    if loose:
        over = routes.plan(conn, [loose[1:3], loose[3:5]])
        assert over["new"] >= 1 and loose[0] in over["segments"] and stretches() == had
        fresh = routes.detail(conn, routes.lay_out(conn, AUTHOR, [loose[1:3], loose[3:5]], "bre999"))
        assert fresh["shape"] == "line" and stretches() == had + over["new"]

    # Laid out again between one end and a junction inside it: the same route, on fewer stretches.
    inner = next(n for n in made["nodes"] if n["id"] not in {e["id"] for e in made["ends"]})
    routes.lay_out_again(conn, made["id"], routes.detail(conn, made["id"])["version"], AUTHOR,
                         [ends[0], (inner["lon"], inner["lat"])])
    shorter = routes.detail(conn, made["id"])
    assert shorter["code"] == "bre18" and shorter["shape"] == "line"
    assert 1 <= shorter["length"]["legs"] < 4 and shorter["maintained_by"] == made["maintained_by"]


def test_a_stretch_is_divided_where_a_route_is_to_stop_beside_it(conn):
    """A hut beside the path, far from any junction: the stretch is divided there."""
    from stiflyt2.domain import corrections

    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    edge, lokalid, metres, lon, lat = conn.execute(f"""
        SELECT e.id::text, sr.external_id, round({routes._LEN})::int, ST_X(m.p), ST_Y(m.p)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = e.id
             AND sr.source_key = 'turrutebasen',
             LATERAL (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.4), 60, 0), 4326) AS p) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (made["id"],)).fetchone()
    start = (made["ends"][0]["lon"], made["ends"][0]["lat"])

    # Planned, the stretch would be divided; nothing is, until a route is made.
    way = routes.plan(conn, [start, (lon, lat)])
    assert way["divided"] == 1 and way["stops"][1]["place"] is None
    assert routes.detail(conn, made["id"])["length"]["legs"] == 4

    spur = routes.detail(conn, routes.lay_out(conn, AUTHOR, [start, (lon, lat)], "bre18x"))
    assert spur["shape"] == "line" and spur["length"]["on_foot_m"] == way["length_m"]
    # The route the stretch belonged to walks both parts now, and is as long as it was.
    whole = routes.detail(conn, made["id"])
    assert whole["length"]["legs"] == 5 and whole["shape"] == "line"
    assert abs(whole["length"]["on_foot_m"] - made["length"]["on_foot_m"]) <= 1
    parts = conn.execute("""
        SELECT e.id::text, (sr.detail->>'part')::int, round(ST_Length(ST_Transform(r.geom, 4326)::geography))::int
        FROM core.source_reference sr JOIN core.edge e ON e.id = sr.subject_id AND e.status = 'active'
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE sr.subject_type = 'edge' AND sr.external_id = %s ORDER BY 2""", (lokalid,)).fetchall()
    assert [p[1] for p in parts] == [1, 2] and abs(parts[0][2] + parts[1][2] - metres) <= 1
    assert conn.execute("SELECT status FROM core.edge WHERE id = %s", (edge,)).fetchone() == ("retired",)
    # A candidate made yours later gets every part of a divided segment.
    assert set(routes._segment_edges(conn, AUTHOR, "t.id = %(id)s", lokalid)) == {p[0] for p in parts}

    # Divided and otherwise alike, the segment is still what Turrutebasen has: nothing to report.
    assert not [c for c in corrections.corrections(conn, "bre") if c["lokalid"] == lokalid]
    # One part called a glacier: now the two differ, and Kartverket is asked to divide it.
    routes.set_stretch(conn, parts[1][0], AUTHOR, kind="glacier")
    told = [c for c in corrections.corrections(conn, "bre") if c["lokalid"] == lokalid]
    assert [c["change"] for c in told] == ["attributes"] and told[0]["text"].startswith("Divide the segment")
    assert "glacier" in told[0]["text"] and "merking NEI" in told[0]["text"]

    # Asked to divide right by an end, the end itself is given back and nothing is divided.
    node = routes.split(conn, parts[0][0], AUTHOR, *start)
    assert node in {n["id"] for n in routes.detail(conn, made["id"])["nodes"]}
    assert routes.detail(conn, made["id"])["length"]["legs"] == 5


def test_an_end_is_moved_and_a_route_sent_another_way_without_touching_any_stretch(conn):
    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    other = routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    route = made["id"]

    def lines():    # every stretch as it lies, and the other route: none of it may change
        return (conn.execute("""SELECT e.id, e.status, ST_AsText(r.geom), r.a_node, r.b_node FROM core.edge e
                                JOIN core.edge_revision r ON r.id = e.current_revision_id ORDER BY e.id""").fetchall(),
                conn.execute("SELECT seq, edge_id FROM core.route_leg WHERE route_id = %s ORDER BY seq",
                             (other,)).fetchall())

    def now():
        return routes.detail(conn, route)

    untouched = lines()
    nodes, edges = routes._in_order(conn, route)
    end, inner = nodes[-1], nodes[-2]
    spot = {n["id"]: (n["lon"], n["lat"]) for n in made["nodes"]}

    # Shown first: dropped on the junction before its end, the route would be cut back to there.
    shown = routes.move_end(conn, route, now()["version"], AUTHOR, end, *spot[inner], dry_run=True)
    assert (shown["dropped"], shown["added"], shown["stretches"]) == (1, 0, 3) and now()["length"]["legs"] == 4
    routes.move_end(conn, route, now()["version"], AUTHOR, end, *spot[inner])
    cut = now()
    assert cut["length"]["legs"] == 3 and cut["shape"] == "line" and inner in {e["id"] for e in cut["ends"]}
    # Dropped where it used to end, it is carried on along the stretch that still runs there.
    told = routes.move_end(conn, route, cut["version"], AUTHOR, inner, *spot[end])
    assert (told["added"], told["dropped"], told["new"]) == (1, 0, 0)
    assert routes._in_order(conn, route)[1] in (edges, edges[::-1]) and lines() == untouched
    with pytest.raises(places.Invalid, match="already ends there"):
        routes.move_end(conn, route, now()["version"], AUTHOR, end, *spot[end])
    with pytest.raises(places.Invalid, match="not an end"):
        routes.move_end(conn, route, now()["version"], AUTHOR, inner, *spot[end])

    # Another way round one of its stretches exists, by a junction to the side: the route is sent
    # that way. The stretch it used stays where it is, for whoever else walks it.
    grabbed = edges[1]
    before, after = nodes[1], nodes[2]
    side = routes._node_at(conn, conn.execute(
        "SELECT ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 150, 150) FROM core.edge e "
        "JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s", (grabbed,)).fetchone()[0])
    for a, b in ((before, side), (side, after)):
        routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(x.geom, y.geom) FROM core.node x, core.node y
                                          WHERE x.id = %(a)s AND y.id = %(b)s)""", {"a": a, "b": b},
                         a, b, "trail", "drawn", True, True)
    untouched = lines()
    at = conn.execute("SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326)) "
                      "FROM core.node WHERE id = %s", (side,)).fetchone()
    told = routes.send_via(conn, route, now()["version"], AUTHOR, grabbed, *at)
    assert (told["added"], told["dropped"]) == (2, 1)
    round_about = routes._in_order(conn, route)
    assert side in round_about[0] and grabbed not in round_about[1] and now()["shape"] == "line"
    assert lines() == untouched
    with pytest.raises(places.Invalid, match="already goes there"):
        routes.send_via(conn, route, now()["version"], AUTHOR, round_about[1][0], *at)


def test_two_parts_made_as_routes_of_their_own_become_one_route(conn):
    """Two parts of one code on either side of a glacier, each made yours separately."""
    one = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    two = routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    withdrawn = routes.withdrawn_by_a_person(conn)
    routes.update(conn, two, routes.detail(conn, two)["version"], AUTHOR, maintainers=["Luster Turlag"],
                  everywhere=True)
    legs = routes.detail(conn, one)["length"]["legs"] + routes.detail(conn, two)["length"]["legs"]
    shared = conn.execute("""SELECT count(*) FROM core.route_leg a JOIN core.route_leg b USING (edge_id)
                             WHERE a.route_id = %s AND b.route_id = %s""", (one, two)).fetchone()[0]

    routes.add_piece(conn, one, routes.detail(conn, one)["version"], candidate(conn, "bre19"), AUTHOR)
    whole = routes.detail(conn, one)
    assert whole["length"]["legs"] == legs - shared
    assert {s["code"] for s in whole["sources"]} == {"bre18", "bre19"}
    # The part that came in keeps who maintains it, as exceptions on its stretches.
    assert {tuple(x["maintainers"]) for x in whole["exceptions"]} == {("Luster Turlag",)}
    # The other route is no route any more, without counting as withdrawn by a person,
    # and its candidate now belongs to the one route.
    assert conn.execute("SELECT status FROM core.route WHERE id = %s", (two,)).fetchone() == ("retired",)
    assert routes.withdrawn_by_a_person(conn) == withdrawn
    assert str(routes.owner_of(conn, candidate(conn, "bre19"))) == one
    with pytest.raises(places.Conflict, match="already part of the route"):
        routes.add_piece(conn, one, whole["version"], candidate(conn, "bre19"), AUTHOR)
    # Where the two pieces do not meet, the gap can be joined over a glacier: walked, signed, never marked.
    if whole["gaps"]:
        gap = whole["gaps"][0]
        routes.add_crossing(conn, one, whole["version"], gap["from_node"], gap["to_node"], "glacier", AUTHOR)
        joined = routes.detail(conn, one)
        assert joined["pieces"] == 1 and joined["length"]["glacier_m"] > 0


def test_a_route_is_compared_with_the_mapped_paths_and_laid_on_them_where_they_run_beside_it(loaded, monkeypatch):
    import httpx
    from conftest import HTTP_CLIENT
    from test_fkb_wfs import Server

    from stiflyt2.importer.adapters import fkb_wfs

    with psycopg.connect(role_dsn("stiflyt2_import")) as c:
        # The one segment of bre49, 1.9 km. A mapped path runs 8 m beside the first half of it and
        # then leaves; another only crosses it further on. Beside the second half nothing is mapped.
        segment, = c.execute("""SELECT rs.segment_id FROM source_index.route r
                                JOIN source_index.route_segment rs ON rs.route_id = r.id WHERE r.label = 'bre49'""").fetchone()
        points = c.execute("""SELECT ST_X(d.geom), ST_Y(d.geom) FROM source_index.trail_segment t,
                              ST_DumpPoints(ST_Segmentize(t.geom, 30)) d WHERE t.id = %s ORDER BY d.path""",
                           (segment,)).fetchall()
        box = c.execute("""SELECT floor(ST_XMin(geom) - 500)::int, floor(ST_YMin(geom) - 500)::int,
                                  ceil(ST_XMax(geom) + 500)::int, ceil(ST_YMax(geom) + 500)::int
                           FROM source_index.trail_segment WHERE id = %s""", (segment,)).fetchone()
    half, far = len(points) // 2, 3 * len(points) // 4
    beside = [f"{x + 8:.2f} {y:.2f}" for x, y in points[:half]] + [f"{points[half][0] + 400:.2f} {points[half][1]:.2f}"]
    across = f"{points[far][0] - 200:.2f} {points[far][1] - 150:.2f} {points[far][0] + 200:.2f} {points[far][1] + 150:.2f}"
    server, real = Server(), HTTP_CLIENT
    server.features = [(" ".join(beside), "sti"), (across, "sti")]
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    fkb_wfs.run("half", tuple(box))
    build.build()

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            route = routes.make(conn, candidate(conn, "bre49"), AUTHOR)
            # (Its stretch may be one an earlier test has left with a history of its own: what counts
            # here is that looking writes no new line.)
            lines = """SELECT count(*) FROM core.edge_revision v JOIN core.route_leg l
                       ON l.edge_id = v.edge_id WHERE l.route_id = %s"""
            before = conn.execute(lines, (route,)).fetchone()
            seen = routes.compare_with_mapped(conn, route, AUTHOR)
            # About half of it has a mapped path beside it. Laid there it is about as long as before,
            # no point moves far, and the half with nothing beside it is not touched.
            assert not seen["done"] and seen["stretches"] == 1
            assert 0.4 < seen["beside_m"] / seen["was_m"] < 0.62 and 0.4 < seen["followed_m"] / seen["was_m"] < 0.6
            assert abs(seen["length_m"] - seen["was_m"]) < 40 and 4 <= seen["moves_m"] <= 12
            assert seen["turrutebasen_m"] == seen["was_m"]
            assert conn.execute(lines, (route,)).fetchone() == before

            # The whole area at once. The route is listed while it would change. Said to be done,
            # it is listed as such and laying all leaves it alone.
            listed = proposals.against_mapped(conn, "bre")
            assert [(r["code"], r["done"], r["length_m"]) for r in listed["routes"]] == [("bre49", False, seen["length_m"])]
            assert listed["compared"] == 1 and listed["was_m"] == seen["was_m"]
            routes.review(conn, route, routes.detail(conn, route)["version"], AUTHOR)
            assert proposals.against_mapped(conn, "bre")["routes"][0]["done"] is True
            assert proposals.lay_all_beside(conn, "bre", AUTHOR) == {
                "routes": 0, "stretches": 0, "was_m": 0, "length_m": 0, "left_done": 1}
            routes.review(conn, route, routes.detail(conn, route)["version"], AUTHOR, done=False)
            done = proposals.lay_all_beside(conn, "bre", AUTHOR)
            assert (done["routes"], done["stretches"], done["length_m"]) == (1, 1, seen["length_m"])
            assert proposals.against_mapped(conn, "bre")["routes"] == []
            first, second = conn.execute("""
                SELECT ST_Length(ST_Intersection(ST_LineSubstring(t.geom, 0, 0.4), ST_Buffer(v.geom, 2))) / (0.4 * t.length_m),
                       ST_Length(ST_Intersection(ST_LineSubstring(t.geom, 0.6, 1), ST_Buffer(v.geom, 0.5))) / (0.4 * t.length_m)
                FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision v ON v.id = e.current_revision_id, source_index.trail_segment t
                WHERE l.route_id = %s AND t.id = %s""", (route, segment)).fetchone()
            assert first < 0.5 and second > 0.97    # the first part has moved over to the path, the last lies as it did
            # Said of the stretch, and nothing more to do: it follows the path wherever one runs beside it.
            assert [h["type"] for h in routes.detail(conn, route)["history"]].count("route.laid_beside") == 1
            again = routes.compare_with_mapped(conn, route, AUTHOR)
            assert again["stretches"] == 0 and again["length_m"] == done["length_m"]
        finally:
            conn.rollback()


def _off_the_end_of_bre14(c) -> tuple:
    """An end of bre14, and a spot 700 m off it that no Turrutebasen segment comes near."""
    return c.execute("""
        SELECT ST_X(e.p), ST_Y(e.p), ST_X(t.p), ST_Y(t.p)
        FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
        JOIN source_index.trail_segment s ON s.id = rs.segment_id,
             LATERAL (VALUES (ST_StartPoint(s.geom)), (ST_EndPoint(s.geom))) e(p),
             generate_series(0, 315, 45) AS turn,
             LATERAL (SELECT ST_Translate(e.p, 700 * cos(radians(turn)), 700 * sin(radians(turn))) AS p) t
        WHERE r.label = 'bre14' AND NOT EXISTS (
            SELECT 1 FROM source_index.trail_segment o WHERE ST_DWithin(o.geom, t.p, 450))
        ORDER BY turn LIMIT 1""").fetchone()


def test_an_end_is_carried_on_along_mapped_paths_where_no_stretch_leads(loaded, monkeypatch):
    import httpx
    from conftest import HTTP_CLIENT
    from test_fkb_wfs import Server

    from stiflyt2.importer.adapters import fkb_wfs

    with psycopg.connect(role_dsn("stiflyt2_import")) as c:
        # From an end of bre14, a mapped path in two pieces out to a spot 700 m off that no segment comes near.
        ex, ey, tx, ty = _off_the_end_of_bre14(c)
    mid = ((ex + tx) / 2 + 20, (ey + ty) / 2)
    server, real = Server(), HTTP_CLIENT
    server.features = [(f"{ex + 2:.2f} {ey:.2f} {mid[0]:.2f} {mid[1]:.2f}", "sti"),
                       (f"{mid[0]:.2f} {mid[1]:.2f} {tx:.2f} {ty:.2f}", "sti")]
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    fkb_wfs.run("onward", (int(min(ex, tx)) - 300, int(min(ey, ty)) - 300, int(max(ex, tx)) + 300, int(max(ey, ty)) + 300))
    build.build()

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            route = routes.make(conn, candidate(conn, "bre14"), AUTHOR)
            before = routes.detail(conn, route)
            end = min(before["ends"], key=lambda e: conn.execute(
                "SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25833)) FROM core.node WHERE id = %s",
                (ex, ey, e["id"])).fetchone()[0])
            lon, lat, off_lon, off_lat = conn.execute("""
                SELECT ST_X(a.g), ST_Y(a.g), ST_X(b.g), ST_Y(b.g)
                FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) AS g) a,
                     (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) AS g) b""",
                (tx + 30, ty, tx + 600, ty + 600)).fetchone()

            # Looked at first: nothing is changed, and it says how far along mapped lines it would go.
            shown = routes.move_end(conn, route, before["version"], AUTHOR, end["id"], lon, lat, dry_run=True)
            assert shown["added"] == 1 and shown["dropped"] == 0 and 690 < shown["mapped_m"] < 760
            assert routes.detail(conn, route)["length"] == before["length"]

            told = routes.move_end(conn, route, before["version"], AUTHOR, end["id"], lon, lat)
            after = routes.detail(conn, route)
            assert told["mapped_m"] == shown["mapped_m"] and after["shape"] == "line"
            assert abs(after["length"]["on_foot_m"] - before["length"]["on_foot_m"] - told["mapped_m"]) < 10
            # One new stretch of yours, drawn along the two mapped pieces, which it says it follows.
            new = conn.execute("""
                SELECT v.kind, v.origin, v.marked, (SELECT count(*) FROM core.source_reference sr
                    WHERE sr.subject_type = 'edge' AND sr.subject_id = e.id AND sr.ref_type = 'traced')
                FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision v ON v.id = e.current_revision_id
                WHERE l.route_id = %s AND v.origin = 'drawn'""", (route,)).fetchall()
            assert new == [("trail", "drawn", None, 2)]
            moved = next(e for e in after["ends"] if e["id"] not in {x["id"] for x in before["ends"]})
            assert conn.execute("SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25833)) < 1 FROM core.node "
                                "WHERE id = %s", (tx, ty, moved["id"])).fetchone()[0]
            assert after["history"][0]["type"] == "route.end_moved" and after["history"][0]["payload"]["along_mapped_m"] > 690

            # Where nothing is mapped either, and it is far, it says so and nothing is changed.
            with pytest.raises(routes.Invalid, match="no mapped path or road lies within 150 m.*further than the 500 m"):
                routes.move_end(conn, route, after["version"], AUTHOR, moved["id"], off_lon, off_lat)
            assert routes.detail(conn, route)["length"] == after["length"]

            # A short way off with nothing mapped, it goes straight there, as between two points laid out:
            # one stretch in the terrain, for a person to say what it is.
            near_lon, near_lat = conn.execute(
                "SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                (tx + 300, ty + 250)).fetchone()
            shown = routes.move_end(conn, route, after["version"], AUTHOR, moved["id"], near_lon, near_lat, dry_run=True)
            assert (shown["added"], shown["mapped_m"]) == (1, 0) and 380 < shown["straight_m"] < 400
            assert routes.detail(conn, route)["length"] == after["length"]
            told = routes.move_end(conn, route, after["version"], AUTHOR, moved["id"], near_lon, near_lat)
            hopped = routes.detail(conn, route)
            assert hopped["shape"] == "line" and told["straight_m"] == shown["straight_m"]
            assert conn.execute("""
                SELECT count(*), round(sum(ST_Length(v.geom)))::int FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision v ON v.id = e.current_revision_id
                WHERE l.route_id = %s AND v.kind = 'off_trail' AND v.origin = 'drawn'""", (route,)).fetchone() == (1, told["straight_m"])
            assert hopped["history"][0]["payload"]["straight_m"] == told["straight_m"]
            there = next(e for e in hopped["ends"] if e["id"] not in {x["id"] for x in after["ends"]})

            # Pointed at on the map (a reach is given), a spot means what is under the pointer: 100 m beside
            # the route's own last stretch is not that stretch, and the route goes straight on to the spot.
            beside_lon, beside_lat = conn.execute(
                "SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                (tx + 86, ty + 202)).fetchone()
            given = routes.move_end(conn, route, hopped["version"], AUTHOR, there["id"], beside_lon, beside_lat, dry_run=True)
            assert (given["added"], given["dropped"]) == (0, 1)     # a position somebody gave: the stretch 100 m off is meant
            pointed = routes.move_end(conn, route, hopped["version"], AUTHOR, there["id"], beside_lon, beside_lat,
                                      reach_m=15, dry_run=True)
            assert (pointed["added"], pointed["dropped"]) == (1, 0) and 214 < pointed["straight_m"] < 224
            assert routes.detail(conn, route)["length"] == hopped["length"]

            # Let go on the arrival point of a hut that lies beside the end, with no stretch to it: the end
            # itself is the nearest junction, but the mark is what was meant.
            hut = routes._node_at(conn, f"SRID=25833;POINT({tx + 300 + 12:.2f} {ty + 250 + 16:.2f})")
            places.at_node(conn, hut, AUTHOR, "Hytta ved enden")
            hut_lon, hut_lat = conn.execute("SELECT ST_X(g), ST_Y(g) FROM (SELECT ST_Transform(geom, 4326) AS g "
                                            "FROM core.node WHERE id = %s) n", (hut,)).fetchone()
            now = routes.detail(conn, route)
            told = routes.move_end(conn, route, now["version"], AUTHOR, there["id"], hut_lon, hut_lat)
            assert (told["added"], told["straight_m"], told["stops"][-1]["place"]) == (1, 20, "Hytta ved enden")
            with pytest.raises(routes.Invalid, match="already ends there"):
                routes.move_end(conn, route, routes.detail(conn, route)["version"], AUTHOR, hut, hut_lon, hut_lat)
        finally:
            conn.rollback()


def test_a_route_is_not_left_with_no_stretches(conn):
    """Taking out its only stretch, or calling it done when it has none: it is withdrawn instead."""
    route = routes.make(conn, candidate(conn, "bre14"), AUTHOR)
    only, = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s", (route,)).fetchall()
    with pytest.raises(routes.Invalid, match="the only stretch of bre14: withdraw the route instead"):
        routes.remove_leg(conn, route, routes.detail(conn, route)["version"], only[0], AUTHOR)
    assert routes.detail(conn, route)["length"]["legs"] == 1
    # One that has come to have none, as could happen before, is not done: it is no route.
    conn.execute("DELETE FROM core.route_leg WHERE route_id = %s", (route,))
    with pytest.raises(routes.Invalid, match="bre14 has no stretches"):
        routes.review(conn, route, routes.detail(conn, route)["version"], AUTHOR)
    routes.retire(conn, route, AUTHOR)
    assert conn.execute("SELECT status FROM core.route WHERE id = %s", (route,)).fetchone() == ("retired",)


def test_a_route_is_sent_another_way_along_mapped_paths_where_no_stretch_leads(loaded, monkeypatch):
    """Two routes on one line in Turrutebasen, one of which goes another way round: jot3 and jot4."""
    import math

    import httpx
    from conftest import HTTP_CLIENT
    from test_fkb_wfs import Server

    from stiflyt2.importer.adapters import fkb_wfs

    with psycopg.connect(role_dsn("stiflyt2_import")) as c:
        segment, = c.execute("""SELECT rs.segment_id FROM source_index.route r
                                JOIN source_index.route_segment rs ON rs.route_id = r.id WHERE r.label = 'bre49'""").fetchone()
        points = c.execute("""SELECT ST_X(d.geom), ST_Y(d.geom) FROM source_index.trail_segment t,
                              ST_DumpPoints(ST_Segmentize(t.geom, 30)) d WHERE t.id = %s ORDER BY d.path""",
                           (segment,)).fetchall()
    # A mapped path all along the route, and one that leaves it six tenths along, runs 300 m to
    # the side of it, and comes back nine tenths along.
    i, j = 6 * len(points) // 10, 9 * len(points) // 10
    dx, dy = points[j][0] - points[i][0], points[j][1] - points[i][1]
    nx, ny = -dy / math.hypot(dx, dy) * 300, dx / math.hypot(dx, dy) * 300
    round_ = [points[i], (points[i][0] + nx, points[i][1] + ny),
              ((points[i][0] + points[j][0]) / 2 + nx, (points[i][1] + points[j][1]) / 2 + ny),
              (points[j][0] + nx, points[j][1] + ny), points[j]]
    text = lambda line: " ".join(f"{x:.2f} {y:.2f}" for x, y in line)
    server, real = Server(), HTTP_CLIENT
    server.features = [(text(points), "sti"), (text(round_), "sti")]
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    xs, ys = [p[0] for p in points + round_], [p[1] for p in points + round_]
    fkb_wfs.run("round", (int(min(xs)) - 200, int(min(ys)) - 200, int(max(xs)) + 200, int(max(ys)) + 200))
    build.build()

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        try:
            route = routes.make(conn, candidate(conn, "bre49"), AUTHOR)
            before = routes.detail(conn, route)
            only, = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s", (route,)).fetchone()
            lon, lat = conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                                    round_[2]).fetchone()
            shown = routes.send_via(conn, route, before["version"], AUTHOR, only, lon, lat, dry_run=True)
            assert shown["mapped_m"] > 600 and routes.detail(conn, route)["length"] == before["length"]
            # Pointed at from far out on the map, the nearest thing of the route's own lies within reach of the
            # pointer. That is not "the route already goes there": the mapped path under the pointer is meant.
            wide = routes.send_via(conn, route, before["version"], AUTHOR, only, lon, lat, reach_m=500, dry_run=True)
            assert wide["mapped_m"] == shown["mapped_m"] and routes.detail(conn, route)["length"] == before["length"]

            told = routes.send_via(conn, route, before["version"], AUTHOR, only, lon, lat)
            after = routes.detail(conn, route)
            # It leaves its line where the mapped way leaves it and comes back where that comes back:
            # the stretch is divided in three, the middle part gives way to two new stretches.
            assert after["shape"] == "line" and told["dropped"] == 1 and told["added"] == 2
            assert after["length"]["legs"] == 4 and told["mapped_m"] == shown["mapped_m"]
            own = math.dist(points[i], points[j])
            assert after["length"]["on_foot_m"] > before["length"]["on_foot_m"] + told["mapped_m"] - 1.6 * own - 60
            drawn = conn.execute("""
                SELECT count(*), bool_and(v.kind = 'trail') FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                JOIN core.edge_revision v ON v.id = e.current_revision_id
                WHERE l.route_id = %s AND v.origin = 'drawn'""", (route,)).fetchone()
            assert drawn == (2, True)
            # The route passes the spot it was sent by, and no longer the middle of its old line.
            far, near = conn.execute("""
                SELECT ST_Distance(g.line, ST_SetSRID(ST_MakePoint(%s, %s), 25833)),
                       ST_Distance(g.line, ST_SetSRID(ST_MakePoint(%s, %s), 25833))
                FROM (SELECT ST_Collect(v.geom) AS line FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                      JOIN core.edge_revision v ON v.id = e.current_revision_id WHERE l.route_id = %s) g""",
                (*points[(i + j) // 2], *round_[2], route)).fetchone()
            assert far > 200 and near < 2
            assert after["ends"] and {e["id"] for e in after["ends"]} == {e["id"] for e in before["ends"]}
            assert after["history"][0]["payload"]["along_mapped_m"] == told["mapped_m"]
            # Sent there again, it is there already.
            with pytest.raises(routes.Invalid, match="already goes there"):
                routes.send_via(conn, route, after["version"], AUTHOR,
                                conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s "
                                             "ORDER BY seq LIMIT 1", (route,)).fetchone()[0], lon, lat)
        finally:
            conn.rollback()


def test_a_new_route_is_laid_along_mapped_paths_between_points_that_no_stretch_joins(conn, monkeypatch):
    """Where Turrutebasen has nothing: jot1 along Gjende. Uses the mapped path the test above loaded."""
    ex, ey, tx, ty = _off_the_end_of_bre14(conn)
    far = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (
        SELECT (ST_DumpPoints(ST_Collect(ST_StartPoint(s.geom), ST_EndPoint(s.geom)))).geom AS p
        FROM source_index.route r JOIN source_index.route_segment rs ON rs.route_id = r.id
        JOIN source_index.trail_segment s ON s.id = rs.segment_id WHERE r.label = 'bre14') q
        ORDER BY ST_Distance(p, ST_SetSRID(ST_MakePoint(%s, %s), 25833)) DESC LIMIT 1""", (ex, ey)).fetchone()
    lonlat = [tuple(conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                                 xy).fetchone()) for xy in (far, (ex, ey), (tx, ty))]

    # From the far end of bre14 over its one segment, and on along the mapped path where no segment runs.
    planned = routes.plan(conn, lonlat)
    assert planned["stretches"] == 2 and planned["new"] == 1 and 690 < planned["mapped_m"] < 760
    assert conn.execute("SELECT count(*) FROM core.route WHERE code = 'bre14y'").fetchone() == (0,)

    # Two points on mapped lines that the mapped lines join only far round, or not at all: a short
    # hop goes straight across. A long one is for the person to steer.
    half = tuple(conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                              ((ex + tx) / 2 + 20, (ey + ty) / 2)).fetchone())
    near_by = routes.plan(conn, [half, lonlat[2]])
    assert near_by["mapped_m"] > 300 and near_by["straight_m"] == 0      # joined by the path: along it
    round_about = [([(0.0, 0.0), (0.0, 9000.0)], ("fkb_wfs", "far round"))]
    monkeypatch.setattr(routes._Paths, "parts", lambda *a: round_about)
    hop = routes.plan(conn, [half, lonlat[2]])
    assert hop["mapped_m"] == 0 and 300 < hop["straight_m"] < 380
    with pytest.raises(routes.Invalid, match="is 9.0 km for 0.7 km as the crow flies: put a point between them on the way"):
        routes.plan(conn, [lonlat[1], lonlat[2]])
    monkeypatch.setattr(routes._Paths, "parts", lambda *a: None)
    assert 690 < routes.plan(conn, [lonlat[1], lonlat[2]])["straight_m"] < 720      # nothing joins them at all
    monkeypatch.undo()

    route = routes.detail(conn, routes.lay_out(conn, AUTHOR, lonlat, "bre14y"))
    assert route["shape"] == "line" and route["length"]["legs"] == 2
    assert abs(route["length"]["on_foot_m"] - planned["length_m"]) <= 1
    assert conn.execute("""SELECT v.origin, v.kind FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                           JOIN core.edge_revision v ON v.id = e.current_revision_id
                           WHERE l.route_id = %s ORDER BY l.seq""", (route["id"],)).fetchall() in (
        [("adopted", "trail"), ("drawn", "trail")], [("drawn", "trail"), ("adopted", "trail")])
    # On to a point with nothing mapped at it: straight there, through the terrain.
    off = conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                       (tx + 2000, ty + 2000)).fetchone()
    beyond = routes.plan(conn, [lonlat[2], tuple(off)])
    assert 2800 < beyond["straight_m"] < 2860 and beyond["stretches"] == 1


def test_a_route_is_said_to_be_discontinued_and_is_kept(conn):
    """Given up in the terrain, wanted here: jot1 along Gjende. It stays, and is left out of what is in use."""
    from datetime import date

    from stiflyt2.domain import corrections, export, worklist

    route = routes.make(conn, candidate(conn, "bre14"), AUTHOR)
    made = routes.detail(conn, route)
    assert made["discontinued"] is None
    with pytest.raises(routes.Invalid, match="say why"):
        routes.discontinue(conn, route, made["version"], AUTHOR, "  ")
    routes.discontinue(conn, route, made["version"], AUTHOR, "Danger of rockfall", date(2026, 6, 1))
    kept = routes.detail(conn, route)
    assert kept["discontinued"] == {"since": date(2026, 6, 1), "why": "Danger of rockfall"}
    assert kept["length"] == made["length"] and kept["history"][0]["type"] == "route.discontinued"

    # In the list it stands as discontinued, and nothing is asked of it.
    listed = worklist.worklist(conn, "bre")
    row, = [r for r in listed["all"] if r["code"] == "bre14"]
    assert row["discontinued"] and row["problems"] == ["Discontinued since 1 Jun 2026: Danger of rockfall"]
    assert not [i for i in listed["items"] if i["label"] == "bre14"] and listed["discontinued_routes"] == 1
    assert str(route) not in worklist.routes_of(conn, "bre") and str(route) in worklist.routes_of(conn, "bre", None)
    # Handed on as long as it is kept, saying what it is: whoever still shows it is to take it out.
    apart, = export.routes_geojson(conn, "bre", changed_only=True)["features"]
    told = apart["properties"]
    assert (told["code"], told["state"], told["discontinued_since"], told["discontinued_why"], told["in_turrutebasen"]) == (
        "bre14", "discontinued", "2026-06-01", "Danger of rockfall", True)
    assert told["changed"] and told["what_changed"] == "discontinued since 1.6.2026: Danger of rockfall"
    assert export.routes_geojson(conn, "bre", discontinued=True)["features"] == [apart]
    assert export.routes_geojson(conn, "bre", discontinued=False)["features"] == []
    # Turrutebasen still has it: Kartverket is asked to take the number off, with the reason.
    told = [c for c in corrections.corrections(conn, "bre") if c["was"].get("rutenummer") == "bre14"]
    assert [c["change"] for c in told] == ["remove from route"]
    assert told[0]["text"].endswith("bre14 is discontinued since 1.6.2026: Danger of rockfall")
    # On the map its stretch is marked so, and a new route is not laid over it.
    assert [f["properties"]["discontinued"] for f in routes.collection(conn)["features"]
            if f["properties"]["codes"] == "bre14"] == [True]
    ex, ey, *_ = _off_the_end_of_bre14(conn)
    ends = sorted(kept["ends"], key=lambda e: -conn.execute(
        "SELECT ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s, %s), 25833)) FROM core.node WHERE id = %s",
        (ex, ey, e["id"])).fetchone()[0])
    between = [(e["lon"], e["lat"]) for e in ends]
    assert routes.plan(conn, between)["straight_m"] > 400      # through the terrain, not over its stretch

    # In use after all: as it was.
    routes.take_up_again(conn, route, kept["version"], AUTHOR)
    again = routes.detail(conn, route)
    assert again["discontinued"] is None
    assert (routes.plan(conn, between)["stretches"], routes.plan(conn, between)["straight_m"]) == (1, 0)
    assert [(f["properties"]["code"], f["properties"]["state"]) for f in export.routes_geojson(conn, "bre")["features"]] == [
        ("bre14", "in use")]
    assert not [c for c in corrections.corrections(conn, "bre") if c["was"].get("rutenummer") == "bre14"]
    with pytest.raises(routes.Conflict, match="not discontinued"):
        routes.take_up_again(conn, route, again["version"], AUTHOR)


def test_the_pieces_of_a_route_are_joined_by_moving_an_end_of_one_to_the_other(conn):
    """jot48 at Keisarpasset: a stretch taken out left two pieces, with a stretch of another route between them."""
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    whole = routes.detail(conn, route)
    inner = [leg for leg in conn.execute("""
        SELECT l.edge_id::text, r.a_node::text, r.b_node::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE l.route_id = %s ORDER BY l.seq""", (route,)).fetchall()
        if not {leg[1], leg[2]} & {e["id"] for e in whole["ends"]}]
    gone, a, b = inner[0]
    routes.remove_leg(conn, route, whole["version"], gone, AUTHOR)
    apart = routes.detail(conn, route)
    assert apart["shape"] == "pieces" and {a, b} <= {e["id"] for e in apart["ends"]}
    lon, lat = conn.execute("SELECT ST_X(g), ST_Y(g) FROM (SELECT ST_Transform(geom, 4326) AS g FROM core.node WHERE id = %s) x",
                            (b,)).fetchone()

    # A proposal says the same: carried on along the stretch that runs between the two ends, however long.
    from stiflyt2.domain import proposals
    offered = [p for p in proposals.proposals(conn, "bre") if p["kind"] == "gap" and p["label"] == "bre18"]
    assert offered and "along the stretch" in offered[0]["text"]

    shown = routes.move_end(conn, route, apart["version"], AUTHOR, a, lon, lat, dry_run=True)
    assert shown["joined"] and shown["added"] == 1 and routes.detail(conn, route)["shape"] == "pieces"
    told = routes.move_end(conn, route, apart["version"], AUTHOR, a, lon, lat)
    joined = routes.detail(conn, route)
    assert told["joined"] and joined["shape"] == "line" and joined["length"] == whole["length"]
    assert joined["history"][0]["payload"]["joined"] is True
    # An end that is no end of any piece is still refused.
    with pytest.raises(routes.Invalid, match="not an end of the route"):
        routes.move_end(conn, route, joined["version"], AUTHOR, a, lon, lat)


def test_a_route_is_laid_straight_through_points_where_nothing_is_mapped_and_described(conn):
    """A route over a glacier: jot38. Nothing of Turrutebasen's and no mapped line where it goes."""
    route = routes.make(conn, candidate(conn, "bre14"), AUTHOR)
    ends = routes.detail(conn, route)["ends"]
    x, y = conn.execute("SELECT ST_X(geom), ST_Y(geom) FROM core.node WHERE id = %s", (ends[0]["id"],)).fetchone()
    # Three points out in the open, each far from every stretch and every mapped line.
    out = conn.execute("""
        SELECT array_agg(ARRAY[ST_X(p.g), ST_Y(p.g)] ORDER BY k)
        FROM (SELECT turn FROM generate_series(0, 315, 45) turn WHERE NOT EXISTS (
                  SELECT 1 FROM source_index.trail_segment o, generate_series(1, 3) k
                  WHERE ST_DWithin(o.geom, ST_SetSRID(ST_MakePoint(%(x)s + 900 * k * cos(radians(turn)), %(y)s + 900 * k * sin(radians(turn))), 25833), 500))
                AND NOT EXISTS (
                  SELECT 1 FROM source_index.path o, generate_series(1, 3) k
                  WHERE ST_DWithin(o.geom, ST_SetSRID(ST_MakePoint(%(x)s + 900 * k * cos(radians(turn)), %(y)s + 900 * k * sin(radians(turn))), 25833), 300))
              ORDER BY turn LIMIT 1) t,
             generate_series(1, 3) k,
             LATERAL (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(%(x)s + 900 * k * cos(radians(t.turn)),
                                                                  %(y)s + 900 * k * sin(radians(t.turn))), 25833), 4326) AS g) p""",
        {"x": x, "y": y}).fetchone()[0]
    points = [(ends[1]["lon"], ends[1]["lat"]), (ends[0]["lon"], ends[0]["lat"]), *[tuple(p) for p in out]]

    planned = routes.plan(conn, points)
    assert planned["stretches"] == 2 and 2690 < planned["straight_m"] < 2710 and planned["mapped_m"] == 0
    laid = routes.detail(conn, routes.lay_out(conn, AUTHOR, points, "bre14z"))
    assert laid["shape"] == "line" and laid["length"]["legs"] == 2
    # Over the stretch that was there, and on in one stretch through the three points: in the terrain, to be said what it is.
    drawn = conn.execute("""SELECT v.kind, v.origin, v.marked, ST_NPoints(v.geom), e.id::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                            JOIN core.edge_revision v ON v.id = e.current_revision_id
                            WHERE l.route_id = %s AND v.origin = 'drawn'""", (laid["id"],)).fetchall()
    assert [d[:4] for d in drawn] == [("off_trail", "drawn", None, 4)]
    routes.set_stretch(conn, drawn[0][4], AUTHOR, kind="glacier", marked=False)
    assert routes.detail(conn, laid["id"])["length"]["glacier_m"] > 2600

    # The line over the ice is pulled like a rubber band: grabbed between two corners, a corner is
    # made and goes where it is let go, however far; grabbed at a corner, that corner moves; a corner
    # let go on the one next to it goes away. The ends stay.
    glacier = drawn[0][4]

    def corners():
        return conn.execute("""SELECT array_agg(ARRAY[ST_X(d.geom), ST_Y(d.geom)] ORDER BY d.path) FROM core.edge e
                               JOIN core.edge_revision v ON v.id = e.current_revision_id, ST_DumpPoints(v.geom) d
                               WHERE e.id = %s""", (glacier,)).fetchone()[0]

    def lonlat(x, y):
        return tuple(conn.execute("SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(%s, %s), 25833), 4326) g",
                                  (x, y)).fetchone())

    was = corners()
    mid = ((was[1][0] + was[2][0]) / 2, (was[1][1] + was[2][1]) / 2)
    aside = (mid[0] + 1500, mid[1] + 1200)
    shown = routes.bend(conn, glacier, AUTHOR, lonlat(*mid), lonlat(*aside), dry_run=True)
    assert shown["length_m"] > shown["was_m"] + 1000 and corners() == was
    routes.bend(conn, glacier, AUTHOR, lonlat(*mid), lonlat(*aside))
    now = corners()
    assert len(now) == 5 and now[:2] == was[:2] and now[3:] == was[2:]
    assert abs(now[2][0] - aside[0]) < 0.1 and abs(now[2][1] - aside[1]) < 0.1
    # The corner that was made, taken hold of 10 m from it and moved on.
    routes.bend(conn, glacier, AUTHOR, lonlat(aside[0] + 6, aside[1] + 8), lonlat(aside[0] - 800, aside[1]))
    moved = corners()
    assert len(moved) == 5 and abs(moved[2][0] - (aside[0] - 800)) < 0.1 and moved[1] == was[1] and moved[3] == was[2]
    # Let go on its neighbour: the corner is gone, and the line is as it was.
    routes.bend(conn, glacier, AUTHOR, lonlat(*moved[2]), lonlat(was[1][0] + 5, was[1][1]))
    assert corners() == was
    with pytest.raises(routes.Invalid, match="pulled at most 10 km"):
        routes.bend(conn, glacier, AUTHOR, lonlat(*mid), lonlat(mid[0] + 12_000, mid[1]))

    # What the route is like, in words: kept with it, handed on with it, and taken away again.
    from stiflyt2.domain import export
    about = "Unmarked. A guide or glacier equipment is needed."
    routes.update(conn, laid["id"], routes.detail(conn, laid["id"])["version"], AUTHOR, description=f"  {about}\n")
    told = routes.detail(conn, laid["id"])
    assert told["description"] == about and told["history"][0]["type"] == "route.described"
    assert [f["properties"]["description"] for f in export.routes_geojson(conn, "bre")["features"]
            if f["properties"]["code"] == "bre14z"] == [about]
    routes.update(conn, laid["id"], told["version"], AUTHOR, description="")
    assert routes.detail(conn, laid["id"])["description"] is None


def test_an_end_dropped_on_the_routes_own_start_closes_it_as_a_round_trip(conn):
    """jot53: from Skogadalsbøen by Vormeli and round, back to Skogadalsbøen."""
    from stiflyt2.domain import export, worklist

    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    before = routes.detail(conn, route)
    assert before["shape"] == "line" and before["round_trip"] is None
    start, end = before["ends"]
    places.at_node(conn, start["id"], AUTHOR, "Starten")
    # Another way back from its end to its start, which the route does not use.
    back = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ARRAY[b.geom,
            ST_Translate(ST_Centroid(ST_Collect(a.geom, b.geom)), 400, 400), a.geom])
        FROM core.node a, core.node b WHERE a.id = %(a)s AND b.id = %(b)s)""",
        {"a": start["id"], "b": end["id"]}, end["id"], start["id"], "trail", "drawn", None, None)
    version = routes.detail(conn, route)["version"]

    shown = routes.move_end(conn, route, version, AUTHOR, end["id"], start["lon"], start["lat"], dry_run=True)
    assert shown["round_trip"] and (shown["added"], shown["dropped"], shown["cut_m"]) == (1, 0, 0)
    assert routes.detail(conn, route)["shape"] == "line"       # looked at: nothing is changed

    routes.move_end(conn, route, version, AUTHOR, end["id"], start["lon"], start["lat"])
    after = routes.detail(conn, route)
    assert after["shape"] == "loop" and after["ends"] == [] and after["generated_name"] is None
    assert (after["round_trip"]["id"], after["round_trip"]["place"]) == (start["id"], "Starten")
    assert after["length"]["legs"] == before["length"]["legs"] + 1     # nothing of it was cut
    assert after["history"][0]["type"] == "route.end_moved" and after["history"][0]["payload"]["round_trip"] is True
    # Walked the way it was laid: out from its start, and back to it along the new way last.
    legs = [e for (e,) in conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq",
                                       (route,))]
    assert legs[-1] == back and len(set(legs)) == len(legs)

    # A round trip is a shape a route may have: nothing is asked of it for that.
    todo = worklist.worklist(conn, "bre")
    mine = [i for i in todo["items"] if i["about"] == "route" and str(i["id"]) == route]
    assert route in {str(r["id"]) for r in todo["routes"]} and "shape" not in {i["problem"] for i in mine}
    assert ("No name: a round trip is not named after its ends" in {i["text"] for i in mine}) is not bool(after["name"])
    routes.review(conn, route, after["version"], AUTHOR)
    handed = next(f for f in export.routes_geojson(conn, "bre")["features"] if f["id"] == route)["properties"]
    assert handed["round_trip"] is True and handed["from"] == handed["to"] == "Starten"

    # It has no ends to move or lay it between: said so. Taking a stretch out opens it again.
    now = routes.detail(conn, route)
    with pytest.raises(routes.Invalid, match="the route is a round trip, with no ends to lay it between"):
        routes.send_via(conn, route, now["version"], AUTHOR, legs[0], start["lon"] + 0.01, start["lat"])
    routes.remove_leg(conn, route, now["version"], back, AUTHOR)
    opened = routes.detail(conn, route)
    assert opened["shape"] == "line" and {e["id"] for e in opened["ends"]} == {start["id"], end["id"]}
    assert opened["round_trip"] is None


def test_a_route_is_kept_without_being_published(conn):
    """jot53: kept for its history for now. Not handed to UT.no, and nothing asked of Kartverket for it."""
    from stiflyt2.domain import corrections, export, worklist

    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    before = routes.detail(conn, route)
    start, end = before["ends"]
    # Carried round to its start along a stretch that Turrutebasen does not have.
    routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ARRAY[b.geom,
            ST_Translate(ST_Centroid(ST_Collect(a.geom, b.geom)), 400, 400), a.geom])
        FROM core.node a, core.node b WHERE a.id = %(a)s AND b.id = %(b)s)""",
        {"a": start["id"], "b": end["id"]}, end["id"], start["id"], "trail", "drawn", None, None)
    routes.move_end(conn, route, routes.detail(conn, route)["version"], AUTHOR, end["id"], start["lon"], start["lat"])
    made = routes.detail(conn, route)
    assert made["unpublished"] is None

    def asked(of="bre18"):
        return [c["change"] for c in corrections.corrections(conn, "bre") if of in c["rutenummer"]
                and c["change"] in ("new segment", "add to route", "remove from route", "route name", "route code")]

    assert asked() == ["new segment"]       # published: Kartverket is asked for the stretch it lacks
    assert [f["properties"]["code"] for f in export.routes_geojson(conn, "bre")["features"]] == ["bre18"]

    with pytest.raises(routes.Invalid, match="say why the route is not published"):
        routes.unpublish(conn, route, made["version"], AUTHOR, " ")
    routes.unpublish(conn, route, made["version"], AUTHOR, "Kept for its history")
    kept = routes.detail(conn, route)
    assert kept["unpublished"] == {"why": "Kept for its history"} and kept["discontinued"] is None
    assert kept["history"][0]["type"] == "route.unpublished" and kept["length"] == made["length"]
    # Not handed on, in any of the files; and nothing asked of Kartverket on its account.
    for only in ({}, {"changed_only": True}, {"discontinued": False}):
        assert export.routes_geojson(conn, "bre", **only)["features"] == []
    # Nothing new is asked of Kartverket for it; what Turrutebasen has of it is asked taken out.
    off = [c for c in corrections.corrections(conn, "bre") if c["was"].get("rutenummer") == "bre18"]
    assert asked() == ["remove from route"] * len(off) and len(off) == before["length"]["legs"]
    assert all(c["text"].endswith("bre18 is not published: Kept for its history") for c in off)
    # Otherwise a route like any other: in the list, saying so, and on the map.
    listed = worklist.worklist(conn, "bre")
    row, = [r for r in listed["all"] if r["code"] == "bre18" and r["kind"] == "yours"]
    assert row["unpublished"] == "Kept for its history" and not row.get("discontinued")
    assert str(route) in worklist.routes_of(conn, "bre")
    assert {f["properties"]["unpublished"] for f in routes.collection(conn)["features"]
            if f["properties"]["codes"] == "bre18"} == {True}

    # Said again with another reason; and published after all.
    with pytest.raises(routes.Conflict, match="said of it already"):
        routes.unpublish(conn, route, kept["version"], AUTHOR, "Kept for its history")
    routes.unpublish(conn, route, kept["version"], AUTHOR, "Not ready to be shown")
    again = routes.detail(conn, route)
    assert again["history"][0]["payload"] == {"why": "Not ready to be shown", "was": "Kept for its history"}
    routes.publish(conn, route, again["version"], AUTHOR)
    out = routes.detail(conn, route)
    assert out["unpublished"] is None and out["history"][0]["type"] == "route.published"
    assert asked() == ["new segment"] and len(export.routes_geojson(conn, "bre")["features"]) == 1
    with pytest.raises(routes.Conflict, match="published already"):
        routes.publish(conn, route, out["version"], AUTHOR)


def test_a_route_that_runs_double_between_two_junctions_is_not_one_line(conn):
    """jot66, jot88 and jot90: Turrutebasen has two segments for one bit of path, and the route took both."""
    from stiflyt2.domain import worklist

    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    made = routes.detail(conn, route)
    assert made["shape"] == "line" and made["doubles"] == []
    first, a, b, metres = conn.execute("""
        SELECT l.edge_id::text, r.a_node::text, r.b_node::text, round(ST_Length(r.geom))::int FROM core.route_leg l
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY l.seq LIMIT 1""", (route,)).fetchone()
    # One more stretch between the two junctions of its first stretch, bowing out beside it.
    beside = routes._new_edge(conn, AUTHOR, """(SELECT ST_MakeLine(ARRAY[a.geom,
            ST_Translate(ST_Centroid(ST_Collect(a.geom, b.geom)), 0, %(far)s), b.geom])
        FROM core.node a, core.node b WHERE a.id = %(a)s AND b.id = %(b)s)""",
        {"a": a, "b": b, "far": metres}, a, b, "trail", "adopted", True, True)
    routes._add_legs(conn, route, [beside])

    twice = routes.detail(conn, route)
    assert twice["shape"] == "doubled"         # not a fork, and not a line: one stretch more than a line has
    (double,) = twice["doubles"]
    assert [g["edge"] for g in double["stretches"]] == [first, beside]     # the one to keep first: the shorter
    todo = [i["text"] for i in worklist.worklist(conn, "bre")["items"] if str(i["id"]) == route and i["problem"] == "shape"]
    assert todo == [f"Two stretches between the same two junctions ({double['stretches'][1]['metres']} m): one too many"]
    # What needs the route to be one line says so.
    with pytest.raises(routes.Invalid, match="the route must be one line with two ends"):
        routes.move_end(conn, route, twice["version"], AUTHOR, made["ends"][0]["id"], 7.5, 61.5, dry_run=True)

    # Proposed: the stretch too many taken out. Approved, it is a line again, as long as it was.
    offered = [p for p in proposals.proposals(conn, "bre") if p["action"].get("route") == route]
    assert [p["kind"] for p in offered] == ["double"]
    assert offered[0]["text"] == (f"Take the {proposals._metres(double['stretches'][1]['metres'])} stretch that runs between "
                                  "two junctions another stretch of it joins already out of bre18")
    assert routes.detail(conn, route)["shape"] == "doubled"                # worked out, and nothing kept
    proposals.approve(conn, AUTHOR, offered[0]["action"])
    mended = routes.detail(conn, route)
    assert mended["shape"] == "line" and mended["length"] == made["length"] and mended["doubles"] == []
    assert not [p for p in proposals.proposals(conn, "bre") if p["action"].get("route") == route]


def test_two_routes_that_cross_between_the_same_two_junctions_share_the_crossing(conn):
    long_one = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    short = routes.make(conn, candidate(conn, "bre14"), AUTHOR)          # all of it is a stretch of bre18 too
    a, b = (e["id"] for e in routes.detail(conn, short)["ends"])
    edges = conn.execute("SELECT count(*) FROM core.edge WHERE status = 'active'").fetchone()[0]

    first = routes.add_crossing(conn, short, routes.detail(conn, short)["version"], a, b, "glacier", AUTHOR)
    assert conn.execute("SELECT count(*) FROM core.edge WHERE status = 'active'").fetchone()[0] == edges + 1
    # The other route is joined between the same two junctions, the other way round: the crossing that is there is its too.
    second = routes.add_crossing(conn, long_one, routes.detail(conn, long_one)["version"], b, a, "glacier", AUTHOR)
    assert second == first
    assert conn.execute("SELECT count(*) FROM core.edge WHERE status = 'active'").fetchone()[0] == edges + 1
    assert conn.execute("SELECT count(*) FROM core.route_leg WHERE edge_id = %s", (first,)).fetchone()[0] == 2
    told = conn.execute("""SELECT payload FROM history.event WHERE type = 'route.crossing_added' AND subject_id = %s
                           ORDER BY seq DESC LIMIT 1""", (long_one,)).fetchone()[0]
    assert told["shared"] == first and told["kind"] == "glacier"
    with pytest.raises(places.Conflict, match="already"):              # and not twice for one route
        routes.add_stretch(conn, long_one, routes.detail(conn, long_one)["version"], AUTHOR, edge_id=first)
    # Kartverket is asked for one segment there, with both numbers: not for two that lie on each other.
    from stiflyt2.domain import corrections
    asked = [c for c in corrections.corrections(conn, "bre") if c["change"] == "new segment"]
    assert len(asked) == 1 and asked[0]["rutenummer"] == ["bre14", "bre18"] and "for bre14, bre18:" in asked[0]["text"]
    # Another kind of crossing there is another stretch.
    by_boat = routes.add_crossing(conn, long_one, routes.detail(conn, long_one)["version"], a, b, "water", AUTHOR)
    assert by_boat != first

    # A stretch routes run along is not taken away; one that came from Turrutebasen is not either.
    with pytest.raises(places.Conflict, match="bre14, bre18 run along that stretch"):
        routes.take_away(conn, first, AUTHOR)
    for route in (long_one, short):
        routes.remove_leg(conn, route, routes.detail(conn, route)["version"], first, AUTHOR)
    routes.remove_leg(conn, long_one, routes.detail(conn, long_one)["version"], by_boat, AUTHOR)
    theirs = conn.execute("""SELECT l.edge_id::text FROM core.route_leg l WHERE l.route_id = %s AND NOT EXISTS (
                                 SELECT 1 FROM core.route_leg o WHERE o.edge_id = l.edge_id AND o.route_id <> l.route_id)
                             ORDER BY l.seq LIMIT 1""", (long_one,)).fetchone()[0]
    with pytest.raises(places.Conflict, match="bre18 runs along that stretch"):
        routes.take_away(conn, theirs, AUTHOR)
    routes.remove_leg(conn, long_one, routes.detail(conn, long_one)["version"], theirs, AUTHOR)
    with pytest.raises(places.Invalid, match="came from Turrutebasen"):
        routes.take_away(conn, theirs, AUTHOR)
    # Left by every route, the crossing that was made here is taken away, and is no stretch any more.
    routes.take_away(conn, first, AUTHOR)
    assert conn.execute("SELECT status FROM core.edge WHERE id = %s", (first,)).fetchone()[0] == "retired"
    assert conn.execute("SELECT type, payload->>'kind' FROM history.event WHERE subject_type = 'edge' AND subject_id = %s",
                        (first,)).fetchall() == [("edge.taken_away", "glacier")]
    with pytest.raises(places.NotFound):
        routes.take_away(conn, first, AUTHOR)
    with pytest.raises(places.NotFound):
        routes.take_away(conn, "no-such-stretch", AUTHOR)


def test_the_map_reads_anew_only_the_stretches_that_changed(conn):
    """A mark per stretch, another when anything the map says of it has changed; and the lines by id."""
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    before = routes.marks(conn)
    lines = {f["properties"]["edge_id"]: f for f in routes.collection(conn)["features"]}
    assert set(before) == set(lines)
    mine = [e for (e,) in conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq", (route,))]
    assert routes.collection(conn, [])["features"] == []
    assert [f["properties"]["edge_id"] for f in routes.collection(conn, mine[:1])["features"]] == mine[:1]

    # What one stretch is: its mark is another, and no other's.
    routes.set_stretch(conn, mine[0], AUTHOR, marked=False)
    after = routes.marks(conn)
    assert {e for e in after if after[e] != before.get(e)} == {mine[0]}
    # The route's code is on every stretch of it: all of its marks are new, and no others.
    routes.update(conn, route, routes.detail(conn, route)["version"], AUTHOR, code="bre18x")
    renamed = routes.marks(conn)
    assert {e for e in renamed if renamed[e] != after[e]} == set(mine)
    # A stretch taken out of its only route is on the map no longer.
    routes.remove_leg(conn, route, routes.detail(conn, route)["version"], mine[-1], AUTHOR)
    assert mine[-1] not in routes.marks(conn) or mine[-1] in {e for e, f in lines.items() if len(f["properties"]["routes"]) > 1}
