# Schema

`source_raw` and `import_control` are built (migration `0001_source_raw`) and
described here as built, and so is `source_index`. `core` and `history` are
still a proposal for review. Types are abbreviated. Geometry is stored in EPSG:25833
and served as GeoJSON in EPSG:4326.

## Boundary

```text
 Geonorge feeds, uploads (GPX/GeoJSON)
              |
        import worker  ----------------- role: stiflyt2_import
              |
   +----------+-----------+-------------------+
   | source_raw           | import_control    |
   | immutable snapshots  | jobs, diffs,      |
   +----------+-----------+ active snapshot   |
              |           +-------------------+
        source_index  (rebuildable)
              |
        read-only views
 = = = = = = =|= = = = = = = = = = = = = = = = = = = = =  no FKs, no writes
              v                                           across this line
          domain API  ------------------ role: stiflyt2_api
              |
   +----------+-----------+
   | core                 |  history
   | areas, graph, routes,|  append-only events,
   | metadata, changesets |  published routes
   +----------------------+
              |
        /api/v1  ->  frontend
```

## Roles

| Role | Rights |
|---|---|
| `stiflyt2_owner` (no login) | Owns all objects |
| `stiflyt2_migrate` | Member of owner; runs Alembic |
| `stiflyt2_import` | Read/write `source_raw` (insert only on snapshot tables), `source_index`, `import_control`. **No rights at all** in `core` or `history` |
| `stiflyt2_api` | Read/write `core`; insert and select only on `history`; select on `source_index` and `import_control`; insert on `import_control.job` and update of its cancel flag; insert and select on `feedback.entry`, and update of its seen and fixed marks; select, insert and update on `height.profile`. No rights in `source_raw` |

The import worker gets the area's polygon and filters as job parameters, so it
never reads `core`. `history` tables also carry a trigger that rejects update
and delete.

## Migrations

- `bootstrap.sql`: roles, schemas, extensions (`postgis`, `pg_trgm`,
  `pgcrypto`). Run once per database by a superuser, or by the database owner
  with CREATEROLE when the database comes from a template that has PostGIS.
- Alembic track `import`: `source_raw`, `source_index`, `import_control`,
  `feedback`, `access`, `height`, `ops`.
- Alembic track `domain`: `core`, `history`. Not started.

## `source_raw` — immutable (built)

| Table | Columns |
|---|---|
| `source` | key (so far `turrutebasen`, `stedsnavn`, `kulturminner`, `teig`, `fkb_wfs`), name, licence, attribution, export_policy (`allowed`, `blocked`, `unresolved`), homepage |
| `snapshot` | id, source_key, scope, state (`loading`, `complete`, `failed`), complete (bool), fetched_at, source_updated_at, checksum, crs, artifact_path, transform_version, manifest JSONB, counts JSONB, error. Unique on (source_key, scope, checksum, transform_version) among snapshots that did not fail |
| `feature_version` | id, source_key, layer, external_id, content_hash, properties JSONB, geom, first_seen (snapshot). Unique on (source_key, layer, external_id, content_hash) |
| `snapshot_member` | snapshot_id, feature_version_id |

- `scope` is what a snapshot claims to cover: `NO` for a nationwide delivery,
  `kommune:<number>` for one municipality, `bbox:<name>` for a named box.
  Diffs and the active snapshot are per source and scope.
- One object is one feature. Where a source delivers an object as several
  rows (one per polygon, same id and attributes), the rows are merged.
- `layer` separates the feature kinds within one source, for example
  `fotrute`, `fotruteinfo` and `ruteinfopunkt` in Turrutebasen.
- An unchanged feature is stored once and listed in each snapshot that
  contains it. `content_hash` covers the properties and the geometry, with
  columns that change on every delivery (copy and extract dates) left out.
- Triggers enforce immutability for every role, the owner included: a feature
  version can never be updated, deleted or truncated; a snapshot can never be
  deleted and is frozen once `complete`; members can only be added while their
  snapshot is `loading`.
- A failed import leaves a `failed` snapshot row with the error, and nothing
  else.
- Original files stay on disk under `<data_dir>/<source>/<first 16 characters
  of the checksum>/`; the manifest records name, size and origin.

Deliveries are first loaded with `ogr2ogr` into unlogged tables in a separate
schema, `import_staging`, owned by the import role. Those tables are dropped
when the import ends.

## `import_control` (built, except `job`)

