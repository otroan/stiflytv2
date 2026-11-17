"""Kulturminner (Riksantikvaren): heritage sites, single monuments and protection zones.

Nationwide FGDB, NLOD, regenerated daily. Verified 2026-11-14:
- A site, monument or zone made up of several separate polygons is delivered as
  one row per polygon, sharing lokalid and every attribute. The rows are merged
  into one feature.
- Some dates are not valid dates (year 0), so date columns are loaded as text,
  exactly as delivered.
- datauttaksdato changes on every delivery and is excluded from the hash.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core, fgdb

KEY = "kulturminner"
UUID = "c72906a0-2bc2-41d7-bea2-c92d368e3c49"
TRANSFORM = "kulturminner-1"
SCOPE = "NO"

_DROP = "- 'ogc_fid' - 'geom' - 'datauttaksdato' - 'shape_length' - 'shape_area'"

LAYERS = [
    ("lokalitet", "kul_lokalitet", True),
    ("enkeltminne", "kul_enkeltminne", True),
    ("sikringssone", "kul_sikringssone", True),
]

SPECS = [
    core.LayerSpec(layer, table, "s.lokalid", f"to_jsonb(s) {_DROP}", min_rows=min_rows,
                   collect=True)
    for (layer, table, _), min_rows in zip(LAYERS, (150_000, 250_000, 70_000), strict=True)
]


def run(file: Path | None = None) -> core.Result:
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "Kulturminner (Askeladden)", "NLOD", "© Riksantikvaren", "allowed",
            f"https://kartkatalog.geonorge.no/metadata/{UUID}",
        )
    return fgdb.run(key=KEY, uuid=UUID, scope=SCOPE, transform=TRANSFORM, layers=LAYERS,
                    specs=SPECS, load_options=["-fieldTypeToString", "DateTime"], file=file)
