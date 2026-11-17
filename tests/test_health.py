"""What runs by itself at night: the page is told when it has not run or went wrong, and old copies are cleared."""
import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from conftest import TEST_DB
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stiflyt2 import health, overlay
from stiflyt2.api.app import create_app
from stiflyt2.cli import app as cli


@pytest.fixture
def owner(loaded):
    """As the database's owner. Nothing a test does here is kept."""
    with psycopg.connect(dbname=TEST_DB) as conn:
        conn.execute("SET ROLE stiflyt2_owner")
        yield conn
        conn.rollback()


def checked(conn, hours_ago, outcome="unchanged", detail="", source="turrutebasen"):
    conn.execute("""INSERT INTO import_control.check_log (source_key, scope, started_at, finished_at, outcome, detail)
                    VALUES (%s, 'NO', now() - make_interval(hours => %s), now() - make_interval(hours => %s), %s, %s)""",
                 (source, hours_ago, hours_ago, outcome, detail))


def test_a_night_that_did_not_run_or_went_wrong_is_said_in_words(owner, tmp_path):
    now = datetime.now(UTC)
    owner.execute("DELETE FROM import_control.check_log")
    owner.execute("UPDATE feedback.entry SET seen_at = now() WHERE seen_at IS NULL")
    nothing = health.status(owner, tmp_path, now)
    assert nothing["warnings"] == [
        "The sources have never been checked for news: the nightly check is not running.",
        "No copy of your places and routes has been written: the nightly copy is not running."]

    # Checked the night before last, and a copy three days old: both have missed a night.
    checked(owner, 40)
    copy = tmp_path / f"overlay-{TEST_DB}-20261113T035000.000000Z.dump"
    copy.write_bytes(b"x")
    then = (now - timedelta(days=3)).timestamp()
    os.utime(copy, (then, then))
    (tmp_path / "overlay-another-20261116T035000.000000Z.dump").write_bytes(b"x")      # not this database's
    late = health.status(owner, tmp_path, now)
    assert late["warnings"] == [
        "The sources were last checked for news 40 hours ago: the nightly check is not running.",
        "The last copy of your places and routes is 3 days old: the nightly copy is not running."]
    assert late["copies"] == 1

    # Last night: one source could not be checked, the others were fine; and a fresh copy.
    checked(owner, 5)
    checked(owner, 5, "error", "Overpass answered 502 after 4 tries", source="osm")
    (tmp_path / f"overlay-{TEST_DB}-20261116T035000.000000Z.dump").write_bytes(b"x")
    assert health.status(owner, tmp_path, now)["warnings"] == [
        "The nightly check failed for 1 of the sources, first osm NO: Overpass answered 502 after 4 tries"]
    # Checked again since, and fine: nothing to say.
    checked(owner, 1, source="osm")
    fine = health.status(owner, tmp_path, now)
    assert fine["warnings"] == [] and fine["copies"] == 2 and now - fine["copied_at"] < timedelta(minutes=1)

    # A job that failed outright says so itself, until somebody has looked at it.
    owner.execute("""INSERT INTO feedback.entry (kind, through, what, message)
                     VALUES ('failed', 'job', 'nightly copy', 'pg_dump failed: no space left on device')""")
    said, = health.status(owner, tmp_path, now)["warnings"]
    assert said.startswith("The nightly copy failed on ") and said.endswith(": pg_dump failed: no space left on device")
    owner.execute("UPDATE feedback.entry SET seen_at = now() WHERE through = 'job'")
    assert health.status(owner, tmp_path, now)["warnings"] == []


def test_old_copies_are_cleared_and_a_failed_job_is_kept_with_the_feedback(loaded, tmp_path, monkeypatch):
    monkeypatch.setenv("STIFLYT2_MIGRATE_URL", f"postgresql+psycopg:///{TEST_DB}")
    for kind in ("overlay", "tool"):
        for day in range(1, 6):
            (tmp_path / f"{kind}-{TEST_DB}-2026110{day}T035000.000000Z.dump").write_bytes(b"x")
    other = tmp_path / "overlay-another-20261101T035000.000000Z.dump"
    other.write_bytes(b"x")
    assert overlay.prune(tmp_path, 0) == []                      # 0 keeps all
    gone = overlay.prune(tmp_path, 2)
    assert len(gone) == 6 and other.exists()
    assert sorted(p.name[-28:-20] for p in tmp_path.glob(f"overlay-{TEST_DB}-*.dump")) == ["20261104", "20261105"]

    # The real thing: a copy of the overlay, and beside it of who uses the tool and the feedback.
    run = CliRunner().invoke(cli, ["overlay", "backup", "--to", str(tmp_path), "--keep", "2"])
    assert run.exit_code == 0 and "2 older copies removed" in run.output
    newest = max(tmp_path.glob(f"tool-{TEST_DB}-*.dump"))
    assert newest.stat().st_size > 1000 and len(list(tmp_path.glob(f"overlay-{TEST_DB}-*.dump"))) == 2

    told = CliRunner().invoke(cli, ["feedback", "failed", "nightly copy", "pg_dump failed: disk full"])
    assert told.exit_code == 0 and told.output.startswith("kept as #")
    with TestClient(create_app()) as client:
        warnings = client.get("/api/v1/health").json()["warnings"]
        assert any(w.endswith("pg_dump failed: disk full") for w in warnings)
    assert CliRunner().invoke(cli, ["feedback", "seen", told.output.strip().split("#")[1]]).exit_code == 0
