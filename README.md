# Stiflyt v2

A validated, owned dataset of Norwegian hiking routes: geometry, metadata and
the graph of how routes connect. Source databases (Turrutebasen,
FKB-TraktorvegSti, GPX, others) are imported as inputs; the authoritative
layer is ours.

Status: the import side is built. Seven sources load as immutable snapshots
with a diff per delivery: Turrutebasen, Stedsnavn and Kulturminner nationwide,
property parcels (Teig) and roads (NVDB Vegnett Pluss) per municipality, and
paths from an open WFS and OpenStreetMap ways per bounding box. OpenStreetMap
is there to look at and compare with (a layer that is off until switched on);
its licence does not let its lines be taken over. A search and map index is built from them, and a read-only map
viewer shows it. Height is sampled per route later, when routes are repaired,
and is not an imported source. The owned
data has begun with places (ADR-002) and routes with crossings (ADR-003);
snapping, publishing and the rest of the editor are not started.

## Development

Development and tests run on a server (checkout in `~/stiflytv2`) against
its PostgreSQL 16, PostGIS 3.4 and GDAL 3.8.

```bash
.venv/bin/pytest                      # uses the throwaway database stiflyt2_test
.venv/bin/stiflyt2 sources list       # what is loaded, and when it was last checked
.venv/bin/stiflyt2 import turrutebasen
.venv/bin/stiflyt2 import stedsnavn
.venv/bin/stiflyt2 import kulturminner
.venv/bin/stiflyt2 import teig 4644   # one municipality
.venv/bin/stiflyt2 import vegnett 3435   # roads of one municipality
.venv/bin/stiflyt2 import osm jot 105000,6795000,180000,6870000   # OpenStreetMap, to look at
.venv/bin/stiflyt2 import fkb-wfs bre 60000,6820000,130000,6880000
.venv/bin/stiflyt2 refresh            # check everything loaded, import what changed
.venv/bin/stiflyt2 sources verify     # kept original files against their checksums
.venv/bin/stiflyt2 index status       # is the search and map index current
.venv/bin/stiflyt2 placenames search arentzbu
.venv/bin/stiflyt2 routes search bre1
```

**The pages** are plain files under `src/stiflyt2/api/static`, with no build
step; the service reads them from disk at each request and tells the browser
to ask every time. Scripts are loaded one after the other and share their
names:

| File | What it is |
|---|---|
| `drift.html`, `front.css` | the front page: its markup and its styles |
| `front/page.js` | where one is in the front page, what has been fetched, what the side shows |
| `front/map.js` | its map, and what a click on it means |
| `front/routes.js` | the areas, an area, a route |
| `front/signs.js` | the signs: sites, posts, the order and its basket |
| `front/start.js` | what happens when it opens |
| `index.html` | the route editor, still one file |
| `common.js` | for both: elements from data, asking the server, moving the map |
| `basemap.js`, `profile.js`, `profile.css` | for both: the base maps, and a route's height profile |

A further part of operations gets a file of its own under `front/`.

**A walk through both pages in a real browser**, with real clicks, is
`tests/browser/smoke.mjs` (Node 22 and Chrome on your own machine, nothing to
install). It changes nothing: every request but GET is stopped before it
leaves the browser. `tests/browser/run.sh` starts a viewer on the test
database on the server, opens a tunnel and runs it:

```bash
STIFLYT2_SSH=<user>@<server> tests/browser/run.sh
STIFLYT2_TOKEN=… node tests/browser/smoke.mjs http://localhost:8100/     # a service with sign-in, read-only
```

A change to a page is live as soon as its file is in the checkout the
service runs from. So a change is tried in a second checkout first
(`STIFLYT2_DIR=stiflytv2-try`, with `.venv` linked and `.env` copied from the
first, and `PYTHONPATH=src` for pytest), and copied to the first when the
tests and the walk-through pass.

A source, municipality or box is added by importing it once. After that
`refresh` keeps it current: it asks the source for the file's date and size
and downloads only when they differ from the active snapshot. A delivery that
fails to load, loses more than 20 % of a layer or changes more than half of
one is refused and changes nothing; `refresh` then exits with status 1.

`refresh` ends by bringing `source_index` up to date. That index is what
search and, later, the API and map read. It is derived data: `stiflyt2 index
build --force` rebuilds all of it from the stored snapshots.

On the server `deploy/refresh-nightly.sh` runs `refresh` from cron at 04:30
UTC. An OpenStreetMap box checked within the last seven days is left alone
unless `refresh osm` asks for it by name. `refresh` also removes the original files of all but the newest 14
snapshots per source and scope (`STIFLYT2_KEEP_ARTIFACTS`; 0 keeps all). The
database keeps every snapshot in full.

## Map viewer

A read-only page over `source_index`: the sources on Kartverket's basemap,
search for places and routes, routes per area, and for a selected route how
much of it lies within 20 m of a mapped path, the heritage sites within 50 m
and the parcels it crosses. Click the map to see what is there.

It also shows where routes start and end. Every route end belongs to an
anchor: a hut or parking place nearby, or the junction where it meets other
routes. An anchor is green when all routes arrive at one spot and orange when
they end apart, with a line from each end to the anchor. Choosing an area
lists its huts and parking places, most scattered first, and marks the routes
that are not one clean line.

It runs on the server on `127.0.0.1:8100` only, started by hand with
`deploy/viewer.sh start` (also `stop`, `restart`, `status`). It does not
start by itself after a reboot. From your own machine:

```bash
ssh -N -L 8100:127.0.0.1:8100 <user>@<server>
```

and open <http://localhost:8100/ruter> (the front page, `/`, shows the areas,
the routes and the signs: see "The front door"). A selected route is part of the address, so
a view can be bookmarked. The API behind the page is under `/api/v1` and is
described at <http://localhost:8100/api/docs>. It connects as `stiflyt2_api`,
which can read the index but not the stored snapshots, and every query is
read-only with a 15 second limit.

### The list and a route

The side shows the list of an area's routes, or one route. Opening a route
puts the list aside; the bar at the top of the route, which stays in view,
leads back ("All routes", or Escape) to the list where it stood, with the row
that was open marked. The two arrows in the bar open the route before and
after it in the list, as it is filtered.

### Making places yours

A hut or parking place in the viewer is a proposal until you make it a place:
click it and choose "Make this a place". The place then belongs to the owned
data, with an access point for each spot where routes arrive and connectors
joining them. In its panel you can rename it, move its reference point, add
or take out access points and choose the primary one. Every change is
recorded with its author in a changeset; rebuilding the index never touches
it. The model is described in
[ADR-002](docs/architecture/ADR-002-places-access-points-connectors.md).

