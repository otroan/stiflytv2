"""Seasons: DNT's winter routes are routes of their own, made of stretches no summer route shares."""
import psycopg
import pytest
from conftest import role_dsn
from fastapi.testclient import TestClient

from stiflyt2 import seasons
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain import places, routes

AUTHOR = "Test Editor"
V1 = "/api/v1"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label, table="route"):
    return conn.execute(f"SELECT id FROM source_index.{table} WHERE label = %s", (label,)).fetchone()[0]


def season_of_edges(conn, route_id):
    return {s for (s,) in conn.execute("""SELECT DISTINCT e.season FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                                          WHERE l.route_id = %s""", (route_id,))}


def test_a_code_says_its_season_and_the_season_its_twin():
    assert (seasons.of_code("jot11v"), seasons.of_code("JOT11"), seasons.of_code("jot77av")) == ("winter", "summer", "winter")
    assert (seasons.twin("jot11"), seasons.twin("jot11v")) == ("jot11v", "jot11")
    assert seasons.code_fits("jot11v", "winter") is None and seasons.code_fits("jot11", "summer") is None
    assert "with a v after it (jot11v)" in seasons.code_fits("jot11", "winter")
    assert "a winter route's code" in seasons.code_fits("jot11v", "summer")
    assert seasons.known(None) == "summer"
    with pytest.raises(seasons.Unknown):
        seasons.known("autumn")


def test_a_winter_route_is_made_from_the_winter_candidate_of_stretches_of_its_own(conn):
    summer = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    winter = routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR)
    made = routes.detail(conn, winter)
    assert (made["code"], made["season"], routes.detail(conn, summer)["season"]) == ("bre8v", "winter", "summer")
    assert season_of_edges(conn, winter) == {"winter"} and season_of_edges(conn, summer) == {"summer"}
    assert made["length"]["legs"] >= 1 and made["maintained_by"] == ["DNT | DNT Oslo og Omegn"]
    # What it was made of is said, with the layer of Turrutebasen it came from.
    assert {r for (r,) in conn.execute("""
        SELECT DISTINCT sr.detail->>'layer' FROM core.route_leg l JOIN core.source_reference sr
        ON sr.subject_type = 'edge' AND sr.subject_id = l.edge_id AND sr.ref_type = 'adopted_from'
        WHERE l.route_id = %s""", (winter,))} == {"skiloype"}
    assert routes.owner_of(conn, candidate(conn, "bre8v", "winter_route")) == winter
    with pytest.raises(places.Conflict, match="already part"):
        routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR)
    # bre12v shares a segment with bre8v: made a route of its own, it shares that stretch, of the winter.
    other = routes.make(conn, candidate(conn, "bre12v", "winter_route"), AUTHOR)
    assert season_of_edges(conn, other) == {"winter"} and conn.execute("""
        SELECT count(*) FROM core.route_leg a JOIN core.route_leg b ON b.edge_id = a.edge_id
        WHERE a.route_id = %s AND b.route_id = %s""", (winter, other)).fetchone()[0] == 1
    # A winter route's code has a v after the number.
    with pytest.raises(places.Invalid, match="a winter route's code is the summer route's with a v"):
        routes.update(conn, winter, routes.detail(conn, winter)["version"], AUTHOR, code="bre8")
    with pytest.raises(places.Invalid, match="a winter route's code"):
        routes.update(conn, summer, routes.detail(conn, summer)["version"], AUTHOR, code="bre18v")


def test_no_route_runs_over_a_stretch_of_the_other_season(conn):
    summer = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    winter = routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR)
    theirs = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s LIMIT 1", (winter,)).fetchone()[0]
    with pytest.raises(places.Invalid, match="a summer route does not run over a winter route's stretch"):
        routes._add_legs(conn, summer, [theirs])
    with pytest.raises(places.Invalid, match="different seasons"):
        routes.absorb(conn, summer, winter, AUTHOR)
    with pytest.raises(places.Invalid, match="its own season only"):
        routes.add_piece(conn, summer, routes.detail(conn, summer)["version"],
                         candidate(conn, "bre12v", "winter_route"), AUTHOR)
    # The database refuses it too, whatever the code that asks.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO core.route_leg (route_id, seq, edge_id) VALUES (%s, 99, %s)", (summer, theirs))


