# Sources — what was verified

Checked 2026-11-14 against live endpoints and sample downloads. Anything not
listed as verified is an assumption.

## Geonorge download API (verified)

`https://nedlasting.geonorge.no/api`. Anonymous for open datasets.

- `GET /capabilities/{uuid}`, `/codelists/area/{uuid}`, `/codelists/format/{uuid}`
- `POST /order` with `{email:"", orderLines:[{metadataUuid, areas, projections, formats}]}`
  returns files with `downloadUrl`; the URL answers `302` to a static file
  that carries `Last-Modified` and `ETag`.

v1 used ATOM feeds with a token in the URL. v2 uses this API.

## Turrutebasen (verified)

- UUID `d1422d17-6d95-4ef1-96ab-8af31744dd63`. CC BY 4.0. Updated weekly.
- Formats: FGDB, GML, PostGIS, SOSI, GPX. Areas: nationwide, county, municipality.
- Nationwide FGDB in EPSG:25833 is 58 MB.
- Layers: `fotrute_senterlinje` (137,951), `fotruteinfo_tabell` (167,914),
  `ruteinfopunkt_posisjon` (9,754), plus ski, bicycle and other routes.
- **`lokalid` is a UUID, unique per segment.** `fotruteinfo.fotrute_fk` is
  that `lokalid`; every info row resolves and every segment has info.
  v1's integer `fotrute_fk` was an artefact of the PostGIS dump format.
- `rutefolger` says what a segment follows: `ST` path (80,280 segments), `BV`
  road, `TR` and `TV` tractor road, `SB` forest road, `GS` foot and cycle
  path, and `VV` water (32 segments: boat routes). 8,201 segments have none.
- `kopidato` changes on every delivery and must be left out of content
  hashes. `versjonid`/`oppdateringsdato` are real change markers.
- Whether `lokalid` survives an upstream edit is **not verified**; it needs
  two deliveries a week apart.

## Stedsnavn (verified)

- UUID `30caed2f-454e-44be-b5cc-26bb5c0110ca`. Open. FGDB or GML.
- Nationwide FGDB in EPSG:25833 is 108 MB.
- Geometry in `sted_posisjon` (719,401), `sted_multipunkt`, `sted_senterlinje`,
  `sted_omrade`: 1,059,162 places in all, each with a unique `stedsnummer`.
- `stedsnavn.sted_fk` is the place's `stedsnummer`; every name resolves to a
  place and every place has a name. Spellings join through
  `skrivemate.stedsnavn_fk` = `stedsnavn.objid`.
- `datauttaksdato` changes per delivery; exclude from hashes.

## FKB-TraktorvegSti (verified: restricted)

- UUID `cc3a2d98-52ac-4699-9947-ed0625903de4`. Norge digitalt licence. The
  download API requires role `nd.filnedlasting`, `nd.landbrukspart` or
  `nd.filnedlasting.fkb`. Free for Norge digitalt members; others buy it.
- Formats FGDB, GML, SOSI; polygon and area selection supported.
- Whether DNT has Norge digitalt access is **not verified**.

### Access for DNT (checked 2026-11-14)