### Making routes yours

A route in the viewer is a candidate from Turrutebasen until you make it
yours: select it, give it a code and choose "Make this route yours". Its
segments become owned stretches, shared with any other route of yours that
uses them. Where Turrutebasen files several parts under one code, add the
parts that belong together and join their loose ends: over water (connected,
but never counted as walking distance), over a glacier (walked and signed,
never marked) or on foot. See
[ADR-003](docs/architecture/ADR-003-owned-routes-and-crossings.md).

`stiflyt2 routes make jot` makes every clean candidate of an area yours in
one go: those that are one line with two ends. They are marked as not
reviewed. Choosing an area in the viewer then shows its to-do list:
candidates in pieces or with extra ends, routes lying far from any mapped
path, routes without a name, temporary codes, and places where routes arrive
at several spots.

An open route is changed on the map itself. There are two ways, kept apart.

**Changing the route** is what a drag does to begin with. It changes only
which stretches the route uses; no line is moved and no other route changes.

- **Drag an end** (the large dots) to where the route should end. It is
  carried on along the shortest way from the nearest junction of the route;
  let go on the route itself and it is cut back to there. Arrival points of
  places light up while you drag: let go on one and the route ends there.
- **Drag the line** to send the route another way: the stretch you took hold
  of gives way to the way round by the spot you let go on.
- **Click a stretch** to take it out of the route or say that someone else
  maintains it there. Click a line that is not part of the route to carry the
  route on along it.

**Fixing the line itself** is asked for ("Fix the line itself"), because a
stretch is shared by every route on it. A badge on the map says so while it
is on.

- **Drag a white dot** to move a junction. Dropped on a mapped path or road
  it lands on it; dropped on another junction the two become one.
- **Drag the line** to pull it over to a path or road. Only the line near
  where it was grabbed changes.
- **Click a stretch** to lay all of it on the path or road, divide it, say
  what it is (a glacier is signed and never marked), or draw it by hand.

In both:

- **Undo**, a button at the top of the map (and Ctrl or Cmd with Z), takes the
  latest change back, whatever it was: a pulled line, joined junctions, a
  route made, a batch of approved proposals. Pressed again it goes further
  back. It is kept on the server, so it works after the page is loaded again.
- **Done with this route** says the route is right as it is, and closes it.
  Nothing else marks a route done: changing it does not.

Two parts of one code that were made routes of their own (either side of a
glacier, say) become one: open the one, and under its shape choose "Add" for
the other part. The gap between them then offers "Join over glacier".

Choosing an area lists every route in it, yours or not, with where it
stands: needs work (and what is the matter), to check, done, or not yours
yet. The list can be narrowed to one of those. A route is opened from the
list or by clicking its line; where several routes share the line, the map
asks which. Below the list, folded away: findings in the network, places,
what to tell Kartverket, and the numbers for the area. A route with more than two ends lists each end with its distance
from the fork and offers to take that end out.

### A route laid out between points

"New route between points" (in the area section) makes a route without a
Turrutebasen candidate behind it: click where it starts and where it goes,
and it is laid along the shortest way over your stretches and Turrutebasen's
segments. More clicks steer it. The way is shown before anything is saved;
give it a code and make it. A segment on the way that was nobody's stretch
becomes yours then, and only then. "Lay it out again between points" in a
route's panel replaces that route's stretches the same way, keeping its code,
name and what is said about it.

A click near a junction or an end means that junction. A click elsewhere
means the spot on the nearest stretch, within 300 m, and the stretch is
divided there: that is how a route stops at a hut that lies beside the middle
of a stretch. Every route that used the stretch then uses both parts. A
stretch can also be divided by hand: click it, "Divide it here". An end
beside a hut that is no place yet offers to make the place.

### What is said about a route

Who maintains a route and its grading belong to the route: "Change name,
code, maintainer or grading" in the route panel sets them once for the whole
route. A stretch says who maintains it only where that differs along the
route (click the stretch, "Someone else maintains it here"). What is physical
stays on the stretch and is shared by every route that uses it: the line,
marked, signed, what it follows. `stiflyt2 routes fill-metadata` gives routes
that say nothing what Turrutebasen has for them.

### The map data of an area

An area rests on mapped paths and roads to lay routes on, and OpenStreetMap
to compare with. Paths and OpenStreetMap are fetched per bounding box, roads
per municipality. The area section says how much of the area each covers.
`stiflyt2 import area ron` fetches what an area lacks: it works out the box
from where Turrutebasen's routes with that prefix lie (2 km to spare) and the
municipalities from the place names along them. What is fetched once is
checked again every night; OpenStreetMap once a week, since its service is
shared. With an area chosen the section says, per kind, whether it is loaded,
in part or not, and when it was last checked.

A click on a mapped path, a road or an OpenStreetMap way says where the line
comes from, under what licence, and whether routes can be laid on it.

### Is the graph right?

What signs and distances are worked out from is the graph: every route one
connected line (or a round trip), every end at a place with a name. Three
things watch over that:

- **The shape of a route**: one line, a round trip, in pieces, with extra
  ends, or `doubled`: it runs along two stretches between the same two
  junctions, as where Turrutebasen has two segments for one bit of path. Every
  shape but the first two is in the to-do list, and what can be mended in one
  step is under "Proposed repairs" (the stretch too many taken out, a short
  extra end taken out, pieces lying close joined).
- **Ends and places**: a route's end at no place is in the to-do list, and
  where something there has a name, a place of that name is proposed. Where a
  route steps onto a glacier or off it, a place is proposed too, of the kind
  "Edge of a glacier": somewhere a sign can point to.
- **Odd things to look at** also finds a stretch that stops short of the
  junction it hangs in (a hole in the line) and two places with one name.

`stiflyt2 report jot` gives the shape problems of an area, and
`stiflyt2 checks jot` the odd things.

### A round trip

A route may start and end at the same junction: jot53, from Skogadalsbøen by
Vormeli and round, back to Skogadalsbøen. Lay it out as far as it goes, then
drag its end onto its own start. The hint says "Let go to make it a round
trip"; let go, and the route is carried on to its start by another way than
it came (stretches it does not use, else the mapped paths and roads), with
nothing cut. Let go anywhere else on the route itself and it is cut back, as
before.

A round trip is a shape of its own: nothing in the to-do list is asked of it
for that. It has no ends, so it is not named after them (type a name), and
its ends cannot be dragged nor its line sent another way. Click a stretch and
take it out, and it is a line again, with an end at each side: change it, and
close it again.

### Carrying a route on where no stretch leads