| Table | Columns |
|---|---|
| `active_snapshot` | source_key, scope, snapshot_id, activated_at |
| `activation_log` | id, source_key, scope, snapshot_id, previous_id, activated_at |
| `diff` | id, source_key, scope, from_snapshot, to_snapshot, counts JSONB, created_at |
| `diff_item` | diff_id, layer, external_id, kind (`created`, `updated`, `missing`), old_version, new_version |
| `check_log` | id, source_key, scope, started_at, finished_at, outcome (`unchanged`, `imported`, `rejected`, `error`), snapshot_id, detail. One row per refresh check |
| `pruned_artifact` | snapshot_id, path, pruned_at. Snapshots whose original file was removed by retention; their data is intact |
| `job` (not built) | id, kind, source_key, area_id (value), params JSONB, state (`queued`, `running`, `succeeded`, `failed`, `cancelled`), idempotency_key (unique), lease_owner, lease_until, attempts, progress JSONB, error, cancel_requested |

`missing` is only produced from a `complete` snapshot, and a delivery in which
a layer shrinks by more than 20 % is refused instead. So is one in which more
than half of a layer's features changed, which normally means the export
changed rather than the data. The kinds `split`,
`merged` and `ambiguous` from the proposal are not built: a feature without a
stable id that changes shows up as one `missing` and one `created`.

## `source_index` — rebuildable from `source_raw` (built)

What the API, the map and search read. It holds the active snapshot of each
source in a shape made for use: typed columns, spatial indexes, true lengths.
The tables are not created by migrations but by `stiflyt2 index build`, from
the SQL in `src/stiflyt2/index/tables.py`.

| Source | Table | Columns |
|---|---|---|
| Turrutebasen | `trail_segment` | id (lokalid), version_id, geom (line), length_m, marked, updated, route_ids[], codes[], attrs JSONB |
| | `route` | id, code (rutenummer, lower case), label (unique), maintainer, area_prefix, name, names[], grading, special_type, route_type, importance, segment_count, length_m, geom |
| | `route_segment` | route_id, segment_id |
| | `info_point` | id, version_id, geom, facility, info, maintainer, attrs JSONB |
| WFS paths | `path` | id, version_id, geom (line), length_m, kind (`sti`, `traktorveg`, `stitrapp`), municipality_no |
| Roads (NVDB Vegnett Pluss) | `road` | id (`nvdbId`), version_id, geom (line), length_m, kind (`bilveg`, `gsv`, `gangveg`, `fortau`, ...), category (E, R, F, K, P, S), name, municipality_no. Carriageway-only links are left out |
| Stedsnavn | `place` | place_id (stedsnummer), name, alternative, language, name_status, spelling_status, type, type_group, main_group, municipality, municipality_no, county, geom (point) |
| Kulturminner | `heritage` | kind (`lokalitet`, `enkeltminne`, `sikringssone`), id, version_id, geom, name, category, art, protection, law, site_id, url, municipality_no |
| Teig | `parcel` | id (teigid), version_id, geom, municipality_no, matrikkel, area_m2, disputed, units JSONB |
| Derived | `route_end` | id, route_id, anchor_id, geom, offset_m, link (line from the end to its anchor), spot |
| | `anchor` | id, kind (`hut`, `parking`, `junction`, `dead end`), name, named, geom, routes, ends, spots, spread_m |
| | `route_shape` | route_id, pieces, ends, shape (`line`, `loop`, `branched`, `pieces`) |
| | `index_state` | source, signature, built_at, seconds, rows JSONB |

- **A route candidate is a code and one group of segments lying together.**
  A code can be used for stretches far apart, so segments of one code more
  than 1 km from the rest become a candidate of their own. The maintainer is
  not part of it: responsibility belongs to the stretch, can change along a
  route and can be shared, so `maintainers` is a list on each segment and
  each route. `label` is unique: the
  code where one candidate carries it, else the code with a part number, west
  to east (`jot91/1`, `jot91/2`). No real code contains a slash. The id is derived from the code and, for a
  split route, its first segment, and is the same after every rebuild.
  The name is the most common one among the route's rows.
- `place` has one row per spelling, historical ones included, with a trigram
  index, so a search finds misspellings and old forms. A place is returned
  once, under the best-matching spelling.
- `length_m` is measured on the ellipsoid, not in the plane of EPSG:25833.
- `version_id` points at the `source_raw.feature_version` a row was made
  from, as a value. Nothing in `core` will refer to these tables by key.
- Each source's tables are built in `source_index_build` and moved into
  `source_index` in one transaction. A reader sees the old index or the new
  one; a failed build leaves the old one. `index_state` records which active
  snapshots a source's tables were built from, so a build only redoes the
  sources that have a new snapshot. `stiflyt2 refresh` runs it.
- Everything here can be dropped and rebuilt with identical content. This is
  tested.

### Route ends and anchors

A proposal worked out from Turrutebasen and Stedsnavn, for a person to confirm
or correct later in the owned layer. v1 named endpoints automatically and
then replaced that with validated names; the same is expected here.

