# To do

What is wanted and not built, by part. Each entry says who asked for it and
when, and what has to be settled before it can be built. What is being worked
on now is in `STATUS.md`; what is decided is in the ADRs.

## The order to build in (agreed 2026-11-19)

The route responsible listed what is missing and agreed to this order. Each
step is still started on their word; the details of each are further down.

1. **The route editor.**
   - *One way of snapping* (the first part built 2026-11-19: ADR-003,
     amendment 37; left: laying out between points by the same rule for the
     pointer, and the same look ahead in "Fix the line itself"), whatever is
     dragged (an end, the line, a new point): to a stretch of ours first, then a mapped path, then a road,
     shown before the pointer is let go. Today an end, a way round and a
     lay-out between points each have their own rules, and what the editor
     refused in use was each time one of them being stricter than the others:
     an end dropped 6 m from a mapped path with a short hop to it (feedback
     1), a way round that meets the route at one spot only (4), "the route
     already goes there" and "already ends there" (22, 25), a lay-out that
     would walk a stretch twice (13), a point 280 m from a path that was
     pulled onto it (14).
   - *A new route from A to B* exists ("New route between points"). Left:
     points to go by, and dragging the proposed line to another way before
     it is kept.
   - *Small gaps*: a button for taking away a stretch no route uses, the
     route panel listing the stretches that say otherwise than the route.
     (Making two places one in the page, feedback 23: built 2026-11-19.)
   - *The other areas* (ron, har, ska, fem, hul, osl) made ours in bulk.
     Operations is of little use where the routes are not ours yet. Done
     2026-11-20 for ron, hul, har and fem, for DNT and DNT Oslo og omegn
     only (198 routes, 84 places); the routes in pieces there are left for
     a person. Left: ska and osl.
2. **One table for what happens on a route** (the pattern of ADR-004). An
   issue, "cut here", "replace this sign or post", a diary entry, a report
   from a work party and an inspection are one kind of thing: an entry with
   a kind, what it is about (a spot or a span along a route, a sign, a post,
   a route), a state, who and when. First: issues marked on the map, with
   replacing a sign tied to the sign order. Then reports as entries that
   close issues. The diary is the timeline of a route.
3. **Photos and GPX.** A photo is placed by the position in its file and can
   hang on an entry or a sign site. A GPX track is a layer and something to
   compare a route with. Whether a route may be laid on tracks is not
   decided.
4. **The route card**: a page to print with what a work party needs, from
   all of the above: map cut-outs, profile, open work, signs, heritage
   sites.
5. **Who owns the ground.** v1 has the access and a module for it: to be
   read, rewritten and taken in. Seeing it is a role of its own (decided
   2026-11-19): owners' names are personal data. v2 needs its own access to
   the cadastre set up by whoever has the agreement; v1's settings are not
   read.
6. **A route planner**, in two forms (wanted 2026-11-19):
   - *For a walker*: a way from A to B anywhere in the country, preferring
     DNT's routes, by huts that are open and where the summer bridges are
     out, in days of a sensible length. Needs a graph of the whole country
     (Turrutebasen's segments with the mapped paths and roads, ours where we
     have them), and which huts are open and which bridges are out, which
     UT.no has and we do not (the API client asked for below).
   - *For work* (built 2026-11-19 as the feature `trips`: README, "Trips
     for work"; with walking instructions and paths and roads between
     routes since the same evening. Left: planning round what is known of
     a route, such as when it was last inspected and what is to be mended,
     once that is kept; going by a hut that lies off the way when none lies
     on it; several short trips from where one can park, in place of one
     long; heights for paths and roads off the routes. OpenStreetMap paths
     are walked between routes since the same evening, as a produced work
     with a credit). Through
     the assistant: "these routes are to be inspected:
     propose a trip that covers them". Covering given stretches with the
     least walked twice, in days that end at a hut. Needs only what is ours
     already, so it can come before the walker's.

Every step from 2 on is built as a feature of its own, behind a flag, as
ADR-005 says (decided 2026-11-19): its own module, endpoints, tools, tables
and part of a page, given to roles and people when it is ready for them.

Beside the steps, as the answers come: handing changes on to Kartverket and
to UT.no ("Handing the changes on" below). Without an answer: a work sheet
per correction for Rett i kartet and a mark that it is reported. For the
descriptions: whether UT-admin can export routes as a file, which beats one
page per route.

Before more is kept in operations: a command to put the second nightly copy
back (`tool.dump`: access, feedback and all of `ops`). The copy itself is
pushed off the server every night and was read back from a fresh clone on
2026-11-19.

## OpenStreetMap beyond the trip planner (a task of its own, noted 2026-11-19)

OpenStreetMap's ways are loaded for every DNT Oslo area and shown as a layer.
The trip planner walks its paths between routes (a produced work, credited).
What is not settled: whether the route editor may lay or snap your own routes
onto OSM lines, or a correction to Kartverket draw on them. ODbL is
share-alike for databases: owned data built from OSM may have to be offered
under ODbL too, which is at odds with handing it to Kartverket and UT.no
under their terms. To be looked into as its own question (what counts as a
derived database here, whether a line traced and then checked in the
terrain is), not decided by the planner.

