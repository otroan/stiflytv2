"""What to ask Kartverket to change in Turrutebasen, worked out from what is yours."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import corrections, places, routes

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_changes_are_told_by_turrutebasens_own_segment_ids(conn):
    route = routes.detail(conn, routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    # Made yours and left alone, a route asks for nothing.
    assert [c for c in corrections.corrections(conn, "bre") if "bre18" in c["rutenummer"]
            and c["change"] != "route name"] == []

    legs = conn.execute("""
        SELECT e.id::text, sr.external_id, ST_Length(r.geom),
               ST_X(m.p), ST_Y(m.p), ST_X(m.q), ST_Y(m.q)
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = e.id
             AND sr.source_key = 'turrutebasen',
             LATERAL (SELECT ST_Transform(ST_LineInterpolatePoint(r.geom, 0.5), 4326) AS p,
                             ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), 30, 30), 4326) AS q) m
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) DESC""", (route["id"],)).fetchall()
    (long_edge, long_id, _, glon, glat, dlon, dlat), (mid_edge, mid_id, *_), *_, (_, short_id, *_) = legs

    # The line pulled aside, a stretch said to be unmarked and a glacier, and one taken out.
    routes.bend(conn, long_edge, AUTHOR, (glon, glat), (dlon, dlat))
    routes.set_stretch(conn, mid_edge, AUTHOR, kind="glacier")
    version = routes.detail(conn, route["id"])["version"]
    short_edge = legs[-1][0]
    routes.remove_leg(conn, route["id"], version, short_edge, AUTHOR)

    found = {(c["change"], c["lokalid"]): c for c in corrections.corrections(conn, "bre")
             if "bre18" in c["rutenummer"]}
    moved = found["geometry", long_id]
    assert 30 < moved["offset_m"] < 45 and moved["geometry"]["type"] == "LineString"
    assert moved["was_geometry"]["coordinates"] != moved["geometry"]["coordinates"]
    assert moved["versjonid"] and moved["text"].startswith("Move the line: it lies up to ")
    changed = found["attributes", mid_id]
    assert changed["was"]["merking"] == "JA" and changed["now"] == {
        "merking": "NEI", "skilting": "JA", "rutefolger": "(glacier)"}
    off = found["remove from route", short_id]
    assert off["was"] == {"rutenummer": "bre18"} and "bre18" in off["text"]
    assert set(found) == {("geometry", long_id), ("attributes", mid_id), ("remove from route", short_id)}

    # Carried on along a segment of another route, and joined to a junction further on.
    onward = conn.execute("""
        SELECT t.id FROM source_index.trail_segment t, core.node n
        WHERE n.id = ANY(%s::uuid[]) AND ST_DWithin(t.geom, n.geom, 1) AND NOT 'bre18' = ANY(t.codes)
        ORDER BY t.id LIMIT 1""", ([e["id"] for e in routes.detail(conn, route["id"])["ends"]],)).fetchone()
    if onward:
        routes.add_stretch(conn, route["id"], routes.detail(conn, route["id"])["version"], AUTHOR,
                           segment_id=onward[0])
        added = [c for c in corrections.corrections(conn, "bre") if c["change"] == "add to route"]
        assert [(c["lokalid"], c["now"]) for c in added] == [(onward[0], {"rutenummer": ["bre18"]})]

    # A name is proposed once a person has looked at the route and it runs between two places.
    for hut in ("place:799507", "place:27269"):
        places.adopt(conn, hut, AUTHOR)
    assert not [c for c in corrections.corrections(conn, "bre") if c["change"] == "route name"
                and c["rutenummer"] == ["bre18"]]       # not said to be done yet: its name is a guess
    routes.review(conn, route["id"], routes.detail(conn, route["id"])["version"], AUTHOR)
    names = [c for c in corrections.corrections(conn, "bre") if c["change"] == "route name"
             and c["rutenummer"] == ["bre18"]]
    theirs = conn.execute("SELECT name FROM source_index.route WHERE label = 'bre18'").fetchone()[0]
    ours = routes.detail(conn, route["id"])["generated_name"]
    if ours and not all(p.lower() in (theirs or "").lower() for p in ours.split(" – ")):
        assert [c["now"] for c in names] == [{"rutenavn": ours}]
    else:
        assert names == []

    # As a file: every change is a feature, and a moved line comes as it is now as well.
    everything = corrections.corrections(conn, "bre", srid=25833)
    file = corrections.as_geojson(everything, 25833)
    assert file["crs"]["properties"]["name"].endswith("25833")
    states = [f["properties"]["state"] for f in file["features"] if f["properties"]["lokalid"] == long_id]
    assert sorted(states) == ["in Turrutebasen now", "proposed"]
    x, y = next(f["geometry"]["coordinates"][0] for f in file["features"]
                if f["properties"]["lokalid"] == long_id)
    assert 0 < x < 300_000 and 6_800_000 < y < 6_900_000
    text = corrections.as_text(everything)
    assert f"{len(everything)} corrections to Turrutebasen" in text and long_id in text


def test_a_few_metres_inside_one_arrival_point_are_reported_as_a_segment_to_remove(conn):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    # A stretch whose two ends have become the same junction: nothing to walk.
    edge, lokalid = conn.execute("""
        SELECT e.id::text, sr.external_id FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = e.id
             AND sr.source_key = 'turrutebasen'
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY ST_Length(r.geom) LIMIT 1""", (route,)).fetchone()
    a, = conn.execute("""SELECT r.a_node::text FROM core.edge e
                         JOIN core.edge_revision r ON r.id = e.current_revision_id WHERE e.id = %s""",
                      (edge,)).fetchone()
    routes._revise(conn, edge, AUTHOR, nodes=(a, a))
    found = [c for c in corrections.corrections(conn, "bre") if c["lokalid"] == lokalid]
    assert [c["change"] for c in found] == ["delete"]
    assert "begins and ends at the same junction" in found[0]["text"]


def test_routes_are_handed_on_whole_with_what_has_changed(conn):
    from stiflyt2.domain import export

    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    routes.make(conn, candidate(conn, "bre19"), AUTHOR)
    before = {f["properties"]["code"]: f for f in export.routes_geojson(conn, "bre")["features"]}
    one = before["bre18"]
    # One unbroken line, though the segments it was made from meet only to within a metre.
    assert one["id"] == str(route) and one["geometry"]["type"] == "LineString"
    assert one["properties"]["turrutebasen_rutenummer"] == "bre18"
    assert one["properties"]["on_foot_m"] == routes.detail(conn, route)["length"]["on_foot_m"]
    assert abs(one["geometry"]["coordinates"][0][0]) < 180       # degrees, unless asked otherwise

    # A change to one route: it is the one that is handed on as changed, under the same id.
    routes.update(conn, route, routes.detail(conn, route)["version"], AUTHOR, maintainers=["Luster Turlag"],
                  everywhere=True, code="bre118")
    changed = export.routes_geojson(conn, "bre", changed_only=True, srid=25833)
    told = {f["id"]: f["properties"] for f in changed["features"]}
    assert str(route) in told and told[str(route)]["code"] == "bre118"
    assert told[str(route)]["earlier_codes"] == "bre18" and told[str(route)]["maintained_by"] == "Luster Turlag"
    assert "vedlikeholdsansvarlig" in told[str(route)]["what_changed"]
    assert before["bre19"]["id"] not in told or told[before["bre19"]["id"]]["changed"]
    assert changed["crs"]["properties"]["name"].endswith("25833")


def test_a_withdrawn_route_that_others_run_over_has_its_number_taken_off_the_segments(conn):
    """A misspelt number, or the same route under two numbers. A route merely left to others says nothing."""
    kept = routes.make(conn, candidate(conn, "bre21"), AUTHOR)
    twin = routes.make(conn, candidate(conn, "bre62"), AUTHOR)
    alone = routes.make(conn, candidate(conn, "bre14"), AUTHOR)
    shared = conn.execute("""SELECT count(*) FROM core.route_leg a JOIN core.route_leg b ON b.edge_id = a.edge_id
                             WHERE a.route_id = %s AND b.route_id = %s""", (kept, twin)).fetchone()[0]
    assert shared == routes.detail(conn, twin)["length"]["legs"] == 3      # every stretch of the one is the other's too

    def about(code):
        return [c for c in corrections.corrections(conn, "bre")
                if c["change"] == "remove from route" and c["was"].get("rutenummer") == code]

    assert about("bre62") == [] and about("bre14") == []
    routes.retire(conn, twin, AUTHOR)
    routes.retire(conn, alone, AUTHOR)
    told = about("bre62")
    assert len(told) == 3 and all(c["lokalid"] and "bre62" in c["rutenummer"] for c in told)
    assert all(c["text"].startswith("Take rutenummer bre62 off the segment (") and
               "bre62 is withdrawn as no route of its own, and bre21 runs here" in c["text"] for c in told)
    assert about("bre14") == []       # nothing else of yours runs there: it is still a route, only not yours
    from stiflyt2.domain import worklist
    rows = {r["code"]: r for r in worklist.worklist(conn, "bre")["all"] if r.get("withdrawn")}
    assert "Kartverket is asked to take the number off" in rows["bre62"]["problems"][0]
    assert rows["bre14"]["problems"] == ["You withdrew it: it is left as a candidate"]
    # Made yours again, it is a route after all.
    routes.make(conn, candidate(conn, "bre62"), AUTHOR)
    assert about("bre62") == []


def test_a_withdrawn_route_of_a_few_metres_has_its_number_taken_off_whatever_runs_there(conn, monkeypatch):
    """jot95: 50 m at Gjendesheim with a number of its own. Withdrawn, it was a number on a stray bit of line."""
    from stiflyt2.domain import worklist

    alone = routes.make(conn, candidate(conn, "bre14"), AUTHOR)       # one segment, nothing else of yours on it
    metres = routes.detail(conn, alone)["length"]["on_foot_m"]
    routes.retire(conn, alone, AUTHOR)

    def about():
        return [c for c in corrections.corrections(conn, "bre")
                if c["change"] == "remove from route" and c["was"].get("rutenummer") == "bre14"]

    assert about() == []                                              # some hundred metres: a route, only not yours
    monkeypatch.setattr(corrections, "STRAY_M", metres + 50)
    told, = about()
    assert told["text"].startswith("Take rutenummer bre14 off the segment (")
    assert told["text"].endswith("bre14 is withdrawn as no route of its own: all of it is "
                                 f"{round(conn.execute('SELECT length_m FROM source_index.route WHERE label = %s', ('bre14',)).fetchone()[0])} m")
    row, = [r for r in worklist.worklist(conn, "bre")["all"] if r["code"] == "bre14"]
    assert row["problems"] == ["You withdrew it: a number on a stray bit of line, which Kartverket is asked to take off"]


def test_a_stray_bit_taken_out_of_its_route_is_not_offered_as_a_route_again(conn):
    """jot50/1: six metres with the number of jot50 on them, away from the route."""
    from stiflyt2.domain import worklist

    stray, real = candidate(conn, "bre17"), candidate(conn, "bre14")     # two candidates of one segment each
    route = routes.make(conn, stray, AUTHOR)
    routes.add_piece(conn, route, routes.detail(conn, route)["version"], real, AUTHOR)
    first, = conn.execute("""SELECT l.edge_id::text FROM core.route_leg l
        JOIN core.source_reference sr ON sr.subject_type = 'edge' AND sr.subject_id = l.edge_id
        JOIN source_index.route_segment rs ON rs.segment_id = sr.external_id AND rs.route_id = %s
        WHERE l.route_id = %s""", (stray, route)).fetchone()
    routes.remove_leg(conn, route, routes.detail(conn, route)["version"], first, AUTHOR)
    assert routes.owner_of(conn, stray) is None and routes.owner_of(conn, real) == str(route)

    listed = worklist.worklist(conn, "bre")["all"]
    row, = [r for r in listed if r["code"] == "bre17" and r["kind"] == "candidate"]
    # Every row of the list says who is said to maintain the route, yours or not yet: what the list is narrowed by.
    assert all(isinstance(r["maintainers"], list) for r in listed) and row["maintainers"] == ["DNT | DNT Oslo og omegn"]
    assert all(r["maintainers"] for r in listed if r["kind"] == "yours")
    assert row["withdrawn"] and row["problems"][0].startswith("Taken out of bre17, which was made from it")
    # Making routes in bulk leaves it alone, and Kartverket is asked to take the number off.
    assert routes.make_default(conn, "bre")["left withdrawn"] >= 1 and routes.owner_of(conn, stray) is None
    assert [c["change"] for c in corrections.corrections(conn, "bre")
            if c["was"].get("rutenummer") == "bre17"] == ["remove from route"]


def test_kartverket_is_asked_to_take_a_maintainer_off_where_a_person_says_nobody_maintains_it(conn):
    route = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    theirs = sorted({m for (ms,) in conn.execute("""
        SELECT rs.maintainers FROM source_index.route c JOIN source_index.route_segment rs ON rs.route_id = c.id
        WHERE c.label = 'bre18'""") for m in ms})
    assert theirs and routes.detail(conn, route)["maintained_by"] == theirs       # made with who Turrutebasen has

    def asked():
        return [c for c in corrections.corrections(conn, "bre") if c["rutenummer"] == ["bre18"]
                and "vedlikeholdsansvarlig" in c["text"]]

    assert asked() == []
    # Nobody, for the route and every stretch of it: that is said, and Kartverket is asked to take them off.
    routes.update(conn, route, routes.detail(conn, route)["version"], AUTHOR, maintainers=[], everywhere=True)
    said = asked()
    assert len(said) >= 1 and all(c["change"] == "route attribute" and c["now"] == {"vedlikeholdsansvarlig": []} for c in said)
    assert sorted({m for c in said for m in c["was"]["vedlikeholdsansvarlig"]}) == theirs
    assert all(c["text"].startswith("Take ") and "off as vedlikeholdsansvarlig for bre18" in c["text"]
               and c["text"].endswith("nobody is said to maintain it there") for c in said)
    # One stretch is somebody's after all: for that one a change is asked, as before.
    leg = routes.detail(conn, route)["version"]
    edge = conn.execute("SELECT edge_id::text FROM core.route_leg WHERE route_id = %s ORDER BY seq LIMIT 1", (route,)).fetchone()[0]
    routes.set_leg(conn, route, leg, edge, AUTHOR, ["Luster Turlag"])
    assert [c["now"]["vedlikeholdsansvarlig"] for c in asked() if c["text"].startswith("Change ")] == [["Luster Turlag"]]
    # A route that is given up has its number asked off instead: nothing is asked about who maintained it.
    routes.discontinue(conn, route, routes.detail(conn, route)["version"], AUTHOR, "another route runs there")
    assert asked() == []
    assert any("Take rutenummer bre18 off" in c["text"] for c in corrections.corrections(conn, "bre"))
