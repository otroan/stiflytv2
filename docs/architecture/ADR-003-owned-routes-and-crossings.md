# ADR-003: Owned routes and crossings

Status: accepted 2026-11-14 by the route responsible. Builds on
[ADR-002](ADR-002-places-access-points-connectors.md). The first step is built
(domain migration `d0003`, `src/stiflyt2/domain/routes.py`, the route panel in
the viewer).

## The problem

A route in the viewer is still a candidate worked out from Turrutebasen. It
cannot be corrected, and two things the route responsible needs cannot be expressed at all:

- **A route that crosses water.** The route runs to the shore, continues on
  the other side, and the boat is part of the route. The two stretches must
  be one route, connected for a shortest-path search, but the water must not
  be counted as walking distance.
- **A route with its own identity.** Turrutebasen's code and name do not
  identify a route (ADR-002, amendment 4). Where one code covers stretches
  more than 1 km apart, the index shows them as parts, `jot91/1` and
  `jot91/2`. Whether two parts are really one route with a crossing, or two
  routes that share a code by mistake, is for a person to say.

## Decision

1. **Making a route yours materialises it.** Each Turrutebasen segment it
   uses becomes an owned edge of kind `trail`, and the segment ends become
   nodes. A segment used by several routes becomes one edge that they share.
   A node that already exists within 1 m is reused, so a route arriving at a
   place joins that place's access point.
2. **An owned route has** a permanent id; a code that is ours and unique in
   the overlay (Turrutebasen's where that is unambiguous, otherwise chosen
   when the route is made); a name, generated from the places at its ends
   unless set by hand; and an ordered list of legs, each an edge and a
   direction. Turrutebasen's code and maintainer are kept as a reference.
3. **A crossing is an owned edge that is not a trail.** Its kind says what it
   is: `water` (boat or ferry), `glacier`, or `other`. It joins two nodes,
   typically two loose ends of the same route, as a straight line to begin
   with, and becomes a leg of the route.
4. **Distances are reported in two parts:** on foot, and by crossing. A
   `water` leg never counts as walking distance, for the route or for a sign.
   It does count for connectivity: a shortest-path search can use it.
5. **One route can be made from several candidates.** Parts such as
   `jot3/1` and `jot3/2` are added to one owned route and joined by a
   crossing or, where they nearly meet, by an ordinary connector.
6. **Routes are not made by default.** Unlike a hut, a route candidate is too
   often wrong (in pieces, with extra ends, sharing a code) for an automatic
   version to be worth owning. A person makes each one.

## First step to build

1. Domain migration: `route`, `route_revision`, `route_leg`.
2. Make a route from a candidate; set its code and name; add another
   candidate as a further piece; join two ends with a crossing of a chosen
   kind; take a leg out.
3. Per route: whether it is one connected line, its ends and the places
   there, length on foot and length by crossing.
4. Viewer: owned routes drawn apart from candidates, and a route panel with
   those actions.

Not in this step: snapping geometry to mapped paths, editing an edge's
geometry, publishing, height profiles.

## Settled with the approval

1. **A glacier leg counts as walking distance, and is never marked.** It is
   signposted but has no marks on the ground. So whether a stretch is marked
   and whether it is signed are facts kept with each stretch, on the edge
   revision: taken from Turrutebasen (`merking`, `skilting`) for an adopted
   segment, and set by its kind for a crossing (glacier: signed, not marked;
   water: not marked). A route reports how much of its walking distance is
   not marked.
2. **A route's code may stay temporary.** The code is typed when the route is
   made, with the part label as the suggestion. A part label kept as the code
   (`jot91/1`) is flagged as temporary. Codes need a central authority that
   assigns them; until there is one, temporary codes stand.

3. **Responsibility belongs to the stretch, not the route** (decided after
   trying jot96). Who maintains a stretch can change along a route and can be
   shared. So the maintainer is not part of what identifies a route: a
   candidate is a code and one group of segments lying together, whoever
   maintains them. Maintainers are kept per segment in the index and per edge
   revision in the overlay, as a list, and a route reports who maintains how
   much of it. In Turrutebasen 497 stretches already have more than one
   maintainer for the same route.

## Amendments, 2026-11-14

Agreed with the route responsible once Jotunheimen had been looked at: 71 of its 84 candidates
are clean lines, and making each by hand was the bottleneck.