## Routes and the graph

### The graph, as it stood on 2026-11-16 (the priorities: correct graphs, connected routes, signs to the edge of a glacier, every place named)

Checked over all 120 routes of ours:

- 119 are one line and one a round trip, counted the old way. Counted
  rightly, **jot66, jot88 and jot90 run double** between two junctions (11 m
  and 40 m, two Turrutebasen segments for one bit of path). The stretch too
  many was taken out of each on 2026-11-16, approved at the route responsible's word;
  Kartverket is asked to take the numbers off those three segments.
- One stretch stops 1.8 m short of its junction (jot91/2).
- 9 ends of routes in use are at no place: jot89 (Bessheim fjellstue lies
  there), jot90, jot98, jot105, jot108 (both ends; Sognefjellsvegen at one),
  jot91/1 (Veodalsvegen), jot116/1, jot116/2. Where nothing there has a
  name, a person names the place.
- All 81 places have a name. Three names are on two places each: Bøvertun
  (588 m apart), Tungestølen turisthytte (0 m apart) and Grotli hotell. To
  put right: withdraw one of the two, and its junction is then proposed as
  an arrival point of the other. Done 2026-11-17: Tungestølen and Grotli
  hotell are one place each (see "Two places that are one"), and the route responsible named
  the second Bøvertun "Bøvertun (Dumma)".
- Routes step onto or off a glacier at 6 junctions, none of them a place:
  bre8 (Fortundalsbreen), jot64 and jot86 (Fannaråkbreen), jot46/1
  (Piggebrean). A place of the kind "edge of a glacier" was made at each
  on 2026-11-16, approved at the route responsible's word: "<glacier> brekant nord" and
  "… sør". The names are the tool's; others may be given.

### Jotunheimen: what was sorted out on 2026-11-15, and what is left

Sorted out by the route responsible in the viewer, with the tools built for it that day:

- jot03 and jot04 (misspellings of jot3 and jot4 on one 80 m segment):
  withdrawn. Kartverket is asked to take the numbers off.
- jot3 is Gjendesheim – Glitterheim over Hestlægerhøe and jot4 the same by
  Tjønnholet; Turrutebasen has both on one line. jot4 was sent by Tjønnholet
  along the mapped path (8.5 km new, 21.5 km) and named so.
- jot1, Gjendesheim – Memurubu along Gjende, gone from Turrutebasen for the
  danger of rockfall: laid along the mapped path and said to be discontinued.
- jot61 is Turtagrø – Fannaråkhytta: carried on up to the hut, done.
- jot48 is Turtagrø – Skogadalsbøen: made yours, the way over Fannaråken taken
  out of it, its two pieces joined at Keisarpasset (16.4 km).

Left:

- The check for a line that goes out and the same way back was too wide when
  it was made (back within 20 m of itself): it found jot60 by Mansberg bru
  and jot73 below Fuglesteg, and both are hairpins of the path, 15 m and
  more between the legs, in routes the route responsible had said are done. Narrowed on
  2026-11-16 to the same line walked twice (within 6 m, for 60 m): it finds
  nothing in Jotunheimen now. jot48's way up to the junction at Keisarpasset
  and down again was put right by the route responsible on 2026-11-15 (16.2 km, done).
- jot49 is Keisarpasset – Skogadalsbøen in Turrutebasen. To ask the route responsible whether
  it is Fannaråkhytta – Skogadalsbøen; if so its end at the pass is dragged
  up to the hut (11.1 km).
- jot38, Leirvassbu – Krossbu over Smørstabbreen: laid out at the route responsible's request on 2026-11-15 (16.5 km, of it 8.1 km glacier), described in the route responsible's
  words and said to be discontinued. On paths where there are paths. **The
  line over the glaciers is a sketch** through the places the description
  names, from Stedsnavn; around Storebjørn it runs over rock close to the
  summit, which cannot be right. The route responsible is to draw it properly ("Fix the line
  itself", the glacier stretch, "Draw it by hand"), or give the points. The
  reason and date of the discontinuation are placeholders to correct.
- jot50: done at the route responsible's request on 2026-11-16. The 6 m stray by
  Sognefjellshytta ("Test turlag") is out of it, the code is jot50, and the
  route says DNT Oslo og omegn; three stretches keep what Turrutebasen says
  of them (DNT, and DNT with DNT Oslo og omegn). Kartverket is asked to take
  jot50 off the 6 m segment.
- jot53 is a round trip, unmarked: Skogadalsbøen by Vormeli and round, back
  to Skogadalsbøen (decided 2026-11-16). Turrutebasen has 2.5 km of it; the route responsible
  carried it on to Vormeli (7.7 km). Closed at the route responsible's request on
  2026-11-16 (10.8 km, the last 3.0 km new along the mapped path), said not
  to be published (kept for its history only, for now), and
  its own five stretches (8.3 km) said to be unmarked. Not done, and without
  a name of its own. Kartverket is asked to take jot53 off the five segments
  (2.5 km) Turrutebasen has it on (decided 2026-11-16).
- jot88: the route responsible's changes to its line on 2026-11-16 (sent another way twice, a
  junction moved) were taken back on request. It is as it was made,
  16.0 km in 17 stretches. What was wanted with it is not known.
