"""The read-only API and the map page, over the fixture index."""
import math

import pytest
from fastapi.testclient import TestClient

from stiflyt2.api.app import create_app

V1 = "/api/v1"
SHARED_SEGMENT = "19fa211c-e4e1-4634-aa5d-299803f37d3f"


@pytest.fixture
def client(loaded):
    with TestClient(create_app()) as c:
        yield c


def tile_at(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    return (int((lon + 180) / 360 * n),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))


def test_search_finds_places_and_routes(client):
    hut = client.get(f"{V1}/placenames", params={"q": "arentsbu"}).json()[0]
    assert hut["name"] == "Arentzbu" and 7 < hut["lon"] < 8 and 61 < hut["lat"] < 62

    assert client.get(f"{V1}/routes", params={"q": "bre18"}).json()[0]["code"] == "bre18"
    in_area = client.get(f"{V1}/routes", params={"area": "bre"}).json()
    assert [r["code"] for r in in_area][:3] == ["bre14", "bre17", "bre18"]
    on_segment = client.get(f"{V1}/routes", params={"segment": SHARED_SEGMENT}).json()
    assert [r["code"] for r in on_segment] == ["bre19", "bre21", "bre22", "bre62"]
    assert "bre" in [a["prefix"] for a in client.get(f"{V1}/areas").json()]
    assert client.get(f"{V1}/routes").status_code == 422


def test_route_detail(client):
    found = client.get(f"{V1}/routes", params={"q": "bre18"}).json()[0]
    route = client.get(f"{V1}/routes/{found['id']}").json()
    assert route["name"] == "Sprongdalshytta - Arentzbu" and route["segment_count"] == 4
    assert route["geometry"]["type"] == "MultiLineString"
    west, south, east, north = route["bbox"]
    assert 7 < west < east < 8 and 61 < south < north < 62
    # The fixture's paths lie elsewhere, so nothing is claimed about path cover.
    assert route["near_path_share"] is None and route["heritage"] == []
    assert isinstance(route["parcels"], list)
    assert client.get(f"{V1}/routes/unknown").status_code == 404


def test_vector_tiles(client):
    hut = client.get(f"{V1}/placenames", params={"q": "Arentzbu"}).json()[0]
    x, y = tile_at(hut["lon"], hut["lat"], 12)
    tile = client.get(f"{V1}/tiles/trails/12/{x}/{y}.mvt")
    assert tile.status_code == 200
    assert tile.headers["content-type"] == "application/vnd.mapbox-vector-tile"
    assert b"trails" in tile.content and b"bre" in tile.content

    x, y = tile_at(hut["lon"], hut["lat"], 5)  # below the layer's minimum zoom
    assert client.get(f"{V1}/tiles/trails/5/{x}/{y}.mvt").content == b""
    assert client.get(f"{V1}/tiles/unknown/12/1/1.mvt").status_code == 404
    assert client.get(f"{V1}/tiles/trails/3/9/9.mvt").status_code == 404


def test_page_and_status(client):
    assert "maplibre" in client.get("/").text
    assert len(client.get(f"{V1}/status").json()) == 8


def test_anchors(client):
    found = client.get(f"{V1}/routes", params={"q": "bre18"}).json()[0]
    assert (found["shape"], found["pieces"], found["ends"]) == ("line", 1, 2)
    route = client.get(f"{V1}/routes/{found['id']}").json()
    assert route["shape"] == {"shape": "line", "pieces": 1, "ends": 2}
    assert sorted(e["name"] for e in route["ends"]) == ["Arentzbu", "Sprongdalshytta"]

    hut = next(e for e in route["ends"] if e["name"] == "Arentzbu")
    anchor = client.get(f"{V1}/anchors/{hut['id']}").json()
    assert anchor["kind"] == "hut" and anchor["spots"] >= 1 and 7 < anchor["lon"] < 8
    assert "bre18" in [e["code"] for e in anchor["route_ends"]]
    assert "Arentzbu" in [a["name"] for a in client.get(f"{V1}/anchors", params={"area": "bre"}).json()]
    assert client.get(f"{V1}/anchors/place:0").status_code == 404

    x, y = tile_at(anchor["lon"], anchor["lat"], 12)
    assert b"Arentzbu" in client.get(f"{V1}/tiles/anchors/12/{x}/{y}.mvt").content
    assert client.get(f"{V1}/tiles/end_links/12/{x}/{y}.mvt").status_code == 200