1. **Clean lines are made in bulk; the rest are still made by hand.** This
   replaces decision 6. A candidate that is one line with two ends is made a
   route by `stiflyt2 routes make <area>` (or the button in the area's
   to-do list), by the author "Stiflyt (automatic)", marked as not reviewed,
   with Turrutebasen's code or the part label as its code. A candidate in
   pieces or with extra ends is left for a person. A route a person has
   withdrawn is not made again in bulk. Unlike places, a route made in bulk
   does not follow its candidate afterwards: once made it is only changed by
   a person.
2. **A route is reviewed or not** (`route.reviewed`, migration `d0005`). Any
   change by a person marks it reviewed, as does "Mark as reviewed".
3. **Each area has a to-do list** (`domain/worklist.py`): candidates not
   yours yet and why; routes with less than half their length within 20 m of
   a mapped path; routes without a name because an end is not at a place;
   routes with a temporary code; places where routes arrive at several spots.
4. **A stretch can be laid onto the mapped path.** The new line is the
   shortest way along the open-WFS paths between the stretch's own two ends,
   which stay where they are. It is refused when no path lies within 60 m of
   an end, when the paths do not connect, or when the result is more than
   three times as long. The earlier line stays as the previous revision, and
   the paths used are recorded as references. A stretch is shared, so every
   route using it follows.
5. **What a stretch is can be changed by hand:** its kind, marked and
   signed. That is how a glacier leg inside an existing route is recorded:
   called a glacier, it becomes signed and not marked.

6. **The network is checked on request, not stored** (`domain/network.py`,
   `domain/report.py`). The graph is every stretch an active route uses plus
   the connectors inside places; water counts as joined. Findings: `near` (a
   loose end within 50 m of a stretch it is not joined to), `touch` (a node
   within 1 m of a stretch it is not an end of), `crossing`, `island`, and
   ends at nothing, told apart as `open end` (a candidate that is not yours
   carries on from the spot) and `dead end`. Near, touch and crossing count
   only when the walk round is longer than four times the gap plus 50 m.
   There is no `validation_finding` table yet: nothing is kept between runs,
   so a finding cannot be dismissed as "looked at and right".

7. **A route is changed by direct manipulation** (decided after repairing jot2 with tools picked from a list). Junctions and the line are dragged on
   the map and snap by themselves; what applies to one stretch is offered
   when that stretch is clicked. A pull on the line is local: it re-lays the
   line for three times the pulled distance to each side, at least 50 m,
   along mapped paths and roads where they join up, and leaves the rest. A
   junction dropped on another is joined to it; a stretch that ran between
   the two is retired and leaves its routes.
8. **A stretch whose two ends are the same node does not make that node a
   junction.** A few metres of Turrutebasen line inside one arrival point of
   a place collapse to such a loop; they are no longer made into stretches,
   and one that exists does not make a route "branched". A route also stays
   the owner of its candidate after stretches are taken out of it, as long
   as it holds most of the candidate's segments.

9. **Done is something a person says** (`route.reviewed`, migration `d0006`).
   Until now any change by a person set it, which left no way to tell a
   finished route from one being worked on. Corrections of a route's name
   are proposed to Kartverket only for routes that are done.
10. **Corrections for Turrutebasen are derived, not recorded**
    (`domain/corrections.py`): what is yours compared with the active
    Turrutebasen snapshot, per segment `lokalid`. A stretch that was retired
    because its two ends were joined is reported as a route number to take
    off the segment when it is a real stretch, and as a segment to remove
    only when it is shorter than 10 m.

11. **Simple repairs are proposed, and a person approves**.
    `domain/proposals.py` works them out on request and stores nothing. A
    proposal is tried inside a transaction that is rolled back and is offered
    only if the route then is one line. Approval carries it out as the person
    who approved, through the same functions as editing by hand.

12. **What is said about a route lives on the route** (decided 2026-11-15; this
    replaces "responsibility belongs to the stretch" in point 3 above, and
    keeps what it was for). `route.maintainers` and `route.grading` hold for
    the whole route; `route_leg.maintainers` is set only where a stretch of
    that route is maintained by someone else (migration `d0007`). A stretch
    is shared between routes, so a maintainer kept on the stretch mixed the
    routes' maintainers; Turrutebasen itself keeps it per route on each
    segment, and the index now does too (`route_segment.maintainers`). A
    route made from a candidate takes what holds for most of its length, and
    its legs take an exception where Turrutebasen differs. The list on the
    edge revision is kept as recorded and no longer read.
