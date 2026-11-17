"""Every area side by side: ground covered and how much of it is marked, a shared stretch counted once."""
import psycopg
import pytest
from conftest import role_dsn
from typer.testing import CliRunner

from stiflyt2.cli import app as cli
from stiflyt2.domain import report, routes

AUTHOR = "Test Editor"


@pytest.fixture
def conn(loaded):
    """A connection as the API role. Nothing a test does here is committed."""
    with psycopg.connect(role_dsn("stiflyt2_api")) as c:
        yield c
        c.rollback()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def bre(found):
    return next(r for r in found["areas"] if r["area"] == "bre")


def test_ground_is_counted_once_where_routes_share_it_and_marked_is_told_apart(conn):
    before = report.areas(conn)
    theirs = bre(before)["turrutebasen"]
    assert theirs["routes"] >= 5 and theirs["route_m"] > theirs["ground_m"] > 0      # routes overlap in Turrutebasen too
    assert theirs["marked_m"] + theirs["unmarked_m"] == pytest.approx(theirs["ground_m"], abs=3)
    assert before["all"]["turrutebasen"]["ground_m"] <= sum(r["turrutebasen"]["ground_m"] for r in before["areas"])
    was = bre(before)["yours"]

    a = routes.make(conn, candidate(conn, "bre18"), AUTHOR)
    b = routes.make(conn, candidate(conn, "bre14"), AUTHOR)        # all of it is a stretch of bre18 too
    la, lb = (routes.detail(conn, r)["length"]["on_foot_m"] for r in (a, b))
    now = bre(report.areas(conn))["yours"]
    assert (now["routes"] - was["routes"], now["done"] - was["done"]) == (2, 0)
    assert now["route_m"] - was["route_m"] == pytest.approx(la + lb, abs=3)
    assert now["ground_m"] - was["ground_m"] == pytest.approx(la, abs=3)             # the shared stretch once
    assert now["marked_m"] + now["unmarked_m"] + now["unknown_m"] == pytest.approx(now["ground_m"], abs=3)
    # The same numbers as the report of the one area.
    one = report.report(conn, "bre")["length"]
    assert (one["on_foot_m"], one["unmarked_m"], one["marking_unknown_m"]) == pytest.approx(
        (now["ground_m"], now["unmarked_m"], now["unknown_m"]), abs=3)

    # A stretch said to be unmarked moves from the one to the other; a glacier is never marked.
    alone, metres = conn.execute("""
        SELECT l.edge_id::text, ST_Length(ST_Transform(v.geom, 4326)::geography) FROM core.route_leg l
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision v ON v.id = e.current_revision_id
        WHERE l.route_id = %s AND NOT EXISTS (SELECT 1 FROM core.route_leg o WHERE o.edge_id = l.edge_id AND o.route_id = %s)
        ORDER BY l.seq LIMIT 1""", (a, b)).fetchone()
    routes.set_stretch(conn, alone, AUTHOR, marked=False)
    after = bre(report.areas(conn))["yours"]
    assert after["unmarked_m"] - now["unmarked_m"] == pytest.approx(metres, abs=2)
    assert now["marked_m"] - after["marked_m"] == pytest.approx(metres, abs=2) and after["ground_m"] == now["ground_m"]
    routes.set_stretch(conn, alone, AUTHOR, kind="glacier")
    assert bre(report.areas(conn))["yours"]["glacier_m"] - now["glacier_m"] == pytest.approx(metres, abs=2)

    # A route said to be done counts as done; a discontinued one is not in use and is left out.
    routes.review(conn, a, routes.detail(conn, a)["version"], AUTHOR)
    routes.discontinue(conn, b, routes.detail(conn, b)["version"], AUTHOR, "Gone")
    left = bre(report.areas(conn))["yours"]
    assert (left["routes"] - was["routes"], left["done"] - was["done"]) == (1, 1)
    assert left["ground_m"] - was["ground_m"] == pytest.approx(la, abs=3)

    # Only what one maintainer looks after; and somebody who maintains nothing has no areas.
    who = routes.detail(conn, a)["maintained_by"][0]
    theirs_only = report.areas(conn, who)
    assert theirs_only["maintainer"] == who and 0 < bre(theirs_only)["yours"]["ground_m"] <= left["ground_m"]
    assert bre(theirs_only)["turrutebasen"]["ground_m"] <= theirs["ground_m"]
    nobody = report.areas(conn, "Nobody at all")
    assert nobody["areas"] == [] and nobody["all"]["yours"]["ground_m"] == 0 == nobody["all"]["turrutebasen"]["ground_m"]

    text = report.areas_as_text(report.areas(conn))
    assert text.splitlines()[0].startswith("Ground covered by routes, in km.") and "\nbre " in text and "\nall " in text


def test_the_report_of_every_area_from_the_command_line(loaded):
    run = CliRunner().invoke(cli, ["report"])
    assert run.exit_code == 0 and "Turrutebasen" in run.output and "\nbre " in run.output
    one = CliRunner().invoke(cli, ["report", "--maintainer", "Nobody at all"])
    assert "Only what Nobody at all is said to maintain." in one.output
    assert CliRunner().invoke(cli, ["report", "bre"]).output.startswith("Area bre")


def test_who_maintains_routes_is_listed_by_the_ground_they_look_after(loaded):
    from fastapi.testclient import TestClient

    from stiflyt2.api.app import create_app

    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        found = report.maintainers(conn, least_m=1)
        assert found and [m["metres"] for m in found] == sorted((m["metres"] for m in found), reverse=True)
        assert all(m["name"] and m["areas"] >= 1 for m in found)
        assert len(report.maintainers(conn, least_m=10**9)) == 0
        # The ground one of them looks after is what the report says for that maintainer.
        first = found[0]
        assert report.areas(conn, first["name"])["all"]["turrutebasen"]["ground_m"] == pytest.approx(first["metres"], abs=3)
    with TestClient(create_app()) as client:
        said = client.get("/api/v1/maintainers").json()
        assert said["home"] == "DNT | DNT Oslo og omegn" and isinstance(said["maintainers"], list)
        assert client.get("/api/v1/report", params={"maintainer": first["name"]}).json()["maintainer"] == first["name"]
