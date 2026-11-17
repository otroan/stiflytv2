"""Stedsnavn (SSR, Kartverket). Nationwide FGDB, open data.

Verified 2026-11-14: places are spread over four geometry layers and share a
unique stedsnummer; stedsnavn.sted_fk is that stedsnummer and
skrivemate.stedsnavn_fk is stedsnavn.objid. One feature version is stored per
place, with its names and spellings nested, so a renamed place is one change.
datauttaksdato changes on every delivery and is excluded from the hash.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core, fgdb

KEY = "stedsnavn"
UUID = "30caed2f-454e-44be-b5cc-26bb5c0110ca"
TRANSFORM = "stedsnavn-1"
SCOPE = "NO"
S = core.STAGING

GEOM_LAYERS = ["sted_posisjon", "sted_multipunkt", "sted_senterlinje", "sted_omrade"]
TABLE_LAYERS = ["stedsnavn", "skrivemate", "annen_skrivemate", "kommune"]

_DROP = ("- 'ogc_fid' - 'geom' - 'datauttaksdato' - 'objid' - 'shape_length' - 'shape_area' "
         "- 'stedsnummer'")


def _spell(table: str) -> str:
    return f"""SELECT stedsnavn_fk, jsonb_agg(jsonb_build_object(
                   'navn', komplettskrivemate, 'status', skrivematestatus,
                   'nr', skrivematenummer, 'statusdato', statusdato)
                   ORDER BY skrivematenummer, komplettskrivemate) AS j
               FROM {S}.{table} GROUP BY 1"""


PREPARE = [
    f"CREATE INDEX ON {S}.ssr_stedsnavn (sted_fk)",
    f"CREATE INDEX ON {S}.ssr_kommune (sted_fk)",
    f"DROP TABLE IF EXISTS {S}.ssr_sted",
    f"""CREATE UNLOGGED TABLE {S}.ssr_sted AS
        WITH sted AS (
            {" UNION ALL ".join(
                f"SELECT stedsnummer, to_jsonb(s) {_DROP} AS p, geom FROM {S}.ssr_{l} s"
                for l in GEOM_LAYERS)}),
        skr AS ({_spell("ssr_skrivemate")}),
        ann AS ({_spell("ssr_annen_skrivemate")}),
        navn AS (
            SELECT n.sted_fk, jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
                       'nr', n.stedsnavnnummer, 'status', n.navnestatus, 'sprak', n.sprak,
                       'sakstatus', n.navnesakstatus, 'offentlig', n.offentligbruk,
                       'skrivemater', skr.j, 'andre_skrivemater', ann.j))
                       ORDER BY n.stedsnavnnummer, n.objid) AS j
            FROM {S}.ssr_stedsnavn n
            LEFT JOIN skr ON skr.stedsnavn_fk = n.objid
            LEFT JOIN ann ON ann.stedsnavn_fk = n.objid
            GROUP BY 1),
        kom AS (
            SELECT sted_fk, jsonb_agg(DISTINCT jsonb_build_object(
                       'nr', kommunenummer, 'navn', kommunenavn, 'fylke', fylkesnavn)) AS j
            FROM {S}.ssr_kommune GROUP BY 1)
        SELECT sted.stedsnummer, sted.geom,
               sted.p || jsonb_build_object('navn', navn.j, 'kommuner', kom.j) AS props
        FROM sted
        LEFT JOIN navn ON navn.sted_fk = sted.stedsnummer
        LEFT JOIN kom ON kom.sted_fk = sted.stedsnummer""",
]

SPECS = [core.LayerSpec("sted", "ssr_sted", "s.stedsnummer", "s.props", min_rows=500_000)]


def run(file: Path | None = None) -> core.Result:
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "Stedsnavn (SSR)", "CC BY 4.0", "© Kartverket", "allowed",
            f"https://kartkatalog.geonorge.no/metadata/{UUID}",
        )
    layers = ([(layer, f"ssr_{layer}", True) for layer in GEOM_LAYERS]
              + [(layer, f"ssr_{layer}", False) for layer in TABLE_LAYERS])
    return fgdb.run(key=KEY, uuid=UUID, scope=SCOPE, transform=TRANSFORM, layers=layers,
                    specs=SPECS, prepare_sql=PREPARE, derived=["ssr_sted"], file=file)
