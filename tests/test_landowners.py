"""Who owns the ground: the properties at a spot and along a route from the property map, and their owners from a
made-up register shaped as Kartverket's answers. Never the real register: the tests ask nobody about real people."""
import io
import json
from types import SimpleNamespace as O

import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.testclient import TestClient

from stiflyt2 import access
from stiflyt2.api.app import create_app
from stiflyt2.config import settings
from stiflyt2.domain import routes
from stiflyt2.domain import undo as routes_undo
from stiflyt2.ops import landowners

AUTHOR = "Test Editor"
V1 = "/api/v1"
PAGE = {"X-Stiflyt-Through": "viewer"}
PERSON_NUMBER = "01017012345"        # a made-up national identity number: never to be passed on


def typed(name, **fields):
    return O(_xsd_type=O(name=name), **fields)


class Register:
    """Owners for any unit asked about: a person (with a share), an organisation, one whose ownership ended, and
    a farm (unit 5301) that owns half, as the farms own a summer farm. The farm is owned by the same two."""

    FARM = 5301

    def __init__(self):
        self.asked = []

    @staticmethod
    def relation(kind, code, teller=1, nevner=1, ended=None, **who):
        return typed(kind, eierforholdKodeId=O(value=code), sluttdato=ended, andel=O(teller=teller, nevner=nevner),
                     datoFra=O(date="2001-05-02"), **who)

    def units(self, ids):
        self.asked.append(("units", list(ids)))
        people = [self.relation("PersonTinglystEierforhold", 1, 1, 2, eierId=O(value=7)),
                  self.relation("JuridiskPersonIkkeTinglystEierforhold", 2, eierId=O(value=8)),
                  self.relation("PersonTinglystEierforhold", 1, ended="2010-01-01", eierId=O(value=9))]
        farm = [self.relation("MatrikkelenhetTinglystEierforhold", 1, 1, 2, eiendeMatrikkelenhetId=O(value=self.FARM)),
                # The same farm again as the current owner, as the register says it: one row with both words.
                self.relation("MatrikkelenhetIkkeTinglystEierforhold", 2, 1, 2, eiendeMatrikkelenhetId=O(value=self.FARM)),
                self.relation("SomethingNewEierforhold", 1)]      # a kind of ownership not known here: left out
        return [O(id=O(value=int(i)), eierforhold=O(item=people + ([] if int(i) == self.FARM else farm))) for i in ids]

    def objects(self, ids):
        self.asked.append(("objects", [x.value for x in ids]))
        known = {
            7: typed("FysiskPerson", id=O(value=7), navn="Nordmann Kari", nummer=PERSON_NUMBER,
                     postadresse=O(adresselinje1="Postboks 5", adresselinje2=None, adresselinje3=None, postnummeromradeId=O(value=50)),
                     bostedsadresse=O(adresselinje1="Hjemmevegen 1")),
            8: typed("JuridiskPerson", id=O(value=8), navn="Fjellstyret AS", nummer=987654321, postadresse=None),
            9: typed("FysiskPerson", id=O(value=9), navn="Gammel Eier", nummer="02027012345", postadresse=None),
            self.FARM: typed("Grunneiendom", id=O(value=self.FARM), bruksnavn="Bergeset",
                             matrikkelnummer=O(kommuneId=O(value=3427), gardsnummer=53, bruksnummer=1, festenummer=0, seksjonsnummer=0)),
            1: O(id=O(value=1), kodeverdi="H", navn=O(entry=[O(key=None, value="Hjemmelshaver")])),
            2: O(id=O(value=2), kodeverdi="AE", navn=O(entry=[O(key=None, value="Aktuell eier")])),
            50: O(id=O(value=50), kretsnummer="6868", kretsnavn="GAUPNE"),
        }
        return [known[x.value] for x in ids if x.value in known]


@pytest.fixture
def register(monkeypatch):
    made = Register()
    monkeypatch.setattr(landowners, "register", lambda: made)
    landowners._owners.clear()
    landowners._known.clear()
    return made


