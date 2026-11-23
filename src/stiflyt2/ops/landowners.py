"""Who owns the ground: at a spot on the map, and along a route. The feature `landowners` (step 5 of the plan).

The properties come from Kartverket's property map (Matrikkelen Eiendomskart
Teig, imported per municipality, CC BY 4.0): each parcel, and the property units
(gnr/bnr/fnr/snr) it belongs to. Who owns a unit is asked of Kartverket's
Matrikkel API when a person asks for it, with the account DNT has on
application, and is not kept: names and addresses of people are personal data.
What is kept is that it was asked, by whom, when and of which units
(`ops.owner_lookup`), so that the use of the register can be accounted for.

A private owner's national identity number, which the register sends, is never
passed on, nor their home address: a person is their name and the postal
address the register has for them. An organisation is its name, its
organisation number (public) and its postal address.

Seeing who owns the ground is a feature of its own, given to a person by name
(decided 2026-11-19): it is in no role.
"""
from __future__ import annotations

import io
import threading
import time
import uuid
from datetime import UTC, datetime

from psycopg.rows import dict_row

from stiflyt2 import features
from stiflyt2.config import settings
from stiflyt2.domain.places import NotFound
from stiflyt2.ops import along as on_route

features.feature("landowners", "see who owns the ground the routes cross", "ask to be given the feature `landowners`")

KEPT_S = 3600           # who owns a unit is asked again after an hour, not on every look
GAP_M = 2               # spans of one property this near are one span
UNKNOWN_M = 20          # a stretch with no property this long is said
NEAR_M = 1000           # as for the roads: a municipality counts when one of its place names lies this near
_POINT = "ST_Transform(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 25833)"
_ROLES = {"H": "owner"}  # the register's own word is given beside it ("Hjemmelshaver"); others in its words


class Unavailable(Exception):
    """The register cannot be asked: no account set up here, or it did not answer."""


def _rows(conn, sql: str, params: dict | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params or {}).fetchall()


# --- the properties -------------------------------------------------------------------------------------------------

def _label(municipality: str | None, u: dict) -> str:
    """A property's number as it is written: municipality-gnr/bnr, with /fnr and /snr where it has them."""
    tail = "".join(f"/{u[k]}" for k in ("fnr", "snr") if u.get(k))
    return f"{municipality or '?'}-{u.get('gnr')}/{u.get('bnr')}{tail}"


def _names(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT municipality_no, min(municipality) FROM source_index.place "
                             "WHERE municipality_no IS NOT NULL GROUP BY 1").fetchall())


def _unit(u: dict, municipality: str | None, names: dict) -> dict:
    """A property unit as it is shown. Number 0 is the property map's own for land and water that has no
    property number ("Mnr mangler", "Mnr vann mangler"): nobody is asked who owns it."""
    town = names.get(municipality, municipality)
    if not u.get("gnr"):
        return {"id": None, "label": "water without a property number" if u.get("bnr") == 1 else "land without a property number",
                "municipality": town, "name": None, "type": None}
    return {"id": str(u["id"]) if u.get("id") is not None else None, "label": _label(municipality, u),
            "municipality": town, "name": (u.get("name") or "").strip() or None, "type": u.get("type")}


def _key(u: dict) -> str:
    return u["id"] or f"{u['municipality']}: {u['label']}"


def _loaded(conn) -> set[str]:
    """The municipalities whose property map is loaded."""
    return {s.removeprefix("kommune:") for (s,) in conn.execute(
        "SELECT scope FROM import_control.active_snapshot WHERE source_key = 'teig'")}


def at(conn, lon: float, lat: float) -> dict:
    """The property at a spot: its parcel's outline and number, and the units it belongs to. Where no parcel is
    known, no parcel, and why where it is known (the municipality's property map not loaded)."""
    found = _rows(conn, f"""
        SELECT p.id::text AS parcel, p.municipality_no, p.matrikkel, p.units, p.area_m2, p.disputed,
               ST_AsGeoJSON(ST_Transform(p.geom, 4326), 6)::json AS outline
        FROM source_index.parcel p WHERE ST_Intersects(p.geom, {_POINT}) ORDER BY p.area_m2 LIMIT 1""",
        {"lon": lon, "lat": lat})
    if not found:
        near = conn.execute(f"""
            SELECT municipality_no, min(municipality) FROM source_index.place
            WHERE municipality_no IS NOT NULL AND ST_DWithin(geom, {_POINT}, 10000)
            GROUP BY 1 ORDER BY min(ST_Distance(geom, {_POINT})) LIMIT 1""", {"lon": lon, "lat": lat}).fetchone()
        said = (f"The property map of {near[1]} is not loaded: no property is known here." if near and near[0] not in _loaded(conn)
                else "No property is known here.")
        return {"parcel": None, "units": [], "outline": None, "said": said}
    p, names = found[0], _names(conn)
    return {"parcel": p["parcel"], "municipality": names.get(p["municipality_no"], p["municipality_no"]),
            "area_m2": round(p["area_m2"]) if p["area_m2"] is not None else None, "disputed": bool(p["disputed"]),
            "units": [_unit(u, p["municipality_no"], names) for u in p["units"] or []], "outline": p["outline"]}


