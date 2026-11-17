# ADR-002: Places, access points and connectors

Status: accepted 2026-11-14 by the route responsible. Extends the `core` part of
[schema.md](schema.md). The first step below is built (domain migration
`d0001`, `src/stiflyt2/domain/places.py`, the place panel in the viewer).

## The problem

Routes mostly run between huts, and a route to Gjendesheim should end at one
logical point: that is what its name, its signs and a shortest-path search
refer to. In Turrutebasen it does not. Of 2,162 huts and parking places where
routes end, 491 have routes arriving at more than one spot. Gjendesheim has 25
route ends at 11 spots. Most gaps between one spot and the next are 25 to
100 m (685 of 1,284 spots); 268 are under 25 m and 305 are 100 to 300 m.

Some of that is sloppy digitising, and some is real: one route does start
50 m down the road, at its own signpost. So the model has to give a single
logical point without pretending the ground is simpler than it is.

## Decision

Three things, kept apart.

| Thing | What it is | Example at Gjendesheim |
|---|---|---|
| **Place** | The logical destination. One per hut, parking place or other named start. Has a name, a kind and a reference point | "Gjendesheim", the hut |
| **Access point** | A graph node that belongs to a place, exactly where it is on the ground. A place has one or several | The post by the door; the one 50 m down the road; the pier |
| **Connector** | An owned edge of kind `connector` joining the access points of a place. Real geometry, real length | The 50 m along the road |

- **A place is not a point that routes are bent to.** Route geometry and
  nodes stay where they are on the ground. A place groups the nodes that
  count as "being there".
- **One access point per place is primary.** It is what distances to the
  place are measured to. By default the access point nearest the reference
  point; a person can choose another.
- **Connectors make the graph connected through a place.** Every access
  point of a place must be reachable from its primary one over connectors or
  ordinary edges. A place where that fails is a validation finding.
- **Signposts attach to nodes, not to places.** Two access points of one
  place can each carry a signpost with different content. The sign tables
  come later; nothing here has to change for them.

### How the questions get answered

- **Where does this route start and end?** At two places. A route revision
  states `from_place` and `to_place`; its first and last legs should end at
  access points of those places, and it is a finding if they do not.
- **Shortest path from Gjendesheim to Memurubu?** From the primary access
  point of one to the primary access point of the other. Connectors carry
  the path across each place, and their length counts.
- **What does the sign at this post say?** Distances from this node to the
  primary access point of each destination place.

### Rejected

- **Snap every route end to one point.** Simple, and wrong on the ground by up
  to hundreds of metres. It erases where signposts stand and makes sign
  distances wrong by the same amount.
- **Only name the nodes (as v1 did).** Several nodes share a name, but
  nothing says they are one destination and nothing connects them. v1 merged
  sign sites by name within 100 m for display only, and its notes record
  that merging by name alone and by distance alone were both rejected.

## From proposal to owned data

`source_index.anchor` and `route_end` stay what they are: a proposal that is
rebuilt whenever the sources change. Adopting one creates owned rows, in one
changeset:

1. A place, from the anchor: name, kind, reference point.
2. A node for each spot where routes end, as access points of the place.
3. Connectors from each access point to the primary one, as straight lines
   to begin with. A person redraws them along the road or path where that
   matters.

Each row records what it was adopted from as plain values (place name id,
info point id, position), never as a key into the index. A person can then
rename or move the place, remove an access point that does not belong (the
pier 277 m away may be its own place, or just a junction), add one, change
the primary, or redraw a connector. A later rebuild of the index changes none
of this; the viewer shows a proposal as adopted where an owned place refers
to it.

## Tables

Additions to the `core` proposal in schema.md. Unchanged tables are not
repeated.

