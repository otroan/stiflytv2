"""Turrutebasen and Stedsnavn adapters against small extracts of real deliveries.

The fixtures hold a few real features per layer, cut by tests/fixtures/build.sh.
A later delivery is made by editing a copy of the fixture, so each test shows
the upstream change it is about.
"""
import uuid

import psycopg
import pytest
from conftest import FIXTURES, later_delivery

from stiflyt2.config import settings
from stiflyt2.importer import core
from stiflyt2.importer.adapters import kulturminner, stedsnavn, teig, turrutebasen, vegnett

TUR = FIXTURES / "turrutebasen.zip"
SSR = FIXTURES / "stedsnavn.zip"
KUL = FIXTURES / "kulturminner.zip"
TEIG = FIXTURES / "teig.zip"
VEG = FIXTURES / "vegnett.zip"

# Segments in the Turrutebasen fixture that the tests change.
RETAGGED = "ce4908aa-d039-413e-996d-96b4440a1dd0"  # route bre20 only
REMOVED = "19fa211c-e4e1-4634-aa5d-299803f37d3f"   # carries four routes
RENAMED = "6ce2c484-2173-4fa7-8978-24f9e42946f0"   # route bre18 only

ARENTZBU = "799507"


def q(sql, *args):
    with psycopg.connect(settings.import_dsn) as conn:
        return conn.execute(sql, args).fetchall()


def members(snapshot_id: str, layer: str) -> dict:
    return dict(q(
        """SELECT fv.external_id, fv.properties FROM source_raw.snapshot_member m
           JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
           WHERE m.snapshot_id = %s AND fv.layer = %s""", snapshot_id, layer))


def changes(snapshot_id: str) -> set:
    return set(q(
        """SELECT i.layer, i.kind, split_part(i.external_id, ':', 1) FROM import_control.diff_item i
           JOIN import_control.diff d ON d.id = i.diff_id WHERE d.to_snapshot = %s""", snapshot_id))


def test_turrutebasen(small, tmp_path):
    r = turrutebasen.run(TUR)
    assert r.counts == {"fotrute": 11, "fotruteinfo": 27, "ruteinfopunkt": 4}

    segments = members(r.snapshot_id, "fotrute")
    assert all(uuid.UUID(lokalid) for lokalid in segments)
    assert not any({"kopidato", "geom", "ogc_fid"} & set(p) for p in segments.values())

    # Every route-info row belongs to a segment in the delivery, and every segment has one.
    info = members(r.snapshot_id, "fotruteinfo")
    assert {p["fotrute_fk"] for p in info.values()} == set(segments)
    assert all(key.split(":")[0] == p["fotrute_fk"] for key, p in info.items())
    assert sorted(p["rutenummer"] for p in info.values() if p["fotrute_fk"] == REMOVED) == [
        "bre19", "bre21", "bre22", "bre62"]

    assert turrutebasen.run(TUR).reused

    # The next weekly delivery: a new copy date on every row and three real changes.
    later = later_delivery(
        TUR, tmp_path,
        "UPDATE fotrute_senterlinje SET kopidato = '2026-11-18T06:00:00Z'",
        "UPDATE ruteinfopunkt_posisjon SET kopidato = '2026-11-18T06:00:00Z'",
        f"UPDATE fotrute_senterlinje SET merking = 'NEI' WHERE lokalid = '{RETAGGED}'",
        f"DELETE FROM fotrute_senterlinje WHERE lokalid = '{REMOVED}'",
        f"DELETE FROM fotruteinfo_tabell WHERE fotrute_fk = '{REMOVED}'",
        f"UPDATE fotruteinfo_tabell SET rutenavn = 'Nytt navn' WHERE fotrute_fk = '{RENAMED}'",
    )
    r2 = turrutebasen.run(later)
    assert r2.counts == {"fotrute": 10, "fotruteinfo": 23, "ruteinfopunkt": 4}
    # A route-info row has no id of its own, so an edited one is a missing and a created.
    assert changes(r2.snapshot_id) == {
        ("fotrute", "updated", RETAGGED),
        ("fotrute", "missing", REMOVED),
        ("fotruteinfo", "missing", REMOVED),
        ("fotruteinfo", "missing", RENAMED),
        ("fotruteinfo", "created", RENAMED),
    }
    assert r2.diff == {"created": 1, "updated": 1, "missing": 6}