- **A route end** is a segment end that no other segment of the same route
  touches within 1 m (the tolerance v1 arrived at).
- **An anchor** is the place an end belongs to: the nearest hut within 300 m
  (a Stedsnavn `turisthytte`, or a Turrutebasen info point for a staffed,
  self-service or unstaffed hut), else a parking info point within 100 m.
  Ends with no such place are grouped where they lie within 30 m of each
  other: a `junction` if routes meet there, a `dead end` if one route simply
  stops. Unnamed anchors carry the nearest place name within 300 m, marked
  `named = false`.
- `spots` counts the distinct positions (5 m apart or more) at which the ends
  of an anchor lie, and `spread_m` is the farthest of them from the anchor. A
  hut with `spots` = 1 has every route arriving at one point; a higher number
  is the Turrutebasen problem of routes to the same place ending in different
  places.
- `route_shape` says whether a route is one line with two ends, a loop, one
  piece with extra ends (spurs or forks), or several disconnected pieces.
- An anchor that is a place keeps its id (`place:<stedsnummer>`) across
  rebuilds; an unnamed one is identified by its position.

On 2026-11-14: 746 hut and 1,416 parking anchors, 491 of them with routes
ending apart; of 1,987 routes with DNT-style codes, 1,226 are one clean line,
615 are in pieces and 146 have extra ends.

Not built: the endpoint-noded graph of source lines for the route engine
(`graph_node`, `graph_edge`). It belongs with route repair.

## `core` — owned

Built so far (domain migrations `d0001` to `d0005`; `place.reviewed` and
`route.reviewed` tell what a person has looked at from what was made by
default; `route` and `route_leg` per
[ADR-003](ADR-003-owned-routes-and-crossings.md), with `code_provisional` on
the route and `marked` and `signed` on each edge revision; who maintains a
route and its grading are on `route`, with exceptions per `route_leg`, since
`d0007`): `changeset`, `place`, `node`, `edge`,
`edge_revision`, `source_reference`, and `history.event`. They follow this
section with the additions of
[ADR-002](ADR-002-places-access-points-connectors.md): `place` is new, a
`node` may belong to a place, and an edge can be a `connector`. Authors are
names for now, since there is no `app_user` table. Validation findings are
worked out on request (`stiflyt2 report <area>`) and not stored. The rest of
this section is still a proposal.

| Table | Columns |
|---|---|
| `app_user` | id, email, name, role (`viewer`, `editor`, `publisher`, `admin`) |
| `area` | id, code, name, prefixes[], dnt_filter JSONB, aoi geom, buffer_m, status, version |
| `area_candidate_decision` | area_id, source_key, candidate_code, decision (`include`, `exclude`), reason, user |
| `changeset` | id, area_id, title, comment, author, state (`open`, `closed`), closed_at |
| `node` | id, geom, kind (`junction`, `endpoint`), name, status, version |
| `edge` | id, status (`active`, `retired`), current_revision_id, version |
| `edge_revision` | id, edge_id, rev_no, geom, a_node, b_node, kind (`trail`, `tractor_road`, `road`, `glacier`, `water`, `off_trail`, `other`), marked (bool), uncertainty, origin (`adopted`, `edited`, `drawn`), changeset_id, author. Immutable |
| `edge_lineage` | parent_edge, child_edge, kind (`split`, `merge`) |
| `route` | id, area_id, code, draft_revision_id, published_revision_id, version |
| `route_revision` | id, route_id, rev_no, state (`draft`, `validated`, `published`, `superseded`), changeset_id, author |
| `route_leg` | route_revision_id, seq, edge_id, direction, from_m, to_m (nullable) |
| `metadata_claim` | id, subject_type, subject_id, predicate, value, origin (`source`, `user`), status (`unknown`, `unverified`, `confirmed`), selected (bool), valid_from, valid_to, reason, author |
| `source_reference` | subject_type, subject_id, source_key, external_id, snapshot_id, content_hash, ref_type (`adopted_from`, `traced`, `evidence`, `claim`). **Values only, no foreign keys** |
| `source_alert` | edge_id, diff_item (value), kind (`source_moved`, `source_removed`, `source_split`), state (`open`, `accepted`, `dismissed`), decided_by |
| `validation_finding` | subject, code, severity, detail, location, run_at |

Rules:

- IDs are UUIDs and never reused. `version` is the optimistic-lock counter.
- Metadata predicates include `name`, `formal_responsibility`,
  `actual_maintenance`, `marking_responsibility`, `route_type`, `grading`.
  Several claims can exist per predicate; exactly one is selected.
- Splitting an edge retires it, creates two edges and rewrites the legs of
  every draft route using it, in one transaction, or fails with the reason.