13. **A link a route walks is the route's**, also when it is of kind
    `connector` and ends at an access point: a place retires only its own
    links. And two pieces of a route are joined along a stretch that already
    runs between the two ends where there is one; a straight link on foot is
    proposed only where there is none.

14. **A route can be laid out between points**, with no Turrutebasen candidate behind it, and an
    existing route can be laid out again the same way. The way is the
    shortest over your stretches and Turrutebasen's segments together. To
    have one graph with the junctions the route will really get, the planner
    makes the segments around the points into stretches inside a transaction
    that is rolled back; making the route runs the planner again for real
    with only the segments the plan used. Place links and water are not
    walked. Such a route belongs to the area its code names.
15. **A stretch can be divided** (`routes.split`). The parts take over its
    facts and both refer to the Turrutebasen segment, with which part they
    are; the routes that used it use both parts; the stretch itself is
    retired. A segment can therefore be several stretches, and is compared
    with Turrutebasen as all its parts together. Kartverket is asked to
    divide a segment only when the parts have been given different facts.

16. **Every change can be undone** (decided after a repair at Krossbu went wrong with no way back). Triggers
    write the row as it was to `core.undo_log` for every change to a route,
    leg, node, edge or place, grouped by transaction: one request is one
    action. Undo puts the latest action's rows back, newest first, and only
    ever the latest, so it is strictly last in, first out. Events and edge
    revisions are never taken away: the history shows the change and that it
    was undone, and a row an action added gets the status `undone`
    (migration `d0008`). There is no redo.