def along(conn, route: str) -> dict:
    """The properties a route crosses, in the order it crosses them, with where along it and how far.

    A property the route crosses more than once has a span for each time. Where the route crosses no known
    property for more than a few metres, that is said, and why where it is known (the municipality's
    property map not loaded).
    """
    try:
        uuid.UUID(route)
    except ValueError:
        raise NotFound(f"no route in use has the id {route}") from None
    r = conn.execute("SELECT code, name FROM core.route WHERE id = %s AND status = 'active'", (route,)).fetchone()
    if not r:
        raise NotFound(f"no route in use has the id {route}")
    walked = on_route.Route(conn, route)
    legs = {leg["seq"]: leg for leg in walked.legs}
    parts = _rows(conn, """
        SELECT x.seq, p.municipality_no, p.units,
               ST_LineLocatePoint(v.geom, ST_StartPoint(part.geom)) AS a, ST_LineLocatePoint(v.geom, ST_EndPoint(part.geom)) AS b,
               ST_AsGeoJSON(ST_Transform(part.geom, 4326), 6)::json -> 'coordinates' AS line
        FROM unnest(%(seqs)s::int[], %(edges)s::uuid[]) x(seq, edge)
        JOIN core.edge e ON e.id = x.edge JOIN core.edge_revision v ON v.id = e.current_revision_id
        JOIN source_index.parcel p ON ST_Intersects(p.geom, v.geom)
        CROSS JOIN LATERAL ST_Dump(ST_CollectionExtract(ST_Intersection(v.geom, p.geom), 2)) part
        WHERE ST_Length(part.geom) > 0.5""",
        {"seqs": list(legs), "edges": [leg["edge"] for leg in legs.values()]})
    names = _names(conn)
    spans = []
    for x in parts:
        leg = legs[x["seq"]]
        lo, hi = sorted((x["a"], x["b"]))
        if leg["reversed"]:
            lo, hi = 1 - hi, 1 - lo
        line = x["line"] if not leg["reversed"] else list(reversed(x["line"]))
        units = [_unit(u, x["municipality_no"], names) for u in x["units"] or []]
        spans.append({"from_m": leg["start"] + leg["m"] * lo, "to_m": leg["start"] + leg["m"] * hi, "units": units,
                      "key": tuple(sorted(_key(u) for u in units)) or ("none",), "line": line})
    spans.sort(key=lambda s: s["from_m"])
    merged: list[dict] = []
    for s in spans:          # one property, crossed without a break: one span
        last = next((m for m in reversed(merged[-4:]) if m["key"] == s["key"]), None)
        if last and s["from_m"] - last["to_m"] <= GAP_M:
            last["to_m"] = max(last["to_m"], s["to_m"])
            last["lines"].append(s["line"])
        else:
            merged.append({**s, "lines": [s["line"]]})
    properties: dict[str, dict] = {}
    for s in merged:
        for u in s["units"] or [{"id": None, "label": "not in the cadastre", "municipality": None, "name": None, "type": None}]:
            p = properties.setdefault(_key(u), {**u, "metres": 0.0, "spans": [], "lines": []})
            p["spans"].append({"from_m": round(s["from_m"]), "to_m": round(s["to_m"])})
            p["metres"] += s["to_m"] - s["from_m"]
            p["lines"] += s["lines"]
    for p in properties.values():
        p["metres"] = round(p["metres"])
    # Where no property is known along the route, and the municipalities near it whose property map is not loaded.
    unknown, at_m = [], 0.0
    for s in sorted(merged, key=lambda s: s["from_m"]):
        if s["from_m"] - at_m > UNKNOWN_M:
            unknown.append({"from_m": round(at_m), "to_m": round(s["from_m"])})
        at_m = max(at_m, s["to_m"])
    if walked.metres - at_m > UNKNOWN_M:
        unknown.append({"from_m": round(at_m), "to_m": round(walked.metres)})
    near = [n for (n,) in conn.execute("""
        SELECT DISTINCT pl.municipality_no FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
        JOIN core.edge_revision v ON v.id = e.current_revision_id
        JOIN source_index.place pl ON ST_DWithin(pl.geom, v.geom, %(near)s)
        WHERE l.route_id = %(r)s AND pl.municipality_no IS NOT NULL""", {"r": route, "near": NEAR_M})]
    loaded = _loaded(conn)
    return {"route": {"id": route, "code": r[0], "name": r[1]}, "metres": round(walked.metres),
            "properties": sorted(properties.values(), key=lambda p: p["spans"][0]["from_m"]),
            "unknown": unknown, "not_loaded": sorted(names.get(n, n) for n in near if n not in loaded)}


