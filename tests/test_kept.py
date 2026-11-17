"""What is slow to work out against the mapped lines is worked out once per stretch, and again when it must be."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import kept, proposals, routes, worklist

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def as_a_whole(conn, route_id, mapped):
    """The share of a route by mapped lines, taken for the route as one line: what the stretches must add up to."""
    return conn.execute(f"""
        SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {mapped} p WHERE ST_DWithin(p.geom, g.geom, 2000)) THEN NULL
               ELSE coalesce(ST_Length(ST_Intersection(g.geom, (
                        SELECT ST_Union(ST_Buffer(p.geom, 20)) FROM {mapped} p
                        WHERE ST_DWithin(p.geom, g.geom, 20)))), 0) / nullif(ST_Length(g.geom), 0) END
        FROM (SELECT ST_Collect(r.geom) AS geom FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
              JOIN core.edge_revision r ON r.id = e.current_revision_id
              WHERE l.route_id = %s AND r.kind NOT IN ('water', 'glacier')) g""", (route_id,)).fetchone()[0]


def test_how_much_of_a_route_lies_by_mapped_lines_is_kept_per_stretch(conn):
    a = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    b = routes.make(conn, candidate(conn, "bre14"), AUTHOR)        # all of it is a stretch of bre18 too
    mapped = routes.mapped_lines(conn)
    share = worklist.near_mapped(conn, [a, b], mapped)
    for route in (a, b):
        whole = as_a_whole(conn, route, mapped)
        assert (share[route] is None) == (whole is None)
        assert whole is None or share[route] == pytest.approx(whole, abs=0.005)

    # One answer per stretch as it is now, however many routes walk it.
    now = {r for (r,) in conn.execute("""
        SELECT e.current_revision_id::text FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = ANY(%s::uuid[]) AND r.kind NOT IN ('water', 'glacier')""", ([a, b],))}
    answers = kept.answers(conn, "near mapped")
    assert now <= set(answers)
    # Asked again, the answers are read, not worked out: one that is changed here shows in what comes back.
    one = next(iter(now))
    metres = answers[one][0]
    answers[one] = (metres, True, metres * 1000)
    assert worklist.near_mapped(conn, [a], mapped)[a] > 1

    # A stretch that is changed is a new revision: it has no answer yet, and gets one of its own.
    edge = conn.execute("SELECT edge_id::text FROM core.edge_revision WHERE id = %s", (one,)).fetchone()[0]
    routes.set_stretch(conn, edge, AUTHOR, marked=False)
    after = conn.execute("SELECT current_revision_id::text FROM core.edge WHERE id = %s", (edge,)).fetchone()[0]
    assert after != one and after not in answers
    again = worklist.near_mapped(conn, [a], mapped)[a]
    assert after in answers and (share[a] is None or again == pytest.approx(share[a], abs=1e-9))

    # When the mapped lines have been built anew, nothing kept is believed.
    kept._built = "another build"
    assert kept.answers(conn, "near mapped") == {}
    assert worklist.near_mapped(conn, [a], mapped)[a] == (None if share[a] is None else pytest.approx(share[a], abs=1e-9))


def test_proposals_come_out_the_same_when_what_was_tried_is_kept(conn, monkeypatch):
    routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    # Every stretch is tried, whatever Turrutebasen says it follows.
    follows = [f for (f,) in conn.execute("SELECT DISTINCT attrs->>'rutefolger' FROM source_index.trail_segment")]
    monkeypatch.setattr(proposals, "ALONG_A_WAY", tuple(f for f in follows if f))
    first = proposals.proposals(conn, "bre")
    assert kept.answers(conn, "snap")

    def not_again(conn, edge_id):
        raise AssertionError("the way over the mapped lines was looked for a second time")

    monkeypatch.setattr(routes, "_trace", not_again)
    assert proposals.proposals(conn, "bre") == first