def test_places_can_be_adopted_and_edited(client, monkeypatch):
    from stiflyt2.config import settings

    anchor = "place:27269"  # Sprongdalshytta
    # Without a configured editor nothing can be changed.
    assert client.get(f"{V1}/editing").json() == {"editor": None, "changeset": None}
    assert client.post(f"{V1}/places", json={"anchor_id": anchor}).status_code == 403

    monkeypatch.setattr(settings, "editor", "Test Editor")
    # A page on another site cannot make changes through the visitor's browser.
    assert client.post(f"{V1}/places", json={"anchor_id": anchor},
                       headers={"Origin": "https://elsewhere.example"}).status_code == 403

    made = client.post(f"{V1}/places", json={"anchor_id": anchor})
    assert made.status_code == 201
    place = made.json()
    try:
        assert place["name"] == "Sprongdalshytta" and place["access_points"][0]["is_primary"]
        assert place["reviewed"] is True  # made by a person
        assert client.get(f"{V1}/anchors/{anchor}").json()["place_id"] == place["id"]
        assert client.post(f"{V1}/places", json={"anchor_id": anchor}).status_code == 409
        # On the map a place is one point, where its routes arrive.
        drawn = [f for f in client.get(f"{V1}/places.geojson").json()["features"]
                 if f["properties"].get("id") == place["id"]]
        primary = place["access_points"][0]
        assert [f["geometry"]["coordinates"] for f in drawn] == [[primary["lon"], primary["lat"]]]

        renamed = client.patch(f"{V1}/places/{place['id']}",
                               json={"version": place["version"], "name": "Sprongdalshytta DNT"})
        assert renamed.status_code == 200 and renamed.json()["name"] == "Sprongdalshytta DNT"
        stale = client.patch(f"{V1}/places/{place['id']}",
                             json={"version": place["version"], "name": "Too late"})
        assert stale.status_code == 409 and "changed since" in stale.json()["detail"]

        added = client.post(f"{V1}/places/{place['id']}/access-points",
                            json={"lon": place["lon"] + 0.001, "lat": place["lat"]})
        assert added.status_code == 201
        points = added.json()["access_points"]
        assert len(points) == len(place["access_points"]) + 1
        # Further access points are drawn as their own, smaller, points.
        extra = [f for f in client.get(f"{V1}/places.geojson").json()["features"]
                 if f["properties"]["type"] == "access" and f["properties"]["place_id"] == place["id"]]
        assert len(extra) == len(points) - 1
        primary = next(p for p in points if p["is_primary"])
        refused = client.delete(f"{V1}/places/{place['id']}/access-points/{primary['id']}")
        assert refused.status_code == 422 and "another primary" in refused.json()["detail"]
        other = next(p for p in points if not p["is_primary"])
        assert client.delete(f"{V1}/places/{place['id']}/access-points/{other['id']}").status_code == 200

        status = client.get(f"{V1}/editing").json()
        assert status["editor"] == "Test Editor" and status["changeset"]["changes"] >= 4
    finally:
        assert client.delete(f"{V1}/places/{place['id']}").status_code == 204
    assert client.get(f"{V1}/places/{place['id']}").status_code == 404
    assert client.get(f"{V1}/anchors/{anchor}").json()["place_id"] is None
    assert client.post(f"{V1}/changeset/close").status_code == 204
    assert client.get(f"{V1}/editing").json()["changeset"] is None


