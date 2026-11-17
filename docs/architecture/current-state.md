# Stiflyt v1 — assessment for v2

Inspected 2026-11-13/14 from local checkouts of `stiflyt` and `stiflyt-db`.
The server was not inspected; host facts come from the
service files and scripts in the v1 repos.

## What v1 is

- **stiflyt-db**: downloads Turrutebasen, teig, stedsnavn and kulturminner
  from Geonorge ATOM feeds and loads them into its database. Each
  refresh replaces the Turrutebasen schema in place, then rebuilds nodes,
  links and views with 24 idempotent SQL migrations. Local corrections live in
  an `ops` schema that survives refreshes.
- **stiflyt**: FastAPI backend (83 endpoints, about 19,600 lines) and a
  React/TypeScript/MapLibre app for sign planning, landowner lookup, route
  validation, photos, GPX comparison and exports. Google sign-in with an email
  allow-list.

## Why v2 is a restart

- A route is "every segment tagged with this rutenummer", so loops, spurs and
  gaps in Turrutebasen are loops, spurs and gaps in the route.
- Node, link and segment IDs are reissued on refresh. Corrections keyed on
  them (`ops.route_link_exclusion`, `ops.route_link_bridge`,
  `ops.unmarked_segment`) have no re-keying step.
- The previous import is dropped, so an upstream deletion or split changes a
  route with no pinned copy to compare against.
- Geometry cannot be edited; only overlays on Turrutebasen exist.

## Reuse

| v1 piece | Where | Use in v2 |
|---|---|---|
| Geonorge ATOM discovery and download, zip handling, retry | `stiflyt-db/scripts/download_kartverket.py` | Basis for the Turrutebasen and stedsnavn adapters' `discover`/`fetch` |
| PostGIS dump / FGDB loading | `stiflyt-db/scripts/load_dataset.py` | Reference only; v2 loads into staging then versioned tables, never `DROP SCHEMA` |
| Import diff by geometry hash | `stiflyt-db/scripts/refresh_diff_report.py` | Idea carried into `import_control` diffs |
| Area codes and names | `stiflyt/areas.yaml` | Seed for `core.area` |
| Area include/exclude | `stiflyt/data/area_routes.yaml` | Seed for manual candidate inclusion |
| Errata (remaps, unmarked boat/glacier, exclusions, bridges, metadata overrides) | `stiflyt/data/route_errata.yaml` | Hints for route reconstruction in the pilot area; not imported as data |
| Route validators (connectivity, loops, metadata) | `stiflyt/services/validators/` | Logic ported to run on the owned graph |
| GPX parsing | `stiflyt/services/gpx_tracks.py` | Ported; v2 also keeps the original file |
| Place-name search | `stiflyt/api/routes.py` `/search/places` | Behaviour reference; v2 uses pg_trgm on `source_index` |
| Google OAuth + allow-list | `stiflyt/api/auth.py`, `services/auth_config.py` | Ported, with roles added |
| MapLibre app patterns | `stiflyt/signs_app/src` | Reference for map, panels, upload progress |
| Sign generation | `stiflyt/services/signs.py`, `sign_candidates.py` | Not in phase 1; later reads validated routes from v2 |

## Not reused

- `services/changeset/` and `api/changeset.py`: reads a `base.segment_base`
  table that does not exist; no frontend uses it.
- The `ops` overlay views and the link builder: replaced by the owned graph.
- Raw-SQL numbered migrations: v2 uses Alembic.

## Findings that affect v2

- **No FKB-TraktorvegSti integration exists in v1.** Access, delivery format
  and licence are unverified. The v2 FKB adapter starts as file upload.
- Turrutebasen is fetched nationwide as a PostGIS dump; there is no per-area
  download. v2 fetches nationwide and indexes only the area of interest.
- v1 stores all geometry in EPSG:25833 and Geonorge delivers in it.
- v1 keeps only parsed GPX geometry, not the uploaded file.