- jot64 (from the road by Sognefjellshytta) and jot86 (from Krossbu) share their
  last 4.0 km and ended at one spot, at the edge of Fannaråkbreen. jot64 was
  carried on to Fannaråkhytta at the route responsible's request on 2026-11-16, in three
  goes; as it stands (11.3 km): from where it ended, 0.5 km along two
  mapped path stubs to the edge of the ice; 1.9 km straight over the glacier
  (at most 19 degrees, ice nearly all the way by Kartverket's terrain type)
  to the top of a third mapped stub, which the route responsible pointed at (FKB
  has it as a path); 0.5 km down that to the path
  from Keisarpasset; and 2.2 km up that path (jot49's stretches) past
  Fannaråknosi to the hut. The line over the ice is straight between the
  two stubs: the route responsible draws it where it goes. The two new path stretches say
  nothing yet of marked or signed. jot86 was carried on to the hut along the
  same stretches, at the route responsible's word: Krossbu – Fannaråkhytta, 13.2 km, sharing
  9.1 km with jot64 (they are two routes because they start at two huts).
  jot64's end by Sognefjellshytta is at no place, so it has no name.
- Kartverket's height service (`ws.geonorge.no/hoydedata/v1/punkt`) answers
  with the kind of terrain as well as the height (`SnøIsbre`, `ÅpentOmråde`).
  Used by hand for jot64. It would make glacier stretches something the tool
  can propose, and tell where a hand-drawn line leaves the ice, without the
  N50 glacier areas that were offered as a source.
- ron2 (Rondetunet – Bjørnhollia) was made ours by the route responsible on 2026-11-16, and at
  the route responsible's word every stretch of it says DNT | DNT Oslo og omegn (six said DNT).
- jot95, 49 m at Gjendesheim between two of the hut's arrival points: withdrawn
  for the route responsible on 2026-11-16 (it looked like something left behind). Kartverket is
  asked to take the number off its two segments: a withdrawn route under
  100 m is taken to be a number on a stray bit of line.
- Whether jot4 is to be discontinued: the route responsible is checking. Nothing is done to it.
- At Gjendesheim and eight other places, 13 arrival points that routes only
  pass are proposed taken out.

### The assistant (MCP): what is left

- Tried with Claude Code over the tunnel on 2026-11-16, looking only. Left:
  a change through a Claude client (dry first), a real token once sign-in
  is on, and a second person on Windows.
- From that trial: a route of ours does not take over Turrutebasen's name
  ("Tindevegen-Fuglesteg" for jot74), and nothing says why. The assistant
  noticed and could not explain it.
- `picture`: an image of a route on the map, for the assistant to look at.
- A tool for the terrain along a line (height, glacier or open ground) from
  Kartverket's height service, used by hand for jot64 on 2026-11-16.
- Try a custom connector in claude.ai / Claude Desktop with a token in a
  request header, which Anthropic's documentation says is enough (no OAuth
  on the server). The route responsible first, with a token of their own; then a second person.

### Handing the changes on: what was found out on 2026-11-16

The route responsible asked what it takes to get the changes to Kartverket and to UT.no,
possibly in several forms: everything as one GeoJSON, and instructions for a
person to enter them by hand at rettikartet.no if it comes to that. Looked
into, nothing built. Sources are in the two research notes of that day
(Kartverket's guide for route data v2.6, rettikartet.no, UT.no's help pages).

What exists: 230 corrections for Kartverket (47 lines to move, 63 numbers to
take off a segment, 34 to put on, 13 new segments, 70 route names, a few
others), 146 of them tied to a segment id, as a list and as GeoJSON; and 119
routes for UT.no as GeoJSON, 91 of which differ from Turrutebasen.

**Kartverket.** Only Kartverket edits Turrutebasen. Two ways in:

- *Rett i kartet*, the module for routes (rettikartet.no/app/turruter): a
  person draws a point, line or area or uploads a GPX, and fills in route
  name, number, grading, marking, signing, who maintains it and free text. No
  API and no bulk import was found, and no way of naming the segment id a
  report is about. UT.no's own help tells DNT's associations to report every
  new, changed or removed route there.
- *Files by agreement*: the guide (§3.6) says whole databases or files with
  routes can be sent by agreement with the county mapping office, and must
  follow the product specification (SOSI "Tur- og friluftsruter", version
  20171210). It also says Kartverket has an agreement on exchange of data
  with DNT. GeoJSON is named nowhere. Not found out: what that agreement
  gives, and which format they would take from us.

  So the question to Kartverket stands, and now has an address: the contact
  of DNT's agreement, or the county mapping office. To build once answered,
  or before for the manual way: a work sheet per route for Rett i kartet
  (where, what to draw, what to type, a GPX to upload where a line is new or
  moved), and a mark on each correction that it has been reported and when,
  so that it is not reported twice. A correction goes away by itself when an
  import shows it done.
- The product specification's `RUTEFØLGER` has no value for glacier (ST, TR,
  SB, US, TV, LT, GS, SV, SF, FO, BV, VV, IT), and `RUTEMERKING` has a third
  value SM beside JA and NEI. Our "change rutefolger from IT to (glacier)"
  has nothing to change it to.