- A crossing is not a junction. Edges connect only by sharing a node.
- An id that ends says what came instead (built, `d0009`):
  `succession` (subject_type, old_id, new_id, how: joined / divided /
  absorbed, at) for junctions joined, stretches divided and routes taken
  into another; `alias` (subject_type, subject_id, kind: code / name, value,
  until) for the codes and names a route or a place has had. References from
  outside `core` hold an id and resolve it with `identity.current`.
- Lengths are computed on the ellipsoid, not planar in EPSG:25833 (planar
  distortion is roughly 0.2 % in western Norway).

## `history` — append-only

| Table | Columns |
|---|---|
| `event` | seq, changeset_id, at, user_id, type, subject_type, subject_id, payload JSONB (before and after revision IDs) |
| `published_route` | id, route_id, route_revision_id, published_at, published_by, geom, legs JSONB (edge revision IDs, direction), metadata JSONB, checksum |

A published route is a frozen copy. It needs nothing from `source_*` and
nothing mutable in `core` to be shown or exported.

## `access` — who may use the tool (built)

Not part of the overlay: made on the import track. The API role reads it and
may only set the two "last" columns.

| Table | Columns |
|---|---|
| `person` | email (as signed in with, lower case), name (unique: what changes are recorded under), role (`reader`, `editor`), added_at, added_by, last_seen_at |
| `token` | id, digest (sha256; the token itself is not kept), email, through (`mcp`, `api`), label, created_at, last_used_at, revoked_at |

`core.undo_log.through` and `history.event.through` (d0013) say how a change
came in: `viewer`, `mcp` or `api`; nothing said counts as the map.

## `feedback` — about the tool (built)

Not part of the overlay: made on the import track, and left standing when
the overlay is taken away.

| Table | Columns |
|---|---|
| `entry` | id, at, kind (refused, failed, reported), through (viewer, mcp, api), who, what (the request or tool), status, message, pattern (generated: the message with ids and numbers taken out, what entries are counted together by), arguments JSONB, about_type, about_id, trace, seen_at, fixed_in |

`about_id` is an id in `core` held as text and looked up when read. Nothing
here is referred to from `core`.

## `height` — how high the ground is (built)

Fetched from Kartverket's height service, not made by a person: made on the
import track, outside the overlay, with no history and nothing to undo.

| Table | Columns |
|---|---|
| `profile` | line (md5 of a stretch's geometry as EWKB: what a profile is found by), step_m, sampled (the points asked about, a LineString in 25833), at_m[] (metres along the line to each point, on the ground), z[] (NULL where the service has none), ground[] (the service's `terreng`: `SnøIsbre`, `Myr`, …), source (the service's `datakilde`), fetched_at |

A profile belongs to a line and not to a stretch: two revisions with the
same line share it, a moved stretch gets a new one. What a route climbs is
worked out from the profiles of its stretches when asked for
(`stiflyt2.height.of_route`) and kept nowhere.

`core.route.utno_id` (d0015) is the number of the route's page on UT.no,
said by a person.

## `ops` — what happens to the routes (signs built)

Made on the import track, outside the overlay; copied with the nightly copy
beside it. Points at `core` by id and never writes it. Written by the API
role for now: a database role of its own for operations is not made.

| Table | Columns |
|---|---|
| `sign_point` | id, edge_id (the stretch it stands on), geom, removed, created_at, created_by: a sign site a person has put at a spot on a route |
| `sign_site` | id, at_type (`place`, `node`, `point`), at_id, area, code (BRE-SS-0001, given the first time a sign of the site is ordered), status (`rejected`: there are to be no signs here; the other values mean nothing any more), name, back_text, send_to_name, send_to_address, updated_at, updated_by |
| `sign_post` | id, node_id, no (1 is the post a junction has without a row), geom (NULL: where it is assumed to stand), taken_out, name_sign (it carries a sign with the place's name), name_text (what that says, if not the site's name), name_to_order, name_up_order, updated_at, updated_by |
| `sign` | id, node_id, first_edge, to_type (`place`, `node`, `point`), to_id, route (what a sign is found by; route is empty unless two signs share the rest), added (a destination a person added: first_edge is the nil uuid, and the way is the shortest there), post_no, direction, colour, text, distance_m, taken_out (NULL: as worked out), to_order (it is to be made: on the order taken out next), why, up_order (the order it was made in, once it is up), updated_at, updated_by |
| `sign_order` | id, at, by, area, note, rows JSONB (each sign as it was ordered). Insert only |
| `event` | seq, at, who, through, subject, subject_id, type, payload. Insert only |

`access.person.role` has a third value, `operations`.

## Deferred

No tables yet for media, journal, tasks, organisations or recipient-specific
exports. They attach to `area.id`, `route.id`, `edge.id` and `node.id`.