Dragging an end follows your stretches and Turrutebasen's segments to where
it is dropped. Where none leads there, the route is carried on from that end
along the mapped paths and roads instead (the nearest within 150 m of the
drop), and that way becomes new stretches of yours: one for each run of paths
and each run of roads, drawn along the mapped lines and saying which they
follow. It ends at a place's arrival point or a junction of yours if one lies
where it was dropped. Whether the new stretches are marked and signed is not
known until you say so.

The same holds for the line: dragged to a spot that no stretch leads to, the
route goes there along the mapped paths and roads. From the spot the mapped
lines are followed towards each end of the route until they meet it; between
those two meeting points the route takes the new way, and before and after it
runs as it did. The stretches it leaves stay for the routes that share them.
This is how one of two routes that Turrutebasen has on the same line is given
its own way (jot4 by Tjønnholet, where jot3 goes over Hestlægerhøe).

A drop that is not on a stretch, and has a mapped path or road clearly nearer
it than any stretch, means that path.

"New route between points" does the same: a click with no stretch near it
means the mapped path or road it was put on, and two points that no stretch
joins are joined along the mapped paths and roads. So a route can be laid
where Turrutebasen has nothing, with a click or two along the way it is to
take (jot1 along Gjende: with only its two ends clicked, the shortest way
over what exists is jot2's, over Besseggen).

A click where nothing at all is mapped (no stretch within 300 m, no mapped
path or road within 150 m) is the point itself: the way goes straight there
and on from it, and such points in a row become one stretch "in the terrain",
at most 10 km from one point to the next. That is how a route is laid over a
glacier or open ground; afterwards the stretch is clicked and said to be what
it is ("Change what it is": a glacier, not marked).

Such a stretch is a line of straights. With its route open its corners
show as small dots, and it is pulled like a rubber band, in either mode
(there is no other way to send it): a corner that is
dragged moves, the line dragged between two corners gets a new corner there,
and a corner let go on the one next to it goes away. It can be pulled up to
10 km at a time; a stretch on a path is pulled 500 m at most, and is laid on
the mapped path where it lands.

### What a route is like, in words

A route can carry a description: where it goes when nothing on the ground
shows it, what it asks of the one who walks it. It is written in the same
form as the name ("Change name, code, maintainer, grading or description"),
shown under the name, and handed on in the export for UT.no.

### The description UT.no has

UT.no shows each route on a page of its own, with a description for each
direction. "Fetch the description from UT.no", in a route's panel, reads
that page and shows what it says: the route's code and name there, the
places at its ends, length, grading, and the two descriptions as one text,
each under the way it is walked. Nothing is changed until you choose "Take
it as the description"; the text can be changed first, and Undo brings the
old description back.

Which page is the route's cannot be looked up: UT.no has no search that is
open to a program (its API wants a client that UT.no issues). So the first
time, find the route on UT.no and paste the address of its page. The number
in it is kept with the route (`core.route.utno_id`, handed on in the export),
and the route's panel links to the page. If UT.no has the route under
another code than yours, you are told so before you take anything.

One page is read when a person asks, never faster than one a second, and
nothing goes through routes on its own. An assistant has the same as the
tool `on_utno`. Descriptions on UT.no are DNT's, licensed CC BY-NC.

### How high a route goes

A route says what it climbs: metres up and metres down, walked from its
first end to its last (the other way the two change places), and the lowest
and highest point. The panel draws the profile, with snow and ice shaded
and the places it passes ticked off; pointing at the profile shows the spot
on the map, and a click goes there.

The heights are Kartverket's: its height service
(`ws.geonorge.no/hoydedata/v1/punkt`) answers how high the ground is at a
point and what kind of ground it is, from the 1 m terrain model. Each
stretch is asked about at its own corners and in between, no further apart
than 10 m, and the answers are kept by the checksum of its line
(`height.profile`): asked once, shared by every route on the stretch, and
asked again only when the line is moved. A stretch put back as it was finds
its heights again.

    .venv/bin/stiflyt2 height           # how much has heights
    .venv/bin/stiflyt2 height fill      # ask about everything that has none (jot: one area)

The nightly check does the same for what was moved during the day, and
opening a route asks at once for what it lacks, as far as a short wait
allows. What a route climbs is handed on (`ascent_m`, `descent_m`,
`lowest_m`, `highest_m` in the export) only when it is known for all of it.

A rise counts from where the line turns upward, and only once it is 2 m or
more (`height.COUNTS_M`): the terrain model is exact to the decimetre and a
line on the map is not, so across a slope it wanders a little up and down,
and every such step would otherwise be counted as climbing. On the routes
of Jotunheimen that takes 2 to 10 % off the plain sum. Water is not walked
and has no part in it.

For each way a route also says how long it takes and what grading its
numbers give. The time is Naismith's rule, as v1 had it: 4.8 km an hour and
six minutes more for every 100 m climbed, for a fit walker on a path with no
rests; and Langmuir's addition for going down steeply: ten minutes more for
every 300 m lost on ground steeper than 12 degrees (21 %, taken over 50 m).
The grading is what length and climb alone make of it by the marking
handbook, walked one way: green under 5 km and 300 m, blue under 10 km and
600 m, red under 20 km and 1,000 m, black beyond. What is lost on that
steep ground counts half of what is climbed, as the handbook says of very
steep terrain; where "very steep" begins it does not say, and 12 degrees is
our reading.

**Where nobody has set a grading, the one its numbers give stands in**: the
harder of the two ways. It is shown as the route's grading, with the words
that it is by length and climb, and handed on in the export (`grading`, with
`grading_from` saying `set` or `length and climb`). It is worked out each
time, so it follows the line when the line is changed, and it is never
written into the route: a grading set by a person, or taken over from
Turrutebasen, is what stands. Kartverket is asked for it too (decided 2026-11-17), once a person has said the route is done, like the name: for a
line nobody has checked it is a guess. "Not graded" (I) counts as nobody
having set one. The ground underfoot and how exposed a route is are the rest of a
grading and are not known here, so the numbers can come out too easy and
never too hard. On 2026-11-17, of 120 routes 3 had a grading set; by the
numbers 52 are black, 47 red, 12 blue and 6 green, and the two that are set
(bre29 black, jot50 red) come out the same.

Time and gradings are in the panel, in the export (`minutes`,
`minutes_back`, `grading_at_least`, `grading_back_at_least`) and in what an
assistant gets. None is given for a route whose heights are known only in
part.

### A route in facts, to write a description from

`GET /api/v1/owned/routes/{id}/facts`, and the tool `route_facts` for an
assistant, give a route as it is walked (`domain/factsheet.py`):

- **Sections**, cut where something changes: at a place, where another
  route joins, leaves or meets it, where path becomes road or glacier, where
  the marking stops. Each with its length, what it climbs, minutes each way,
  the ground under it as Kartverket's height service has it (open, forest,
  bog, snow and ice, lake, river) and its steepest 200 m. A few metres
  between two junctions, or between two arrival points of one place, are
  folded into the section beside them.