@pytest.fixture
def route(loaded):
    """bre18 made a route of ours for the test, with parcels of its own along it, and both taken away after.

    The fixture's property map has no parcel along a route: property A lies over the route's first stretch and
    its third, B over its second, 30 m to either side. The rest of the route crosses no parcel.
    """
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as conn, \
            psycopg.connect(role_dsn("stiflyt2_import"), autocommit=True) as index:
        start = conn.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
        with conn.transaction():
            made = str(routes.make(conn, conn.execute("SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0], AUTHOR))
        a = [{"id": "900001", "gnr": 12, "bnr": 3, "type": "Grunneiendom", "name": "Øvre Seter"}]
        b = [{"id": "900002", "gnr": 12, "bnr": 4, "fnr": 1, "type": "Festegrunn", "name": " "}]
        try:
            for seq, units in ((0, a), (1, b), (2, a)):
                shape = conn.execute("""
                    SELECT ST_AsEWKT(ST_Multi(ST_Buffer(v.geom, 30))) FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
                    JOIN core.edge_revision v ON v.id = e.current_revision_id WHERE l.route_id = %s ORDER BY l.seq OFFSET %s LIMIT 1""",
                    (made, seq)).fetchone()[0]
                index.execute("""INSERT INTO source_index.parcel (id, version_id, geom, municipality_no, matrikkel, area_m2, disputed, units)
                                 VALUES ('test-' || %s, 0, ST_GeomFromEWKT(%s), '4644', %s, 1, false, %s)""",
                              (seq, shape, f"{units[0]['gnr']}/{units[0]['bnr']}", json.dumps(units)))
            yield conn, made
        finally:
            index.execute("DELETE FROM source_index.parcel WHERE id LIKE 'test-%'")
            while (latest := routes_undo.last(conn)) and latest["txid"] > start:
                with conn.transaction():
                    routes_undo.undo(conn, AUTHOR)


def a_spot(conn):
    """A spot inside a parcel of a numbered property of the fixture's municipality, as lon, lat."""
    return conn.execute("""
        SELECT ST_X(g), ST_Y(g) FROM (SELECT ST_Transform(ST_PointOnSurface(p.geom), 4326) AS g FROM source_index.parcel p
        WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(p.units) u WHERE (u->>'gnr')::int > 0) ORDER BY p.area_m2 DESC LIMIT 1) x""").fetchone()


def test_the_properties_along_a_route_in_the_order_it_crosses_them(route):
    conn, made = route
    found = landowners.along(conn, made)
    props = found["properties"]
    assert found["route"]["code"] == "bre18" and found["metres"] > 0
    # A, crossed twice with B between: two spans of A, one of B; their numbers and names as they are written.
    assert [(p["label"], p["name"], len(p["spans"])) for p in props] == [("4644-12/3", "Øvre Seter", 2), ("4644-12/4/1", None, 1)]
    assert props[0]["spans"][0]["from_m"] == 0 and props[0]["spans"][0]["to_m"] <= props[1]["spans"][0]["from_m"] + 61
    assert found["unknown"] and found["unknown"][-1]["to_m"] == found["metres"]
    # In the order the route crosses them; every span within the route, and as long as it says.
    firsts = [p["spans"][0]["from_m"] for p in props]
    assert firsts == sorted(firsts)
    for p in props:
        assert all(0 <= s["from_m"] <= s["to_m"] <= found["metres"] + 1 for s in p["spans"])
        assert abs(p["metres"] - sum(s["to_m"] - s["from_m"] for s in p["spans"])) <= len(p["spans"])
        assert p["lines"] and all(len(line) >= 2 for line in p["lines"])
    # What is on a property and what lies on none add up to the route, near enough.
    on = sum(p["metres"] for p in props) + sum(s["to_m"] - s["from_m"] for s in found["unknown"])
    assert on == pytest.approx(found["metres"], rel=0.05)
    assert found["not_loaded"] == [] or all(isinstance(n, str) for n in found["not_loaded"])
    with pytest.raises(landowners.NotFound):
        landowners.along(conn, "not-a-route")


def test_owners_as_the_register_says_never_a_persons_number_or_home(route, register):
    conn, made = route
    found = landowners.along(conn, made)
    units = [p for p in found["properties"] if p["id"]]
    assert units
    landowners.with_owners(found, found["properties"])
    told = units[0]["owners"]
    # The ownership that ended is left out, and the kind not known here; registered owners first.
    assert [(o["name"], o["kind"], o["role"], o["share"], o["registered"]) for o in told] == [
        ("3427-53/1 Bergeset", "property", "owner", "1/2", True), ("Nordmann Kari", "person", "owner", "1/2", True),
        ("Fjellstyret AS", "organisation", "Aktuell eier", "1/1", False)]
    # The farm that owns half: who owns it, one step on, and no further.
    farm = told[0]
    assert farm["label"] == "3427-53/1" and [n["name"] for n in farm["owners"]] == ["Nordmann Kari", "Fjellstyret AS"]
    assert farm["said"] == "Hjemmelshaver, Aktuell eier"
    told = told[1:]
    assert told[0]["address"] == "Postboks 5, 6868 GAUPNE" and told[0]["org_number"] is None and told[0]["since"] == "2001-05-02"
    assert told[1]["org_number"] == "987654321" and told[1]["said"] == "Aktuell eier"
    said = json.dumps(found)
    assert PERSON_NUMBER not in said and "Hjemmevegen" not in said and "Gammel Eier" not in said
    # Asked again within the hour: the register is not asked again; a unit is asked once however often it is crossed.
    asked = len(register.asked)
    landowners.owners([u["id"] for u in units])
    assert len(register.asked) == asked
    assert register.asked[0] == ("units", list(dict.fromkeys(u["id"] for u in units)))
    assert ("units", [str(Register.FARM)]) in register.asked


def test_without_an_account_it_says_so_and_the_properties_stand(route, monkeypatch):
    conn, made = route
    monkeypatch.setattr(settings, "matrikkel_username", "")
    monkeypatch.setattr(landowners, "_register", None)
    landowners._owners.clear()
    found = landowners.with_owners(landowners.along(conn, made), [])
    found = landowners.with_owners(found, [{"id": "123"}])
    assert "no account for Kartverket's property register" in found["owners_unavailable"]


def test_landowners_are_a_feature_of_their_own_and_every_lookup_is_kept(route, register, monkeypatch):
    conn, made = route
    monkeypatch.setattr(settings, "editor", AUTHOR)
    lon, lat = a_spot(conn)
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as owner, TestClient(create_app()) as client:
        owner.execute("SET ROLE stiflyt2_owner")
        before = owner.execute("SELECT coalesce(max(id), 0) FROM ops.owner_lookup").fetchone()[0]
        try:
            # Not given to anybody by a role: the editor does not have it, not even to look.
            no = client.get(f"{V1}/landowners/at", params={"lon": lon, "lat": lat}, headers=PAGE)
            assert (no.status_code, no.json()["detail"]) == (403, "that is not switched on for you: ask to be given the feature `landowners`")
            assert "landowners" not in client.get(f"{V1}/auth/me").json()["features"]

            access.set_state(owner, "landowners", "all", "test")
            at = client.get(f"{V1}/landowners/at", params={"lon": lon, "lat": lat}, headers=PAGE).json()
            assert at["units"] and at["outline"]["type"] in ("Polygon", "MultiPolygon") and "owners" not in at["units"][0]
            assert owner.execute("SELECT count(*) FROM ops.owner_lookup WHERE id > %s", (before,)).fetchone()[0] == 0
            # Who owns it, asked for: said, and that it was asked is kept, not what was said.
            at = client.get(f"{V1}/landowners/at", params={"lon": lon, "lat": lat, "owners": True}, headers=PAGE).json()
            assert "Nordmann Kari" in [o["name"] for o in at["units"][0]["owners"]]
            kept = owner.execute("SELECT who, through, what, units FROM ops.owner_lookup WHERE id > %s", (before,)).fetchall()
            assert kept == [(AUTHOR, "viewer", f"a spot at {lat:.5f}, {lon:.5f}", [u["id"] for u in at["units"] if u["id"]])]
            assert client.get(f"{V1}/landowners/at", params={"lon": 4.0, "lat": 58.0}, headers=PAGE).json() == {
                "parcel": None, "units": [], "outline": None, "said": "No property is known here."}

            along = client.get(f"{V1}/landowners/along", params={"route": made, "owners": True}, headers=PAGE).json()
            assert along["route"]["code"] == "bre18" and any(p.get("owners") for p in along["properties"])
            listed = client.get(f"{V1}/landowners/along.xlsx", params={"route": made}, headers=PAGE)
            assert listed.status_code == 200 and "grunneiere-bre18.xlsx" in listed.headers["content-disposition"]
            from openpyxl import load_workbook

            sheet = load_workbook(io.BytesIO(listed.content)).active
            rows = list(sheet.iter_rows(values_only=True))
            assert rows[3][:6] == ("Property", "Municipality", "Name of the property", "Along the route, km", "Metres", "Owner")
            assert "Nordmann Kari" in {r[5] for r in rows[4:]} and PERSON_NUMBER not in str(rows)
            assert "3427-53/1 Bergeset (a property)" in {r[5] for r in rows[4:]}
            assert ("Nordmann Kari, through 3427-53/1", "1/2 of 1/2") in {(r[5], r[6]) for r in rows[4:]}
            assert owner.execute("SELECT count(*) FROM ops.owner_lookup WHERE id > %s AND what = 'bre18'", (before,)).fetchone()[0] == 2
            assert client.get(f"{V1}/landowners/along", params={"route": "nowhere"}, headers=PAGE).status_code == 404
        finally:
            owner.execute("DELETE FROM access.feature WHERE key = 'landowners'")
            owner.execute("DELETE FROM ops.owner_lookup WHERE id > %s", (before,))
