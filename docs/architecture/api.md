# Proposed REST contract (phase 0, for review)

Not implemented. Base path `/api/v1`. This lists resources and rules; the
OpenAPI document generated from the FastAPI code becomes the contract once
phase 1A exists, and the TypeScript client is generated from it.

## Conventions

- JSON; geometry as GeoJSON in EPSG:4326. Pydantic DTOs, never ORM objects.
- IDs are UUIDs and stable for the life of the object.
- **Concurrency:** every mutable resource returns `ETag`; `PATCH`, `PUT` and
  action endpoints require `If-Match` and return `412` when stale.
- **Changesets:** every graph, route or metadata mutation requires the header
  `X-Changeset-Id` naming an open changeset owned by the caller.
- **Idempotency:** job-creating `POST`s accept `Idempotency-Key`.
- **Lists:** cursor pagination (`limit`, `cursor`); map endpoints require
  `bbox`.
- **Errors:** `{"code", "message", "details"}` with stable codes, e.g.
  `stale_version`, `changeset_closed`, `topology_blocked`, `not_in_area`,
  `publish_policy_failed`, `licence_unresolved`.
- **Roles:** `viewer` reads; `editor` edits drafts and runs imports;
  `publisher` publishes and exports; `admin` manages users and areas.

## Areas

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/areas` | List, create |
| GET, PATCH | `/areas/{id}` | Prefixes, DNT filter, AOI, buffer |
| GET | `/areas/{id}/route-candidates` | Included and excluded candidates with reason |
| POST | `/areas/{id}/route-candidates/{code}/include` | Manual include |
| POST | `/areas/{id}/route-candidates/{code}/exclude` | Manual exclude |

## Sources and import

| Method | Path | Purpose |
|---|---|---|
| GET | `/sources` | Sources with licence and export policy |
| GET | `/sources/{key}/capabilities` | Fetchable or upload-only, formats |
| POST | `/areas/{id}/imports/preview` | What an import would fetch and change |
| POST | `/areas/{id}/imports` | Start an import job |
| POST | `/areas/{id}/uploads` | Upload GPX/GeoJSON (multipart) |
| GET | `/areas/{id}/imports` | Import history |
| GET | `/import-jobs/{id}` | State, progress, error |
| POST | `/import-jobs/{id}/cancel` | Request cancel |
| GET | `/imports/{id}/diff` | Created/updated/missing/split/merged/ambiguous |
| GET | `/areas/{id}/source-features?bbox=&source=` | Active source geometry for the map |

## Changesets (ADR-001)

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/areas/{id}/changesets` | List, open a changeset |
| GET, PATCH | `/changesets/{id}` | Title, comment |
| POST | `/changesets/{id}/close` | Close |
| GET | `/changesets/{id}/events` | What it changed |
| POST | `/changesets/{id}/revert` | Undo: new revisions restoring the prior state |
| GET | `/changesets/{id}/export?target=&format=geojson` | Neutral delta against a source's active snapshot (phase 1D) |

## Graph

| Method | Path | Purpose |
|---|---|---|
| GET | `/areas/{id}/graph?bbox=` | Owned nodes and edges |
| POST | `/areas/{id}/edges` | Draw a new edge |
| POST | `/areas/{id}/edges/adopt` | Materialise source feature(s) as owned edges |
| GET, PATCH | `/edges/{id}` | Geometry, kind, marked, uncertainty |
| GET | `/edges/{id}/revisions` | History |
| POST | `/edges/{id}/split` | Split at a point; rewrites affected draft legs |
| POST | `/edges/{id}/retire` | Retire; blocked if a draft route uses it |
| POST | `/graph/connect` | Join two endpoints at one node |
| POST | `/graph/disconnect` | Separate an edge from a node |
| GET | `/areas/{id}/snap-suggestions?bbox=&tolerance_m=` | Near-misses to review |
| GET, PATCH | `/nodes/{id}` | Position, name |

## Route engine (ADR-001)

| Method | Path | Purpose |
|---|---|---|
| POST | `/areas/{id}/paths` | A to B. Body: from, to, optional via points, preference, alternatives (max 3). Returns ordered owned edges and source candidates with length per source |
| POST | `/areas/{id}/route-candidates/{code}/reconstruct` | Proposed path for a Turrutebasen candidate, plus what did not fit |

Both are read-only. Nothing is created until the result is sent to
`PUT /routes/{id}/legs`.

## Routes

| Method | Path | Purpose |
|---|---|---|
| GET, POST | `/areas/{id}/routes` | List, create (blank or from a candidate) |
| GET, PATCH | `/routes/{id}` | Draft and published summary |
| PUT | `/routes/{id}/legs` | Replace the ordered legs; adopts any source candidates included |
| GET, PUT | `/routes/{id}/metadata` | Claims per predicate; add a claim, select one, set status |
| POST | `/routes/{id}/validate` | Run validators; returns findings |
| POST | `/routes/{id}/publish` | Freeze a published copy; fails on policy |
| GET | `/routes/{id}/revisions` | Drafts and published versions |
| GET | `/routes/{id}/export?format=geojson\|gpx&revision=` | Export |

## Quality and alerts

| Method | Path | Purpose |
|---|---|---|
| GET | `/areas/{id}/findings?bbox=&code=` | Validation findings |
| POST | `/areas/{id}/validate` | Validate the whole area (job) |
| GET | `/areas/{id}/source-alerts` | Source moved/removed/split for owned edges |
| POST | `/source-alerts/{id}/accept` or `/dismiss` | Decide |

## Junctions (ADR-001, phase 1D)

| Method | Path | Purpose |
|---|---|---|
| GET | `/areas/{id}/junctions?bbox=` | Named nodes where published routes meet or end |
| GET | `/nodes/{id}/destinations` | Per route through the node: reachable named destinations and distances |

## Places and auth

| Method | Path | Purpose |
|---|---|---|
| GET | `/places/search?q=&area_id=` | Place-name search; never mutates |
| GET | `/auth/login`, `/auth/callback`, `/auth/me`; POST `/auth/logout` | Google sign-in, as v1 |

## Open points in this contract

- Minimum metadata required to publish (see plan-and-risks.md).
- Whether `revert` is allowed on a changeset whose objects were edited again
  afterwards; proposed: blocked with the list of conflicts.
- Vector tiles for large layers are deferred; `bbox` GeoJSON first.