- **Points** in order: places, junctions and which routes they are with,
  water under the line with the bridge or ford where one is known, the info
  points of Turrutebasen, the highest and lowest point.
- **Steep parts**: 20 % or more over 200 m or more, and which way they go.
- **Names** from Stedsnavn within 200 m (lakes, valleys, glaciers and
  mountains within 600 m: their names lie in the middle of them), with how
  far along, how far off and on which side as it is walked.
- **What it cannot tell** (`not_known`): what the path is like underfoot,
  exposure, most stream crossings and bridges, the seasons.

It is meant for drafts: an assistant writes a description from it in
Norwegian, asks the person about what the data does not say, and sets it
(`describe_route`) only when the person has read it. Bridges are the thin
part. The paths we load carry no mark for a bridge; what is known is a named
bridge, footbridge or ford in Stedsnavn (11,211, 131 and 1,015 in all of
Norway, 10 of them beside our routes) and a bridge among Turrutebasen's info
points (304 in all, 2 beside our routes). See `docs/TODO.md`.

### A route against the mapped paths

The panel of a route of yours says how much of it has a mapped path or road
beside it (within 25 m), and what laying it on them would give: its length
then, against its length now and in Turrutebasen, how many stretches change
and by how much at most. "Show it on the map" draws the line it would be;
"Lay it on them" does it, as one action that Undo takes back.

The line is looked at every 10 m. Where a mapped line runs beside it for
120 m or more, and is about as long as the line itself there, the route
takes the mapped line. Where nothing is mapped beside it, or a path only
crosses it, the line stays as it is. This is for a route that follows a
mapped path in the valley and cairns over the top. Fixing the line itself,
a click on a stretch offers the same for that stretch alone, beside "Lay all
of it on the path between its ends", which replaces the whole line with the
mapped way between its two ends. Laid once, a route stays as it is when laid
again.

"Against the mapped paths" in the area section compares every route of the
area at once (half a minute for Jotunheimen) and lists those that would
change, each with its share and its length before and after. A route is laid
from the list, or all that are not done in one action. Stretches that a route
you have said is done runs over are then left alone.

### Proposed repairs

The area section lists repairs simple enough to approve at a glance, under
"Proposed repairs": a short extra end to take out (up to 100 m), two pieces
lying close to join on foot (up to 150 m), a few metres that begin and end at
the same junction, a route that ends one stretch short of where a place's
routes meet to carry on along that stretch, a route end at no place to make
a place of where something there has a name, and a stretch Turrutebasen says
follows a road or track to lay on the mapped line (when it moves 3 to 60 m).

An end is named after a hut within 300 m; else after a hotel, a summit, a
summer farm, a farm, a cabin, an old site or a bridge in Stedsnavn near it
(60 to 200 m by kind); else after the road it ends on, with the nearest named
thing added ("Sognefjellsvegen ved Dumma") where the road is long or the name
is taken. Where the name belongs to a place already and the end is within
150 m, the proposal is one more arrival point for that place. An end with
nothing named near it is left for a person to name. Each is tried out first, in
a transaction that is rolled back, and offered only if the route comes out as
one line. Click one to see it; approve it, approve a whole group, or open the
route and do it your own way. A candidate that is not yours yet is made yours
by the approval. An approved repair does not make the route done.

A finding about the network that is right as it is (an isolated route, a
dead end at a summit) can be said to be so: click it, "This is right as it
is". It moves to a list of its own and can be brought back.

### What to tell Kartverket

`stiflyt2 corrections jot` lists what your changes mean for Turrutebasen, and
`--geojson corrections.geojson` (with `--srid 25833` for UTM 33) writes them
as a file to hand over; the area section of the viewer has the same list and
file under "For Kartverket". It is the difference between what is yours and
the Turrutebasen that is loaded now, per segment, by Turrutebasen's own
`lokalid` and with the segment's `versjonid`:

- a line that has moved more than 1 m, as proposed and as it is now;
- `merking`, `skilting`, `rutefolger` or `vedlikeholdsansvarlig` that differ.
  Where you say that nobody maintains a route or a stretch of it and
  Turrutebasen names somebody, Kartverket is asked to take them off: a
  route made from Turrutebasen starts with who Turrutebasen has, so nobody
  is something a person has said;
- a segment that is no stretch at all: a few metres that begin and end at
  the same junction;
- a `rutenummer` to take off a segment the route no longer uses, or to add
  to one it now runs along; a segment to add where you joined two ends;
- a `rutenummer` to take off every segment of a route you have withdrawn,
  where other routes of yours run over (95 % of) what it ran over: a misspelt
  number such as jot03 beside jot3, or one route under two numbers. A route
  withdrawn that nothing else of yours covers is a route left to others, and
  asks for nothing;
- a `rutenavn`, for a route you have said is done.

Nothing is stored: when Kartverket has made a change and it has been
imported, that line is gone from the list.

### Routes to hand on

Corrections for Kartverket are told per Turrutebasen segment. For UT.no and
others who read whole routes, `stiflyt2 routes export routes.geojson`
(`--area jot`, `--changed` for only those that differ from Turrutebasen,
`--srid 25833`) writes one feature per route: its line, drawn unbroken
through its junctions, code, name, the places at its ends, who maintains it,
grading, lengths, what it climbs, its number on UT.no where that is known,
and the changes from Turrutebasen that touch it. The
feature id is the route's own id, the same from one export to the next. The
same file is behind "For UT.no" in the area section
(`GET /api/v1/routes.geojson?area=&changed=&srid=`).

### Where an area stands

`stiflyt2 report jot` (`--json` for the same as data) and the area section of
the viewer give the report for an area: how many routes are yours and
reviewed, kilometres of route and of ground, over water and glacier, not
marked, who maintains how much, places, and the to-do list in groups.

The report checks that the network hangs together. It reads the graph your
routes and places make and reports spots where the map looks joined and the
graph is not: an end that stops short of another stretch (within 50 m), an
end or junction lying on a stretch that is not divided there, two stretches
crossing without a junction, routes that hang together but not with the rest
of the area, and ends at no place. A spot counts only when the way round on
foot is much longer than the gap, so a knot of lines beside a junction they
all hang in is left alone. In the viewer each spot is an orange ring.

`stiflyt2 places make` makes a place of every proposed hut and parking place
that is not one yet (`--area jot` for one area). Those are marked as not
reviewed and follow their proposals until you review or change them; what you
have reviewed, changed or withdrawn is left alone.

