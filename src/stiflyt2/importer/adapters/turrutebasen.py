"""Turrutebasen (Kartverket). Nationwide FGDB, CC BY 4.0, updated weekly.

Verified 2026-11-14: fotrute.lokalid is a UUID unique per segment and
fotruteinfo.fotrute_fk refers to it. kopidato changes on every delivery and is
excluded so that an unchanged segment keeps its content hash.

The ski trails (skiloype, skiloypeinfo) are taken in as well since 2026-11-26,
the same way: among them are DNT's winter routes, staked anew each winter, with
the summer code and a v (jot11v). The cycle routes and other routes are not.
"""
from __future__ import annotations

from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core, fgdb

KEY = "turrutebasen"
UUID = "d1422d17-6d95-4ef1-96ab-8af31744dd63"
TRANSFORM = "turrutebasen-2"     # 2: with the ski trails
SCOPE = "NO"

# Volatile or derived columns that must not affect the content hash.
_DROP = "- 'ogc_fid' - 'geom' - 'kopidato' - 'shape_length'"

LAYERS = [
    ("fotrute_senterlinje", "tur_fotrute", True),
    ("fotruteinfo_tabell", "tur_fotruteinfo", False),
    ("ruteinfopunkt_posisjon", "tur_ruteinfopunkt", True),
    ("skiloype_senterlinje", "tur_skiloype", True),
    ("skiloypeinfo_tabell", "tur_skiloypeinfo", False),
]

SPECS = [
    core.LayerSpec("fotrute", "tur_fotrute", "s.lokalid", f"to_jsonb(s) {_DROP}",
                   min_rows=50_000),
    # Info rows have no id of their own: one row per (segment, route). Identity
    # is the segment plus a hash of the row, and exact duplicates are collapsed.
    core.LayerSpec(
        "fotruteinfo", "tur_fotruteinfo",
        f"s.fotrute_fk || ':' || md5((to_jsonb(s) {_DROP})::text)",
        f"to_jsonb(s) {_DROP}", geom_sql=None, min_rows=50_000, distinct=True,
    ),
    core.LayerSpec("ruteinfopunkt", "tur_ruteinfopunkt", "s.lokalid", f"to_jsonb(s) {_DROP}",
                   min_rows=1_000),
    core.LayerSpec("skiloype", "tur_skiloype", "s.lokalid", f"to_jsonb(s) {_DROP}", min_rows=5_000),
    core.LayerSpec(
        "skiloypeinfo", "tur_skiloypeinfo",
        f"s.skiloype_fk || ':' || md5((to_jsonb(s) {_DROP})::text)",
        f"to_jsonb(s) {_DROP}", geom_sql=None, min_rows=5_000, distinct=True,
    ),
]


def run(file: Path | None = None) -> core.Result:
    """Fetch (or take a local zip), load and activate a nationwide snapshot."""
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "Turrutebasen", "CC BY 4.0", "© Kartverket", "allowed",
            f"https://kartkatalog.geonorge.no/metadata/{UUID}",
        )
    return fgdb.run(key=KEY, uuid=UUID, scope=SCOPE, transform=TRANSFORM, layers=LAYERS,
                    specs=SPECS, file=file)