- Geonorge's organisation register lists DNT as not a Norge digitalt party
  (https://register.geonorge.no/organisasjoner/den-norske-turistforening), and
  the 2026 party criteria have no category for voluntary organisations.
- The Matrikkel API account v1 uses is a different access scheme (property
  data, granted on application). It does not carry the Geonorge download role.
  Not tested by logging in.
- Without party status FKB is bought from the municipality or a reseller. A
  purchase gives a right of use for that version; publishing geometry derived
  from it needs a separate publishing right. Prices are not published.
- Geosynkronisering (change log subscription) exists for FKB, for parties and
  by agreement for others. It is pull-based and needs a subscriber client.

## Open alternative: Traktorveg og Skogsbilveg WFS (verified)

- `https://wms.geonorge.no/skwms1/wms.traktorveg_skogsbilveger`, layer
  `ms:traktorveg_sti`. Catalogued as open data, no login. The capabilities
  document also says "Fees: Norge Digitalt", so the reuse terms are unclear.
- Derived from FKB. A 70 x 60 km box around Breheimen returned 9,436 paths
  and tractor roads in one request, uncapped.
- **Geometry and `typeveg` only.** No `lokalid`, no capture date, no accuracy
  or method. `gml:id` is a server row number. Identity must be geometry hash.
- Every response carries a `timeStamp` attribute, so the same data never has
  the same bytes twice. The adapter leaves it out of the checksum.
- When GDAL reads a response it fetches the layer schema from the server
  (`DescribeFeatureType`). Without the schema it guesses: attributes that are
  always empty get no column and `kommunenummer` becomes a number. The adapter
  therefore fetches the schema itself, keeps it with the delivery and loads
  from that copy only.

- 2,677,517 paths and tractor roads nationwide (hit count, 2026-11-14). A
  response holds at most 10,000 features; the adapter splits a denser tile.
- **The terms contradict each other.** The catalogue entry for the WMS says
  CC BY 4.0. The entry for the WFS says open data but lists "Norge Digitalt"
  as a use limitation, and the service itself states "Fees: Norge Digitalt"
  and "Copyright Statens kartverk". It was made for a NIBIO web client.
  Kartverket (Landdivisjonen) owns it.
- **Decision 2026-11-14:** the route responsible has verified that the licence allows this use,
  so the WFS is the path source for snapping. It is loaded per work area, when
  an area is to be snapped, not harvested nationwide. A loaded box is a local
  snapshot like any other source: snapping reads the stored copy, never the
  live service, and the nightly refresh re-fetches each loaded box.
- The licence is CC BY 4.0 and geometry derived from the paths may be
  published (confirmed by the route responsible 2026-11-14). The source is registered with
  attribution "© Kartverket" and export policy `allowed`.

Usable as the FKB geometry candidate now, with weaker provenance and diffing
than the licensed product.

Fully open alternatives are weaker: N50 Kartdata (CC BY 4.0) has paths and
tractor roads but is generalised, with a stated accuracy of 2 to 50 m.

## NVDB (verified: not a substitute yet)

- FKB-TraktorvegSti is being moved into NVDB. Kartverket's plan has the first
  municipalities from 1 April 2026 and about half during 2026.
- NVDB road network API v4 does return `typeveg=sti` and `traktorveg`, with
  stable IDs, capture date, method and accuracy.
- Coverage in the candidate pilot areas today (segments, sti / traktorveg):
  Luster 0/0, Skjåk 0/8, Stryn 0/0, Lom 0/5, Engerdal 0/0, Røros 0/6,
  Dovre 0/0, Sel 0/0, Folldal 0/0, Nord-Fron 2/0.
- The Geonorge "NVDB Vegnett Pluss" file for Luster has no paths or tractor
  roads at all.

NVDB will be the long-term open source for this geometry. An adapter is worth
adding when a pilot municipality has migrated.

## Roads: NVDB Vegnett Pluss (verified and loaded 2026-11-14)

- UUID `97e6a869-8dd4-4379-bf39-f7d7dbf94863`. Kartverket, open data,
  CC BY 4.0 according to the catalogue. Every drivable road longer than 50 m
  or part of a network, and foot and cycle ways.
- Geonorge download API, per municipality (357), county or nationwide; GML or
  SOSI; EPSG:5973 only (UTM 33 with heights). About 3 MB per municipality.
- One GML file per municipality with eight layers. Only `Veglenke` (road
  links) is stored. `nvdbId` is unique per link; `lokalId` names the link
  sequence and is shared by several links.
- The file has no schema beside it, so GDAL works columns and their types out
  from the data. The adapter loads everything as text and leaves empty
  attributes out, so a link reads the same whichever file it comes in.
- `detaljnivå` is `VT`/`VTKB` for the road as a whole and `KB` for one
  carriageway. The index table `road` leaves out the `KB` links.
- The root element has a `timeStamp`, so an unchanged municipality still
  gives a new snapshot, with no changes in it.
- Why it is loaded: Turrutebasen marks 23,544 segments, 7,297 km, as
  following a car road (`rutefolger = BV`), and the open WFS has paths and
  tractor roads only. Roads and paths are one network for snapping: lines
  join at their ends, and where one ends on the middle of another.
- Loaded for eleven municipalities (the eight with parcels, and Vågå, Vang
  and Øystre Slidre): 59,984 links. Of the 93 owned stretches in Jotunheimen
  that Turrutebasen says follow a road or tractor road, 91 have a mapped way
  between their ends (41.7 km).
- Not seen yet: two different deliveries of the same municipality, so how
  stable `nvdbId` is over time is not known.

## OpenStreetMap (loaded 2026-11-15, to look at)

- Ways with a `highway` tag, every tag, cut per named bounding box (by its
  extent in degrees; a way over the edge comes whole) from Geofabrik's extract
  of Norway (`https://download.geofabrik.de/europe/norway/norway-latest.osm.pbf`,
  1.4 GB, made every night), read with GDAL's OSM driver (`osmconf.ini` beside
  the adapter keeps every tag and closed ways as lines). The download is kept
  while its checksum (`.md5` beside it) is the one served, so the boxes of a
  night share it. Since 2026-11-23; before, from the Overpass API in 25 km
  tiles, which says its public endpoint is for people at a browser, and was too
  often at capacity at night.