Every area side by side: "Every area side by side" under the list of areas in
the page, `stiflyt2 report` with no area, or `GET /api/v1/report`. Above the
list of areas the page has a choice of whose routes to show: everybody's, or
only what one maintainer looks after (it starts with `STIFLYT2_HOME_MAINTAINER`,
DNT | DNT Oslo og omegn unless set, and is remembered per browser). The choice
narrows both the report and the list of areas.
Per area, the kilometres of **ground** its routes cover and how much of that
is marked, for Turrutebasen as it is and for what is yours. Ground, not route
length: a stretch that several routes share is counted once in an area (a
stretch that routes of two areas share counts in both, and once in the sum).
Yours is the routes in use; discontinued ones are left out. With
`--maintainer "DNT | DNT Oslo og omegn"` only the ground that maintainer is
said to look after.

### Ids that hold

A route, a junction, a stretch and a place each have an id that stays while
they are moved, re-laid or renamed. When an id does end (junctions joined, a
stretch divided, a route taken into another) it is recorded what came
instead. `GET /api/v1/current/{route|node|edge|place}/{id}` says what an id
stands for now: `current`, `replaced` (follow `now`), `divided` (several: a
person chooses) or `gone`. Earlier codes and names are kept:
`stiflyt2 lookup jot46/2` and `GET /api/v1/lookup?q=` find a route or a place
by what it is called or was called before.

Changes are accepted only when `STIFLYT2_EDITOR` names the author.

### A route that is discontinued

A route given up in the terrain (rockfall, a bridge gone) can be wanted here
all the same, for its history. Withdrawing it would say it never was a route
of yours. "It is discontinued" at the bottom of a route's panel says since
when and why; "In use after all" takes it back. A discontinued route stays a
route of yours with its line, stretches and history. It is drawn grey and
dotted and stands as "Discontinued" in the list, and it is left out of:

- the network checks and the to-do list;
- the planner: a stretch that only discontinued routes run over is not used
  when a route is laid out or an end is moved;
- proposals, and laying routes on the mapped paths.

It is reported for as long as it is kept here. Where Turrutebasen still has
it, Kartverket is asked to take its number off every segment, with the reason,
until an import shows that they have. In the export for UT.no it is handed on
with the others, with `state` discontinued, since when and why, and whether
Turrutebasen still has it: whoever still shows it is to take it out.
`--in-use` (`discontinued=false`) gives a file of only the routes in use,
`--discontinued` only the discontinued ones.

### A route that is kept and not published

A route can be wanted here without being for the public: kept for its
history, or not ready to be shown. "It is not to be published" at the bottom
of a route's panel asks why; "Publish it after all" takes it back. It is
another thing than discontinued: nothing is said about the terrain. A route
that is not published

- is left out of every file for UT.no;
- has nothing new asked of Kartverket on its account (no segment added for
  it, no number put on a segment), and where Turrutebasen has the route,
  Kartverket is asked to take its number off, with the reason;
- is otherwise a route like any other: in the list (with "Not published"), on
  the map, in the network checks and the to-do list, and it can be done.

### Putting a route back as it was

Undo at the top of the map takes back the latest change, whatever it was and
whoever made it, and goes back in order. When the changes you want gone lie
under changes to other routes, use "Put it back as it was…" under a route's
history instead. It lists the changes to that route and its line that can be
taken back, newest first. Choose how far back: that change and every later
change to the route are taken back, and what was done to other routes in
between stays. (A change to a stretch the route shares counts as a change to
its line, whichever route was open when it was made.)

Where a later change to another route touches the same stretches or
junctions, taking the route back would lose it or leave it hanging. Then
nothing is done, and the list says what stands in the way: take that back
first, with Undo. Over the API: `GET /api/v1/owned/routes/{id}/changes` and
`POST /api/v1/owned/routes/{id}/back` with the `txid` to go back before.

### A route that is no route

A route always has at least one stretch. Its only stretch cannot be taken
out, and a route with none cannot be said to be done: it is withdrawn
instead ("Withdraw route" at the bottom of its panel, or in the popup of its
only stretch). In the list its Turrutebasen candidate then stands as
"Withdrawn", with what follows from it: Kartverket is asked to take the
number off where other routes of yours run over nearly all of it, or where
all of it is under 100 m (a number on a stray bit of line); otherwise it is
left as a candidate.

### Starting over

Everything that is yours (places, routes, stretches, the history, undo) is
the schemas `core` and `history`: the overlay. The imported sources are not
part of it.

```sh
.venv/bin/stiflyt2 overlay backup                         # a copy in var/backup/
.venv/bin/stiflyt2 overlay reset --database stiflyt2      # remove all of it; a copy is written first
.venv/bin/stiflyt2 overlay restore var/backup/overlay-….dump --database stiflyt2
```

`reset` leaves the overlay as on a new installation: afterwards
`stiflyt2 places make --area jot` and `stiflyt2 routes make jot` make an
area's places and routes again. `restore` puts a copy back exactly, ids and
history included, and writes a copy of what it replaces. Both ask before
they go on, and only when the database is named. Restart the viewer after.

### Looking things up

What a person sees at a glance on the map, as questions that can be asked:
for an assistant working through the API, and for checking by hand. All of
them only read.

| Question | Asked with |
|---|---|
| Everything in Turrutebasen that carries a route number: its candidates, and each segment with its id, length, the numbers on it, what it follows, marked or not, who maintains it, the name it lies by, and the routes of yours over it | `GET /api/v1/turrutebasen/{code}`, `stiflyt2 turrutebasen jot48` |
| What lies around a point, nearest first: names, mapped paths and roads, Turrutebasen segments, stretches and junctions of yours, places | `GET /api/v1/here?lon=&lat=&radius_m=` |
| The shortest way over mapped paths and roads through some points: its length, how much is path and how much road, and how much of it a named route of yours already follows | `POST /api/v1/mapped-way` |
| What two routes of yours share, and how far each goes alone | `GET /api/v1/same-line?a=&b=` |
| The odd things of an area | `GET /api/v1/areas/{area}/checks`, `stiflyt2 checks jot`, "Odd things to look at" in the area section |

The odd things are found by counting what is otherwise only seen: two routes
that share nine tenths of the longer one (jot3 and jot4 on one line), a
route under 100 m (a number on a stray bit), a number that is another one
with a zero put in (jot03), a maintainer that is none ("Test turlag"), a
line that goes at least 60 m out and the same way back, and a stretch that
Turrutebasen has unmarked or in terrain by a glacier's name and that is not
said to be glacier. Each is a sentence with a spot on the map. None is
certainly wrong: they are where to look. Candidates you have withdrawn or
taken out of a route are left out.

