"""GPX tracks: read as they came, cleaned (jumps, stops, gaps, cuts by hand), kept once, behind a flag."""
import datetime as dt
import math
import xml.etree.ElementTree as ET

import numpy as np
import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from test_mcp import call, rpc
from typer.testing import CliRunner

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.api.auth import Author
from stiflyt2.cli import app as cli
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Invalid
from stiflyt2.ops import tracks, undo

AUTHOR = Author("Test Editor", "viewer")
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}
START = dt.datetime(2025, 8, 14, 7, 0, tzinfo=dt.UTC).timestamp()


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event)").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
            owner.execute("SET ROLE stiflyt2_owner")
            owner.execute("DELETE FROM ops.track_line; DELETE FROM ops.track; DELETE FROM ops.track_raw")
            owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark[0],))
            owner.execute("DELETE FROM ops.event WHERE seq > %s", (mark[1],))
            # What was saved stays saved: taking the mark away would make what others saved before unsaved again.
        while (latest := routes_undo.last(c)) and latest["txid"] > start:
            with c.transaction():
                routes_undo.undo(c, "Test Editor")


def act(conn, what):
    with conn.transaction():
        return what()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def walk(n=600, step=1.25, every=1.0, x0=0.0, y0=0.0, t0=START, z0=800.0):
    """A walk east at 1.25 m a second, a point a second, climbing a metre every 20 m: x, y, z, t."""
    i = np.arange(n, dtype=float)
    return np.column_stack([x0 + i * step, y0 + np.zeros(n), z0 + i * step / 20, t0 + i * every])


def gpx(parts, name="Rondane", times=True, heights=True, ns=True):
    """A GPX file as a watch writes it, from parts of lon, lat, height, time."""
    pts = []
    for part in parts:
        pts.append("<trkseg>" + "".join(
            f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">' + (f"<ele>{z:.1f}</ele>" if heights else "")
            + (f"<time>{dt.datetime.fromtimestamp(t, dt.UTC):%Y-%m-%dT%H:%M:%SZ}</time>" if times else "") + "</trkpt>"
            for lon, lat, z, t in part) + "</trkseg>")
    xmlns = ' xmlns="http://www.topografix.com/GPX/1/1"' if ns else ""
    return (f'<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="test"{xmlns}>'
            f'<metadata><time>2025-08-14T07:00:00Z</time></metadata>'
            f'<wpt lat="61" lon="8"><name>A waypoint</name></wpt><trk><name>{name}</name>{"".join(pts)}</trk></gpx>').encode()


def middle(conn, route):
    """The middle of a route's first stretch, in metres (EPSG:25833)."""
    return conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_LineInterpolatePoint(r.geom, 0.5) AS p FROM core.route_leg l
        JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY l.seq LIMIT 1) m""", (route,)).fetchone()


def lonlat(conn, route, part):
    """A walk in metres from the middle of a route's first stretch, as lon, lat, height, time."""
    x0, y0 = middle(conn, route)
    got = conn.execute("""SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_SetSRID(ST_MakePoint(x, y), 25833), 4326) AS p
                          FROM unnest(%s::float8[], %s::float8[]) WITH ORDINALITY u(x, y, n) ORDER BY n) q""",
                       ([x0 + x for x in part[:, 0]], [y0 + y for y in part[:, 1]])).fetchall()
    return np.column_stack([np.array(got), part[:, 2], part[:, 3]])


# --- reading and cleaning, with no database ---------------------------------------------------------------------------