17. **A network finding can be accepted** (`core.accepted_finding`, by the
    finding's key). It is then not listed as something to do.

18. **Changing a route and fixing a line are two things** (after Krossbu,
    where dragging an end joined two junctions and took a stretch out of two
    other routes). By default a drag changes only which stretches the route
    uses: an end is moved (`routes.move_end`: carried on along the shortest
    way from the nearest junction of the route, or cut back when dropped on
    the route), or the route is sent another way (`routes.send_via`). Moving
    junctions, pulling lines and dividing stretches change what every route
    on the stretch shares, and are done in a mode that is asked for.
19. **A part that is already a route of yours is taken into another**
    (`routes.absorb`) when it is added as a further part: two parts of one
    code made separately, either side of a glacier, become one route with a
    gap to join.
20. **Ids hold, and an id that ends says what came instead** (decided 2026-11-15, asking how signs can survive changes underneath them;
    `domain/identity.py`, migration `d0009`). A route, a junction, a stretch
    and a place keep their id while they are moved, re-laid or renamed. Only
    three changes end an id: junctions joined, a stretch divided, a route
    taken into another. Each writes `core.succession` (old id, new id, how).
    `identity.current` answers what an id stands for now: itself, the one
    thing that came instead, several things (a person has to choose), or
    nothing. Earlier codes and names are kept in `core.alias`, and
    `identity.lookup` finds a route or a place by them. Undo takes these rows
    back with the change. What was done before was filled in from the event
    log. Whatever is built on top (signs, a diary, work orders) points at
    these ids, keeps a copy of what it saw, and asks `current` before use.
21. **A route end is proposed a place where something there has a name**
    (decided 2026-11-15; `places.suggest`). Of 45 ends at no place in Jotunheimen and
    Breheimen, 28 lie at a hut, a summit, a farm, a bridge or on a named
    road. The name is proposed, a person approves, and the place refers to
    the Stedsnavn entry it is named after. A second end beside the same
    thing becomes one more arrival point (`places.take_in`), never a second
    place of the same name. Ends where routes merely meet in the terrain get
    no proposal: there is nothing to name them after.
22. **Mapped paths are used wherever they run beside a route** (decided 2026-11-15; `routes.lay_beside`, `routes.compare_with_mapped`). Snapping a
    whole stretch to the mapped way between its ends fails for a long
    stretch that is only partly mapped: bre17 has one stretch of 10 km with
    a mapped path beside half of it. Laying a stretch beside keeps its own
    line where nothing is mapped and takes the mapped line where one runs
    along it for 120 m or more. The route panel shows the share and both
    lengths before anything is changed.
23. **A route is carried where no stretch leads, along the mapped lines**
    (2026-11-15, for bre28 to Grotli, jot4 by Tjønnholet and jot1 along
    Gjende). An end dragged, a line dragged or a click when a route is laid
    out between points means your stretches and Turrutebasen's segments
    first. Where none leads there, the way over the mapped paths and roads is
    taken and becomes new stretches (`origin` drawn, one for each run of
    paths and of roads), which say which mapped lines they follow. A line
    sent another way leaves its old line where the mapped way leaves it and
    rejoins where that comes back.
24. **A route can be discontinued and kept** (decided 2026-11-15, for jot1, given up for the danger of rockfall;
    migration `d0010`, `routes.discontinue`). It is a state of an active
    route, with a date and a reason, not a withdrawal: the route keeps its
    id, line and history, and is left out of the network checks, the
    planner, proposals and what is handed on. A withdrawn route says "this
    was never a route of mine"; a discontinued one says "this was a route,
    and is not walked any more".
25. **A route keeps at least one stretch; a withdrawn route that others run
    over was a number too many** (after jot03 and jot04, misspellings of
    jot3 and jot4 on one 80 m segment). Kartverket is asked to take the
    number of such a route off its segments.
26. **Where nothing is mapped, a route goes straight from point to point**
    (decided 2026-11-15, for jot38 over Smørstabbreen; this is the "new stretch
    where none exists" that had waited). Laying a route out between points,
    a point with no stretch and no mapped line under it is the point itself.
    The points of that kind in a row become one stretch of kind `off_trail`,
    origin `drawn`, which a person then says is a glacier or what it is. A
    route also has a description in words (`route.description`, `d0011`).
27. **A route may be a round trip** (decided 2026-11-16, for jot53: unmarked,
    from Skogadalsbøen by Vormeli and round, back to Skogadalsbøen). A route
    that is one line ending at the junction it starts from is a shape of its
    own (`loop`), and nothing is asked of it for that. It is made by
    dropping an end of a route on the route's own other end: the route is
    carried on to its start by another way than it came (over stretches it
    does not use, else along the mapped paths and roads), and nothing is
    cut. Where the shortest mapped way back is the way it came, it is
    refused. It is walked the way it was laid, from the junction it sets out
    from, or from a place on it. It has no ends, so it is not named after
    them, and moving an end or sending it another way is refused with words
    for it: a stretch taken out opens it again. Handed on, it says
    `round_trip`, and `from` and `to` are the same place. Not built: a round
    trip with a stem (out along a stretch, round, and back along the same
    stretch), which would walk a stretch twice.

28. **A route may be kept and not published** (decided 2026-11-16, for jot53). Said with a
    reason (`route.unpublished_why`, `d0012`); no reason means published. It
    is left out of what is handed to UT.no, and nothing new is asked of
    Kartverket for it (no segment, no number on a segment). Where
    Turrutebasen has the route, Kartverket is asked to take its number off,
    with the reason (decided the same day; taken as the rule for every unpublished route until said
    otherwise). In everything else it is a route like any other. Not the
    same as discontinued (amendment 23), which says the route is given up in
    the terrain.
29. **A route can be put back as it was, out of order** (decided 2026-11-16,
    after jot88: the route responsible's changes to it lay under changes to jot53 made for them
    through the API, and Undo took those back instead). `undo.back` takes
    back one change to a route and every later change to that route or its
    line (the route's own row, its legs, and the stretches it uses or used),
    leaving other actions in place. It is refused where a later action that
    is not among them changed a row one of them changed, or uses a stretch
    or junction one of them made. Undo at the top is unchanged: the latest
    action, whatever it was.

30. **A route that runs double between two junctions is not one line** (decided 2026-11-16). A line through N junctions has N − 1
    stretches and a round trip N; a route with more has a loop inside it,
    usually two Turrutebasen segments drawn for one bit of path (jot66,
    jot88, jot90, which had passed as lines because they have two ends).
    That shape is `doubled`. What needs one line refuses it; the to-do list
    asks about it; and the proposal is to take the stretch too many out: of
    two between the same junctions, the one fewer of the other routes use,
    then the longer. The index's verdict on Turrutebasen's candidates still
    calls such a candidate a line: it is caught once it is ours.
31. **The edge of a glacier is a kind of place** (decided the same day; `place.kind = 'glacier_edge'`, `d0014`).
    Where a route steps onto the ice or off it, a place is proposed, named
    after the glacier whose name lies nearest the stretch over the ice and
    the side of the crossing ("Fannaråkbreen brekant nord"), since a
    crossing has two edges and a name is one place's. A person may name it
    otherwise; it is the edge of a glacier all the same.

32. **Heights are Kartverket's, kept per line, and no part of the overlay**
    (decided 2026-11-16; `height.profile`, import track
    `0007`). v1 asked the height service about a route's own corners and
    kept the answer per route. Here a stretch is asked about every 10 m
    and at its corners (a glacier stretch is two points), and the answer is
    kept by the checksum of the line, since stretches are shared and a line
    is what the heights are of. What a route climbs is worked out when asked
    for, over its stretches in walking order, a rise counting once it is
    2 m. The kind of ground the service answers with is kept too: it tells
    ice from open ground. Walking time waits for a rule to be chosen.
33. **Which page on UT.no is a route's is said by a person, and its
    description is taken on their word** (decided the same day; `route.utno_id`, `d0015`). UT.no's
    API is closed to us, so the route's public page is read, one at a time
    and only when asked for. UT.no describes a route once for each
    direction and a route here has one description: the two are taken as
    one text, each under the way it is walked ("A – B"), rather than kept
    as two fields tied to which end is the first, which changes when an end
    is moved. When changes are written back to UT.no, that is the thing to
    look at again.

34. **Time is Naismith's rule, and the numbers give a grading "at least"**
    (decided 2026-11-17). Both are worked
    out for each way from length and climb, and kept nowhere. Going down
    steeply adds time and counts half towards the grading. The grading a route carries stays one, as
    Turrutebasen has it; UT.no has one for each way, which is for when
    changes are written back. **Where nobody has set one, the grading its
    numbers give stands in**: the harder of the two ways, worked out when
    asked for and marked as such, and never written into the route.
    Kartverket is asked for it once the route is said to be done.
35. **A description is drafted from a fact sheet, and a person says it is
    right** (decided the same day, on having descriptions written by an assistant). The fact sheet is everything the data knows about a
    route as it is walked, and says what it does not know. Nothing is
    written into a route's description without a person having read it.
36. **The front door is the areas and the routes; signs are a part one
    chooses** (decided 2026-11-17). An area
    says what its routes add up to; a route shows length, climb, time,
    grading, profile and description, and no more; the
    statistics belong to the area as a whole. Nothing is changed from there. Later parts of operations
    come in the same way: a part to choose beside the signs. Looking only
    is the reader role, which was there already; no new role was made.

## Built so far, and not yet

Built: making a candidate yours; edges shared between routes that use the
same segment; adding another candidate as a further part; joining two loose
ends over water, over a glacier or on foot; changing code and name;
withdrawing. A route reports its shape, its ends and the places there, and
its length on foot, over water, over glacier and not marked. A place made
after a route takes over the nodes the route already ends at.

Turrutebasen records what each segment follows (`rutefolger`). The value
`VV` means water: 32 segments nationwide, among them the boat on Bygdin
(`jot96/2`, 29.4 km) and the boats on Femunden, Halnefjorden and Rondvatnet.
Such a segment becomes an edge of kind `water` when its route is made yours,
so it is never counted as walked. The boat on Gjende is not in Turrutebasen.

Departure from the first step as written: there is no `route_revision`
table yet. Legs belong to the route's working version; revisions arrive with
publishing.

Also built, with the amendments: bulk making of clean lines; the to-do list
per area; laying a stretch onto the mapped path; changing a stretch's kind,
marked and signed; taking a stretch out of a route in the viewer.

Also built: laying any stretch again between its two ends, through points
clicked by hand. By default the line follows the mapped paths: each point
within 25 m of a path is taken to mean the path, the line takes the way over
the paths between two such points unless that is more than four times the
straight distance plus 200 m, and runs straight elsewhere. Without
following it is straight from point to point (which is how a crossing gets
its real line). Existing lines are preferred to drawn ones: a route is
carried on along an existing stretch rather than given a copy of it; moving a node, at most 500 m at a
time and never onto another node, with every stretch that meets there
following; carrying a route on along a stretch that touches it, one of
yours or a Turrutebasen segment made yours on the spot.

Not yet: drawing a new stretch where none exists; changing maintainers by hand; joining ends other than the
nearest pair the panel proposes; proposals in bulk for stretches that
Turrutebasen says follow a road; publishing; height.

Roads (NVDB Vegnett Pluss) are loaded beside the paths, and snapping and
redrawing follow both as one network. Lines of that network join at their
ends and where one ends on the middle of another, so a path that comes out
on a road continues along the road.