### More than one person

**Who gets in.** Reached through the proxy, nobody gets anything without
signing in with Google as a person listed in `access.person`; a reader looks,
an editor changes. People are listed from the command line on the server
(`stiflyt2 access add|remove|list`), as the database's owner: the service
reads the list and cannot change it. A program acts as a person with a token
(`stiflyt2 access token`), and what it does is recorded as that person's,
come in through an assistant (`mcp`) or a script (`api`). Without a Google
client set, the service is local: it answers only on localhost, as the editor
named in `STIFLYT2_EDITOR`. How to switch sign-in on is in
[Parallel deployment](docs/architecture/deployment.md).

**Seeing the pages as somebody else does.** One who may change things can
have the pages shown as for one who may do less: "Show the pages as" at the
foot of the left side, for an editor as operations or as a reader, for
operations as a reader. It is the server that answers as to that role, so
what is refused is refused there and not only hidden. The choice is the
tab's, a stripe says so for as long as it lasts, and it only ever goes
downwards. Programs with a token are not affected.

**Undo is your own.** Undo in the map takes back your latest change made in
the map. What others have done since stays, and so does what an assistant did
in your name: the assistant takes that back, or the route is put back from
its history. Where a later change by someone else touches the same stretches,
the undo is refused and says what stands in the way.

**Seeing each other.** The page asks every few seconds whether anything has
been changed. When it has, the lines, the list and the open route are brought
up to date, and the foot of the left side says who it was. A change to a
stretch carries the revision the map had of it, and is refused when somebody
has changed the stretch in the meantime.

**At night.** A copy of what is yours is written at 03:50 UTC
(`deploy/backup-nightly.sh`, the newest 14 kept), and the viewer starts again
after a reboot. The copy also leaves the server: where `var/backup-repo` is a
git repository with a remote, the newest copy of each kind is committed there
as `overlay.dump` and `tool.dump` and pushed, so that the repository's history
is the copies night by night (they are written uncompressed, so git keeps
only what differs). That repository must be private: the copies hold names,
e-mail addresses and who changed what. The key it pushes with is named in
`var/backup-repo/.key`; make it a deploy key that can write to that one
repository and nothing else. A push that fails is reported like a copy that
fails. Putting a copy back is `stiflyt2 overlay restore <file> --database
<name>`; tried from a fresh clone of the pushed copy into the test database
on 2026-11-17. When the nightly check of the sources or the copy did not run
or failed, the page says so to an editor (`GET /api/v1/health`).

A new person starts with "How to use this" at the foot of the left side
(`/guide`).

### An assistant: the MCP server

`POST /mcp` on the same service speaks the Model Context Protocol, so that a
person can work on the routes by talking to an assistant. Nothing is installed
on the person's machine: the assistant is given the address and a token.

```sh
.venv/bin/stiflyt2 access token kari@… "Claude Code" --through mcp     # shown once
```

The token acts as that person: what the assistant changes is recorded under
their name as having come through an assistant, a reader's assistant can only
look, and taking the person off the list shuts the token out. Run locally
(no sign-in), the endpoint needs no token and acts as the editor of the
settings. How a person connects their client is in
[An MCP server](docs/architecture/mcp.md).

The tools call the same domain functions as the map (`src/stiflyt2/api/mcp.py`):

- **Looking**: `find`, `route` (with its stretches and their ids), `area`,
  `areas`, `checks`, `proposed`, `turrutebasen`, `what_is_here`, `mapped_way`,
  `shared_line`, `plan_way`, `corrections_for`, `changes`.
- **Changing**: `make_yours`, `move_end`, `send_via`, `lay_out`,
  `lay_out_again`, `take_stretch_out`, `set_stretch`, `divide_stretch`,
  `describe_route`, `lay_on_mapped`, `set_state`, `approve_proposal`,
  `make_place`.
- **Taking back and telling**: `undo_last` (the assistant's own latest
  change), `put_back` (a route, to before one of its changes), `report_gap`.

A tool that changes takes `dry_run`, true unless said otherwise: it says what
it would do and keeps nothing. `set_state` (done, discontinued, not published,
withdrawn) is refused unless the assistant says the person has said so. What a
tool refuses, and what fails, is kept with the feedback, through `mcp`; and
the server's instructions tell the assistant to call `report_gap` when it
cannot do what was asked.

### What the tool refuses, and what people say is missing

Whoever looks after the tool gets to know where people get stuck without
being told. The server keeps, in `feedback.entry`:

- **refused**: every time the tool says no, with the sentence the person was
  given, what was asked, by whom, and about which route;
- **failed**: every error and every question that took too long, with its
  trace, and faults in the page itself;
- **reported**: what a person writes under "Something wrong or missing
  here?" at the bottom of the left side (the route that is open and the part
  of the map in view are noted with it), and what an assistant reports when
  it cannot do what it was asked (`POST /api/v1/feedback`).

Changes taken back within two minutes are read out of the undo log and
listed with them.

```sh
.venv/bin/stiflyt2 feedback                # what nobody has looked at: people's words first, then by how often
.venv/bin/stiflyt2 feedback show 41        # every entry that says the same as 41, with what was asked
.venv/bin/stiflyt2 feedback seen 41        # looked at: it leaves the list, with all that say the same
.venv/bin/stiflyt2 feedback fixed 41 c0ffee1
.venv/bin/stiflyt2 feedback --all
```

Entries that say the same thing, numbers and ids aside, are one line with a
count. This is about the tool, not about the routes: it is no part of the
overlay, and `overlay reset` leaves it. A database from before this needs
the two statements at the top of `migrations/import/versions/0005_feedback.py`
run by its owner before `alembic upgrade head`.

## The front door: areas and routes

`/` is where everybody comes in, whatever their role (`static/drift.html`).
It shows what there is and changes nothing about a route; the route editor
described above is at `/ruter`. They are one service with one sign-in.

- **The areas** that have routes of yours in use, each with how many routes
  and how far they go. In use means not discontinued and not kept back from
  publishing. A click on a route on the map opens its area.
- **One area**: what its routes add up to, then the routes. Kilometres of
  route (each walked for itself) and of ground (a stretch that routes share
  counted once), how much of the ground is marked, over glacier and over
  water, the highest and the lowest the routes go and on which route, how
  many routes there are of each grading (and how many of those gradings
  are worked out from length and climb because nobody has set one), the
  places the routes pass by kind, and the sign sites and signs. The list
  has code, name, length and grading, and a field to find a route in.
  Pointing at a row shows the route on the map; a click on a route on the
  map opens it, and asks which where several run together.