# --- who owns them: Kartverket's Matrikkel API ----------------------------------------------------------------------

class Register:
    """The Matrikkel API's store of objects, as far as who owns a property. Made when first asked for."""

    def __init__(self) -> None:
        import requests
        from zeep import Client
        from zeep import Settings as Zeep
        from zeep.transports import Transport

        if not settings.matrikkel_username or not settings.matrikkel_password:
            raise Unavailable("who owns it cannot be asked here: the server has no account for Kartverket's "
                              "property register (STIFLYT2_MATRIKKEL_USERNAME and _PASSWORD)")
        session = requests.Session()
        session.auth = (settings.matrikkel_username, settings.matrikkel_password)
        session.headers["User-Agent"] = settings.user_agent
        self.store = Client(f"{settings.matrikkel_url}/StoreServiceWS?WSDL",
                            transport=Transport(session=session, timeout=30, operation_timeout=60),
                            settings=Zeep(strict=False, xml_huge_tree=True))
        # The register's coordinates as they are stored: a postal area has a shape, and asks for that.
        self.context = self._type("MatrikkelContext")(
            klientIdentifikasjon="stiflyt2", systemVersion="1.0", locale="nb_NO", brukOriginaleKoordinater=True,
            koordinatsystemKodeId=self._type("KoordinatsystemKodeId")(value=25833))
        self.unit_id, self.id_list = self._type("MatrikkelenhetId"), self._type("MatrikkelBubbleIdList")

    def _type(self, name: str):
        from zeep.exceptions import LookupError as Missing

        for ns in ("ns0", "ns1", "ns2", "ns3", "ns4", "ns5", "ns6", "ns7", "ns8", "ns9"):
            try:
                return self.store.get_type(f"{ns}:{name}")
            except (Missing, ValueError):      # not in that namespace, or no such prefix
                continue
        raise Unavailable(f"the property register's description has no {name}")

    def objects(self, ids: list) -> list:
        """The objects of the register with these ids; those it no longer has are left out."""
        if not ids:
            return []
        got = self.store.service.getObjectsIgnoreMissing(self.id_list(item=ids), self.context)
        return list(getattr(got, "item", None) or got or [])

    def units(self, ids: list[str]) -> list:
        return self.objects([self.unit_id(value=int(i)) for i in ids])


_register: Register | None = None
_lock = threading.Lock()
_owners: dict[str, tuple[float, list[dict]]] = {}       # unit id: (when asked, its owners)
_known: dict[tuple[str, int], object] = {}              # postal areas and kinds of ownership, which hardly change


def register() -> Register:
    global _register
    if _register is None:
        try:
            _register = Register()
        except Unavailable:
            raise
        except Exception as e:                  # the description could not be fetched: said, and tried again next time
            raise Unavailable(f"the property register did not answer ({type(e).__name__})") from e
    return _register


def _value(x) -> object:
    return getattr(x, "value", x)


def _owner(relation, who, postal: dict, kinds: dict) -> dict:
    """One owner, as it is passed on: never a person's identity number or home address."""
    kind = kinds.get(_value(relation.eierforholdKodeId))
    code, said = (kind.kodeverdi, next((e.value for e in kind.navn.entry if e.key in (None, "no_NO")), None)) if kind else (None, None)
    share = relation.andel
    address = who.postadresse
    lines = [line for line in (getattr(address, f"adresselinje{i}", None) for i in (1, 2, 3)) if line] if address else []
    area = postal.get(_value(address.postnummeromradeId)) if address and address.postnummeromradeId else None
    if area is not None:
        lines.append(f"{area.kretsnummer} {area.kretsnavn}".strip())
    since = relation.datoFra
    organisation = (who._xsd_type.name if hasattr(who, "_xsd_type") else type(who).__name__).startswith("Juridisk")
    return {"name": who.navn, "kind": "organisation" if organisation else "person",
            "org_number": str(who.nummer) if organisation and getattr(who, "nummer", None) else None,
            "address": ", ".join(lines) or None,
            "share": f"{share.teller}/{share.nevner}" if share and share.teller and share.nevner else None,
            "role": _ROLES.get(code, said), "said": said,
            "registered": "IkkeTinglyst" not in relation._xsd_type.name if hasattr(relation, "_xsd_type") else None,
            "since": str(getattr(since, "date", since)) if since else None}


