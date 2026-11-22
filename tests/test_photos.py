"""Photos from the routes: kept once, placed by the camera or by hand, found near a route, behind a flag."""
import io

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient
from PIL import Image
from PIL.TiffImagePlugin import IFDRational
from test_mcp import call, rpc

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.api.auth import Author
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.domain.places import Invalid
from stiflyt2.ops import issues, photos, undo

AUTHOR = Author("Test Editor", "viewer")
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}


@pytest.fixture
def conn(loaded):
    """Autocommit, so that each call is a transaction of its own, as each request is. Cleared afterwards."""
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        start = c.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        mark = c.execute("SELECT (SELECT coalesce(max(id), 0) FROM ops.undo_log), (SELECT coalesce(max(seq), 0) FROM ops.event)").fetchone()
        yield c
        with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner:
            owner.execute("SET ROLE stiflyt2_owner")
            owner.execute("DELETE FROM ops.photo")
            owner.execute("DELETE FROM ops.entry")
            owner.execute("DELETE FROM ops.undo_log WHERE id > %s", (mark[0],))
            owner.execute("DELETE FROM ops.event WHERE seq > %s", (mark[1],))
            owner.execute("DELETE FROM ops.saved WHERE who = 'Test Editor'")
        while (latest := routes_undo.last(c)) and latest["txid"] > start:
            with c.transaction():
                routes_undo.undo(c, "Test Editor")


def act(conn, what):
    with conn.transaction():
        return what()


def on_route(conn, route, east=10):
    """A point a few metres beside the middle of a route's first stretch, as lon, lat."""
    return conn.execute("""
        SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_Transform(ST_Translate(ST_LineInterpolatePoint(r.geom, 0.5), %s, 0), 4326) AS p
        FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id JOIN core.edge_revision r ON r.id = e.current_revision_id
        WHERE l.route_id = %s ORDER BY l.seq LIMIT 1) m""", (east, route)).fetchone()


def dms(value):
    d = int(value)
    m = int((value - d) * 60)
    s = round(((value - d) * 60 - m) * 60 * 1000)
    return (IFDRational(d, 1), IFDRational(m, 1), IFDRational(s, 1000))


def jpeg(where=None, colour=(90, 120, 60), turned=False, taken="2026:07:14 12:30:00", facing=None):
    """A photo as a camera writes it: 800 × 600, with where, when and which way, and how it is to be turned."""
    img = Image.new("RGB", (800, 600), colour)
    exif = img.getexif()
    exif.get_ifd(0x8769)[36867] = taken
    if turned:
        exif[274] = 6                   # to be turned a quarter: it is upright as 600 × 800
    if where:
        gps = exif.get_ifd(0x8825)
        gps.update({1: "N", 2: dms(where[1]), 3: "E", 4: dms(where[0])})
        if facing is not None:
            gps[17] = IFDRational(round(facing * 10), 10)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def candidate(conn, label):
    return conn.execute("SELECT id FROM source_index.route WHERE label = %s", (label,)).fetchone()[0]