def test_a_gpx_file_is_read_as_its_parts_with_heights_and_times_and_its_name():
    a, b = np.array([[8.0, 61.0, 900.0, START], [8.001, 61.0, 905.0, START + 60]]), np.array([[8.002, 61.0, 910.0, START + 120], [8.003, 61.0, 915.0, START + 180]])
    parts, name, has_height, has_time = tracks.parse(gpx([a, b]))
    assert (len(parts), name, has_height, has_time) == (2, "Rondane", True, True)
    assert parts[1][1].tolist() == [8.003, 61.0, 915.0, START + 180]
    # With no namespace, and with no heights or times: a line alone.
    parts, _, has_height, has_time = tracks.parse(gpx([a], times=False, heights=False, ns=False))
    assert (has_height, has_time) == (False, False) and parts[0][:, 2:].tolist() == [[0, 0], [0, 0]]
    # A route (rte) is read as a part too.
    route = b'<gpx><rte><name>Planned</name><rtept lat="61" lon="8"/><rtept lat="61.01" lon="8"/></rte></gpx>'
    parts, name, _, _ = tracks.parse(route)
    assert (len(parts), name) == (1, "Planned")
    with pytest.raises(Invalid, match="not a GPX file"):
        tracks.parse(b"<gpx><trk>")
    with pytest.raises(Invalid, match="no track in the file"):
        tracks.parse(b'<gpx><wpt lat="61" lon="8"/></gpx>')


def test_a_walk_cleaned_is_as_long_as_it_walked_and_times_its_walking():
    told = tracks.clean([walk()], True, True)
    assert len(told["parts"]) == 1 and told["length_m"] == pytest.approx(599 * 1.25)
    assert (told["total_s"], told["moving_s"]) == (599, 599)
    assert told["up_m"] == pytest.approx(599 * 1.25 / 20, abs=4) and told["down_m"] == 0
    assert told["cleaned"]["jumps"] == told["cleaned"]["still"] == 0


def test_a_jump_away_and_back_is_taken_out_and_a_stop_is_one():
    w = walk(900)
    w[300, 1] += 200                        # out 200 m north for a second, and back
    w[500:800, :2] = w[500, :2] + np.random.default_rng(1).normal(0, 1.5, (300, 2))    # stood still five minutes
    w[800:, 0] = w[800:, 0] - w[800, 0] + w[500, 0] + 1.25
    told = tracks.clean([w], True, True)
    c = told["cleaned"]
    assert c["jumps"] == 1 and len(c["stops"]) == 1 and c["stops"][0]["s"] == pytest.approx(299, abs=12) and c["still"] > 290
    assert told["total_s"] == 899 and told["moving_s"] == pytest.approx(899 - c["stops"][0]["s"], abs=1)
    # The line goes on where it stood, and is as long as the walking.
    assert told["length_m"] == pytest.approx((500 + 100) * 1.25, rel=0.02)


def test_a_track_is_cut_where_the_device_was_off_and_joined_where_the_file_was_paused():
    first = walk(200)
    paused = walk(200, x0=first[-1, 0] + 8, t0=first[-1, 3] + 300)            # paused five minutes, on again 8 m on
    drove = walk(200, x0=paused[-1, 0] + 3000, t0=paused[-1, 3] + 1800)       # 3 km and half an hour later
    told = tracks.clean([first, paused, drove], True, True)
    assert len(told["parts"]) == 2 and told["cleaned"]["gaps"] == 0
    assert told["total_s"] == drove[-1, 3] - first[0, 3]
    # Walking: neither the drive nor the pause (an hour's gap and five minutes at one spot are no walking).
    assert told["moving_s"] == pytest.approx(3 * 199, abs=2)
    # In one part of the file: cut in two all the same, and said to be a gap.
    told = tracks.clean([np.vstack([first, drove])], True, True)
    assert len(told["parts"]) == 2 and told["cleaned"]["gaps"] == 1


def test_a_ride_faster_than_one_walks_is_left_out_and_is_no_jump():
    first = walk(200)
    drive = walk(120, step=15, x0=first[-1, 0] + 15, t0=first[-1, 3] + 1)          # two minutes at 54 km/h
    last = walk(200, x0=drive[-1, 0] + 1.25, t0=drive[-1, 3] + 1)
    told = tracks.clean([np.vstack([first, drive, last])], True, True)
    c = told["cleaned"]
    assert (c["rides"], c["jumps"], c["gaps"], len(told["parts"])) == (1, 0, 0, 2)
    assert c["ride_m"] == pytest.approx(120 * 15, abs=20)
    assert told["length_m"] == pytest.approx(2 * 199 * 1.25, abs=2) and told["moving_s"] == pytest.approx(2 * 199, abs=2)
    # Fast for a few seconds only (a jog down a slope): kept.
    quick = walk(30, step=7, x0=first[-1, 0] + 7, t0=first[-1, 3] + 1)
    told = tracks.clean([np.vstack([first, quick])], True, True)
    assert (told["cleaned"]["rides"], told["cleaned"]["jumps"], len(told["parts"])) == (0, 0, 1)