def test_a_route_candidate_can_be_made_yours(client, monkeypatch):
    from stiflyt2.config import settings

    monkeypatch.setattr(settings, "editor", "Test Editor")
    found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
    made = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"], "code": "T-19"})
    assert made.status_code == 201
    route = made.json()
    try:
        assert route["code"] == "T-19" and route["shape"] == "line" and route["length"]["on_foot_m"] > 0
        assert client.get(f"{V1}/routes/{found['id']}").json()["owned_route_id"] == route["id"]
        lines = client.get(f"{V1}/owned/routes.geojson").json()["features"]
        assert "T-19" in {f["properties"]["codes"] for f in lines}
        again = client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"]})
        assert again.status_code == 409
        renamed = client.patch(f"{V1}/owned/routes/{route['id']}",
                               json={"version": route["version"], "name": "Arentzbu til Fast"})
        assert renamed.json()["name"] == "Arentzbu til Fast"
        ends = renamed.json()["ends"]
        joined = client.post(f"{V1}/owned/routes/{route['id']}/crossings",
                             json={"version": renamed.json()["version"], "from_node": ends[0]["id"],
                                   "to_node": ends[1]["id"], "kind": "water"})
        assert joined.status_code == 201 and joined.json()["length"]["water_m"] > 0
        assert joined.json()["length"]["on_foot_m"] == route["length"]["on_foot_m"]
    finally:
        assert client.delete(f"{V1}/owned/routes/{route['id']}").status_code == 204
    assert client.get(f"{V1}/owned/routes/{route['id']}").status_code == 404
    assert client.get(f"{V1}/routes/{found['id']}").json()["owned_route_id"] is None
    assert client.post(f"{V1}/changeset/close").status_code == 204


def test_a_stretch_changed_by_someone_else_in_the_meantime_is_not_written_over(client, monkeypatch):
    """Two maps with the same stretch on them: the one that comes second is told, and looks again."""
    from stiflyt2.config import settings

    monkeypatch.setattr(settings, "editor", "Test Editor")
    start = (client.get(f"{V1}/undo").json() or {"txid": 0})["txid"]
    found = client.get(f"{V1}/routes", params={"q": "bre19"}).json()[0]
    client.post(f"{V1}/owned/routes", json={"candidate_id": found["id"]}).json()
    try:
        before = client.get(f"{V1}/latest").json()
        assert before["author"] == "Test Editor" and before["through"] == "api"
        line = next(f["properties"] for f in client.get(f"{V1}/owned/routes.geojson").json()["features"]
                    if f["properties"]["codes"] == "bre19")
        edge, seen = line["edge_id"], {"X-Stiflyt-Seen-Rev": str(line["rev"])}
        # The first change, from a map that has the stretch as it is, goes in, and the page of anyone else can tell.
        assert client.patch(f"{V1}/owned/stretches/{edge}", json={"marked": False}, headers=seen).status_code == 204
        after = client.get(f"{V1}/latest").json()
        assert after["seq"] > before["seq"] and after["type"] == "edge.changed"
        # A second map still has it as it was: refused, in words that say who.
        late = client.patch(f"{V1}/owned/stretches/{edge}", json={"signed": False}, headers=seen)
        assert late.status_code == 409
        assert late.json()["detail"].startswith("Test Editor has changed that stretch since your map was drawn")
        assert client.post(f"{V1}/owned/stretches/{edge}/split", json={"lon": 7.5, "lat": 61.5}, headers=seen).status_code == 409
        # Having looked again, it goes in. A request that says nothing of what it has seen is taken as it comes.
        now = next(f["properties"]["rev"] for f in client.get(f"{V1}/owned/routes.geojson").json()["features"]
                   if f["properties"]["edge_id"] == edge)
        assert now == line["rev"] + 1
        assert client.patch(f"{V1}/owned/stretches/{edge}", json={"signed": False},
                            headers={"X-Stiflyt-Seen-Rev": str(now)}).status_code == 204
        assert client.patch(f"{V1}/owned/stretches/{edge}", json={"marked": True}).status_code == 204
    finally:
        # Taken back change by change, the making of the route last: nothing of this is left for other tests.
        while (latest := client.get(f"{V1}/undo").json()) and latest["txid"] > start:
            assert client.post(f"{V1}/undo", json={"txid": latest["txid"]}).status_code == 200