def test_stedsnavn(small, tmp_path):
    r = stedsnavn.run(SSR)
    assert r.counts == {"sted": 7}

    places = members(r.snapshot_id, "sted")
    assert set(places) == {ARENTZBU, "131486", "373618", "27269", "555559", "1052910", "1077751"}
    assert not any({"datauttaksdato", "objid", "geom", "ogc_fid"} & set(p) for p in places.values())

    def spellings(place):
        return sorted(s["navn"] for name in place["navn"] for s in name["skrivemater"])

    hut = places[ARENTZBU]
    assert hut["navneobjekttype"] == "turisthytte" and spellings(hut) == ["Arentzbu"]
    assert spellings(places["131486"]) == ["Sprangdalseggi", "Sprongeggi"]
    assert any("andre_skrivemater" in name for name in places["131486"]["navn"])
    assert spellings(places["1077751"]) == ["Čuonjajienjat"]
    assert len(places["555559"]["kommuner"]) == 2

    # One place per stedsnummer, whichever of the four geometry layers it came from.
    kinds = q("""SELECT GeometryType(fv.geom), count(*) FROM source_raw.snapshot_member m
                 JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                 WHERE m.snapshot_id = %s GROUP BY 1""", r.snapshot_id)
    assert dict(kinds) == {"POINT": 3, "MULTIPOINT": 2, "MULTILINESTRING": 1, "MULTIPOLYGON": 1}

    assert stedsnavn.run(SSR).reused

    # A later delivery: a new extract date everywhere, and one spelling corrected.
    later = later_delivery(
        SSR, tmp_path,
        *(f"UPDATE {layer} SET datauttaksdato = '2026-11-20T23:00:00Z'"
          for layer in stedsnavn.GEOM_LAYERS),
        "UPDATE skrivemate SET komplettskrivemate = 'Arentzbu turisthytte' "
        "WHERE komplettskrivemate = 'Arentzbu'",
    )
    r2 = stedsnavn.run(later)
    assert r2.diff == {"created": 0, "updated": 1, "missing": 0}
    assert changes(r2.snapshot_id) == {("sted", "updated", ARENTZBU)}
    assert spellings(members(r2.snapshot_id, "sted")[ARENTZBU]) == ["Arentzbu turisthytte"]


@pytest.mark.parametrize(("adapter", "fixture", "edit"), [
    (turrutebasen, TUR, "UPDATE fotrute_senterlinje SET kopidato = '2026-11-25T06:00:00Z'"),
    (stedsnavn, SSR, "UPDATE sted_posisjon SET datauttaksdato = '2026-11-27T23:00:00Z'"),
])
def test_a_small_nationwide_delivery_is_refused(adapter, fixture, edit, tmp_path):
    with pytest.raises(core.ImportRejected, match="expected at least"):
        adapter.run(later_delivery(fixture, tmp_path, edit))


def test_kulturminner(small, tmp_path):
    r = kulturminner.run(KUL)
    assert r.counts == {"lokalitet": 2, "enkeltminne": 3, "sikringssone": 1}

    # An object delivered as one row per polygon is one feature with all its pieces.
    pieces = dict(q("""SELECT fv.layer || ' ' || fv.external_id, ST_NumGeometries(fv.geom)
                       FROM source_raw.snapshot_member m
                       JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                       WHERE m.snapshot_id = %s""", r.snapshot_id))
    assert pieces == {"lokalitet 57986": 2, "lokalitet 303680-0": 1, "sikringssone 19488": 2,
                      "enkeltminne 57986-2": 1, "enkeltminne 57986-3": 1,
                      "enkeltminne 303680-0": 1}

    sites = members(r.snapshot_id, "lokalitet")
    assert sites["57986"]["navn"] == "RV 866 Langslett - Hamneidet"
    monuments = members(r.snapshot_id, "enkeltminne")
    assert sorted(k for k, p in monuments.items() if p["lokalitetid"] == 57986) == [
        "57986-2", "57986-3"]
    # A date the source got wrong is kept as delivered instead of stopping the import.
    assert sites["303680-0"]["datafangstdato"].startswith("0000/12/30")
    assert not any("datauttaksdato" in p for p in [*sites.values(), *monuments.values()])

    assert kulturminner.run(KUL).reused

    # A later delivery: a new extract date, and one monument given another protection.
    # Only that monument's row is edited in its layer, because rewriting a row
    # with ogrinfo moves its 1900 placeholder dates by a second.
    later = later_delivery(
        KUL, tmp_path,
        "UPDATE sikringssone SET datauttaksdato = '2026-11-15T22:00:00Z'",
        "UPDATE enkeltminne SET vernetype = 'AUT', datauttaksdato = '2026-11-15T22:00:00Z' "
        "WHERE lokalid = '57986-2'",
    )
    r2 = kulturminner.run(later)
    assert changes(r2.snapshot_id) == {("enkeltminne", "updated", "57986-2")}