**UT.no.** No documented API. The site is served by a GraphQL API
(`api.ut.no/v1/graphql`) that answers what its schema is and refuses to give
a route without a client; the schema shows that UT.no issues API clients,
and has reading, creating, updating and exporting of routes. The old
Nasjonal Turbase is gone. A route on UT.no has a code, a name, places A, via
and B, a description for each direction, grading and time for each
direction, distance, height figures, marking, and its geometry built from
Kartverket's segments (which is why Turrutebasen has to be right first).
Routes are made one by one in UT-admin. To do: ask UT.no for an API client
for DNT Oslo og omegn (post@ut.no), for reading the descriptions that exist
and, if they agree, writing changes; and look in UT-admin whether its CSV
export covers routes. A route page also carries its own data, descriptions
included, for whoever reads the page; UT.no's terms forbid automated
collection for commercial use, so that is the last resort and to be asked
about, not assumed. Ours has one description per route, UT.no one per
direction, and ours has no time or height yet.

**Height.** Built on 2026-11-16 (README, "How high a route goes"): per
line from Kartverket's point service, every 10 m, with the kind of ground;
what a route climbs and its profile in the panel and in the export. Left:

- Walking time is Naismith's rule since 2026-11-17, for each way, with time
  added for going down steeply, and the grading that length and climb alone
  give stands in where none is set. Not taken into account: glaciers, the
  ground underfoot, and rounding to what a sign says. Where "very steep"
  begins for the grading is our reading (12 degrees); ask whoever keeps the
  handbook. Gradings by the numbers are asked of Kartverket for routes
  that are done (decided 2026-11-17).
- **Bridges** (decided 2026-11-17). Not from what is loaded: the open path service
  gives a line and its kind only. Known today: named bridges, footbridges
  and fords in Stedsnavn, and bridges among Turrutebasen's info points; a
  dozen beside our 1,077 km. To look into: whether N50 Kartdata (open) marks
  a path or road on a bridge (`medium`), and its streams, so that a crossing
  with no bridge can be told; and UT.no's bridges, summer bridges among
  them, which need the API client asked for above (decided 2026-11-17: to be asked
  for in the same letter as the client). OpenStreetMap has footbridges and
  is not a source here (ODbL).
- The kind of ground is kept and only drawn (snow and ice shaded). It can
  check what a stretch is said to be: on 2026-11-16 jot64 had 1.6 km on snow
  and ice by Kartverket's ground, with the heights for part of it still
  missing. A check "said to be a path, and the ground is ice" (and the other
  way) belongs with the odd things to look at.
- The profiles of lines no longer in use are never removed. They are small.

