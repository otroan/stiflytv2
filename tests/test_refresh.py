"""Checking for new deliveries: download only what changed, and log every check."""
import shutil
from datetime import UTC, datetime

import psycopg
from conftest import FIXTURES, deliver, later_delivery, line
from typer.testing import CliRunner

from stiflyt2 import cli
from stiflyt2.config import settings
from stiflyt2.importer import core, delivery, fgdb, geonorge, refresh

TUR = FIXTURES / "turrutebasen.zip"
RETAGGED = "ce4908aa-d039-413e-996d-96b4440a1dd0"


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


class Geonorge:
    """Stands in for the download API: serves one file with a size and a date."""

    def __init__(self, monkeypatch):
        self.downloads = 0
        monkeypatch.setattr(geonorge, "order", lambda *a: ("delivery.zip", "https://x.invalid/d"))
        monkeypatch.setattr(geonorge, "probe", lambda url: (self.file.stat().st_size, self.date))
        monkeypatch.setattr(geonorge, "download", self.download)

    def serve(self, file, date):
        self.file, self.date = file, date

    def download(self, url, dest_dir, name):
        self.downloads += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / name
        shutil.copyfile(self.file, path)
        return geonorge.Fetched(path, delivery.sha256(path), path.stat().st_size, url, self.date)


def fetch():
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(conn, "weekly", "weekly", "test", "test", "allowed")
    return fgdb.run(
        key="weekly", uuid="-", scope="NO", transform="weekly-1",
        layers=[("fotrute_senterlinje", "fx_weekly", True)],
        specs=[core.LayerSpec("fotrute", "fx_weekly", "s.lokalid",
                              "to_jsonb(s) - 'ogc_fid' - 'geom' - 'kopidato' - 'shape_length'")],
    )


def test_nothing_is_downloaded_until_the_source_serves_a_new_file(monkeypatch, tmp_path):
    source = Geonorge(monkeypatch)
    source.serve(TUR, datetime(2026, 11, 11, 6, 22, tzinfo=UTC))
    r1 = fetch()
    assert not r1.reused and r1.counts == {"fotrute": 11} and source.downloads == 1

    r2 = fetch()
    assert r2.reused and r2.snapshot_id == r1.snapshot_id and source.downloads == 1

    source.serve(
        later_delivery(TUR, tmp_path,
                       "UPDATE fotrute_senterlinje SET kopidato = '2026-11-18T06:00:00Z'",
                       f"UPDATE fotrute_senterlinje SET merking = 'NEI' WHERE lokalid = '{RETAGGED}'"),
        datetime(2026, 11, 18, 6, 20, tzinfo=UTC))
    r3 = fetch()
    assert r3.diff == {"created": 0, "updated": 1, "missing": 0} and source.downloads == 2
    (r3_date, r3_checksum), = q(
        "SELECT source_updated_at, checksum FROM source_raw.snapshot WHERE id = %s", r3.snapshot_id)
    assert r3_date == source.date

    # Only the original zip is kept: no download left behind, no unpacked copy.
    kept = settings.data_dir / "weekly"
    assert not list((kept / "incoming").iterdir())
    assert sorted(p.name for p in kept.glob("*/*")) == ["delivery.zip", "delivery.zip"]
    assert CliRunner().invoke(cli.app, ["sources", "verify", "--source", "weekly"]).exit_code == 0

    # Retention removes the original file of the older snapshot, never its data,
    # and never the file of the snapshot in use.
    assert refresh.prune(1, "weekly") == 1
    assert [p.parent.name for p in kept.glob("*/*")] == [r3_checksum[:16]]
    assert q("SELECT count(*) FROM source_raw.snapshot_member WHERE snapshot_id = %s",
             r1.snapshot_id) == [(11,)]
    result = CliRunner().invoke(cli.app, ["sources", "verify", "--source", "weekly"])
    assert result.exit_code == 0 and "removed by retention" in result.output
    assert refresh.prune(1, "weekly") == 0


def test_every_check_is_logged_and_one_failure_does_not_stop_the_rest(monkeypatch, tmp_path):
    features = [line("r1", "R", [[300000, 6800000], [300100, 6800000]])]
    snapshots = {key: deliver(tmp_path, features, key=key).snapshot_id
                 for key in ("check_a", "check_b", "check_c", "check_d")}

    def again(source_key, scope, manifest):
        if source_key == "check_a":
            return core.Result(snapshots[source_key], True, {}, {})
        if source_key == "check_b":
            raise core.ImportRejected("layer line shrank")
        if source_key == "check_c":
            raise TimeoutError("no answer")
        return core.Result(snapshots[source_key], False, {"line": 1}, {"created": 0, "updated": 1})

    monkeypatch.setattr(refresh, "SOURCES", tuple(snapshots))
    monkeypatch.setattr(refresh, "again", again)

    result = CliRunner().invoke(cli.app, ["refresh"])
    assert result.exit_code == 1  # something needs attention
    assert q("""SELECT source_key, outcome, detail FROM import_control.check_log
                WHERE source_key LIKE 'check\\_%%' ORDER BY 1""") == [
        ("check_a", "unchanged", ""),
        ("check_b", "rejected", "layer line shrank"),
        ("check_c", "error", "TimeoutError: no answer"),
        ("check_d", "imported", '{"created": 0, "updated": 1}'),
    ]

    listed = CliRunner().invoke(cli.app, ["sources", "list"]).output
    assert next(row for row in listed.splitlines() if row.startswith("check_b")).count(
        "rejected") == 1
