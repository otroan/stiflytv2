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
    # A delivery refused as faulty: said as what it means to the one who works here, and not how to get round the check.
    checked(owner, 1, "rejected", "layer ruteinfopunkt: 9724 of 9754 features changed (more than 50%); "
            "set STIFLYT2_MAX_CHURN=1 to accept this delivery", source="turrutebasen")
    refused = ("A new delivery of turrutebasen was refused last night, as it looks faulty (layer ruteinfopunkt: 9724 of "
               "9754 features changed (more than 50%)). The one before it is still in use, and nothing of yours is changed.")
    assert health.status(owner, tmp_path, now)["warnings"] == [refused]
    checked(owner, 0)       # and a good one has come since

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


def test_a_source_not_reached_is_warned_of_only_when_it_has_not_caught_up(owner, tmp_path):
    now = datetime.now(UTC)
    owner.execute("DELETE FROM import_control.check_log")
    owner.execute("UPDATE feedback.entry SET seen_at = now() WHERE seen_at IS NULL")
    (tmp_path / f"overlay-{TEST_DB}-20261123T035000.000000Z.dump").write_bytes(b"x")
    checked(owner, 1)
    # OpenStreetMap is asked for weekly: busy last night, fetched three days ago: nothing to say yet.
    checked(owner, 24 * 3, source="osm")
    checked(owner, 2, "unreached", "Overpass answers 502, at capacity, after 6 tries over 15 minutes", source="osm")
    # The mapped paths nightly: not reached last night, but the night before: nothing either.
    checked(owner, 30, source="fkb_wfs")
    checked(owner, 3, "unreached", "Kartverket's WFS did not answer", source="fkb_wfs")
    assert health.status(owner, tmp_path, now)["warnings"] == []
    # Nine days without OpenStreetMap, three without the paths: now both are said, and that nothing is lost.
    owner.execute("UPDATE import_control.check_log SET finished_at = now() - interval '9 days' WHERE source_key = 'osm' AND outcome = 'unchanged'")
    owner.execute("UPDATE import_control.check_log SET finished_at = now() - interval '3 days' WHERE source_key = 'fkb_wfs' AND outcome = 'unchanged'")
    assert health.status(owner, tmp_path, now)["warnings"] == [
        ("fkb_wfs NO has not been reached for 3 days (Kartverket's WFS did not answer). "
         "What was fetched before is still in use, and it is asked again every night."),
        ("osm NO has not been reached for 9 days (Overpass answers 502, at capacity, after 6 tries over 15 minutes). "
         "What was fetched before is still in use, and it is asked again every night.")]
    # Reached again: nothing to say.
    checked(owner, 0, source="osm")
    checked(owner, 0, source="fkb_wfs")
    assert health.status(owner, tmp_path, now)["warnings"] == []


def test_what_there_is_to_warn_of_is_sent_once_where_a_way_to_send_is_set_up(tmp_path, monkeypatch):
    import httpx

    from stiflyt2.config import settings

    state, now = tmp_path / "alerted.json", datetime.now(UTC)
    said = ["The sources were last checked for news 40 hours ago: the nightly check is not running."]
    monkeypatch.setattr(settings, "alert_url", "")
    monkeypatch.setattr(settings, "alert_to", "")
    assert health.alert([], state, now) == "nothing to send"
    assert health.alert(said, state, now).startswith("1 to warn of, not sent: no way to send is set up")
    assert not state.exists()

    posted = []
    monkeypatch.setattr(settings, "alert_url", "https://push.example.org/stiflyt2-test")
    monkeypatch.setattr(settings, "public_url", "https://stiflyt2.example.org")
    monkeypatch.setattr(httpx, "post", lambda url, content, headers, timeout: posted.append((url, content.decode(), headers))
                        or httpx.Response(200, request=httpx.Request("POST", url)))
    assert health.alert(said, state, now) == "sent 1 to warn of to push.example.org"
    assert posted == [("https://push.example.org/stiflyt2-test", said[0] + "\n\nhttps://stiflyt2.example.org",
                       {"Title": "Stiflyt v2: something to look at"})]
    # Three hours later the same is said with other numbers: not sent again. Something new is, alone.
    later = ["The sources were last checked for news 43 hours ago: the nightly check is not running.",
             "No copy of your places and routes has been written: the nightly copy is not running."]
    assert health.alert(later, state, now + timedelta(hours=3)) == "sent 1 to warn of to push.example.org"
    assert posted[-1][1].startswith("No copy of your places") and len(posted) == 2
    # The next day it is sent again, if it is still so.
    assert health.alert(later[:1], state, now + timedelta(hours=21)).startswith("sent 1")
    # A way to send that fails is said, not swallowed.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(500, request=httpx.Request("POST", a[0])))
    with pytest.raises(httpx.HTTPStatusError):
        health.alert(["Something else entirely."], state, now)
