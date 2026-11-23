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


def beside():
    """Every row of what is kept beside the overlay, table by table."""
    with psycopg.connect(dbname=TEST_DB) as conn:
        conn.execute("SET ROLE stiflyt2_owner")
        tables = [t for (t,) in conn.execute("""SELECT format('%I.%I', schemaname, tablename) FROM pg_tables
                                                WHERE schemaname IN ('access', 'feedback', 'ops') ORDER BY 1""")]
        return {t: conn.execute(f"SELECT to_jsonb(x)::text FROM {t} x ORDER BY 1").fetchall() for t in tables}


def test_what_is_kept_beside_the_overlay_is_put_back_exactly(loaded, tmp_path, monkeypatch):
    from stiflyt2 import access, feedback
    from stiflyt2.ops import issues

    monkeypatch.setenv("STIFLYT2_MIGRATE_URL", f"postgresql+psycopg:///{TEST_DB}")
    kari, ola = "kari@example.org", "ola@example.org"
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as conn, \
            psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
        owner.execute("SET ROLE stiflyt2_owner")
        start = conn.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = conn.execute("SELECT coalesce(max(id), 0) FROM ops.undo_log").fetchone()[0]
        with conn.transaction():
            route = str(routes.make(conn, conn.execute("SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0], AUTHOR))
        # People with a role, a feature and a token (the keys between them); an issue (its undo); feedback.
        access.add(owner, kari, "Kari Nordmann", "editor", "test")
        access.grant(owner, kari, "trips", True, "test")
        access.new_token(owner, kari, "mcp", "laptop")
        with conn.transaction():
            tree = issues.report(conn, AUTHOR, route=route, kind="clearing", text="A birch", about="route")
        feedback.record(owner, "reported", "api", "note", "Kept before the copy")
        before = beside()
        assert before["access.person"] and before["ops.entry"] and before["access.token"]

        copy = overlay.backup_beside(tmp_path)
        assert overlay.kind_of(copy) == "tool"

        # After the copy: somebody new, somebody gone, the issue said done, more feedback.
        access.add(owner, ola, "Ola Nordmann", "reader", "test")
        access.remove(owner, kari)
        with conn.transaction():
            issues.change(conn, AUTHOR, tree["id"], {"state": "done"})
        feedback.record(owner, "reported", "api", "note", "Said after the copy")
        assert beside() != before

        # Not without naming the database; the overlay's own copy is not this one.
        run = CliRunner().invoke(app, ["overlay", "restore", str(copy), "--database", "stiflyt2", "--to", str(tmp_path), "--yes"])
        assert run.exit_code != 0
        run = CliRunner().invoke(app, ["overlay", "restore", str(copy), "--database", TEST_DB, "--to", str(tmp_path), "--yes"])
        assert run.exit_code == 0, run.output
        assert "what is kept beside the overlay" in run.output.lower() and "people" in run.output
        assert beside() == before                                   # row for row, the undo of operations included
        assert len(list(tmp_path.glob("tool-*.dump"))) == 2         # what was there is kept first

        # Usable as before: the next feedback gets a number of its own, and the token still lets Kari in.
        latest = max(r[0] for r in owner.execute("SELECT id FROM feedback.entry"))
        assert feedback.record(owner, "reported", "api", "note", "After putting back") > latest
        assert access.person(owner, kari) is not None

        # A file that is no copy is refused before anything is changed.
        junk = tmp_path / "junk.dump"
        junk.write_text("no copy")
        with pytest.raises(overlay.Failed):
            overlay.kind_of(junk)

        # Cleared: what this test added, and the route made.
        access.remove(owner, kari)
        owner.execute("DELETE FROM ops.entry WHERE id = %s", (tree["id"],))
        owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark,))
        owner.execute("DELETE FROM feedback.entry WHERE message IN ('Kept before the copy', 'After putting back')")
        while (latest := undo.last(conn)) and latest["txid"] > start:
            with conn.transaction():
                undo.undo(conn, AUTHOR)