def test_a_photo_is_kept_once_where_the_camera_says_it_was_taken_with_copies_that_say_nothing_of_the_camera(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    where = on_route(conn, route)
    data = jpeg(where, turned=True, facing=123.4)
    p = act(conn, lambda: photos.add(conn, AUTHOR, data, caption="The bridge"))
    assert (p["placed"], p["area"], p["caption"], str(p["taken_at"])) == ("camera", "bre", "The bridge", "2026-07-14 12:30:00")
    assert abs(p["lon"] - where[0]) < 1e-5 and abs(p["lat"] - where[1]) < 1e-5 and p["facing"] == pytest.approx(123.4)
    assert (p["width"], p["height"], p["ext"]) == (600, 800, "jpg")
    original, shown, thumb = (photos.path(p["sha256"], size, "jpg") for size in ("original", "1600", "thumb"))
    assert original.read_bytes() == data
    assert Image.open(shown).size == (600, 800) and Image.open(thumb).size == (photos.THUMB_PX, photos.THUMB_PX)
    assert not Image.open(shown).getexif().get_ifd(0x8825) and str(settings.photos_dir) in str(shown)
    # The same file again is the photo already kept.
    again = act(conn, lambda: photos.add(conn, AUTHOR, data))
    assert again["id"] == p["id"] and again["again"] and len(photos.listing(conn, "bre")) == 1
    # Found near its route, and not near one far off.
    assert [x["id"] for x in photos.listing(conn, route=route)] == [p["id"]]
    with pytest.raises(Invalid, match="not a photo"):
        act(conn, lambda: photos.add(conn, AUTHOR, b"not a picture at all"))


def test_a_photo_as_a_phone_writes_it_with_more_pictures_in_it_is_kept_as_jpeg(conn):
    first, second = Image.new("RGB", (400, 300), (5, 90, 5)), Image.new("RGB", (400, 300), (90, 5, 5))
    buf = io.BytesIO()
    first.save(buf, "MPO", save_all=True, append_images=[second])
    assert Image.open(io.BytesIO(buf.getvalue())).format == "MPO"
    p = act(conn, lambda: photos.add(conn, AUTHOR, buf.getvalue()))
    assert (p["ext"], p["width"], p["height"]) == ("jpg", 400, 300)


def test_a_photo_with_no_position_waits_to_be_placed_and_can_show_an_issue(conn):
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    p = act(conn, lambda: photos.add(conn, AUTHOR, jpeg(colour=(10, 20, 30))))
    assert (p["placed"], p["lon"], p["area"]) == (None, None, None)
    assert [x["id"] for x in photos.listing(conn, unplaced=True)] == [p["id"]]
    where = on_route(conn, route, east=30)
    placed = act(conn, lambda: photos.change(conn, AUTHOR, p["id"], {"lon": where[0], "lat": where[1], "caption": "Wet"}))
    assert (placed["placed"], placed["area"], placed["caption"]) == ("hand", "bre", "Wet") and photos.listing(conn, unplaced=True) == []
    # It shows an issue, and is listed with it; then with none.
    issue = act(conn, lambda: issues.report(conn, AUTHOR, route=route, kind="wet", text="Bog", spot={"m": 100}))
    act(conn, lambda: photos.change(conn, AUTHOR, p["id"], {"entry": issue["id"]}))
    assert [x["id"] for x in photos.listing(conn, entry=issue["id"])] == [p["id"]]
    # Placed by hand where it says, given with the file: an issue's spot, say.
    q = act(conn, lambda: photos.add(conn, AUTHOR, jpeg(colour=(1, 2, 3)), lon=where[0], lat=where[1], entry=issue["id"]))
    assert q["placed"] == "hand" and q["entry"] == issue["id"]
    act(conn, lambda: photos.change(conn, AUTHOR, p["id"], {"entry": None}))
    assert [x["id"] for x in photos.listing(conn, entry=issue["id"])] == [q["id"]]


def test_a_photo_taken_away_comes_back_with_undo(conn):
    p = act(conn, lambda: photos.add(conn, AUTHOR, jpeg(colour=(200, 10, 10))))
    act(conn, lambda: photos.take_away(conn, AUTHOR, p["id"]))
    assert photos.listing(conn, unplaced=True) == [] and undo.last(conn, AUTHOR)["what"] == "a photo taken away"
    act(conn, lambda: undo.undo(conn, AUTHOR))
    assert [x["id"] for x in photos.listing(conn, unplaced=True)] == [p["id"]]
    assert photos.file(conn, p["id"], "thumb")[0].exists()


def test_photos_are_a_feature_of_their_own_in_the_page_and_for_an_assistant(conn, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    route = act(conn, lambda: routes.make(conn, candidate(conn, "bre18"), AUTHOR))
    data = jpeg(on_route(conn, route), colour=(33, 66, 99))
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        try:
            no = client.post(f"{V1}/photos", content=data, headers={**PAGE, "Content-Type": "image/jpeg"})
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `photos`")
            assert "route_photos" not in {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

            access.set_state(owner, "photos", "all", "test")
            made = client.post(f"{V1}/photos", params={"caption": "Cairn"}, content=data, headers={**PAGE, "Content-Type": "image/jpeg"})
            assert made.status_code == 201 and made.json()["placed"] == "camera"
            pid = made.json()["id"]
            thumb = client.get(f"{V1}/photos/{pid}/thumb")
            assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg" and "immutable" in thumb.headers["cache-control"]
            assert client.get(f"{V1}/photos/{pid}/original").content == data
            assert client.get(f"{V1}/photos/{pid}/huge").status_code == 422
            assert [x["id"] for x in client.get(f"{V1}/photos", params={"route": route}).json()] == [pid]
            assert client.patch(f"{V1}/photos/{pid}", json={"caption": "A cairn"}, headers=PAGE).json()["caption"] == "A cairn"
            assert client.post(f"{V1}/photos", content=b"", headers=PAGE).status_code == 422
            said, refused = call(client, "route_photos", route="bre18")
            assert not refused and said["photos"][0]["shown_at"] == f"/api/v1/photos/{pid}/1600" and "sha256" not in said["photos"][0]
            assert client.delete(f"{V1}/photos/{pid}", headers=PAGE).status_code == 204
            assert client.get(f"{V1}/photos", params={"area": "bre"}).json() == []
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'photos'")
