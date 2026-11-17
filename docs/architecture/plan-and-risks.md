# Task plan, decisions needed and risks

Phases follow [build plan v3](../plan/build-plan-v3.md) section 7 as amended
by [ADR-001](ADR-001-amendments-to-v3.md).

## Phase 0 — this proposal

Done: v1 assessment, amendments, schema, REST contract, deployment proposal.
**Stop point: schema and API need approval before any code.**

## Phase 1A — foundation and isolation

1. Repo skeleton: FastAPI app, worker entry point, React/Vite app, Docker
   Compose with PostGIS, lint, type check, CI.
2. `bootstrap.sql` and both Alembic tracks with the full schema.
3. Database tests: import role cannot write `core`/`history`; `history`
   rejects update and delete.
4. Google sign-in with roles; OpenAPI-generated TypeScript client.
5. Changeset open/close and the event log.

## Phase 1B — import

1. Job queue (lease, retry, progress, cancel) and worker.
2. Areas: prefixes, DNT filter, AOI, candidate include/exclude.
3. Upload adapter for GPX and GeoJSON, keeping the original file.
4. Turrutebasen adapter: snapshot, feature versions, route candidates.
5. Stedsnavn adapter and `/places/search`.
6. `source_index` build with staging swap; rebuild test.
7. Import UI: sources, upload, preview, job status, history, diff.
8. FKB adapter once access is confirmed; until then FKB arrives as upload.

## Phase 1C — graph, route engine, editor

1. Adopt source features as owned edges; draw; move; split; connect.
2. Route engine: A to B, alternatives, candidate reconstruction.
3. Route legs, metadata claims, draft save.
4. Editor UI: source layers, tools, route list, metadata panel, findings.

## Phase 1D — quality, publication, export

1. Validators ported from v1 plus crossing/gap checks.
2. Publish policy and frozen published copies.
3. Route export (GeoJSON, GPX); changeset delta export.
4. Changeset revert; junction read model for signs.

## Phase 1E — re-import safety

1. Diff between snapshots; source alerts on owned edges.
2. Tests: source feature deleted, source feature split, failed or partial
   import, `source_index` dropped and rebuilt.

## Decisions needed

| # | Question | Proposed default |
|---|---|---|
| 1 | Pilot area | BRE (most recorded corrections, includes a glacier case) |
| 2 | How do we get FKB-TraktorvegSti, and under what licence? | Unknown. Start with file upload of an extract |
| 3 | Minimum metadata to publish | Code, name, and `actual_maintenance` not `unknown`; route connected; no error-level findings |
| 4 | May geometry adopted from FKB be exported to ut.no or Kartverket? | Unknown. Export checks `export_policy` and blocks when unresolved |
| 5 | What formats do Kartverket and ut.no accept for corrections? | Unknown. Neutral GeoJSON delta first |
| 6 | Who edits | A few trusted editors, no review step |
| 7 | Where is the v2 repo hosted | Same GitHub account as v1 |

## Risks

| Risk | Effect | Mitigation |
|---|---|---|
| FKB access or licence not obtainable | The preferred geometry source is missing | Upload adapter works with any extract; design does not depend on FKB |
| Licence forbids exporting derived geometry | Changeset export limited | Provenance per edge; export policy per source |
| Turrutebasen `lokalid` not stable across deliveries | Diffs show false created/missing pairs | Diff also matches on content hash and geometry; verify on two real deliveries in 1B |
| Reconstruction quality on badly broken routes | More manual work per route | A-to-B from scratch is always available; errata used as hints |
| Shared server resources | v1 slowed by v2 imports | Limits and scheduling in deployment.md |
| Snapshot retention fills the disk | Server outage affecting v1 | Pilot-area indexing, retention setting, disk check before install |
| Editing UI is the largest piece | Phase 1C overruns | Read-only source comparison ships first; tools added one at a time |
| Planar lengths in EPSG:25833 | Sign distances off by about 0.2 % | Ellipsoidal length everywhere distances are reported |