- **One route**: length, what it climbs and the time each way, grading, the
  height profile (pointing at it shows the spot on the map), the
  description, its page on UT.no where that is known, and "Open in the
  route editor" (`/ruter?yours=<id>`).
- **Signs** are a part to choose: the tab "Signs", the line about signs in
  the area's numbers, or a click on one of the small points on the map,
  which are the sign sites. The order's basket is there only among the
  signs.

The address says where one is, so a view can be linked to and the browser's
back goes back: `/?area=bre`, `&route=bre8`, `&view=signs`, `&site=…`. With
no area in the address, the page opens at the area last looked at.

Behind it: `GET /api/v1/owned/areas` and `GET /api/v1/owned/areas/{area}`
(`domain/overview.py`), and for a route the same as the editor uses. The
height profile is one piece of code for both pages (`static/profile.js`,
`profile.css`), as the base map is (`static/basemap.js`).

Somebody who is only to look is listed as a reader
(`stiflyt2 access add <e-mail> "<name>"`; reader is the role when none is
given). A reader sees all of the above, the signs and the route editor, and
every button that changes something is refused by the server (403). An
assistant gets in only with a token made for the person
(`stiflyt2 access token`); with a reader's token it can look things up, and
every tool that changes something is refused, so is telling what is missing.

## Operations: signs

Operations is what happens to the routes, as opposed to what the routes
are, and never changes a route. The first part of it is signs: the "Signs"
part of the front page (`src/stiflyt2/ops/signs.py`, `api/signs.py`,
`static/drift.html`).

**Worked out from the routes, each time:**