**The description from UT.no.** Built the same day (README, "The
description UT.no has"): a button that reads the route's public page and
lets a person take the description. Left:

- Finding the page from the route's code needs UT.no's API (a client from
  post@ut.no). Until then a person pastes the address once per route.
- UT.no's grading words (TOUGH, …) are shown as they are and not turned into
  G, B, R, S: the full list of them is not known.
- One description per direction: here they are one text under two headings
  (ADR-003 amendment 33). To look at again if changes are to be written back.

### What a route is called: nobody hands out the numbers

To think through; nothing is decided (raised 2026-11-18).

The case that raised it: `jot91` in Turrutebasen is two routes that have
nothing to do with each other, 15 km apart: Gjendesheim – Bessheim (3.7 km,
named, and the one that fits the series, where jot88 to jot91 all end at
Bessheim) and Glitterheim – Veodalen (7.6 km, "Ukjent"). The first has been
given `jot91`; the second keeps the temporary code `jot91/1` until this is
settled. The same week Kartverket delivered a file in which every
`rutenummer` was "Ukjent", and jot90 turned out to be the stub of a route
whose number had been taken off most of it.

What there is today: no authority for route numbers, in DNT or at Kartverket.
`rutenummer` in Turrutebasen is free text, written by whoever registered the
route (`jot91`, `f_20170606`). Here a route has an id of its own that holds
(ADR-003, amendment 20), a code that is unique among ours and may be marked
temporary, a name that is given or made from the places at its ends, and
the codes and names it has had before.

Three ways it could go, which do not exclude each other:

- **The id is what the route is, and nothing else is.** A uuid, as now.
  Nothing has to be agreed with anybody, and it survives every renaming.
  But nobody can say it, print it on a route card or find it on a map, and
  Turrutebasen and UT.no have no field for it that we know of.
- **A registry of numbers.** Somebody hands them out: per area prefix as
  today (`jot`, `bre`), with rules for what happens to a number when a
  route is given up, divided or joined, and for variants of one route
  (jot3 and jot4, by Tjønnholet). Who keeps it is the question: each
  association for its own areas (which is what the uniqueness among ours
  already amounts to), DNT centrally, or Kartverket. Routes of others in
  the same area have numbers too.
- **From, to and via.** "Glitterheim – Bessheim, by Tjønnholet": what
  people say, and what the name is already made from. It needs no
  registry, but it changes when a place is renamed or an end is moved, two
  routes between the same places need a "via" to be told apart, and it is
  long for a key.

What it hangs on: what Kartverket keys a route on after the move to NVDB,
and whether `rutenummer` survives it; what UT.no keys on; and where a number
is shown to people at all (the signs carry none). A likely end is all three,
each for its own use: the id in every exchange of data, a short number for
people from whoever keeps the registry, and from, to and via as the name.

### Two places that are one

Built 2026-11-19 (README, "Round a place"): the panel of a place lists the
other places near it or of its name, and takes one in; and the route ends
near it that are not its arrival points, to be made arrival points or ended
where the routes arrive. Asked for on 2026-11-17 (feedback entry 23), after
Tungestølen and Grotli hotell each were two places of the same name and the
page had no way of making them one. Left: the same as a proposal in the
to-do list where two places share a name, and joining two junctions that
lie a few metres apart from the panel (today: drag one onto the other in
"Fix the line itself").

## Operations (drift)

The part that is about what happens to routes, as opposed to what the routes
are: signs, work, a diary, closures. It is to be a layer of its own on top of
the routes and the graph. Not started.

How it is meant to sit (recommended 2026-11-15, not yet confirmed by the route responsible): one
database and one login; `core` (places, junctions, routes, stretches) written
only with the route-responsible role; operations in a schema and database role
of its own that reads `core` and cannot write it. What it holds points at
`core` by id, keeps a copy of what it saw (name, position, distance), and asks
`identity.current` before use (ADR-003, amendment 20).

### What the route responsible decided on 2026-11-17

Asked how the operations layer is to sit beside the routes, and where signs
are handled. Not built: the first step is to be proposed and agreed.

- **One service, one sign-in, one database; operations as a page of its
  own**, not a mode of the route editor. The two share the map, the API and
  the assistant. Address: see below, not settled.
- **Start from the graph**, not from v1's data: sign sites and what the
  signs say are worked out from the routes as they are now. v1 is to be read
  and what is good in it taken over, what is weak made better.
- **The sign order has the same form as v1's**: one row per sign, with
  Antall, Tekst på skiltet, Km, Pilretning, Baksidetekst, Skiltfarge, Ruter,
  Sendes til (`services/sign_excel.py` in v1; the back text is the place's
  name and its UTM 32V position to 10 m).
- **A role of its own for operations, with overlap**: a person may have
  both. The one who orders signs or reports work does not change routes
  unless they are also route responsible.
- **v1 is replaced function by function**, and lives until then.
- Wanted, as v1 has them: sign sites and signs with the direction set by
  hand, the order taken out; route cards; work to do; GPX uploaded; reports
  from work parties (dugnad); something like an issue tracker.
- Asked and not answered: can the whole overlay be exported as GeoJSON and
  read in again, as a backup? Answered: the backup is the nightly copy of
  the database (complete, with history), which lies on the same server only
  and has been put back on the test database only. A GeoJSON bundle both
  ways can be built as a readable second copy; it would not carry history.
  Recommended first: a copy off the server, and a restore tried on a copy
  of the real data.

What v1 keeps (read from its source on 2026-11-17, `services/
operational_store.py`): a sign site (position of its own, the junction it
stands at, name, back text, who the signs are sent to, a status proposed /
accepted / rejected / installed, a site code); per destination a sign with
direction, colour (trehvit, grønn), distance and status; and one table for
everything that happens to a route (`route_annotations`: diary, inspection,
dugnad, work of five kinds, with a position along the route, a date, who,
and when it was resolved). Photos from the field with tags, and GPX tracks
that are compared with the route to measure how much longer the walked way
is than the line.

How v1 works out signs (read from its source the same day; `services/
signs.py`, `sign_candidates.py`, `sign_pdf.py`, `route_card.py`,
`signs_app/src`; `MapView.tsx`, most of `App.tsx` and `api/routes.py` not
read):

- A site is proposed at every end of a route and every junction of three
  ways or more; sites of the same name within 100 m are one. A person can
  add a site at a free point on a route.
- From a site each of its routes is walked both ways, and **a sign is made
  for every route end on the way**, not only the next hut. The rule decided on 2026-11-16 is the next hut or parking only: that is the rule to build.
  Further destinations are added by hand per site ("through"), with the
  distance over the whole graph.
- Distance: metres along the route times a factor per area (1.05, measured
  from GPX tracks), under 10 km cut down to the half kilometre, from 10 km
  rounded to whole. Over a glacier or by boat the distance stops at the
  edge: "X via …" and "(til brekanten)" or "(til båt)". No time on signs.
- Direction is never worked out: a person picks venstre, rett fram or høyre.
- Per sign a person can set direction, colour, distance and text. These,
  and which sites are accepted, are kept by v1's junction and link numbers:
  when those change underneath, they fall away without a word.
- Nothing is frozen and nothing records that an order was taken out; a
  changed route changes an accepted sign's text unseen.
- Orders: the Excel list, all or a selection; and a field sheet as PDF, one
  page per site (name, code, routes, map, UTM, back text, table of signs,
  up to three photos within 50 m).
- Route card: printable page with lengths, climb, Naismith, map, profile,
  heritage sites, photos, diary, inspections, dugnads, open and solved work.
- Field page for a phone (map, photos, work), using the phone's position;
  not usable without coverage. No roles: everyone signed in changes all.

To carry over: the rounding and the factor, stopping at the glacier edge,
proposed / accepted / rejected / installed with site codes, what a person
sets per sign, same-name sites as one, one table for what happens to a
route, the photo handling, the field sheet. To do better: point at
junctions and places by ids that hold, freeze what is ordered and keep the
order, history, roles, the rule for destinations, a direction proposed
from the map.

### Signs: built on 2026-11-17, and what is left

The answers that day: the front door `/` is operations and the route
editor is at `/ruter`; the factor 1.05 stands; the arrow is worked out from
where the post stands, the post can be moved to the other side, and a place
can have several posts (Nørdstedalseter). Built as described in the README,
"Operations: signs". On the real data Breheimen comes out as 63 sites and
178 signs; nothing is accepted yet.

Left, of what v1 has and of what was said:

- Photos. (The field sheet is built since, as a page to print: v1 made a
  PDF with reportlab and Pillow, neither of which is installed here.)
- Chosen by the route responsible from a review of the page on 2026-11-17 and built: signs
  linked to the map, a button for signs that hang on two posts of a place,
  "it is up" after an order, a field to find a site and stepping through
  them, the field sheet, and the order of signs on a post. For the last, shortest
  sign on top was remembered; the sign manual says "Skilt til høyre
  samles og skilt til venstre samles. Skilt som viser til samme retning:
  Lengst avstandsangivelse monteres øverst, kortest nederst", and that is
  what is built. The manual also says a name sign carries no logo; the one
  at Viva has the T, and the page draws it with the T.
- The states of a sign site (proposed, accepted, installed) were taken away
  at the route responsible's word the same day: a site only says "no signs here", and gets
  its code with its first order.
- Not chosen yet, from the same review: less text and fewer buttons per
  post, the map showing which sites have something on the order, a guide
  for the page, the route panel in folds, the standing red warnings, what
  actually hangs today (photos), a page for the phone.
- Ordering was turned round on 2026-11-17 at the route responsible's word (most signs hang
  where they are to hang, so an order is mainly choosing which signs are to
  be replaced): signs are clicked and added to the order, and the order is
  what is marked, not the signs of accepted sites. Left of that thought:
  nothing records what hangs on a post today, as against what the routes
  give (the picture of Arentzbu shows they differ). "They are up" is said
  per site only. Why a sign is ordered (`why`) is kept and has no field in
  the page yet.
