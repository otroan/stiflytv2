"""NVDB Vegnett Pluss (Kartverket): the road network, per municipality.

CC BY 4.0. Every drivable road longer than 50 m or part of a network, and foot
and cycle ways. Routes that follow a road are snapped to it. Verified
2026-11-14 on Lom:
- One GML file per municipality, in EPSG:5973 (UTM 33 with heights). Heights
  are dropped and the lines stored in EPSG:25833.
- Only the layer Veglenke is stored: the road links. Speed limits, road
  classes and the rest remain in the kept original file.
- `nvdbId` is unique per link (3,138 of 3,138). `lokalId` is not: it names the
  link sequence, which several links share.
- The file has no schema beside it, so GDAL works the columns out from the
  data: an attribute that is empty everywhere gets no column, and a column's
  type follows its values. Everything is therefore loaded as text and empty
  values are left out of the stored properties, so the same link reads the
  same whichever file it comes in.
- `detaljnivå` says what a link stands for: the road as a whole (VT, VTKB) or
  one carriageway (KB). The index leaves out the carriageway-only links.
- The root element carries a timeStamp, so the same data never has the same
  bytes twice: an unchanged municipality gives a new snapshot with no changes.
"""
from __future__ import annotations

import re
from pathlib import Path

import psycopg

from stiflyt2.config import settings
from stiflyt2.importer import core, fgdb, geonorge

KEY = "vegnett"
UUID = "97e6a869-8dd4-4379-bf39-f7d7dbf94863"
TRANSFORM = "vegnett-1"
PROJECTION = "5973"

LAYERS = [("Veglenke", "vegnett_veglenke", True)]
LOAD = ["-oo", "DOWNLOAD_SCHEMA=NO", "-fieldTypeToString", "All", "-dim", "XY"]
SPECS = [core.LayerSpec("veglenke", "vegnett_veglenke", "s.nvdbid",
                        "to_jsonb(s) - 'ogc_fid' - 'geom' - 'gml_id'")]


def run(kommune: str, file: Path | None = None) -> core.Result:
    """Fetch (or take a local zip of) the road network of one municipality, by its 4-digit number."""
    if not re.fullmatch(r"\d{4}", kommune):
        raise core.ImportRejected(f"not a municipality number: {kommune!r}")
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(
            conn, KEY, "NVDB Vegnett Pluss", "CC BY 4.0", "© Kartverket", "allowed",
            f"https://kartkatalog.geonorge.no/metadata/{UUID}",
        )
    area = geonorge.area(UUID, kommune) if file is None else geonorge.NATIONWIDE
    return fgdb.run(key=KEY, uuid=UUID, scope=f"kommune:{kommune}", transform=TRANSFORM,
                    layers=LAYERS, specs=SPECS, area=area, load_options=LOAD, file=file,
                    fmt="GML", projection=PROJECTION, pattern="*.gml")