def test_a_way_is_found_over_the_stretches_of_its_own_season(conn):
    winter = routes.detail(conn, routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR))
    ends = [(e["lon"], e["lat"]) for e in winter["ends"]][:2]
    if len(ends) < 2:
        pytest.skip("bre8v comes in pieces here")
    # The winter's way between its ends is the winter route itself.
    way = routes.plan(conn, ends, season="winter")
    assert way["new"] == 0 and way["stretches"] == winter["length"]["legs"]
    # Asked of the summer, the same points find no winter stretch: what it lays is of the summer, or nothing.
    try:
        summer_way = routes.plan(conn, ends, season="summer")
        assert not set(summer_way.get("edges", [])) & {str(e) for (e,) in conn.execute(
            "SELECT edge_id FROM core.route_leg WHERE route_id = %s", (winter["id"],))}
    except (places.Invalid, routes.NoWay):
        pass
    # Laid out, a winter route takes its season from its code, and must have a winter code.
    twin = routes.detail(conn, routes.lay_out(conn, AUTHOR, ends, "bre99v"))
    assert twin["season"] == "winter" and season_of_edges(conn, twin["id"]) == {"winter"}
    with pytest.raises(places.Invalid, match="a winter route's code"):
        routes.lay_out(conn, AUTHOR, ends, "bre98", season="winter")


def test_the_map_and_the_area_show_the_season_asked_for(loaded, monkeypatch):
    from stiflyt2.domain import undo as routes_undo

    monkeypatch.setattr(settings, "editor", "Test Editor")
    tester = "Season Tester"        # not the page's editor: what is taken back here is nothing the pages could bring back
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        try:
            with c.transaction():
                routes.make(c, candidate(c, "bre18"), tester)
            with c.transaction():
                routes.make(c, candidate(c, "bre8v", "winter_route"), tester)
            summer_lines = {f["properties"]["edge_id"] for f in routes.collection(c)["features"]}
            winter_lines = {f["properties"]["edge_id"] for f in routes.collection(c, season="winter")["features"]}
            assert summer_lines and winter_lines and not summer_lines & winter_lines
            assert set(routes.marks(c, season="winter")) == {str(e) for e in winter_lines}
            with TestClient(create_app()) as client:
                got = client.get(f"{V1}/owned/routes.geojson", params={"season": "winter"}).json()
                assert {f["properties"]["codes"] for f in got["features"]} == {"bre8v"}
                assert client.get(f"{V1}/owned/routes.geojson", params={"season": "autumn"}).status_code == 422
                assert "bre8v" not in {f["properties"]["codes"] for f in client.get(f"{V1}/owned/routes.geojson").json()["features"]}
        finally:
            while (latest := routes_undo.last(c)) and latest["txid"] > start:
                with c.transaction():
                    routes_undo.undo(c, tester)


def test_a_winter_route_goes_with_the_summer_route_of_its_number_and_follows_its_code(conn):
    from stiflyt2.domain import lookup

    summer = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    winter = routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR)
    lone = routes.make(conn, candidate(conn, "bre12v", "winter_route"), AUTHOR)
    assert routes.detail(conn, winter)["twin"] is None
    # Given the number bre8, the summer route has bre8v as its winter route, and the other way round.
    routes.update(conn, summer, routes.detail(conn, summer)["version"], AUTHOR, code="bre8")
    assert routes.detail(conn, winter)["twin"]["code"] == "bre8"
    assert (routes.detail(conn, summer)["twin"]["code"], routes.detail(conn, summer)["twin"]["season"]) == ("bre8v", "winter")
    # Its number changed, the winter route follows; where the winter's number is taken, neither changes.
    routes.update(conn, summer, routes.detail(conn, summer)["version"], AUTHOR, code="bre9")
    assert routes.detail(conn, winter)["code"] == "bre9v"
    assert conn.execute("""SELECT payload->>'following' FROM history.event WHERE subject_id = %s
                           AND type = 'route.code_changed' ORDER BY seq DESC LIMIT 1""", (winter,)).fetchone()[0] == "bre9"
    routes.update(conn, lone, routes.detail(conn, lone)["version"], AUTHOR, code="bre13v")
    with pytest.raises(places.Conflict, match="bre9v would follow bre13 to it"):
        routes.update(conn, summer, routes.detail(conn, summer)["version"], AUTHOR, code="bre13")
    assert routes.detail(conn, summer)["code"] == "bre9"
    # Among the winter's checks: a winter route with no summer route of its number.
    found = [c for c in lookup.checks(conn, "bre", season="winter")["checks"] if c["kind"] == "twin"]
    assert any("bre13v has no summer route bre13" in c["text"] for c in found)
    assert not any("bre9v has no summer route" in c["text"] for c in found)