- Done 2026-11-17 after the route responsible's picture of the post at Viva: a sign with the
  place's name on top of a post, said per post; and a post near a hut
  points on to where the hut's signs point (under 5 km to the hut, under
  25 km in all, or thereabouts). The two numbers are there to tune: with
  them Viva comes out as the post in the picture. At end points only (a
  place where a route ends), never at a junction on the way (decided the same day).
  The edge of a glacier has no part in it either.
- Done 2026-11-17 at the route responsible's word, after a picture of the post at Arentzbu:
  a destination added by hand to a post (nearer than the next hut, like
  Åsete, or further on), two signs where two routes reach a place by
  different ways (bre8 and bre9 from Sota sæter to Nørdstedalseter), and a
  comma in the distance, also in the list for the maker. The mountain signs
  are ordered with kilometres.
- A guide for the page.
- Done the same day at the route responsible's word: a sign site at a spot on a route, the
  sign tools for an assistant, under 500 m to the hundred metres ("0,2 km"),
  and Nørdstedalseter as one place with a post at the hut and one at the
  junction by the road (the junction made an arrival point of the place;
  posts of one place are one post only within 30 m of each other, not 100).
- The route responsible saw a sign to Nørdstedalseter on "the post at Nørdstedalseter". That
  was the junction 60 m from the hut while it was a site of its own. A post
  never points to the place it belongs to; a junction near a hut that is
  not an arrival point of the hut's place does point to the hut, and should
  perhaps be proposed as an arrival point (the proposals do that within
  150 m only for route ends).
