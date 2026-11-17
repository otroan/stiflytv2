"""The network checks: spots where the owned graph is not joined although the map looks it."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import network, routes

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def drawn(conn, route_id, edge, start_m, end_m):
    """Add a straight stretch to a route, at right angles to an edge's midpoint.

    It runs from start_m to end_m off the edge; a negative distance is on the other side.
    """
    wkt, = conn.execute("""
        SELECT ST_AsEWKT(ST_MakeLine(ST_Translate(p, %(s)s * sin(az), %(s)s * cos(az)),
                                     ST_Translate(p, %(e)s * sin(az), %(e)s * cos(az))))
        FROM (SELECT ST_LineInterpolatePoint(r.geom, 0.5) AS p,
                     ST_Azimuth(ST_LineInterpolatePoint(r.geom, 0.49),
                                ST_LineInterpolatePoint(r.geom, 0.51)) + pi() / 2 AS az
              FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
              WHERE e.id = %(edge)s) x""", {"edge": edge, "s": start_m, "e": end_m}).fetchone()
    a, b = (routes._node_at(conn, conn.execute(
        f"SELECT {end}(ST_GeomFromEWKT(%s))", (wkt,)).fetchone()[0])
        for end in ("ST_StartPoint", "ST_EndPoint"))
    new = routes._new_edge(conn, AUTHOR, "ST_GeomFromEWKT(%(wkt)s)", {"wkt": wkt}, a, b,
                           "trail", "drawn", True, True)
    routes._add_legs(conn, route_id, [new])
    return new


def kinds(found):
    return sorted(f["kind"] for f in found["findings"])


def test_a_route_that_hangs_together_has_only_its_two_ends_to_show(conn):
    route = routes.make(conn, routes_candidate(conn, "bre18"), AUTHOR)
    found = network.validate(conn, [route])
    assert (found["stretches"], found["nodes"], found["parts"], found["largest_share"]) == (4, 5, 1, 1.0)
    # No huts are places here, so both ends are ends at nothing or where another candidate carries on.
    assert len(found["findings"]) == 2 and set(kinds(found)) <= {"dead end", "open end"}
    assert all(61 < f["lat"] < 63 and 6 < f["lon"] < 9 for f in found["findings"])
    assert network.validate(conn, []) == {"stretches": 0, "nodes": 0, "parts": 0,
                                          "largest_share": None, "findings": []}


def routes_candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_ends_that_stop_short_touch_or_cross_are_found(conn):
    route = routes.make(conn, routes_candidate(conn, "bre18"), AUTHOR)
    longest = str(conn.execute("""
        SELECT e.id FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC LIMIT 1""", (route,)).fetchone()[0])
    base = kinds(network.validate(conn, [route]))

    def extra(found):
        """What the drawn stretches added, beyond the route's own two ends."""
        rest = kinds(found)
        for kind in base:
            rest.remove(kind)
        return rest

    # A stretch that stops 10 m short of the route: so close it was meant to meet it.
    with conn.transaction(force_rollback=True):
        drawn(conn, route, longest, 10, 210)
        found = network.validate(conn, [route])
        assert found["parts"] == 2 and extra(found) == ["dead end", "island", "near"]
        texts = {f["kind"]: f["text"] for f in found["findings"]}
        assert texts["near"] == "An end of bre18 stops 10 m short of bre18"
        assert texts["island"].endswith("the nearest stretch is 10 m away")
        # So near, the finding is a spot: where the two come nearest each other.
        assert not next(f for f in found["findings"] if f["kind"] == "island").get("whole")

    # A stretch 3 km off: it stands apart as a whole. No spot is pointed at; all of it is given.
    with conn.transaction(force_rollback=True):
        drawn(conn, route, longest, 3000, 3200)
        apart = next(f for f in network.validate(conn, [route])["findings"] if f["kind"] == "island")
        assert apart["whole"] and apart["text"].endswith(" km away")
        west, south, east, north = apart["bbox"]
        assert west <= apart["lon"] <= east and south <= apart["lat"] <= north and east - west < 0.02

    # A stretch that ends on the route, where the route is not divided.
    with conn.transaction(force_rollback=True):
        drawn(conn, route, longest, 0, 200)
        found = network.validate(conn, [route])
        assert found["parts"] == 2 and extra(found) == ["dead end", "island", "touch"]
        texts = {f["kind"]: f["text"] for f in found["findings"]}
        assert texts["touch"].startswith("An end of bre18 lies on bre18, which is not divided")
        assert texts["island"].endswith("it touches or crosses it without a junction")

    # A stretch straight across the route, with no node where they cross.
    with conn.transaction(force_rollback=True):
        drawn(conn, route, longest, -100, 100)
        found = network.validate(conn, [route])
        assert extra(found) == ["crossing", "dead end", "dead end", "island"]
        assert "Two stretches of bre18 cross without a junction" in [f["text"] for f in found["findings"]]

    # Two loose ends 10 m apart, facing each other: said once, not from each side.
    with conn.transaction(force_rollback=True):
        drawn(conn, route, longest, 60, 90)
        drawn(conn, route, longest, 100, 300)
        found = network.validate(conn, [route])
        assert found["parts"] == 3 and extra(found).count("near") == 1
        assert "An end of bre18 and an end of bre18 are 10 m apart" in [f["text"] for f in found["findings"]]

    # A short spur that leaves from a real node is joined: nothing but its own far end.
    with conn.transaction(force_rollback=True):
        end = conn.execute("""SELECT ST_AsEWKT(ST_MakeLine(n.geom, ST_Translate(n.geom, 20, 20)))
                              FROM core.edge e JOIN core.edge_revision r ON r.id = e.current_revision_id
                              JOIN core.node n ON n.id = r.a_node WHERE e.id = %s""",
                           (longest,)).fetchone()[0]
        a, b = (routes._node_at(conn, conn.execute(
            f"SELECT {pt}(ST_GeomFromEWKT(%s))", (end,)).fetchone()[0])
            for pt in ("ST_StartPoint", "ST_EndPoint"))
        routes._add_legs(conn, route, [routes._new_edge(
            conn, AUTHOR, "ST_GeomFromEWKT(%(wkt)s)", {"wkt": end}, a, b, "trail", "drawn", True, True)])
        found = network.validate(conn, [route])
        assert found["parts"] == 1 and not {"near", "touch", "crossing", "island"} & set(kinds(found))


