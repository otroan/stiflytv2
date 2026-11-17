"""The open WFS adapter against a canned server: no network, made-up paths."""
from pathlib import Path

import httpx
import psycopg
import pytest
from typer.testing import CliRunner

from stiflyt2 import cli
from stiflyt2.config import settings
from stiflyt2.importer import core
from stiflyt2.importer.adapters import fkb_wfs

# The layer schema as the server gives it (DescribeFeatureType, 2026-11-14).
SCHEMA = b"""<?xml version='1.0' encoding="UTF-8" ?>
<schema
   targetNamespace="http://mapserver.gis.umn.edu/mapserver"
   xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
   xmlns:xsd="http://www.w3.org/2001/XMLSchema"
   xmlns="http://www.w3.org/2001/XMLSchema"
   xmlns:gml="http://www.opengis.net/gml/3.2"
   elementFormDefault="qualified" version="0.1" >
  <import namespace="http://www.opengis.net/gml/3.2"
          schemaLocation="http://schemas.opengis.net/gml/3.2.1/gml.xsd" />
  <element name="traktorveg_sti"
           type="ms:traktorveg_stiType"
           substitutionGroup="gml:AbstractFeature" />
  <complexType name="traktorveg_stiType">
    <complexContent>
      <extension base="gml:AbstractFeatureType">
        <sequence>
          <element name="msGeometry" type="gml:GeometryPropertyType" minOccurs="0" maxOccurs="1"/>
          <element name="objtype" minOccurs="0" type="string"/>
          <element name="vegkategori" minOccurs="0" type="string"/>
          <element name="vegfase" minOccurs="0" type="string"/>
          <element name="vegnummer" minOccurs="0" type="string"/>
          <element name="strekningnummer" minOccurs="0" type="string"/>
          <element name="kommunenummer" minOccurs="0" type="string"/>
          <element name="klasselandbruksveg" minOccurs="0" type="string"/>
          <element name="typeveg" minOccurs="0" type="string"/>
        </sequence>
      </extension>
    </complexContent>
  </complexType>
</schema>
"""

HEADER = """<?xml version='1.0' encoding="UTF-8" ?>
<wfs:FeatureCollection
   xmlns:ms="http://mapserver.gis.umn.edu/mapserver"
   xmlns:gml="http://www.opengis.net/gml/3.2"
   xmlns:wfs="http://www.opengis.net/wfs/2.0"
   timeStamp="{stamp}" numberMatched="{matched}" numberReturned="{returned}"{more}>
"""

# Attributes are empty elements when unset, as in the real responses.
MEMBER = """    <wfs:member>
      <ms:traktorveg_sti gml:id="traktorveg_sti.{n}">
        <ms:msGeometry>
          <gml:LineString gml:id="traktorveg_sti.{n}.1" srsName="urn:ogc:def:crs:EPSG::25833">
            <gml:posList srsDimension="2">{coords}</gml:posList>
          </gml:LineString>
        </ms:msGeometry>
        <ms:objtype>Veglenke</ms:objtype>
        <ms:vegkategori></ms:vegkategori>
        <ms:vegfase></ms:vegfase>
        <ms:vegnummer></ms:vegnummer>
        <ms:strekningnummer></ms:strekningnummer>
        <ms:kommunenummer>4644</ms:kommunenummer>
        <ms:klasselandbruksveg></ms:klasselandbruksveg>
        <ms:typeveg>{typeveg}</ms:typeveg>
      </ms:traktorveg_sti>
    </wfs:member>
"""

BOX = (100_000, 6_800_000, 150_000, 6_850_000)  # four tiles
A = ("101000 6801000 101500 6801200", "sti")
B = ("102000 6802000 102600 6802100", "traktorveg")
C = ("124900 6805000 125100 6805000", "sti")  # crosses a tile edge: both tiles return it
D = ("130000 6830000 130400 6830300", "sti")
A_MOVED = ("101000 6801000 101500 6801250", "sti")


def meets(feature, box) -> bool:
    numbers = [float(v) for v in feature[0].split()]
    xs, ys = numbers[0::2], numbers[1::2]
    return min(xs) <= box[2] and max(xs) >= box[0] and min(ys) <= box[3] and max(ys) >= box[1]


class Server:
    """Answers like the WFS: the features that meet the requested box."""

    def __init__(self):
        self.features = [A, B, C, D]
        self.stamp = "2026-11-14T11:22:18"
        self.cap = 10_000  # most features sent in one response
        self.lost = 0      # features the response claims to hold but does not
        self.links = True  # a response that is cut short links to the next page

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = request.url.params
        if query["request"] == "DescribeFeatureType":
            return httpx.Response(200, content=SCHEMA)
        box = [int(v) for v in query["bbox"].split(",")[:4]]
        matched = [f for f in self.features if meets(f, box)]
        sent = matched[: self.cap]
        # As the real service: no count for a tile it sends only the first page of, nor for an empty one.
        cut = len(sent) < len(matched)
        body = HEADER.format(stamp=self.stamp, returned=len(sent),
                             matched="unknown" if cut or not matched else len(matched),
                             more='\n   next="https://wfs.example/next"' if cut and self.links else "")
        body += "".join(MEMBER.format(n=n, coords=coords, typeveg=typeveg)
                        for n, (coords, typeveg) in enumerate(sent[: len(sent) - self.lost], 1))
        return httpx.Response(200, content=(body + "</wfs:FeatureCollection>\n").encode())