def owners(unit_ids: list[str]) -> dict[str, list[dict]]:
    """Who owns each of these units now, as the register says: asked when not asked within the hour."""
    now, out = time.time(), {}
    wanted = [i for i in dict.fromkeys(unit_ids) if i]
    asked = [i for i in wanted if not (i in _owners and now - _owners[i][0] < KEPT_S)]
    if asked:
        try:
            with _lock:
                r = register()
                units = r.units(asked)
                relations = {str(_value(u.id)): [e for e in (getattr(u.eierforhold, "item", None) or []) if not e.sluttdato]
                             for u in units}
                people = {_value(p.id): p for p in r.objects([e.eierId for rs in relations.values() for e in rs])}
                new = {}
                for rs in relations.values():
                    for e in rs:
                        new[("kind", _value(e.eierforholdKodeId))] = e.eierforholdKodeId
                        address = getattr(people.get(_value(e.eierId)), "postadresse", None)
                        if address is not None and address.postnummeromradeId is not None:
                            new[("postal", _value(address.postnummeromradeId))] = address.postnummeromradeId
                missing = [k for k in new if k not in _known]
                for got in r.objects([new[k] for k in missing]):
                    kind = "kind" if hasattr(got, "kodeverdi") else "postal"
                    _known[(kind, _value(got.id))] = got
        except Unavailable:
            raise
        except Exception as e:
            raise Unavailable(f"the property register did not answer: {str(e)[:200]}") from e
        kinds = {k[1]: v for k, v in _known.items() if k[0] == "kind"}
        postal = {k[1]: v for k, v in _known.items() if k[0] == "postal"}
        for i in asked:
            found = [_owner(e, people[_value(e.eierId)], postal, kinds)
                     for e in relations.get(i, []) if _value(e.eierId) in people]
            _owners[i] = (now, sorted(found, key=lambda o: (o["role"] != "owner", o["name"] or "")))
    for i in wanted:
        out[i] = _owners[i][1] if i in _owners else []
    return out


def asked_by(conn, who, what: str, unit_ids: list[str]) -> None:
    """Keep that the register was asked, by whom and of what: not what it said."""
    conn.execute("INSERT INTO ops.owner_lookup (who, email, through, what, units) VALUES (%s, %s, %s, %s, %s)",
                 (str(who), getattr(who, "email", None), getattr(who, "through", None), what[:200],
                  [i for i in dict.fromkeys(unit_ids) if i]))


def with_owners(found: dict, units: list[dict]) -> dict:
    """The owners put on each unit; where the register cannot be asked, why, and the units as they were."""
    try:
        said = owners([u["id"] for u in units if u.get("id")])
    except Unavailable as e:
        return {**found, "owners_unavailable": str(e)}
    for u in units:
        u["owners"] = said.get(u["id"], []) if u.get("id") else []
    return found


# --- as a list to take along ----------------------------------------------------------------------------------------

def workbook(found: dict) -> bytes:
    """The properties along a route and their owners, as an Excel list: one row for each owner of each property."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    route = found["route"]
    sheet.title = route["code"][:31]
    sheet.append([f"Landowners along {route['code']} {route.get('name') or ''}".strip()])
    sheet.append([(f"From Kartverket's property map and property register, {datetime.now(UTC):%-d %b %Y}. "
                   "Personal data: for the work on the route, not to be passed on.")])
    sheet.append([])
    head = ["Property", "Municipality", "Name of the property", "Along the route, km", "Metres", "Owner", "Share",
            "Role", "Postal address", "Organisation number"]
    sheet.append(head)
    for cell in sheet[4]:
        cell.font = Font(bold=True)
    sheet["A1"].font = Font(bold=True, size=13)
    for p in found["properties"]:
        spans = ", ".join(f"{s['from_m'] / 1000:.1f}–{s['to_m'] / 1000:.1f}".replace(".", ",") for s in p["spans"])
        start = [p["label"], p["municipality"], p["name"], spans, p["metres"]]
        for o in p.get("owners") or [None]:
            sheet.append(start + ([o["name"], o["share"], o["said"] or o["role"], o["address"], o["org_number"]]
                                  if o else ["", "", "", "", ""]))
    if found.get("owners_unavailable"):
        sheet.append([])
        sheet.append([f"Owners not given: {found['owners_unavailable']}"])
    for i, width in enumerate((16, 14, 24, 18, 9, 32, 7, 16, 40, 14), start=1):
        sheet.column_dimensions[get_column_letter(i)].width = width
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