def test_a_winter_route_made_from_turrutebasen_ends_at_the_huts_its_line_stops_short_of(conn):
    # A hut 60 m off one end of bre8v's line in Turrutebasen, where the summer's routes would end.
    end = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT (ST_DumpPoints(ST_Boundary(ST_LineMerge(geom)))).geom AS p
                          FROM source_index.winter_route WHERE label = 'bre8v') x ORDER BY ST_X(p) LIMIT 1""").fetchone()
    node = conn.execute("INSERT INTO core.node (geom) VALUES (ST_SetSRID(ST_MakePoint(%s, %s), 25833)) RETURNING id::text",
                        (end[0] + 60, end[1])).fetchone()[0]
    hut = places.at_node(conn, node, AUTHOR, name="Trial hut")
    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR))
    at = [e["place"] for e in made["ends"]]
    assert "Trial hut" in at and made["shape"] == "line"
    assert conn.execute("SELECT place_id::text FROM core.node WHERE id = %s", (node,)).fetchone()[0] == hut
    assert any(e["id"] == node for e in made["ends"])                # the hut's own arrival point, not a spot beside it
    moved = [h for h in made["history"] if h["type"] == "route.end_moved"]
    assert moved and moved[0]["payload"]["to"] == "Trial hut" and moved[0]["payload"]["as_made"] is True
    # The other end, with no place within 200 m, is left where Turrutebasen's line ends.
    assert None in at


def test_a_winter_route_whose_line_passes_beside_a_hut_ends_at_the_hut_itself(conn):
    # A hut 8 m beside bre8v's line in Turrutebasen, 40 m in from one end of it: the way there stops on the line
    # first, at the spot nearest the hut. It is carried on from there to the hut's own arrival point.
    line = conn.execute("""SELECT CASE WHEN ST_X(ST_StartPoint(g)) <= ST_X(ST_EndPoint(g)) THEN g ELSE ST_Reverse(g) END
                           FROM (SELECT ST_GeometryN(ST_LineMerge(geom), 1) AS g FROM source_index.winter_route
                                 WHERE label = 'bre8v') x""").fetchone()[0]
    (x1, y1), (x2, y2) = [conn.execute("SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_LineInterpolatePoint(%s::geometry, %s / ST_Length(%s::geometry)) AS p) x",
                                       (line, m, line)).fetchone() for m in (35, 45)]
    d = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    node = conn.execute("INSERT INTO core.node (geom) VALUES (ST_SetSRID(ST_MakePoint(%s, %s), 25833)) RETURNING id::text",
                        ((x1 + x2) / 2 - 8 * (y2 - y1) / d, (y1 + y2) / 2 + 8 * (x2 - x1) / d)).fetchone()[0]
    places.at_node(conn, node, AUTHOR, name="Hut beside the line")
    made = routes.detail(conn, routes.make(conn, candidate(conn, "bre8v", "winter_route"), AUTHOR))
    assert any(e["id"] == node for e in made["ends"]), [(e["place"], e["lon"], e["lat"]) for e in made["ends"]]
    assert made["shape"] == "line" and "Hut beside the line" in [e["place"] for e in made["ends"]]
