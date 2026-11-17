"""Snapshot semantics, using a small synthetic source."""
import psycopg
import pytest
from conftest import deliver, line

from stiflyt2.config import settings
from stiflyt2.importer import core

KEY = "fixture"


A = line("a", "A", [[100000, 6800000], [100100, 6800000]])
B = line("b", "B", [[100100, 6800000], [100200, 6800000]])
C = line("c", "C", [[100200, 6800000], [100300, 6800000]])
D = line("d", "D", [[100300, 6800000], [100400, 6800000]])
B_MOVED = line("b", "B", [[100100, 6800000], [100200, 6800050]])


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def active(key=KEY):
    return str(q("SELECT snapshot_id FROM import_control.active_snapshot WHERE source_key = %s", key)[0][0])


def test_lifecycle(tmp_path):
    # First delivery: everything is new.
    r1 = deliver(tmp_path, [A, B, C])
    assert r1.counts == {"line": 3} and r1.diff["created"] == 3
    assert active() == r1.snapshot_id

    # The same file again is the same snapshot; nothing is duplicated.
    r1b = deliver(tmp_path, [A, B, C])
    assert r1b.reused and r1b.snapshot_id == r1.snapshot_id
    assert q("SELECT count(*) FROM source_raw.feature_version WHERE source_key = %s", KEY) == [(3,)]

    # Second delivery: B moved, C gone, D new, A untouched.
    r2 = deliver(tmp_path, [A, B_MOVED, D], max_shrink=0.9)
    assert r2.diff == {"created": 1, "updated": 1, "missing": 1}
    assert active() == r2.snapshot_id
    kinds = dict(q("""SELECT external_id, kind FROM import_control.diff_item i
                      JOIN import_control.diff d ON d.id = i.diff_id WHERE d.to_snapshot = %s""",
                   r2.snapshot_id))
    assert kinds == {"b": "updated", "c": "missing", "d": "created"}

    # The source serves the first file again: its snapshot becomes the active
    # one again, with the reverse diff, and nothing new is stored.
    back = deliver(tmp_path, [A, B, C], max_shrink=0.9)
    assert back.snapshot_id == r1.snapshot_id and active() == r1.snapshot_id
    assert not back.reused and back.diff == {"created": 1, "updated": 1, "missing": 1}
    assert deliver(tmp_path, [A, B_MOVED, D], max_shrink=0.9).snapshot_id == r2.snapshot_id
    assert active() == r2.snapshot_id

    # A is stored once and shared; B has two versions; the old snapshot is intact.
    assert q("SELECT count(*) FROM source_raw.feature_version WHERE external_id = 'a'") == [(1,)]
    assert q("SELECT count(*) FROM source_raw.feature_version WHERE external_id = 'b'") == [(2,)]
    old = q("""SELECT fv.external_id, ST_AsText(fv.geom) FROM source_raw.snapshot_member m
               JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
               WHERE m.snapshot_id = %s ORDER BY 1""", r1.snapshot_id)
    assert [e for e, _ in old] == ["a", "b", "c"]
    assert old[1][1] == "LINESTRING(100100 6800000,100200 6800000)"

    # A delivery that lost most of its rows is refused: nothing becomes "missing".
    before = active()
    with pytest.raises(core.ImportRejected, match="shrank"):
        deliver(tmp_path, [A])
    assert active() == before
    assert q("SELECT count(*) FROM source_raw.snapshot WHERE source_key = %s AND state = 'failed'",
             KEY) == [(1,)]
    assert q("""SELECT count(*) FROM import_control.diff d
                JOIN source_raw.snapshot s ON s.id = d.to_snapshot
                WHERE s.source_key = %s AND s.state = 'failed'""", KEY) == [(0,)]


def test_duplicate_ids_are_refused(tmp_path):
    before = active()
    with pytest.raises(core.ImportRejected, match="duplicate ids"):
        deliver(tmp_path, [A, A, B_MOVED, D])
    assert active() == before


def test_unreadable_file_changes_nothing(tmp_path):
    before = active()
    bad = tmp_path / "bad.geojson"
    bad.write_text("{ this is not geojson")
    with pytest.raises(core.ImportRejected):
        core.ogr_load(bad, None, "fx_line")
    assert active() == before


def test_rows_sharing_an_id_become_one_feature(tmp_path):
    west = line("m", "M", [[100000, 6810000], [100100, 6810000]])
    east = line("m", "M", [[100200, 6810000], [100300, 6810000]])
    other = line("n", "N", [[100400, 6810000], [100500, 6810000]])
    r1 = deliver(tmp_path, [west, east, other], key="parts", collect=True)
    assert r1.counts == {"line": 2}
    assert q("""SELECT GeometryType(geom), ST_NumGeometries(geom), ST_Length(geom)
                FROM source_raw.feature_version
                WHERE source_key = 'parts' AND external_id = 'm'""") == [("MULTILINESTRING", 2, 200.0)]

    # The order of the rows in a delivery does not matter: nothing has changed.
    r2 = deliver(tmp_path, [east, other, west], key="parts", collect=True)
    assert not r2.reused and r2.diff == {"created": 0, "updated": 0, "missing": 0}

    # Rows that share an id but disagree are not one object.
    renamed = line("m", "Other", [[100200, 6810000], [100300, 6810000]])
    with pytest.raises(core.ImportRejected, match="differ in attributes"):
        deliver(tmp_path, [west, renamed, other], key="parts", collect=True)
    assert active("parts") == r2.snapshot_id


def test_delivery_that_changes_most_features_is_refused(tmp_path, monkeypatch):
    def rows(changed):
        return [line(f"c{i}", "after" if i < changed else "before",
                     [[200000 + 100 * i, 6800000], [200050 + 100 * i, 6800000]]) for i in range(10)]

    r1 = deliver(tmp_path, rows(0), key="churn")
    with pytest.raises(core.ImportRejected, match="6 of 10 features changed"):
        deliver(tmp_path, rows(6), key="churn")
    assert active("churn") == r1.snapshot_id

    # A few changes are normal, and a mass change can be accepted deliberately.
    assert deliver(tmp_path, rows(2), key="churn").diff["updated"] == 2
    monkeypatch.setattr(settings, "max_churn", 1.0)
    assert deliver(tmp_path, rows(10), key="churn").diff["updated"] == 8
