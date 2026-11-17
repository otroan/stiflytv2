# ADR-001: Amendments to build plan v3

Status: accepted 2026-11-14 (amendments 1-4 approved by the route responsible).
Base document: [build plan v3](../plan/build-plan-v3.md). Where this ADR and
v3 disagree, this ADR wins.

## 1. Changesets are part of the foundation

v3 lists `external_delta` as a future module. Changeset control is needed from
the start so that edits can later be exported to ut.no or Kartverket.

- Every mutation of graph, routes or metadata happens inside an open
  changeset and is recorded as an event carrying that changeset's ID.
- The changeset tables and the event log are in the first domain migration.
- Export of a changeset as a delta is built in phase 1D as neutral GeoJSON
  (before/after geometry and properties, the recipient's own IDs where known,
  reason). Recipient-specific formats wait until we know what Kartverket and
  ut.no accept.
- No submit/review/approve workflow in phase 1. A changeset is open or closed.

## 2. A basic route engine is in phase 1C

v3 section 9 puts advanced routing out of scope and has the editor follow
edges by hand. Phase 1C adds:

- A to B shortest path over owned edges plus active source candidates inside
  the area, weighted by a source preference (default: owned, FKB, Turrutebasen
  only, road), adjustable per request.
- Up to three alternatives that differ meaningfully.
- Reconstruction of a Turrutebasen route candidate: the same search with a
  strong preference for edges claimed for that route code, returning the path
  plus what did not fit.
- Accepting a path that uses source candidates materialises them as owned
  edges in the same changeset.

Still out of scope: turn restrictions, elevation or time costs, nationwide
routing, automatic bulk matching.

Implementation: in-process graph (NetworkX) built per request from a bounding
box. This matches v3's stack and needs no server extension. pgRouting is the
fallback if performance requires it.

## 3. "Done" includes what sign generation needs

Sign generation itself stays out of phase 1, but the validated dataset must
answer its questions. Phase 1D adds a read model and endpoint giving, per
junction node: its name, the published routes passing through it, and for each
route the reachable named destinations with distances.

## 4. Pilot area is BRE or FEM, not RON

v1 has recorded corrections for Breheimen and Femundsmarka (remaps, bridges,
exclusions, boat and glacier segments). They give known problems to test
route reconstruction against. Final choice is open; see plan-and-risks.md.

## Trims

- Job queue: lease, retry, progress and cancel on a single machine. No
  priorities, no distributed workers.
- Two Alembic tracks (import side, domain side), not one per schema.
- Import criteria on an area are versioned with the area row; no separate
  revision model.

## Consequence for "published routes never change"

Draft route legs reference owned edges, so moving an edge moves every draft
route that uses it. Publishing freezes a copy: the published record stores its
own geometry, metadata and the exact edge revisions used. Later edge edits
mark the route as having unpublished changes; they do not alter what was
published.