def test_teig(tmp_path):
    r = teig.run("4644", TEIG)
    assert r.counts == {"teig": 5}
    assert q("SELECT scope FROM source_raw.snapshot WHERE id = %s", r.snapshot_id) == [
        ("kommune:4644",)]

    parcels = members(r.snapshot_id, "teig")
    units = {k: [(u["gardsnummer"], u["bruksnummer"]) for u in p["matrikkelenheter"]]
             for k, p in parcels.items()}
    assert sorted(units["239751175"]) == [(20, 5), (20, 8)]
    assert len(units["368518980"]) == 3
    assert parcels["52558758"]["arealmerknader"] == [
        "HarPunktfester", "HjelpelinjeVannkant", "HjelpelinjeVegkant", "NoenFiktiveGrenser"]
    assert not any("datauttaksdato" in p for p in parcels.values())

    # A parcel delivered as one row per piece is one feature.
    pieces = dict(q("""SELECT fv.external_id, ST_NumGeometries(fv.geom)
                       FROM source_raw.snapshot_member m
                       JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                       WHERE m.snapshot_id = %s""", r.snapshot_id))
    assert pieces == {"239751175": 1, "368518980": 1, "230985469": 3, "52558758": 4,
                      "239563645": 2}

    assert teig.run("4644", TEIG).reused

    # A later delivery: a new extract date, and one property unit renumbered.
    later = later_delivery(
        TEIG, tmp_path,
        "UPDATE teig SET datauttaksdato = '2026-11-22T17:30:00Z'",
        "UPDATE matrikkelenhet SET bruksnummer = 99 "
        "WHERE teig_fk = '239751175' AND bruksnummer = 5",
    )
    r2 = teig.run("4644", later)
    assert changes(r2.snapshot_id) == {("teig", "updated", "239751175")}


def test_teig_wants_a_municipality_number():
    with pytest.raises(core.ImportRejected, match="municipality number"):
        teig.run("Luster")


def test_vegnett(tmp_path):
    """Road links of one municipality, from a cut of the real GML file for Luster."""
    import zipfile

    r = vegnett.run("4644", VEG)
    assert r.counts == {"veglenke": 25}     # the two speed limits in the file are not stored
    assert q("SELECT scope FROM source_raw.snapshot WHERE id = %s", r.snapshot_id) == [
        ("kommune:4644",)]
    links = members(r.snapshot_id, "veglenke")
    link = links["365490_3"]
    # Everything is text, whatever GDAL made of the values; empty attributes are left out.
    assert (link["typeveg"], link["detaljnivå"], link["adressenavn"], link["kommunenummer"]) == (
        "bilveg", "VTKB", "Krekavegen", "4644")
    assert not any({"geom", "ogc_fid", "gml_id"} & set(p) for p in links.values())
    assert q("""SELECT DISTINCT ST_SRID(fv.geom), ST_NDims(fv.geom), GeometryType(fv.geom)
                FROM source_raw.snapshot_member m
                JOIN source_raw.feature_version fv ON fv.id = m.feature_version_id
                WHERE m.snapshot_id = %s""", r.snapshot_id) == [(25833, 2, "LINESTRING")]
    assert vegnett.run("4644", VEG).reused

    # A later delivery: generated at another time, and one road renamed.
    with zipfile.ZipFile(VEG) as z:
        gml = z.read("4644NVDBVegnettPluss.gml").decode()
    assert gml.count("<adressenavn>Bakkingavegen</adressenavn>") == 2
    gml = gml.replace('timeStamp="', 'timeStamp="2099').replace(
        "<adressenavn>Bakkingavegen</adressenavn>", "<adressenavn>Bakkingvegen</adressenavn>", 1)
    later = tmp_path / "later.zip"
    with zipfile.ZipFile(later, "w") as z:
        z.writestr("4644NVDBVegnettPluss.gml", gml)
    r2 = vegnett.run("4644", later)
    assert len(changes(r2.snapshot_id)) == 1 and {c[:2] for c in changes(r2.snapshot_id)} == {
        ("veglenke", "updated")}


def test_vegnett_wants_a_municipality_number():
    with pytest.raises(core.ImportRejected):
        vegnett.run("Luster")
