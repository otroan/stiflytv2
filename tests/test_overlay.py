"""The overlay as a whole: taken away it is a new installation, and a copy puts it back exactly."""
import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from typer.testing import CliRunner

from stiflyt2 import overlay
from stiflyt2.cli import app
from stiflyt2.domain import routes, undo

AUTHOR = "Test Editor"
STATE = {
    "routes": "SELECT id, code, status, version, maintainers FROM core.route ORDER BY id",
    "legs": "SELECT route_id, seq, edge_id FROM core.route_leg ORDER BY 1, 2",
    "nodes": "SELECT id, ST_AsEWKT(geom), status FROM core.node ORDER BY id",
    "revisions": "SELECT id, edge_id, rev_no, ST_AsEWKT(geom) FROM core.edge_revision ORDER BY id",
    "places": "SELECT id, name, status FROM core.place ORDER BY id",
    "events": "SELECT seq, type, subject_id, payload, txid FROM history.event ORDER BY seq",
    "undo": "SELECT id, txid, tbl, op, key FROM core.undo_log ORDER BY id",
    "succession": "SELECT * FROM core.succession ORDER BY id",
}


def state():
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        return {name: conn.execute(sql).fetchall() for name, sql in STATE.items()}


def test_the_overlay_is_taken_away_and_put_back_exactly(loaded, tmp_path, monkeypatch):
    monkeypatch.setenv("STIFLYT2_MIGRATE_URL", f"postgresql+psycopg:///{TEST_DB}")
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        candidate = conn.execute("SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0]
        start = conn.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        made = routes.make(conn, candidate, AUTHOR)
        sources = conn.execute("SELECT count(*) FROM source_index.route").fetchone()[0]
    before = state()
    assert before["routes"] and before["events"]

    # Not without naming the database that is meant.
    run = CliRunner().invoke(app, ["overlay", "reset", "--database", "stiflyt2", "--to", str(tmp_path), "--yes"])
    assert run.exit_code != 0 and state() == before

    run = CliRunner().invoke(app, ["overlay", "reset", "--database", TEST_DB, "--to", str(tmp_path), "--yes"])
    assert run.exit_code == 0, run.output
    (copy,) = tmp_path.glob("overlay-*.dump")
    assert all(rows == [] for rows in state().values())
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        # What is imported is untouched, and the empty overlay works: the route can be made again.
        assert conn.execute("SELECT count(*) FROM source_index.route").fetchone()[0] == sources
        assert routes.owner_of(conn, candidate) is None
        again = routes.make(conn, candidate, AUTHOR)
        assert str(again) != str(made)

    run = CliRunner().invoke(app, ["overlay", "restore", str(copy), "--database", TEST_DB,
                                   "--to", str(tmp_path), "--yes"])
    assert run.exit_code == 0, run.output
    assert state() == before
    assert len(list(tmp_path.glob("overlay-*.dump"))) == 2     # one copy before each replacement
    with psycopg.connect(role_dsn("stiflyt2_api")) as conn:
        # Usable as before: a change gets the next number in the history, not a used one.
        routes.review(conn, str(made), routes.detail(conn, str(made))["version"], AUTHOR)
        conn.rollback()
        # And undo knows what was done before the copy was written: the route made above is taken back.
        assert undo.last(conn)["txid"] > start and undo.last(conn)["what"] == "bre18 made yours"
        undo.undo(conn, AUTHOR)
        assert routes.owner_of(conn, candidate) is None

    with pytest.raises(overlay.Failed, match="not a copy of an overlay|no such file"):
        overlay.restore(tmp_path / "nothing.dump", tmp_path)