- The factor is one number, not one per area measured from GPX.
- The front page is a file per part and the two pages share their helpers,
  the base map and the height profile (README, "The pages"). The route
  editor is still one file of 2,000 lines. A database role of its own for
  operations is not made: the API role writes `ops`.
- The walk through the pages in a browser (`tests/browser`) is run by hand
  from a machine with Chrome; nothing runs it by itself. It does not try
  anything that changes data: ordering a sign, dragging a post, editing a
  route are still tried by hand on the test database.
- Copies off the server (2026-11-17): the nightly copy is pushed to a private
  git repository. Not covered: the imported sources' older versions
  (`source_raw`, 5 GB, too big for git, and upstream serves only the latest),
  and there is no command to put `tool.dump` back (who may sign in, feedback,
  what is decided about signs): it goes in with `pg_restore` by hand.
- How long the editor's look-ups take (measured 2026-11-17 on the real data, before and after):
  the to-do list of jot 8.7 s, now 4.2 s the first time and 1.7 s after;
  of bre 4.8 s, now 2.1 s and 1.1 s; the report of bre 5.2 s, now 1.4 s;
  proposals for jot 3.8 s, now 2.4 s and 0.5 s; corrections for everything
  4.8 s, now 3.3 s. What was done: stretches that cross are found boxes
  first, how much of a route lies by mapped lines is worked out per stretch
  and kept while the process runs (`domain/kept.py`), as is what trying a
  snap gave, a route with nothing to mend is not looked up in full, and
  statements are not compiled first (`jit=off`). What is left is mostly
  `routes.detail` once per route (about 20 ms each), and "Compare with the
  mapped paths" for a whole area, which takes 19 s for jot and is only run
  when its fold is opened.
- The front door (areas, routes, one route; signs as a part to choose) was
  built on 2026-11-17, see the README. Left there: the areas have no names
  beyond their three letters; an area's numbers and list are worked out on
  each request (0.4 s for bre, 0.8 s for jot); the list is not brought up
  to date while the page is open when somebody changes a route.
- 9 route ends at no place give signs with no name; an order refuses them.
- A decision follows its sign when the routes are mended, what no longer
  finds its sign is shown as adrift, and decisions about signs can be
  undone (2026-11-17, ADR-004). What is left of it is under "Left" there.

### Bridges from UT.no, and how a sign looks: looked into on 2026-11-17

**An export of bridges from UT-admin** (a CSV file; not copied anywhere): 241 places of the type Bro, all DNT Oslo og
Omegn's and all published. Columns: id, status, name, type, who is
responsible, area, municipality, county, made, last updated, and the
addresses on UT.no and in UT-admin. **No position.** The public page of a
bridge (`ut.no/sted/<id>`, one fetched) has name, description, photos and
area, and no position either.

- Whether a bridge stays out all year is in the name only, by a habit that
  is mostly kept: 152 end in helårsbro (or -bru, bilbro), 65 in sommerbro,
  22 say neither. That is the one thing about bridges no map has.
- Where: Jotunheimen 54, Hardangervidda 44, Breheimen 19, Skarvheimen 23,
  Femundsmarka and Rondane some 30 each, the rest spread.
- Kept up: 240 of 241 were last changed in 2026.
- Finding them by name was tried for Jotunheimen and Breheimen (73): 34
  have one thing of that name in Stedsnavn within 400 m of a route of ours,
  4 have several, 35 none. What is found is the name of the stream, some 50
  to 350 m from the route, not the bridge; "øvre" and "nedre", "1" and "2"
  land on the same spot. Good for "which route", not for a position.