def test_a_path_that_comes_out_on_a_road_joins_the_road_there():
    """The network that snapping follows: lines join where one ends on the middle of another."""
    road = (("vegnett", "r"), [(0.0, 0.0), (100.0, 0.0), (200.0, 0.0)], [(0.3, 60.0, 0.4)])
    path = (("fkb_wfs", "p"), [(60.0, 80.0), (60.0, 0.4)], [])
    net = routes._Paths([road, path])
    up, along = net.attach((62.0, 70.0), 25), net.attach((180.0, 3.0), 25)
    assert up["tied"] and along["tied"] and up["ref"] == ("fkb_wfs", "p")
    line, used = net.way(up, along)
    # Down the path, onto the road where the path ends, and along the road.
    assert used == {("fkb_wfs", "p"), ("vegnett", "r")}
    assert line[0] == (60.0, 70.0) and line[-1] == (180.0, 0.0)
    assert 185 < routes._Paths.along(line) < 195
    # Left uncut, the two do not join, and a point far from every line is not tied to any.
    apart = routes._Paths([(road[0], road[1], []), path])
    assert apart.way(apart.attach((62.0, 70.0), 25), apart.attach((180.0, 3.0), 25)) is None
    assert net.attach((500.0, 500.0), 25) == {"tied": False, "metres": pytest.approx(583.1, abs=0.1)}