| Table | Columns |
|---|---|
| `place` (new) | id, name, kind (`hut`, `parking`, `other`), geom (reference point), primary_node_id, status (`confirmed`, `retired`), version |
| `node` | as proposed, plus place_id (nullable: set for an access point) and an optional local name ("ved brygga") |
| `edge_revision` | as proposed; `kind` gains `connector` |
| `route_revision` | as proposed, plus from_place_id and to_place_id (nullable) |
| `source_reference` | as proposed; `subject_type` gains `place` |
| `validation_finding` | as proposed; new codes: place not connected, place without a primary access point, route end not at an access point of its stated place |

A node belongs to at most one place. A node of a place need not be a route
end: a route passing through a hut uses an access point too.

## First step to build

The smallest piece that makes an anchor yours:

1. Domain migration with `changeset`, `history.event`, `place`, `node`,
   `edge`, `edge_revision` (used for connectors only at first) and
   `source_reference`. ADR-001 already puts changesets and the event log in
   the first domain migration.
2. API: adopt a proposed anchor; rename and move a place; add, remove and
   re-point access points; redraw a connector. Every change inside a
   changeset, recorded as events.
3. Viewer: adopted places drawn apart from proposals, and an edit panel for
   one place.

Not in this step: routes, legs, snapping route geometry, signs, sign-in.

## Settled with the approval

1. **The author of a change** is a configured editor name (`STIFLYT2_EDITOR`,
   a person's name, set on the server) until sign-in exists. With no name set, the API
   accepts no changes.
2. **A parking place is a place** in the same sense as a hut.
3. **A hut's reference point is the building**, as Stedsnavn has it. The
   primary access point starts as the one nearest to it.

## Amendments, 2026-11-14

Agreed with the route responsible after the first version was in use.

1. **Places are made by default** for every proposed hut and parking place
   (`stiflyt2 places make`), by the author "Stiflyt (automatic)", and marked
   as not reviewed. While unreviewed, a place follows its proposal: if the
   proposal changes, the place is made again. A place a person has reviewed
   or changed is never touched, and a proposal whose place a person withdrew
   is not made again. Junctions and dead ends are not made into places.
2. **A route starts and ends at a node, not necessarily at a place.** Many
   routes end where they meet another route, and such a junction is an
   ordinary node. A place is needed only where something is a destination.
   So a route revision will state its end nodes; the place, where there is
   one, follows from the node. This replaces `from_place_id` and
   `to_place_id` in the table above.
3. **A missing place at a junction costs nothing for routing.** Two routes
   that share a node are connected. Route ends that nearly meet (1,117
   junctions where the ends lie apart) need a repair of the nodes, which is
   separate from places.
4. **Turrutebasen's code does not identify a route**, and neither does its
   name. jot91 covers two stretches 16 km apart plus a stray; 488 DNT-style
   codes are used by more than one maintainer; 1,342 of 1,987 DNT-coded
   candidates are called "Ukjent" or nothing. An owned route will therefore
   have its own permanent id, a code we control that is unique in the
   overlay, a name generated from the places it runs between, and
   Turrutebasen's code kept as a reference for sending corrections back.
   Until routes are owned, the index splits a code whose segments lie more
   than 1 km apart into separate candidates and labels them `jot91/1`,
   `jot91/2`, west to east. Letters are not used: Turrutebasen uses a, b, c
   for real alternatives between the same two places, and no real code
   contains a slash.
5. **On the map a place is one point,** drawn where its routes arrive (its
   primary access point). Further access points are smaller points joined to
   it. The reference point (the building) is shown only while the place is
   open, since two points for one hut read as two places.

## Built so far, and not yet

Built: adopting a proposal; renaming; changing kind; moving the reference
point; adding an access point; taking one out; choosing the primary one;
withdrawing a place. Every change is an event in the author's changeset. A
place is kept connected: an access point that loses its connector gets a new
one to the primary.

A junction or dead end can be made a place too, by a person: the name is
asked for then, since such a spot has only the nearest place name. On the
map a click on a marker opens the marker, also where a route line or a
finding's ring lies over it.

Not yet: redrawing a connector along a road or path (they are straight
lines), moving an access point, naming an access point, routes stating the
places they run between, and the validation findings.
