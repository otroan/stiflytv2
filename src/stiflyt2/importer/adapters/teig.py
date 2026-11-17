"""Matrikkelen Eiendomskart Teig (Kartverket): property parcels, per municipality.

CC BY 4.0. The nationwide file is 5 GB, so parcels are fetched one municipality
at a time and the scope is `kommune:<number>`. Verified 2026-11-14 on Luster:
- A parcel in several pieces is delivered as one row per piece, sharing teigid
  and every attribute. The rows are merged into one feature.
- matrikkelenhet rows (the property units: gnr/bnr/fnr) refer to their parcel
  by teig_fk = teigid. They are nested into the parcel, as are area remarks, so
  a parcel that changes owner unit is one change.
- The boundary lines and points in the delivery are not stored. They remain in
  the kept original file.
- datauttaksdato changes on every delivery and is excluded from the hash.
"""
from __future__ import annotations

import re
from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core, fgdb, geonorge

KEY = "teig"
UUID = "74340c24-1c8a-4454-b813-bfe498e80f16"
TRANSFORM = "teig-1"
S = core.STAGING

LAYERS = [
    ("teig", "teig_teig", True),
    ("matrikkelenhet", "teig_matrikkelenhet", False),
    ("teig_arealmerknad", "teig_arealmerknad", False),
]

_DROP = "- 'ogc_fid' - 'geom' - 'datauttaksdato' - 'shape_length' - 'shape_area'"

PREPARE = [
    f"CREATE INDEX ON {S}.teig_matrikkelenhet (teig_fk)",
    f"DROP TABLE IF EXISTS {S}.teig_flate",
    f"""CREATE UNLOGGED TABLE {S}.teig_flate AS
        WITH enhet AS (
            SELECT teig_fk, jsonb_agg(DISTINCT to_jsonb(m) - 'ogc_fid' - 'teig_fk' - 'objtype') AS j
            FROM {S}.teig_matrikkelenhet m GROUP BY 1),
        merknad AS (
            SELECT teig_fk, jsonb_agg(DISTINCT arealmerknadkode_fk) AS j
            FROM {S}.teig_arealmerknad GROUP BY 1)
        SELECT t.teigid, t.geom,
               (to_jsonb(t) {_DROP})
               || jsonb_build_object('matrikkelenheter', enhet.j, 'arealmerknader', merknad.j)
               AS props
        FROM {S}.teig_teig t
        LEFT JOIN enhet ON enhet.teig_fk = t.teigid
        LEFT JOIN merknad ON merknad.teig_fk = t.teigid""",
]

SPECS = [core.LayerSpec("teig", "teig_flate", "s.teigid", "s.props", collect=True)]


def run(kommune: str, file: Path | None = None) -> core.Result:
    """Fetch (or take a local zip of) the parcels of one municipality, by its 4-digit number."""
    if not re.fullmatch(r"\d{4}", kommune):
        raise core.ImportRejected(f"not a municipality number: {kommune!r}")
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "Matrikkelen - Eiendomskart Teig", "CC BY 4.0", "© Kartverket", "allowed",
            f"https://kartkatalog.geonorge.no/metadata/{UUID}",
        )
    area = geonorge.area(UUID, kommune) if file is None else geonorge.NATIONWIDE
    return fgdb.run(key=KEY, uuid=UUID, scope=f"kommune:{kommune}", transform=TRANSFORM,
                    layers=LAYERS, specs=SPECS, area=area, prepare_sql=PREPARE,
                    derived=["teig_flate"], file=file)