def test_with_no_times_a_jump_is_seen_by_its_shape_and_points_on_top_of_each_other_are_one():
    w = walk(100, step=10)
    w[50, 1] += 150
    w = np.insert(w, 20, w[20] + [0.5, 0, 0, 0], axis=0)
    told = tracks.clean([w], False, False)
    assert told["cleaned"]["jumps"] == 1 and told["cleaned"]["doubles"] == 1
    assert told["length_m"] == pytest.approx(990, abs=1) and told["moving_s"] is None and told["up_m"] is None


def test_what_is_cut_out_by_hand_is_left_out_and_the_track_goes_on_after_it():
    told = tracks.clean([walk(600)], True, True, cuts=[{"from": 100, "to": 299}])
    assert len(told["parts"]) == 2 and told["cleaned"]["cut"] == 200 and told["points"] == 400
    assert told["length_m"] == pytest.approx((99 + 299) * 1.25)


def test_a_line_goes_to_postgis_and_back_with_its_heights_and_times():
    parts = [walk(5), walk(3, x0=100)]
    back = tracks._parts_of(tracks._ewkb(parts, 25833))
    assert [p.tolist() for p in back] == [p.tolist() for p in parts]


# --- kept, in the database ---------------------------------------------------------------------------------------------

def test_a_track_is_kept_once_of_the_area_it_was_walked_in_with_what_it_comes_to(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    w = walk(900)
    w[300, 1] += 200
    data = gpx([lonlat(conn, route, w)])
    t = act(conn, lambda: tracks.add(conn, AUTHOR, data, filename="activity_1.gpx"))
    assert (t["name"], t["area"], t["has_time"], t["has_height"], t["parts"], t["points_came"]) == ("Rondane", "bre", True, True, 1, 900)
    assert t["length_m"] == pytest.approx(899 * 1.25, rel=0.01) and t["cleaned"]["jumps"] == 1
    assert t["started_at"] == dt.datetime.fromtimestamp(START, dt.UTC) and t["moving_s"] == pytest.approx(899, abs=2)
    assert tracks.path(conn.execute("SELECT sha256 FROM ops.track_raw").fetchone()[0]).read_bytes() == data
    # Heights and times went through the projection as they were.
    assert conn.execute("SELECT ST_Z(ST_PointN(ST_GeometryN(geom, 1), 3)), ST_M(ST_PointN(ST_GeometryN(geom, 1), 3)) "
                        "FROM ops.track_raw").fetchone() == (pytest.approx(w[2, 2], abs=0.05), START + 2)
    again = act(conn, lambda: tracks.add(conn, AUTHOR, data))
    assert again["id"] == t["id"] and again["again"]
    assert [x["id"] for x in tracks.listing(conn, "bre")] == [t["id"]] and tracks.listing(conn, "jot") == []
    drawn = tracks.lines(conn, "bre")["features"]
    assert len(drawn) == 1 and drawn[0]["geometry"]["type"] == "MultiLineString" and drawn[0]["properties"]["name"] == "Rondane"
    shown = tracks.one(conn, t["id"], drawn=True)
    assert shown["line"]["type"] == "MultiLineString" and shown["came"]["type"] == "MultiLineString" and shown["cuts"] == []
    with pytest.raises(Invalid, match="not a GPX"):
        act(conn, lambda: tracks.add(conn, AUTHOR, b"no gpx"))


def test_a_part_cut_out_by_hand_is_put_back_and_undo_takes_it_back_too(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    w = lonlat(conn, route, walk(600))
    t = act(conn, lambda: tracks.add(conn, AUTHOR, gpx([w])))
    cut = act(conn, lambda: tracks.cut(conn, AUTHOR, t["id"], w[400, :2].tolist(), w[200, :2].tolist()))
    assert cut["parts"] == 2 and cut["cut_out"] == 1 and cut["cleaned"]["cut"] == 201
    assert (cut["cuts"][0]["from"], cut["cuts"][0]["to"]) == (200, 400) and cut["cuts"][0]["to_m"] - cut["cuts"][0]["from_m"] == pytest.approx(250, abs=1)
    assert undo.last(conn, AUTHOR)["what"] == "a part cut out of a track"
    # Another that runs into it is one with it.
    more = act(conn, lambda: tracks.cut(conn, AUTHOR, t["id"], w[350, :2].tolist(), w[450, :2].tolist()))
    assert [(c["from"], c["to"]) for c in more["cuts"]] == [(200, 450)]
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert [(c["from"], c["to"]) for c in tracks.one(conn, t["id"], drawn=True)["cuts"]] == [(200, 400)]
    back = act(conn, lambda: tracks.put_back(conn, AUTHOR, t["id"], 0))
    assert back["parts"] == 1 and back["cuts"] == [] and back["length_m"] == pytest.approx(t["length_m"])
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert tracks.one(conn, t["id"])["parts"] == 2
    with pytest.raises(Invalid, match="same point"):
        act(conn, lambda: tracks.cut(conn, AUTHOR, t["id"], w[10, :2].tolist(), w[10, :2].tolist()))


def test_a_track_taken_away_comes_back_with_undo_and_is_renamed(conn):
    t = act(conn, lambda: tracks.add(conn, AUTHOR, gpx([np.column_stack([8 + np.arange(20) * 1e-4, np.full(20, 61.0), np.full(20, 900.0), START + np.arange(20) * 10])]), area="JOT"))
    assert t["area"] == "jot"           # near no route: of the area it was added in
    renamed = act(conn, lambda: tracks.change(conn, AUTHOR, t["id"], {"name": "Galdhøpiggen"}))
    assert renamed["name"] == "Galdhøpiggen"
    act(conn, lambda: tracks.take_away(conn, AUTHOR, t["id"]))
    assert tracks.listing(conn, "jot") == [] and undo.last(conn, AUTHOR)["what"] == "a track taken away"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert [x["name"] for x in tracks.listing(conn, "jot")] == ["Galdhøpiggen"]
    # The track cleaned, as GPX: read back, it is the same track.
    body, name = tracks.gpx(conn, t["id"])
    parts, named, has_height, has_time = tracks.parse(body)
    assert (name, named, has_height, has_time, len(parts[0])) == ("Galdhøpiggen (cleaned).gpx", "Galdhøpiggen", True, True, 20)
    ET.fromstring(body)


def test_v1s_tracks_are_taken_in_as_lines_from_its_table_and_cleaned_again_when_asked(conn, tmp_path):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    x0, y0 = middle(conn, route)
    line = ", ".join(f"{x0 + i * 10} {y0}" for i in range(50))
    exported = tmp_path / "v1-tracks.csv"
    exported.write_text("id,area_code,name,point_count,length_m,uploaded_by,uploaded_at,wkt\n"
                        f'7,bre,Fra v1,50,490,someone,2025-07-01,"MULTILINESTRING (({line}))"\n', encoding="utf-8")
    run = CliRunner()
    told = run.invoke(cli, ["tracks", "take-in", str(exported), "--by", "Kari"])
    assert told.exit_code == 0 and '"added": 1' in told.output, told.output
    t = tracks.listing(conn, "bre")[0]
    assert (t["name"], t["origin"], t["made_by"], t["has_time"], t["has_height"], t["moving_s"]) == ("Fra v1", "v1:7", "Kari", False, False, None)
    assert t["length_m"] == pytest.approx(490) and not t["has_file"]
    assert '"again": 1' in run.invoke(cli, ["tracks", "take-in", str(exported), "--by", "Kari"]).output
    # The cleaned lines are left out of the nightly copy: made again when missing.
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
        owner.execute("SET ROLE stiflyt2_owner")
        owner.execute("DELETE FROM ops.track_line")
    assert tracks.listing(conn, "bre")[0]["length_m"] is None
    assert '"cleaned": 1' in run.invoke(cli, ["tracks", "clean-again"]).output
    assert tracks.listing(conn, "bre")[0]["length_m"] == pytest.approx(490)
    assert "bre 1" in run.invoke(cli, ["tracks"]).output


def test_tracks_are_a_feature_of_their_own_in_the_page_and_for_an_assistant(conn, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    w = lonlat(conn, route, walk(300))
    data = gpx([w])
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/tracks", content=data, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `tracks`")
            assert "route_tracks" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

            access.set_state(owner, "tracks", "all", "test")
            as_reader = {**PAGE, "X-Stiflyt-As": "reader"}
            no = client.post(f"{V1}/tracks", content=data, headers=as_reader)
            assert (no.status_code, no.json()["detail"]) == (403, "you may look at tracks; adding and changing them needs the editor or operations role")
            made = client.post(f"{V1}/tracks", params={"filename": "Morning walk.gpx", "area": "bre"}, content=data, headers=PAGE)
            assert made.status_code == 201 and made.json()["area"] == "bre"
            tid = made.json()["id"]
            assert client.get(f"{V1}/tracks", params={"area": "bre"}, headers=as_reader).json()[0]["id"] == tid
            assert client.get(f"{V1}/tracks.geojson", params={"area": "bre"}).json()["features"][0]["properties"]["id"] == tid
            shown = client.get(f"{V1}/tracks/{tid}").json()
            assert shown["line"]["type"] == "MultiLineString" and shown["stops"] == []
            assert client.get(f"{V1}/tracks/{tid}/gpx").content == data
            cleaned = client.get(f"{V1}/tracks/{tid}/gpx", params={"cleaned": True})
            assert cleaned.status_code == 200 and "Rondane%20%28cleaned%29.gpx" in cleaned.headers["content-disposition"]
            cut = client.post(f"{V1}/tracks/{tid}/cuts", json={"from_point": w[100, :2].tolist(), "to_point": w[150, :2].tolist()}, headers=PAGE)
            assert cut.status_code == 200 and cut.json()["parts"] == 2
            assert client.delete(f"{V1}/tracks/{tid}/cuts/0", headers=PAGE).json()["parts"] == 1
            assert client.delete(f"{V1}/tracks/{tid}/cuts/0", headers=PAGE).status_code == 404
            assert client.patch(f"{V1}/tracks/{tid}", json={"name": "Up bre18"}, headers=PAGE).json()["name"] == "Up bre18"
            assert client.patch(f"{V1}/tracks/{tid}", json={"name": "x"}, headers=as_reader).status_code == 403
            assert client.post(f"{V1}/tracks", content=b"", headers=PAGE).status_code == 422
            said, refused = call(client, "route_tracks", route="bre18")
            assert not refused and said["tracks"][0]["id"] == tid and said["tracks"][0]["name"] == "Up bre18"
            assert client.delete(f"{V1}/tracks/{tid}", headers=PAGE).status_code == 204
            assert client.get(f"{V1}/tracks", params={"area": "bre"}).json() == []
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'tracks'")


def test_the_device_climb_counts_a_rise_once_it_comes_to_four_metres():
    z = 800 + 3 * np.sin(np.arange(200) / 3)            # up and down three metres: noise, no climb
    assert tracks._climb(z) == (0.0, 0.0)
    up, down = tracks._climb(np.r_[np.linspace(800, 900, 50), np.linspace(900, 850, 30)])
    assert (math.isclose(up, 100, abs_tol=4), math.isclose(down, 50, abs_tol=4)) == (True, True)