- **Open Database License (ODbL 1.0).** Showing it and comparing with it is
  free with the attribution "© OpenStreetMap contributors". Taking its lines
  over makes what is ours a derived database under the same licence, which
  Turrutebasen (CC BY 4.0) cannot take in. So it is registered with export
  policy `unresolved`, it is a map layer that is off until switched on, and it
  is **not** among the lines that snapping and redrawing follow. Whether it
  ever should be is for the route responsible to decide.
- A way is its OSM id and its tags. No editor names, versions or changesets
  are fetched.
- The checksum is over the ways of a box, not the file, so unchanged ways give
  no new snapshot; coordinates are kept to 7 decimals, as OpenStreetMap stores
  them. The snapshot's time is when Geofabrik made the file.
- The nightly refresh checks the boxes when the last check of one of them is
  seven days old (`EVERY_DAYS` in `importer/refresh.py`), and then every box
  together, from one download.
- Loaded for the boxes `jot` (11,042 ways) and `bre` (7,085): 16,329 in the
  index, of them 4,305 paths (3,338 km) and 1,518 tracks.

## Kulturminner (verified)

- UUID `c72906a0-2bc2-41d7-bea2-c92d368e3c49`. Riksantikvaren, NLOD.
  Nationwide FGDB in EPSG:25833 is 253 MB (1.2 GB unpacked), regenerated daily.
- Layers `lokalitet`, `enkeltminne` and `sikringssone`, all polygons, each row
  with a `lokalid`.
- **`lokalid` is not unique per row.** An object made of several separate
  polygons comes as one row per polygon (up to 389), with identical
  attributes. The adapter merges them.
- **Some dates are not valid** (`0000/12/30`). Date columns are loaded as text.
- `datauttaksdato` changes per delivery; excluded from hashes.

## Matrikkelen Eiendomskart Teig (verified)

- UUID `74340c24-1c8a-4454-b813-bfe498e80f16`. Kartverket, CC BY 4.0.
- The nationwide FGDB is 5.2 GB zipped, too large to keep beside v1 on the
  current server. One municipality is small (Luster 16 MB, 9,562 parcels), so
  the adapter works per municipality.
- `teig` holds the parcels; `matrikkelenhet` (gnr/bnr/fnr) refers to them by
  `teig_fk` = `teigid`. A parcel in several pieces comes as one row per piece.
- Owner names are not in this dataset. v1 gets them from the Matrikkel API,
  which needs its own account.

## How to learn that a source has changed (checked 2026-11-14)

Nothing push-based is available to DNT. Every mechanism is a poll.

| Source | File regenerated | Signal |
|---|---|---|
| Turrutebasen | Weekly by its metadata; file dates suggest on change | `Last-Modified` on the file |
| Stedsnavn | Daily | `Last-Modified` |
| Kulturminner | Daily, about 03:00 UTC | `Last-Modified` |
| Eiendomskart Teig | Weekly, Sundays, only if changed | `Last-Modified` |
| WFS paths | Unknown | None; the data must be fetched and compared |

`stiflyt2 refresh` asks for each file's date and size and downloads only when
they differ from the active snapshot. Geonorge's ATOM feeds carry the same
date. NVDB has open change endpoints, relevant when paths have moved there.

## Height (verified)

**Decision 2026-11-14:** height is not imported as a source. As in v1, a
route's profile is sampled from Kartverket's point API (a REST service, 1 m
model) once the route's geometry has been repaired, and the result is stored
with the route. It is built together with route repair, not with the importer.

- Point API `https://ws.geonorge.no/hoydedata/v1/punkt`: DTM1 (1 m), max 50
  points per call. v1 uses it with a per-route cache.
- DTM10 tiles on Geonorge (UUID `dddbb667-1303-4ac5-8640-7ec04c0e3918`,
  CC BY 4.0): 254 tiles of 50 x 50 km, about 56 MB each, GeoTIFF. Marked
  historic, based on 2013 data, accuracy 2 to 3 m.
- DTM1 as 15 km tiles through an ATOM feed; current, but large.

## Sources

- https://kartkatalog.geonorge.no/metadata/fkb-traktorvegsti/cc3a2d98-52ac-4699-9947-ed0625903de4
- https://www.kartverket.no/geodataarbeid/forvaltning-drift-og-vedlikehold/elveg-2.0-blir-til-nvdb-vegnett-pluss
- https://kartverket.no/globalassets/om-kartverket/fylkeskartkontorene/kartverket-oslo/referat-nvdb-brukerforum-ooab-12032026.pdf
- https://www.kartverket.no/globalassets/geodataarbeid/geovekst/geovekst-fkb-kvalitetsplan-2025_2024-1205.pdf
