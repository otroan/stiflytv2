"""Who maintains a route: names that Turrutebasen spells in more than one way are one maintainer."""
import psycopg
import pytest
from conftest import role_dsn

from stiflyt2.domain import places, report, routes, whose


def test_names_are_compared_without_case_or_the_dnt_in_front_and_dnt_alone_is_dnt_oslo_og_omegn():
    assert {whose.norm(n) for n in ("DNT | DNT Oslo og omegn", "DNT Oslo og Omegn", " DNT |  DNT Oslo og Omegn ",
                                    "dnt|dnt oslo og omegn")} == {"dnt oslo og omegn"}
    assert whose.kin("DNT | DNT Oslo og omegn") == ["dnt oslo og omegn", "dnt"]
    assert whose.kin("DNT | DNT Lillehammer") == ["dnt lillehammer"] and whose.kin("DNT") == ["dnt"] and whose.kin(None) is None


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def test_an_area_is_made_yours_for_one_maintainer_only(conn):
    # As the database compares them, the same as here.
    said = conn.execute("SELECT m FROM unnest(%s::text[]) m WHERE " + whose.sql("ARRAY[m]").replace("%(kin)s", "%s"),
                        (["DNT Oslo og Omegn", "DNT", "DNT | DNT Lillehammer", "Lom kommune"],
                         whose.kin("DNT | DNT Oslo og omegn"), whose.kin("DNT | DNT Oslo og omegn"))).fetchall()
    assert [m for (m,) in said] == ["DNT Oslo og Omegn", "DNT"]

    clean = {label for (label,) in conn.execute(
        "SELECT r.label FROM source_index.route r JOIN source_index.route_shape s ON s.route_id = r.id "
        "WHERE r.area_prefix = 'bre' AND s.shape = 'line'")}
    before = {code for (code,) in conn.execute("SELECT code FROM core.route WHERE status = 'active'")}
    # For a maintainer that looks after none of them: nothing is made, no place and no route.
    places_before = conn.execute("SELECT count(*) FROM core.place WHERE status = 'confirmed'").fetchone()[0]
    assert places.make_default(conn, "bre", "DNT | Bergen og Hordaland Turlag")["made"] == 0
    assert routes.make_default(conn, "bre", "DNT | Bergen og Hordaland Turlag")["made"] == 0
    assert conn.execute("SELECT count(*) FROM core.place WHERE status = 'confirmed'").fetchone()[0] == places_before
    # For DNT Oslo og omegn, in any spelling: theirs are made.
    places.make_default(conn, "bre", "dnt oslo og omegn")
    done = routes.make_default(conn, "bre", "DNT Oslo og Omegn")
    made = {code for (code,) in conn.execute("SELECT code FROM core.route WHERE status = 'active'")} - before
    assert made and made <= clean and done["made"] == len(made)
    # And the numbers of the area for that maintainer count the variants and "DNT" alone as theirs.
    every = report.areas(conn)["all"]["turrutebasen"]["ground_m"]
    home = report.areas(conn, "DNT | DNT Oslo og omegn")["all"]["turrutebasen"]["ground_m"]
    assert 0 < home <= every and report.areas(conn, "dnt oslo og omegn")["all"]["turrutebasen"]["ground_m"] == home