@pytest.fixture
def server(monkeypatch):
    s = Server()
    real = httpx.Client
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(s), **kw))
    return s


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def test_checksum_ignores_the_response_timestamp():
    first = HEADER.format(stamp="2026-11-14T11:22:18", matched=0, returned=0, more="").encode()
    later = HEADER.format(stamp="2026-11-14T11:22:54", matched=0, returned=0, more="").encode()
    assert first != later
    assert fkb_wfs.content_sha256(first) == fkb_wfs.content_sha256(later)


def test_checksum_changes_with_the_data():
    body = HEADER.format(stamp="2026-11-14T11:22:18", matched=1, returned=1, more="")
    before = (body + MEMBER.format(n=1, coords=A[0], typeveg="sti")).encode()
    after = (body + MEMBER.format(n=1, coords=A[0], typeveg="traktorveg")).encode()
    assert fkb_wfs.content_sha256(before) != fkb_wfs.content_sha256(after)


def test_tiles_cover_the_box_without_overlap():
    assert fkb_wfs.tiles((0, 0, 50_000, 30_000)) == [
        (0, 0, 25_000, 25_000), (0, 25_000, 25_000, 30_000),
        (25_000, 0, 50_000, 25_000), (25_000, 25_000, 50_000, 30_000),
    ]


def test_import_stores_each_path_once(server):
    r = fkb_wfs.run("first", BOX)
    assert r.counts == {"veglenke": 4} and r.diff["created"] == 4
    props = [p for (p,) in q(
        """SELECT fv.properties FROM source_raw.snapshot_member m
           JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
           WHERE m.snapshot_id = %s""", r.snapshot_id)]
    assert sorted(p["typeveg"] for p in props) == ["sti", "sti", "sti", "traktorveg"]
    # Typed by the kept schema: a string, and unset attributes are left out.
    assert all(p["kommunenummer"] == "4644" and "vegkategori" not in p for p in props)
    artifact = q("SELECT artifact_path FROM source_raw.snapshot WHERE id = %s", r.snapshot_id)
    assert (Path(artifact[0][0]) / fkb_wfs.SCHEMA_FILE).read_bytes() == SCHEMA


def test_fetching_unchanged_data_again_is_the_same_snapshot(server):
    r1 = fkb_wfs.run("again", BOX)
    server.stamp = "2026-11-14T11:22:54"
    r2 = fkb_wfs.run("again", BOX)
    assert r2.reused and r2.snapshot_id == r1.snapshot_id
    assert q("SELECT count(*) FROM source_raw.snapshot WHERE scope = 'bbox:again'") == [(1,)]


def test_moved_path_is_one_missing_and_one_created(server):
    fkb_wfs.run("moved", BOX)
    server.features = [A_MOVED, B, C, D]
    r = fkb_wfs.run("moved", BOX)
    assert r.diff == {"created": 1, "updated": 0, "missing": 1}


def test_incomplete_response_is_refused(server):
    r1 = fkb_wfs.run("partial", BOX)
    server.lost = 1
    with pytest.raises(core.ImportRejected, match="body holds 2 features, header says 3"):
        fkb_wfs.run("partial", BOX)
    assert q("""SELECT snapshot_id::text FROM import_control.active_snapshot
                WHERE scope = 'bbox:partial'""") == [(r1.snapshot_id,)]


def test_dense_tile_is_fetched_as_smaller_tiles(server):
    server.cap = 2  # the first tile holds A, B and C
    r = fkb_wfs.run("dense", BOX)
    assert r.counts == {"veglenke": 4}
    tiles = q("SELECT manifest->'tiles' FROM source_raw.snapshot WHERE id = %s", r.snapshot_id)
    assert len(tiles[0][0]) == 7  # three whole tiles and four quarters
    assert tiles[0][0]["100000,6800000,112500,6812500"] == 2


def test_dense_tile_is_fetched_as_smaller_tiles_also_when_no_next_page_is_named(server):
    server.cap, server.links = 2, False
    r = fkb_wfs.run("dense-unlinked", BOX)
    assert r.counts == {"veglenke": 4}


def test_tile_with_nothing_in_it_is_no_reason_to_refuse(server):
    server.features = [A, B]  # three of the four tiles hold nothing
    r = fkb_wfs.run("sparse", BOX)
    assert r.counts == {"veglenke": 2}
    tiles = q("SELECT manifest->'tiles' FROM source_raw.snapshot WHERE id = %s", r.snapshot_id)
    assert sorted(tiles[0][0].values()) == [0, 0, 0, 2]


def test_box_too_dense_to_fetch_is_refused(server):
    server.cap = 0
    with pytest.raises(core.ImportRejected, match="even at 500 m"):
        fkb_wfs.run("crowded", BOX)


def test_kept_files_are_checked_against_the_stored_checksum(server):
    r = fkb_wfs.run("kept", BOX)
    verify = ["sources", "verify", "--source", "fkb_wfs"]
    assert CliRunner().invoke(cli.app, verify).exit_code == 0

    kept = Path(q("SELECT artifact_path FROM source_raw.snapshot WHERE id = %s", r.snapshot_id)[0][0])
    tile = kept / "100000_6800000_125000_6825000.gml"
    original = tile.read_bytes()
    tile.write_bytes(original.replace(b">traktorveg<", b">sti<"))
    result = CliRunner().invoke(cli.app, verify)
    tile.write_bytes(original)
    assert result.exit_code == 1 and "CHANGED" in result.output