Advice given: not worth importing by name. Worth it once positions come
with it, and then as something read from UT.no and never changed here (kept
by UT.no's id, refreshed, a bridge that is gone there goes here), since
UT.no is where DNT Oslo keeps them and where the public sees them: a second
place to keep them up is how they come apart. Ask UT.no for positions in
the export, or for the API client. If bridges are later to be worked on
here (taken in, put out, inspected), that is the operations layer's, and
then the question of who writes to whom has to be answered first.

**The sign manual** (Skiltmanual 2023, merkehandboka.no; 27 pages, read as
text): green is the rule (RAL 6005, white text and arrow); "i utsatt
terreng, slik som i høyfjellet … kan det fungere bedre med trehvite skilt",
which "har oftest svart skrift og svart pil". A sign has a square walker
symbol, who is responsible for it (logo, or the name under the symbol at 15
to 20 % of the text size), the destination, the distance and the arrow. On
a sign pointing right in that order; pointing left the other way round, so
the distance is always next to the arrow. Height 100 to 120 mm, length 550
to 700 mm, a grotesque typeface. Distance in km with one decimal under 10 km
and none from 10 ("Strømsvika 1,1 km", with a comma); the manual does not
cut down to the half kilometre, v1 and we do. The symbol may carry the
grading's colour.

The route responsible sent a picture of a post in Breheimen (2026-11-17; by its seven signs it
is Arentzbu): planks of weathered grey wood with square ends, at the end
the arrow is at the red T in the ring "Den Norske Turistforening", then a
small black arrow, then the name in black italics. No distance on any of
them. The page draws a wood sign like that now, and a green one as the
manual has it (arrow, distance, name, the walker at the far end); the
distance has a comma, as a sign has. The Excel list still has a full stop,
as v1's: asked the route responsible whether it should change.

What the picture says about the signs themselves, against what is worked out
for Arentzbu (Fast, Hyrnavollen, Mørkri, Fagredalen, Sprongdalshytta,
Nørdstedalseter):

- Three are the same. Two are spelt otherwise on the wood: Hødnevollen for
  Hyrnavollen, Mørkris for Mørkri. A text by hand does that; or the place is
  named as the sign has it.
- bre20's sign says Fåberg, and the route ends at a place called Fagredalen
  here.
- There is a seventh sign, to Åsete, which lies on the way to Fast and is no
  hut or parking place. **A destination added by hand to a post** is wanted
  for that (nearer than the next hut) as well as for v1's "through"
  (further than it).
- The signs carry no kilometres. Whether an order is to have them is the route responsible's
  to say: v1's list has the column.

### Notices on a route: closed, diverted, warning

Asked for by the route responsible 2026-11-15. **Not to be built yet.**

The case: bre59 is temporarily closed because of works at the dam at
Skålavatnet. There is nowhere to record that today. A route is either active
or withdrawn, and withdrawing it is wrong here: the route would drop out of
the network and out of the export.

What is wanted:

- A notice attached to a route, or to one stretch of it where only a part is
  affected.
- Kind: closed, diverted or warning. A text ("Works at the dam at
  Skålavatnet"). From a date, until a date that may be left open ("until
  further notice"). Who entered it.
- The route and its line stay as they are. The notice shows as a mark in the
  route list and on the map, goes out with the route in the export for UT.no,
  and lapses by itself when its end date has passed.
- History is kept: that the route was closed in 2026 can be seen later.
- The route responsible sets it in the same map as the routes, although it
  belongs to this layer and not to the graph.

To settle first:

- Does the closure of bre59 cover the whole route or only the stretch at the
  dam? (Decides whether notices on stretches are needed from the start.)
- Is the opening date known, or is it "until further notice"?
- Should a closed route still be offered by the planner ("lay out between
  points"), and how does a closed stretch show on the routes that share it?
- What UT.no wants a closure to look like in the export.

### The rest of v1's functions

Named by the route responsible 2026-11-15 as what comes when the data is right: a separate
front end with what v1 has for signs, a diary, work to do, route cards
and the rest.

- **Signs.** A sign stands at a junction or a place and has a position of its
  own: the post does not move when a line is repaired. What it says
  (destinations, distances) is worked out from the graph and frozen in the
  order when the order is sent. The one who orders signs does not change
  routes; an error seen in a route is reported as a note on the map that
  lands in the route responsible's list.
  The route responsible asked on 2026-11-16 whether the geometry is good enough to work out
  the signs of Breheimen, by the rule: a sign at every end point and every
  fork, pointing only to the next hut or parking, never past it, never to a
  further fork. Worked out on the real data, read only, nothing built: yes.
  All 41 routes can be walked end to end in order, every one of the 26
  loose ends is at a place, and there are 23 forks. Read by route (at each
  spot, for each route through it and each direction, the next hut or
  parking on that route, else the end of the route) it gives 222 sign lines,
  half of them under 8.7 km and none over 24.3 km. Read by network (every
  hut that can be reached that way without passing one) it gives up to six
  destinations on an arm and up to 71 km, which is not what a sign says.
  To settle or mend first:
  - 17 of the 40 places are neither hut nor parking (Grotli hotell,
    Fagredalen, Mørkri, …) and one only is a parking. 46 of the 222 lines
    point to such a place. Which of them are destinations for a sign?
  - Two places are both called Bøvertun (a hut and an "other"), and three
    places have two arrival points on the routes (Turtagrø hotell,
    Tungestølen turisthytte, Bøvertun): distance to which?
  - Time: a sign says hours, which needs height along the route (decided,
    not built).
  - Routes of Breheimen go on to places of Jotunheimen: to be worked out on
    the whole network, not area by area.
  - Which routes get signs: not the unmarked (bre8 over the glacier), the
    discontinued or the unpublished.
- **Work to do** and **a diary** per route or area.
- **Route cards.** Need height per route (decided: Kartverket's point API,
  after repair; not built).
- When `core` changes under something here (a junction moved, a route re-laid),
  what points at it is flagged "check", worked out on request.

Needed before any of it:

- A read API that is stable enough for a separate frontend.
- Logins and roles. Today the editor is one name in the server's settings
  behind an SSH tunnel.
- The format of v1's data, to carry over what exists (signs, diary entries,
  GPX tracks in `ops.gpx_tracks`). v1's database has not been looked at.