- A **post** stands where a route ends, where three ways or more meet, and
  at a hut or parking place a route passes. Junctions of one place within
  30 m of each other share a post; a place with junctions further apart has
  a post at each (Nørdstedalseter: one at the hut, one at the junction by
  the road). A person can put a further post at a junction ("Another post
  here") and move signs onto it.
- A site at a junction that is no place has no name. It is told by its
  routes and by what it lies near: the nearest name in Stedsnavn within
  500 m ("Junction of bre19, bre22, bre49, by Åsete"). "Call it Åsete" gives
  it that name, which then goes on the back of its signs.
- A **sign site at another spot** ("A sign site at another spot", then a
  click on a route) is for signs that stand where no routes meet: where a
  path leaves a road, at a bridge. It lies on a stretch of a route, at
  least 15 m from its ends, and has a sign each way along every route that
  runs there. It has its code from the start, and can be taken away again.
- A **sign** points along one stretch out of the junction, to the next hut
  or parking place on a route that leaves that way, or to where that route
  ends if it ends first. Never past a hut, never to a junction on the way.
  Routes that go the same way to the same place share a sign. Two routes
  that set out along one stretch and reach the place by different ways have
  a sign each, with its own distance (from Sota sæter to Nørdstedalseter:
  bre8 over the glacier, bre9 round it), unless the two would say the same
  on the wood (from Arentzbu to Hyrnavollen, bre21 and bre62 are both
  15 km: one sign).
- **On past a hut that is near**, at an end point only: a post at a place
  where a route ends, never a junction on the way, and never the edge of a
  glacier (which is not walked on from, nor pointed on to, either). Where the place a sign
  there points to is under 5 km off (as the sign says it), people walk the
  next stage too, so the post also points on to where the signs at that
  place point: the same way,
  the whole distance, as long as that is under 25 km, is not back the way
  one came, and is not somewhere the post points to already (decided 2026-11-17, from the post at Viva: Sprongdalshytta 2,5 km, and on to
  Arentzbu 19 km and Sota sæter 22 km; Slæom would be 26 and is left out).
  Such a sign says past which place it leads. `NEAR_KM` and `ONWARD_KM` in
  `ops/signs.py`. In Breheimen that is 6 signs at 3 of 62 sites, in
  Jotunheimen 25 at 10 of 113; one that is not wanted is taken out.
- A **name sign on top** ("A name sign on top" on a post): a sign with the
  name of the place and no arrow, for where nothing else says where one is.
  The exception, so a person says which posts have one. It is ordered with
  the post's other signs.
- A **destination by hand** ("Another destination" on a post, then a click
  on the map) is for what the routes do not give a post: a place nearer
  than the next hut, like a farm on the way, or one further on. A click
  within 150 m of a place of ours means that place; elsewhere it means the
  spot on the route there, and the sign is told what to say. The sign
  points the shortest way there over the routes, worked out each time, and
  says which routes that is by.
- The **distance** is the metres along the route times 1.05 (what v1
  measured that one walks more than the line), under 10 km cut down to the
  half kilometre, from 10 km rounded to the whole; under half a kilometre it
  is cut down to the hundred metres and never less ("0,2 km"; v1 said 0.0).
  Written as the sign manual has it, with a comma, in the page and in the
  list for the maker.
  Where the way leads over
  a glacier or by boat it is the distance to the ice or the boat, and the
  sign says "… via Fortundalsbreen (til brekanten)".
- The **arrow** is read by someone standing at the junction and looking at
  the post: the stretch leaves to the left, to the right, or within 25
  degrees of straight ahead. A post is assumed to stand 4 m from the
  junction in the widest opening between the paths. Drag it on the map to
  where it stands and the arrows follow; on the other side of the path they
  turn.

On a post the signs are listed as the sign manual says they hang: the name
sign on top, the signs that point right together and those that point left
together, and of signs that point the same way the longest distance on top,
the shortest at the bottom. The list for the maker and the field sheet have
them in that order too. The posts of a site are numbered, in the panel and
on the map; pointing at a sign lights up the way it points on the map, and
pointing at that line lights up its signs. Where a place has several posts,
"Take out the N that hang elsewhere too" takes out of one post what hangs
on another post of the place as well.

The **field sheet** ("Field sheet" on a site, "Field sheets" on an order)
is a page to print or to save as PDF, one sheet per site, in Norwegian: a
map with the routes and the numbered posts, the text for the back, and for
each post a table of what hangs there, with a box to tick. Of one site, or
of the sites an order has signs at (`GET /api/v1/signs/sheet?area=&site=`
or `&order=`; `ops/sheet.py`).

The list of sites has a field to find a site by name, code, route or what
it lies by, and an open site has buttons for the one before and the next in
the list as it is shown.

In the page a sign is drawn as it hangs. A wood sign (trehvit), as in the
mountains: a plank of weathered wood with the red T in its ring at the end
the arrow is at, a small black arrow, the distance and the name in italics.
A green sign as the sign manual has it: arrow, distance, name, and the
walker at the far end. A sign that points right is the same turned round.

**Decided by a person, and kept** (`ops.*`): that a site is to have no signs
("No signs here": nothing of it is ordered); its name, the
text on the back (the name and the UTM position to 10 m unless said
otherwise) and who the signs are sent to; where a post stands, or that it is
taken out; for a sign another arrow, text, distance or colour (trehvit,
grønn), which post it hangs on, or that it is taken out. What is said by
hand stands when the routes change, and is found again by the junction, the
stretch and the destination, by the routes' own ids. Every decision is in
`ops.event` with who made it.

**An order** is the signs a person has chosen to have made. Most signs hang
where they are to hang already, so an order is seldom a whole site: it is
the worn sign here and the two there that say something that is no longer
so. In the page: click the signs, at one site and the next (a click chooses
a sign, "Change" under it is for saying something else of it), then "Add
to the order". The order is the basket at the top right of the map, with
how many signs are in it: a click opens it, where a sign can be taken off
it again, and "Take out the order" gives the list
for whoever makes the signs, with v1's columns: Antall, Tekst på skiltet,
Km, Pilretning, Baksidetekst, Skiltfarge, Ruter, Sendes til. "Choose all
its signs" is for a site that is new. An order is kept as it was taken
out: what a sign said then stays, and a sign that says something else now
is marked. A sign that has been ordered is waiting until a person says "it
is up" (under the sign, or "They are up" for the signs chosen); "Ordered,
not up" in the list shows what is waiting. A sign site has no state of its own: it is its signs that hang, are
ordered and are put up. It gets its code, like BRE-SS-0001, the first time
a sign of it is ordered. On the map a site is orange with something on the
order, green with signs ordered and not up, and grey with no signs.

The base map is the same in both pages and chosen with the layers button at
the top right of the map: Kartverket's grey or colour map, or OpenStreetMap
(to look at; nothing is traced from it). What is chosen is remembered, and
is what the other page opens with (`static/basemap.js`, shared by the two
pages).

Roles: a `reader` looks; `operations` decides about signs and cannot change
routes; an `editor`, the route responsible, does both
(`stiflyt2 access add … --role operations`).

**What is decided stays with its sign while the routes are mended**
([ADR-004](docs/architecture/ADR-004-how-operations-hangs-on-the-routes.md)).
A decision is kept under the junction, the stretch and the place it was made
about, and read through what stands for those now: the junction it was joined
to, the part of a divided stretch that leaves the sign's junction, the place
a route end has become, the route that had the code. What still finds no
sign is **adrift**: the routes no longer give a sign from there to there. The
signs list says how many there are; each says what it said, where it was and
why, and a person says which sign it is about now or that it can be forgotten
(`GET /api/v1/signs/adrift`, `POST …/adrift/{id}/move` and `/forget`). If the
routes change back, it finds its sign again by itself.

**Others at work.** The signs are asked for as they are now when one goes in
among them and when a sign site is opened, as a route is when it is opened:
what somebody else has decided since the area was chosen shows then. Two who
decide about the same sign at once are not stopped: the later stands, for the
arrow, the text and the rest each by itself.

**Undo.** "Undo: …" at the foot of the side, and Ctrl/⌘+Z, take back your own
latest decision about signs, made in the page: a sign changed, a post moved,
forty signs put on the order in one go. What others have decided since stays;
where that is about the same sign, nothing is done. An order that has been
taken out stands (`GET` and `POST /api/v1/signs/undo`, `ops/undo.py`).

An assistant has the same (`signs`, `sign_site`, `sign_post`, `sign`,
`new_sign_site`, `add_sign_destination`, `order_signs`, `signs_adrift`,
`settle_adrift`, `undo_signs`), dry unless told otherwise; the operations
role is enough for them.

Not there yet, of what v1 has: the field sheet as PDF, and photos. See
`docs/TODO.md`.

## Setup

Setting up from nothing, where your OS user is not a PostgreSQL superuser:

1. Once, as a superuser: `ALTER ROLE <you> CREATEROLE`, create the database
   `stiflyt2_template` owned by you, and `CREATE EXTENSION postgis` in it.
2. `createdb -T stiflyt2_template stiflyt2`, then
   `psql -d stiflyt2 -f db/bootstrap.sql`, `.venv/bin/alembic upgrade head`
   and `.venv/bin/alembic -n domain upgrade head`.
3. Give `stiflyt2_import` and `stiflyt2_api` passwords and put them in
   `~/.pgpass` for host `127.0.0.1`.
4. A `.env` in the checkout:

   ```
   STIFLYT2_IMPORT_DSN="host=127.0.0.1 dbname=stiflyt2 user=stiflyt2_import"
   STIFLYT2_API_DSN="host=127.0.0.1 dbname=stiflyt2 user=stiflyt2_api"
   STIFLYT2_EDITOR="Your Name"
   STIFLYT2_TEST_TEMPLATE=stiflyt2_template
   STIFLYT2_TEST_ROLE_CONN="host=127.0.0.1"
   ```

With a superuser OS account and password-free local logins, none of steps 1,
3 and 4 are needed: `createdb stiflyt2` and continue from step 2.

## Documents

- [Build plan v3](docs/plan/build-plan-v3.md) and its
  [start prompt](docs/plan/claude-prompt-v3.md) — the target
- [ADR-001](docs/architecture/ADR-001-amendments-to-v3.md) — amendments:
  changesets, route engine, sign readiness, pilot area
- [ADR-002](docs/architecture/ADR-002-places-access-points-connectors.md) —
  places, access points and connectors
- [ADR-003](docs/architecture/ADR-003-owned-routes-and-crossings.md) —
  owned routes, and crossings over water
- [ADR-004](docs/architecture/ADR-004-how-operations-hangs-on-the-routes.md) —
  how what operations keeps stays with what it is about while the routes
  are mended, and how it is taken back
- [To do](docs/TODO.md) — what is wanted and not built, by part; the
  operations part (signs, work, closures) is collected there
- [v1 assessment](docs/architecture/current-state.md)
- [Schema](docs/architecture/schema.md)
- [REST contract](docs/architecture/api.md)
- [An MCP server](docs/architecture/mcp.md) — proposed, not built: the tools,
  what stays in the map, and how refusals, errors and reported gaps are
  collected
- [Parallel deployment](docs/architecture/deployment.md)
- [Task plan, decisions and risks](docs/architecture/plan-and-risks.md)

v1 lives in `../stiflyt` (app) and `../stiflyt-db` (dataset loader).

## Licence

The code is under the Apache License 2.0 (`LICENSE`). The small test
fixtures in `tests/fixtures` are cut from open data: Turrutebasen, Stedsnavn,
Eiendomskart Teig and the road network, © Kartverket and Statens vegvesen,
CC BY 4.0 and NLOD; Kulturminner, © Riksantikvaren, NLOD. None of the data
the tool loads or the data made with it is in this repository.
