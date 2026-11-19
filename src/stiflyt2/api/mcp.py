"""An MCP server: the routes, for an assistant that a person talks to.

`POST /mcp` speaks the Model Context Protocol over HTTP (the streamable HTTP
transport, answering each request with one JSON object; there is no stream to
listen on, so `GET /mcp` is refused). It is part of the same service as the
map, behind the same gate: an assistant comes in with a token that acts as a
person (`stiflyt2 access token`), and what it changes is recorded as that
person's, come in through an assistant. The tools call the same domain
functions as the map does. Nothing is installed on the person's machine.

Written by hand rather than on an SDK: the protocol needed here is four
methods, the tools are plain functions over a database connection, and who is
asking is already known when a request gets this far.

What a tool refuses, and what fails, is kept with the feedback
(`feedback.entry`, through `mcp`), like what the map's requests are refused.
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, get_type_hints
from urllib.parse import urlparse

from anyio import to_thread
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from stiflyt2 import access, feedback, utno
from stiflyt2.api import auth
from stiflyt2.domain import (
    corrections,
    factsheet,
    identity,
    lookup,
    places,
    proposals,
    report,
    routes,
    undo,
)
from stiflyt2.features import FEATURES
from stiflyt2.index import query
from stiflyt2.ops import signs as sign_ops
from stiflyt2.ops import undo as sign_undo

log = logging.getLogger("stiflyt2.mcp")

PATH = "/mcp"
SERVER = {"name": "stiflyt2", "title": "Stiflyt v2: hiking routes", "version": "0.1.0"}
VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")       # newest first; what a client asks for, if it is here
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
REFUSALS = (places.Invalid, places.Conflict, places.NotFound, feedback.NotFound, utno.Unavailable)

INSTRUCTIONS = """\
Stiflyt v2 holds DNT's own, corrected copy of Norway's hiking routes, laid over Kartverket's Turrutebasen and \
the mapped paths and roads (FKB, NVDB). You work on it for the person you are talking to; what you change is \
recorded under their name as having come through an assistant.

How it is put together: a route (code like jot48) is an ordered list of stretches between junctions. A stretch \
may be shared by several routes, so moving its line moves them all; changing which stretches a route uses \
changes only that route. An area is the three letters a code starts with (jot Jotunheimen, bre Breheimen). \
Coordinates are longitude then latitude, WGS84. Lengths are metres.

How to work:
- Look before you change: `find`, `route`, `turrutebasen`, `what_is_here`, `mapped_way`, `checks`. The data shows \
that something is odd, not what is right: where a route goes is for the person to say.
- Tools that change take `dry_run`, and it is true unless you say otherwise. Run a change dry first, tell the \
person what it would do (lengths, what is added and left out), then run it for real when that is what they want.
- A refusal is a sentence. Tell the person what it says. Do not look for a way round it without asking.
- `set_state` to done, withdrawn, discontinued or not published only when the person has said so in this \
conversation. A route that has been mended is not thereby done.
- You do not see the terrain. A line laid where nothing is mapped (a glacier, bare ground) is a sketch: say so \
plainly, and say what it rests on.
- OpenStreetMap is not a source here and must not be traced.
- A description of a route is written from `route_facts` and nothing else: what the data does not say (what the \
path is like underfoot, exposure, fords, most bridges) is asked of the person, not made up. It is a draft until \
they have read it; `on_utno` gives what UT.no already has.
- `undo_last` takes back your own latest change, not the person's, and `redo_last` brings back what you took \
back last. `put_back` takes a route back to before one of its changes.
- When you cannot do what was asked, had to go round, or the person says something is wrong or missing in the \
tool, call `report_gap` with their words. That is how the tool gets better.

Signs are the other half: what happens to the routes, not what they are. Sign sites, their posts and what each \
sign says (destination, kilometres, arrow) are worked out from the routes each time; `signs` shows them. A sign \
points to the next hut or parking place on its route, or to where the route ends; where that is under 5 km \
off, also on to where the signs there point, up to 25 km in all (`onward` says past which place). What a person decides is laid \
over that and kept: `sign_site` (no signs here, name, back text), `sign_post` (where the post stands, which \
turns the arrows; a further post), `sign` (another arrow, text, distance, colour; taken out), `new_sign_site` (a \
site at a spot on a route), `add_sign_destination` (a destination the routes do not give a post). Most signs hang \
already: a sign that is to be replaced is marked with `sign` and `to_order`. None of them changes a route: a sign that is wrong because a route is wrong is put \
right in the route. `order_signs` takes out an order and keeps what each sign said: only when the person says so. What is decided about a sign follows the sign when the routes are mended; what no longer finds its sign is in `signs_adrift`, for the person to settle (`settle_adrift`). `undo_signs` takes back your latest decision about signs.
"""


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


Lon = Annotated[float, Field(ge=-180, le=180)]
Lat = Annotated[float, Field(ge=-90, le=90)]
Point = tuple[Lon, Lat]
RouteRef = Annotated[str, Field(min_length=1, max_length=40, description="A route of ours: its code (jot48) or its id")]
Area = Annotated[str, Field(pattern="^[A-Za-z]{3}$", description="Area: the three letters codes start with, e.g. jot")]
Stretch = Annotated[str, Field(pattern=UUID.pattern, description="The id of a stretch, from `route`")]
DryRun = Annotated[bool, Field(description="True (the default): say what it would do and change nothing. "
                                           "False: do it.")]


@dataclass
class Call:
    conn: Any
    author: auth.Author
    person: access.Person


@dataclass
class Tool:
    name: str
    title: str
    description: str
    args: type[Args]
    run: Callable[[Call, Any], Any]
    changes: bool           # needs the editor role, and a connection that may write
    writes: bool            # needs a connection that may write, though it keeps nothing
    operations: bool = False    # about signs and work, not routes: the operations role is enough to change
    feature: str | None = None  # the feature it belongs to (ADR-005): only who has it sees the tool and may call it


TOOLS: dict[str, Tool] = {}


def tool(title: str, *, changes: bool = False, writes: bool = False, operations: bool = False, feature: str | None = None):
    """Register a function as a tool. Its docstring is the description; its second argument's type, the input."""
    def register(fn):
        args = next(t for n, t in get_type_hints(fn).items() if n not in ("c", "return"))
        TOOLS[fn.__name__] = Tool(fn.__name__, title, " ".join((fn.__doc__ or "").split()), args, fn, changes,
                                  changes or writes, operations, feature)
        return fn
    return register


def _route_id(conn, ref: str) -> str:
    """The id of a route of ours, given its code or its id."""
    ref = ref.strip()
    if UUID.match(ref):
        return ref
    known = [f for f in identity.lookup(conn, ref) if f["type"] == "route"]
    # By the code it has now; else by a code it had before, which still finds it.
    found = ([f for f in known if str(f["label"]).lower() == ref.lower()]
             or [f for f in known if f.get("kind") == "code" and str(f.get("matched") or "").lower() == ref.lower()])
    if not found:
        raise places.NotFound(f"no route of ours is called {ref}. `find` looks in Turrutebasen too, and "
                              "`make_yours` takes a route in from there")
    return str(found[0]["id"])


def _lean(value, lines: bool = False):
    """A result without what an assistant cannot read: lines of hundreds of points, and ids of every junction."""
    drop = () if lines else ("geometry", "was_geometry", "nodes", "bbox")
    if isinstance(value, dict):
        return {k: _lean(v, lines) for k, v in value.items() if k not in drop}
    if isinstance(value, list | tuple):
        return [_lean(v, lines) for v in value]
    return value


def _stretches(conn, route_id: str) -> list[dict]:
    """The stretches of a route in walking order: what an assistant needs to point at one."""
    with conn.cursor() as cur:
        return [dict(zip(("stretch", "kind", "marked", "signed", "origin", "length_m", "from", "to", "also_on"), row,
                         strict=True))
                for row in cur.execute("""
            SELECT e.id::text, r.kind, r.marked, r.signed, r.origin, round(ST_Length(r.geom))::int,
                   ARRAY[round(ST_X(a.p)::numeric, 5), round(ST_Y(a.p)::numeric, 5)]::float8[],
                   ARRAY[round(ST_X(b.p)::numeric, 5), round(ST_Y(b.p)::numeric, 5)]::float8[],
                   (SELECT coalesce(array_agg(y.code ORDER BY y.code), '{}') FROM core.route_leg o
                    JOIN core.route y ON y.id = o.route_id AND y.status = 'active'
                    WHERE o.edge_id = e.id AND o.route_id <> l.route_id)
            FROM core.route_leg l JOIN core.edge e ON e.id = l.edge_id
            JOIN core.edge_revision r ON r.id = e.current_revision_id,
                 LATERAL (SELECT ST_Transform(CASE WHEN l.reversed THEN ST_EndPoint(r.geom) ELSE ST_StartPoint(r.geom) END, 4326) AS p) a,
                 LATERAL (SELECT ST_Transform(CASE WHEN l.reversed THEN ST_StartPoint(r.geom) ELSE ST_EndPoint(r.geom) END, 4326) AS p) b
            WHERE l.route_id = %s ORDER BY l.seq""", (route_id,))]


def _told(conn, route_id: str) -> dict:
    """A route in brief, as the answer to a change."""
    r = routes.detail(conn, route_id)
    return {"route": r["code"], "id": r["id"], "shape": r["shape"], "length": r["length"],
            "name": r["name"] or r["generated_name"], "done": r["reviewed"],
            "ends": [{"junction": e["id"], "place": e["place"], "lon": e["lon"], "lat": e["lat"]} for e in r["ends"]],
            "round_trip_from": r["round_trip"] and r["round_trip"]["place"]}


# ---- Looking ----

class Find(Args):
    text: str = Field(min_length=1, max_length=80, description="A code, a route name or a place name, also an "
                                                               "earlier one. Part of a name will do.")


@tool("Find a route or a place")
def find(c: Call, a: Find) -> dict:
    """Find routes and places by what they are or were called: routes of ours (also by an earlier code or
    name), routes in Turrutebasen that are not ours yet, and place names (Stedsnavn) with their position.
    Start here when the person names something."""
    return {"ours": identity.lookup(c.conn, a.text),
            "turrutebasen": [{k: r.get(k) for k in ("label", "name", "maintainer", "length_km", "shape")}
                             for r in query.search_routes(c.conn, a.text, limit=12)],
            "place_names": [{k: p.get(k) for k in ("name", "type", "municipality", "lon", "lat")}
                            for p in query.search_places(c.conn, a.text, limit=8)]}


class OneRoute(Args):
    route: RouteRef
    with_line: bool = Field(False, description="Also give its line as coordinates. Long: ask only when needed.")


@tool("One route of ours")
def route(c: Call, a: OneRoute) -> dict:
    """Everything about one route of ours: its shape and lengths, what it climbs (`height`: metres up and down
    walked from its first end, lowest and highest), its ends and the places there, who maintains
    it, whether it is done, discontinued or not published, its latest history, and its stretches in walking
    order with their ids (for `send_via`, `take_stretch_out`, `set_stretch`, `divide_stretch`)."""
    rid = _route_id(c.conn, a.route)
    found = routes.detail(c.conn, rid)
    found["history"] = found["history"][:10]
    found["stretches"] = _stretches(c.conn, rid)
    return _lean(found, a.with_line)


class Areas(Args):
    maintainer: str | None = Field(None, max_length=120, description="Only the ground this maintainer looks after, "
                                                                     "e.g. 'DNT | DNT Oslo og omegn'")


@tool("Every area side by side")
def areas(c: Call, a: Areas) -> dict:
    """Every area side by side: kilometres of ground its routes cover and how much of it is marked, a stretch
    that routes share counted once. For Turrutebasen as it is and for what is ours. Use for 'how many km of
    marked route'."""
    return report.areas(c.conn, a.maintainer)


class OneArea(Args):
    area: Area


@tool("Where an area stands")
def area(c: Call, a: OneArea) -> dict:
    """Where one area stands: how many routes are ours and done, lengths, who maintains what, the network, and
    the to-do list (what is asked of which route), with every route of the area and its status."""
    found = report.report(c.conn, a.area)
    for heavy in ("yours", "accepted", "map_data"):
        found.pop(heavy, None)
    found["all"] = [{k: r.get(k) for k in ("code", "kind", "name", "km", "done", "withdrawn", "discontinued",
                                           "unpublished", "problems") if r.get(k) not in (None, False, [])}
                    for r in found["all"]]
    return _lean(found)


@tool("The odd things of an area")
def checks(c: Call, a: OneArea) -> dict:
    """What looks odd in an area and is otherwise only seen on a map: two numbers on one line, a route of a
    few metres, a number that looks misspelt, a maintainer that is none, a line that goes out and the same way
    back, ice not said to be ice. Each is a place to look, not an error."""
    return lookup.checks(c.conn, a.area)


@tool("Repairs proposed for an area", writes=True)
def proposed(c: Call, a: OneArea) -> list:
    """Repairs simple enough to approve one by one: a short extra end, pieces lying close, a place where a
    route ends at something with a name. Each has an `action` to give to `approve_proposal`."""
    return _lean(proposals.proposals(c.conn, a.area))


class Numbered(Args):
    code: str = Field(min_length=1, max_length=40, description="A route number as Turrutebasen has it, e.g. jot48")


@tool("What Turrutebasen has under a number")
def turrutebasen(c: Call, a: Numbered) -> dict:
    """Everything in Kartverket's Turrutebasen that carries a route number: its candidates, and each segment
    with its id, length, every number on it, what it follows, marked or not, who maintains it, the name it lies
    by, and which routes of ours run over it. Use to see what a route was made from, or why two routes overlap."""
    return lookup.turrutebasen(c.conn, a.code)


class Here(Args):
    lon: Lon
    lat: Lat
    radius_m: float = Field(200, gt=0, le=lookup.HERE_MAX_M)


@tool("What lies around a point")
def what_is_here(c: Call, a: Here) -> dict:
    """What lies around a point, nearest first: place names, mapped paths and roads, Turrutebasen segments,
    stretches and junctions of ours with the routes on them, and places."""
    return lookup.here(c.conn, a.lon, a.lat, a.radius_m)


class Way(Args):
    points: list[Point] = Field(min_length=2, max_length=20, description="[lon, lat] of each point, in order")
    route: RouteRef | None = Field(None, description="A route of ours to hold the way against")
    with_line: bool = False


@tool("The mapped way between points")
def mapped_way(c: Call, a: Way) -> dict:
    """The shortest way over mapped paths and roads through some points, making nothing: its length, how much
    is path and how much road, and how much of it a named route of ours already follows. Use to see whether a
    path exists where a route should go."""
    rid = _route_id(c.conn, a.route) if a.route else None
    return _lean(lookup.mapped_way(c.conn, a.points, rid), a.with_line)


class Two(Args):
    a: RouteRef
    b: RouteRef


@tool("What two routes share")
def shared_line(c: Call, a: Two) -> dict:
    """How far two routes of ours run over the same stretches, and how far each goes alone."""
    return lookup.shared(c.conn, _route_id(c.conn, a.a), _route_id(c.conn, a.b))


class Points(Args):
    points: list[Point] = Field(min_length=2, max_length=50, description="[lon, lat] of each point, in order")
    reach_m: float | None = Field(None, ge=1, le=500, description="How near a junction a point must lie to mean it")


@tool("What a route laid through points would be", writes=True)
def plan_way(c: Call, a: Points) -> dict:
    """What a route laid out through the points would be, as `lay_out` and `lay_out_again` would lay it: over
    our stretches and Turrutebasen's segments where they lead, along mapped paths and roads where they do not,
    and straight where nothing is mapped. Says how much of each. Makes nothing."""
    return _lean(routes.plan(c.conn, a.points, a.reach_m))


class RouteOnly(Args):
    route: RouteRef


@tool("What Kartverket is asked")
def corrections_for(c: Call, a: OneArea) -> list:
    """What our changes mean for Turrutebasen, per segment, in an area: what Kartverket is to be asked."""
    return [{k: x[k] for k in ("change", "lokalid", "rutenummer", "text")} for x in corrections.corrections(c.conn, a.area)]


@tool("A route as it is walked, in facts")
def route_facts(c: Call, a: RouteOnly) -> dict:
    """What a description of a route can be written from: the route cut into sections where something changes
    (a place, a junction with another route, path to road or glacier, the marking), each with its length, what
    it climbs, minutes each way (Naismith's rule), the ground under it and its steepest part; the points passed
    in order (places, junctions and which routes join, leave or meet there, water under the line with the
    bridge or ford if one is known, info points, highest and lowest point); the steep parts; and the names
    along it from Stedsnavn, with the side they lie on as it is walked. `whole.there` and `whole.back` give
    time and the grading that length and climb alone make it (G, B, R, S), for each way.

    When you write a description from this: write only what the facts say, in Norwegian, as it is walked from
    `route.from`; do not make up what `not_known` lists (what the path is like underfoot, exposure, fords,
    bridges the data lacks), ask the person about it instead. Show the person the text as a draft, and set it
    with `describe_route` only when they have said it is right."""
    return factsheet.of_route(c.conn, _route_id(c.conn, a.route))


class OnUtno(Args):
    route: RouteRef
    address: str | None = Field(None, max_length=300, description="The address of the route's page on UT.no, like "
                                "https://ut.no/rutebeskrivelse/13394424, or the number in it. Needed the first "
                                "time; after that the route remembers it. Ask the person for it: it cannot be "
                                "searched for from here")


@tool("What UT.no says about a route")
def on_utno(c: Call, a: OnUtno) -> dict:
    """What UT.no's page for a route says: its code and name there, the places at its ends, the description for
    each direction, grading and length. Nothing is changed. `text` is the description as it would be taken, and
    `same_code` whether UT.no has the route under a code of ours: if not, make sure with the person that it is
    the same route. To take the description: `describe_route` with `description` set to the text (or what the
    person makes of it) and `utno_id` to `number`. One page is read per call; do not go through routes with it."""
    from stiflyt2.api.owned_routes import utno_says

    return utno_says(routes.detail(c.conn, _route_id(c.conn, a.route)), a.address)


@tool("The changes to a route that can be taken back")
def changes(c: Call, a: RouteOnly) -> list:
    """The changes to a route and its line that can be taken back, newest first, each with its `txid` for
    `put_back`, who made it, and what stands in the way if anything."""
    return undo.of_route(c.conn, _route_id(c.conn, a.route))


# ---- Changing ----

class Make(Args):
    turrutebasen: str = Field(min_length=1, max_length=40, description="The label of a candidate in Turrutebasen "
                                                                       "(from `find` or `turrutebasen`), e.g. ron2 or jot91/1")


@tool("Take a route in from Turrutebasen", changes=True)
def make_yours(c: Call, a: Make) -> dict:
    """Make a route in Turrutebasen ours, as it is there. It is then not done: a person looks at it."""
    found = c.conn.execute("SELECT id FROM source_index.route WHERE lower(label) = lower(%s)", (a.turrutebasen.strip(),)).fetchone()
    if not found:
        raise places.NotFound(f"Turrutebasen has no candidate labelled {a.turrutebasen}")
    return _told(c.conn, routes.make(c.conn, found[0], c.author))


class MoveEnd(Args):
    route: RouteRef
    end: str = Field(pattern=UUID.pattern, description="The junction id of the end to move, from `route` (ends)")
    lon: Lon
    lat: Lat
    dry_run: DryRun = True


@tool("Move an end of a route", changes=True)
def move_end(c: Call, a: MoveEnd) -> dict:
    """Move one end of a route to another spot. It follows our stretches and Turrutebasen's segments there, or
    the mapped paths and roads where none leads (new stretches). Dropped on the route itself it is cut back;
    dropped on its own other end it becomes a round trip. No line is moved and no other route is changed."""
    rid = _route_id(c.conn, a.route)
    told = routes.move_end(c.conn, rid, routes.detail(c.conn, rid)["version"], c.author, a.end, a.lon, a.lat,
                           dry_run=a.dry_run)
    return _lean(told) if a.dry_run else {"told": _lean(told), **_told(c.conn, rid)}


class Via(Args):
    route: RouteRef
    stretch: Stretch
    lon: Lon
    lat: Lat
    dry_run: DryRun = True


@tool("Send a route another way", changes=True)
def send_via(c: Call, a: Via) -> dict:
    """Send a route by way of another spot: the stretch named gives way to the way round by the spot. Over our
    stretches and Turrutebasen's where they lead, else along mapped paths and roads. No line is moved."""
    rid = _route_id(c.conn, a.route)
    told = routes.send_via(c.conn, rid, routes.detail(c.conn, rid)["version"], c.author, a.stretch, a.lon, a.lat,
                           dry_run=a.dry_run)
    return _lean(told) if a.dry_run else {"told": _lean(told), **_told(c.conn, rid)}


class LayOut(Args):
    points: list[Point] = Field(min_length=2, max_length=50, description="[lon, lat] of each point, in order")
    code: str = Field(min_length=1, max_length=40, description="The code of the new route")
    name: str | None = Field(None, max_length=160)
    reach_m: float | None = Field(None, ge=1, le=500)


@tool("Make a new route through points", changes=True)
def lay_out(c: Call, a: LayOut) -> dict:
    """Make a new route of ours through the points. Look at it with `plan_way` first: this has no dry run."""
    return _told(c.conn, routes.lay_out(c.conn, c.author, a.points, a.code, a.name, a.reach_m))


class LayAgain(Args):
    route: RouteRef
    points: list[Point] = Field(min_length=2, max_length=50, description="[lon, lat] of each point, in order")
    reach_m: float | None = Field(None, ge=1, le=500)


@tool("Lay a route out again through points", changes=True)
def lay_out_again(c: Call, a: LayAgain) -> dict:
    """Replace the stretches of a route by the way through the points. It stays the same route. Look at it
    with `plan_way` first: this has no dry run. A point with no stretch and nothing mapped under it is the
    point itself, reached straight (a stretch of kind off_trail, to be said what it is with `set_stretch`)."""
    rid = _route_id(c.conn, a.route)
    routes.lay_out_again(c.conn, rid, routes.detail(c.conn, rid)["version"], c.author, a.points, a.reach_m)
    return _told(c.conn, rid)


class StretchOf(Args):
    route: RouteRef
    stretch: Stretch


@tool("Take a stretch out of a route", changes=True)
def take_stretch_out(c: Call, a: StretchOf) -> dict:
    """Take a stretch out of a route. The stretch stays for other routes. Out of the middle it leaves the route
    in two pieces; out of a round trip it opens it."""
    rid = _route_id(c.conn, a.route)
    routes.remove_leg(c.conn, rid, routes.detail(c.conn, rid)["version"], a.stretch, c.author)
    return _told(c.conn, rid)


class SetStretch(Args):
    stretch: Stretch
    kind: Literal["trail", "tractor_road", "road", "glacier", "water", "off_trail", "connector", "other"] | None = None
    marked: bool | None = None
    signed: bool | None = None


@tool("Say what a stretch is", changes=True)
def set_stretch(c: Call, a: SetStretch) -> dict:
    """Say what a stretch is (path, road, glacier, water, bare ground with no path) and whether it is marked
    and signed. It holds for every route on the stretch. A glacier is signed and never marked."""
    facts = a.model_dump(exclude={"stretch"}, exclude_none=True)
    if not facts:
        raise places.Invalid("say what to change: kind, marked or signed")
    routes.set_stretch(c.conn, a.stretch, c.author, **facts)
    return {"stretch": a.stretch, **facts}


class Divide(Args):
    stretch: Stretch
    lon: Lon
    lat: Lat


@tool("Divide a stretch in two", changes=True)
def divide_stretch(c: Call, a: Divide) -> dict:
    """Divide a stretch at the spot on it nearest a point, so that its parts can be said to be different
    things, or a route can end there. Every route on it then uses both parts."""
    return {"junction": routes.split(c.conn, a.stretch, c.author, a.lon, a.lat)}


class Describe(Args):
    route: RouteRef
    code: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=160)
    maintainers: list[Annotated[str, Field(max_length=120)]] | None = Field(None, max_length=10)
    everywhere: bool = Field(False, description="With maintainers: also on the stretches that say otherwise")
    grading: Literal["", "G", "B", "R", "S", "I"] | None = None
    description: str | None = Field(None, max_length=12000)
    utno_id: int | None = Field(None, ge=0, le=10**12, description="The number of the route's page on UT.no; "
                                "0: it has none. Given with a description: the description was taken from there")


@tool("Change what is said about a route", changes=True)
def describe_route(c: Call, a: Describe) -> dict:
    """Change a route's code, name, who maintains it, its grading, its description in words, or which page on
    UT.no is this route's."""
    rid = _route_id(c.conn, a.route)
    said = a.model_dump(exclude={"route", "everywhere"}, exclude_none=True)
    if not said:
        raise places.Invalid("say what to change")
    routes.update(c.conn, rid, routes.detail(c.conn, rid)["version"], c.author, everywhere=a.everywhere, **said)
    return _told(c.conn, rid)


class OnMapped(Args):
    route: RouteRef
    dry_run: DryRun = True


@tool("Lay a route on the mapped paths beside it", changes=True)
def lay_on_mapped(c: Call, a: OnMapped) -> dict:
    """Lay a route on the mapped paths and roads wherever one runs beside it (within 25 m); elsewhere its
    line stays. Dry, it says how much runs beside a mapped line and what the route would become. This moves
    lines, so every route on those stretches moves too."""
    rid = _route_id(c.conn, a.route)
    return _lean(routes.compare_with_mapped(c.conn, rid, c.author, routes.detail(c.conn, rid)["version"],
                                            dry_run=a.dry_run))


class State(Args):
    route: RouteRef
    state: Literal["done", "not done", "discontinued", "in use", "not published", "published", "withdrawn"]
    why: str | None = Field(None, max_length=400, description="For discontinued and not published: the reason")
    since: date | None = Field(None, description="For discontinued: since when (today if left out)")
    the_person_said_so: bool = Field(False, description="True only when the person has said this in the conversation")


@tool("Say that a route is done, discontinued, not published or withdrawn", changes=True)
def set_state(c: Call, a: State) -> dict:
    """Say where a route stands. Done: a person has looked and it is right. Discontinued: given up in the
    terrain, kept for its history. Not published: kept, and not shown to the public. Withdrawn: no route of
    ours. Each has its opposite (not done, in use, published). Only on the person's word."""
    if not a.the_person_said_so:
        raise places.Invalid(f"a route is said to be {a.state} by a person, not by an assistant: ask them, and "
                             "say so with the_person_said_so")
    rid = _route_id(c.conn, a.route)
    now = routes.detail(c.conn, rid)
    version, code = now["version"], now["code"]
    if a.state == "withdrawn":
        routes.retire(c.conn, rid, c.author)
        return {"route": code, "withdrawn": True}
    if a.state in ("done", "not done"):
        routes.review(c.conn, rid, version, c.author, done=a.state == "done")
    elif a.state == "discontinued":
        routes.discontinue(c.conn, rid, version, c.author, a.why or "", a.since)
    elif a.state == "in use":
        routes.take_up_again(c.conn, rid, version, c.author)
    elif a.state == "not published":
        routes.unpublish(c.conn, rid, version, c.author, a.why or "")
    else:
        routes.publish(c.conn, rid, version, c.author)
    told = routes.detail(c.conn, rid)
    return {**_told(c.conn, rid), "discontinued": told["discontinued"], "not_published": told["unpublished"]}


class Approve(Args):
    action: dict = Field(description="The `action` of one proposal, exactly as `proposed` gave it")


@tool("Carry out one proposed repair", changes=True)
def approve_proposal(c: Call, a: Approve) -> dict:
    """Carry out one proposal from `proposed`, as the person who approved it."""
    return {"route": proposals.approve(c.conn, c.author, a.action)}


class Place(Args):
    junction: str = Field(pattern=UUID.pattern, description="The junction, e.g. an end of a route from `route`")
    name: str | None = Field(None, max_length=120, description="Left out: named after what lies there, if anything")


@tool("Make a place where a route ends", changes=True)
def make_place(c: Call, a: Place) -> dict:
    """Make a place at a junction, with that junction as its arrival point. A route is named after the places
    at its ends."""
    return _lean(places.detail(c.conn, places.at_node(c.conn, a.junction, c.author, a.name)))


SiteKey = Annotated[str, Field(max_length=60, description="The key of a sign site, from `signs`: place:…, node:… or point:…")]


def _site_after(conn, area: str, key: str | None = None, *, post: str | None = None, sign: str | None = None) -> dict:
    """A sign site as it is now: by its key, or the one that has a post or a sign."""
    for site in sign_ops.listing(conn, area)["sites"]:
        if (site["key"] == key or any(p["key"] == post for p in site["posts"])
                or any(g["key"] == sign for p in site["posts"] for g in p["signs"])):
            return site
    raise places.NotFound("no such sign site, post or sign in that area: `signs` lists them with their keys")


class Signs(Args):
    area: Area
    site: str | None = Field(None, max_length=120, description="A site's name, code (BRE-SS-0001) or key: gives that "
                                                               "site in full. Left out: every site in short")


@tool("The signs of an area")
def signs(c: Call, a: Signs) -> dict:
    """The sign sites of an area, worked out from the routes with what people have decided laid over. In short:
    each site's key, name (or, for a site with none, what it lies near), code and how many posts and signs it
    has. With `site`: that site in full, with
    each post (its key, where it stands, whether that is assumed) and each sign (its key, text, kilometres,
    arrow, colour, routes, what it would say untouched, what is said by hand, and whether it says something else
    than when it was last ordered)."""
    found = sign_ops.listing(c.conn, a.area)
    sites = found["sites"]
    if a.site:
        said = a.site.strip().lower()
        hits = [s for s in sites if said in (s["key"].lower(), (s["code"] or "").lower(), (s["name"] or "").lower())] or [
            s for s in sites if s["name"] and said in s["name"].lower()]
        if not hits:
            raise places.NotFound(f"no sign site in {a.area} is called {a.site}. Leave `site` out to see them all")
        return {"area": a.area, "sites": hits}
    return {"area": a.area, "totals": found["totals"], "sites": [
        {"key": s["key"], "name": s["name"], "code": s["code"], "routes": s["routes"],
         **({"no_signs": True} if s["no_signs"] else {}),
         "posts": sum(not p["taken_out"] for p in s["posts"]),
         "signs": sum(not g["taken_out"] for p in s["posts"] if not p["taken_out"] for g in p["signs"]),
         **({"free": True} if s["free"] else {}), **({"near": s["near"]["name"]} if s["near"] else {})} for s in sites]}


class SignSite(Args):
    area: Area
    site: SiteKey
    no_signs: bool | None = Field(None, description="True: there are to be no signs here, and nothing of it is "
                                                    "ordered. False: there are after all")
    name: str | None = Field(None, max_length=120, description="Empty: back to the place's own name")
    back_text: str | None = Field(None, max_length=400, description="Empty: back to name and position")
    send_to_name: str | None = Field(None, max_length=160)
    send_to_address: str | None = Field(None, max_length=400)
    dry_run: DryRun = True


@tool("Decide about a sign site", changes=True, operations=True)
def sign_site(c: Call, a: SignSite) -> dict:
    """Say that a sign site is to have no signs, or say its name, the text on the back of its signs and who they
    are sent to. A site has no other state: it is its signs that are ordered and put up (`sign`). It gets its
    code the first time a sign of it is ordered. Gives the site as it is after."""
    sign_ops.set_site(c.conn, a.site, c.author, **a.model_dump(exclude={"site", "dry_run"}))
    return _site_after(c.conn, a.area, a.site)


class SignPost(Args):
    area: Area
    post: str | None = Field(None, max_length=60, description="The key of a post, from `signs`. Left out with "
                                                              "`junction`: a further post is put there")
    junction: str | None = Field(None, max_length=40, description="For a further post: the junction (the first part "
                                                                  "of a post's key) it stands at")
    lon: Lon | None = None
    lat: Lat | None = None
    assumed: bool = Field(False, description="True: back to where a post is assumed to stand")
    taken_out: bool | None = None
    name_sign: bool | None = Field(None, description="Whether the post carries a sign with the place's name on top "
                                                     "(no arrow), where nothing else says where one is")
    name_text: str | None = Field(None, max_length=120, description="What the name sign says, if not the site's name")
    dry_run: DryRun = True


@tool("Say where a sign post stands", changes=True, operations=True)
def sign_post(c: Call, a: SignPost) -> dict:
    """Say where a post stands (longitude, latitude), which works its arrows out anew: an arrow is read by someone
    standing at the junction and looking at the post, so a post on the other side of the path turns them. Or put
    it back where it is assumed to stand, take it out, say that it carries a sign with the place's name on top,
    or put a further post at a junction (`junction`, `lon`, `lat`), for a place with several. Signs are moved
    onto a further post with `sign`. Gives the site after."""
    if a.post is None:
        if a.junction is None or a.lon is None or a.lat is None:
            raise places.Invalid("name a post, or give a junction and where the further post stands")
        made = sign_ops.add_post(c.conn, a.junction, c.author, a.lon, a.lat)
        return _site_after(c.conn, a.area, post=made)
    sign_ops.set_post(c.conn, a.post, c.author, lon=a.lon, lat=a.lat, assumed=a.assumed, taken_out=a.taken_out,
                      name_sign=a.name_sign, name_text=a.name_text)
    return _site_after(c.conn, a.area, post=a.post)


class OneSign(Args):
    area: Area
    sign: str = Field(max_length=160, description="The key of a sign, from `signs`")
    direction: Literal["", "venstre", "rett fram", "høyre"] | None = Field(None, description="Empty: as worked out")
    colour: Literal["", "trehvit", "grønn"] | None = None
    text: str | None = Field(None, max_length=120, description="Empty: as worked out")
    distance_m: float | None = Field(None, ge=0, le=500_000, description="Metres, as the sign is to say them "
                                                                         "(rounded as signs are). 0: as worked out")
    post_no: int | None = Field(None, ge=0, le=20, description="Which post at its junction it hangs on. 0: the first")
    taken_out: bool | None = None
    to_order: bool | None = Field(None, description="True: it is to be made, and goes on the order that is taken out "
                                                    "next. False: off it")
    why: str | None = Field(None, max_length=200, description="Why it is to be made: worn, broken, says the wrong thing")
    up: bool | None = Field(None, description="For a sign that has been ordered: True, it is up; False, not after all")
    dry_run: DryRun = True


@tool("Say something else of a sign", changes=True, operations=True)
def sign(c: Call, a: OneSign) -> dict:
    """Say something else of one sign than what is worked out: its arrow, colour, text, distance, which post it
    hangs on, or that it is taken out; or mark it to be made (`to_order`, with `why`), which puts it on the order
    that is taken out next. What is said stands when the routes change; an empty value goes back to what is
    worked out. Gives the site after."""
    said = a.model_dump(exclude={"area", "sign", "dry_run", "up"}, exclude_none=True)
    if said:
        sign_ops.set_sign(c.conn, a.sign, c.author, **said)
    if a.up is not None and not sign_ops.mark_up(c.conn, [a.sign], c.author, a.up):
        raise places.Invalid("that sign has not been ordered, so it is not waiting to be put up")
    return _site_after(c.conn, a.area, sign=a.sign)


class NewSite(Args):
    area: Area
    at: Point
    name: str | None = Field(None, max_length=120)
    dry_run: DryRun = True


@tool("Put a sign site at a spot on a route", changes=True, operations=True)
def new_sign_site(c: Call, a: NewSite) -> dict:
    """Put a sign site at a spot on a route that is no junction (where a path leaves a road, at a bridge). The
    spot must lie on a route in use, and not at a junction: junctions have their sites already. Gives the site,
    with signs both ways along the route."""
    return _site_after(c.conn, a.area, sign_ops.add_site(c.conn, c.author, a.area, a.at[0], a.at[1], a.name))


class Destination(Args):
    area: Area
    post: str = Field(max_length=60, description="The key of the post, from `signs`")
    place: str | None = Field(None, max_length=120, description="A place of ours, by name or id")
    at: Point | None = Field(None, description="Or where it is: a place of ours within 150 m is meant, else the spot "
                                               "on the route there, which needs `name`")
    name: str | None = Field(None, max_length=120, description="What the sign is to say, for a spot that is no place")
    dry_run: DryRun = True


@tool("Add a destination to a sign post", changes=True, operations=True)
def add_sign_destination(c: Call, a: Destination) -> dict:
    """Add a destination to a post that the routes do not give it: a place nearer than the next hut (a farm on
    the way), or one further on than it. It points the shortest way there over the routes, and is told as added
    by hand. Give a place of ours, or a position: a place within 150 m of it is the one meant, else it is a spot
    on a route, and `name` says what the sign is to say. Gives the site after."""
    place = None
    if a.place:
        if UUID.match(a.place.strip()):
            place = a.place.strip()
        else:
            found = [f for f in identity.lookup(c.conn, a.place) if f["type"] == "place"
                     and str(f["label"]).lower() == a.place.strip().lower()]
            if len(found) != 1:
                raise places.NotFound(f"{len(found) or 'no'} places of ours are called {a.place}: give its id, or where it is")
            place = str(found[0]["id"])
    got = sign_ops.add_destination(c.conn, c.author, a.post, place=place, lon=a.at[0] if a.at else None,
                                   lat=a.at[1] if a.at else None, name=a.name)
    if "needs" in got:
        raise places.Invalid(got["why"])
    return _site_after(c.conn, a.area, sign=got["key"])


class OrderSigns(Args):
    area: Area
    signs: list[Annotated[str, Field(max_length=160)]] | None = Field(None, max_length=2000, description=(
        "Keys of the signs to order. Left out: the signs that are marked to be made"))
    note: str | None = Field(None, max_length=400)
    dry_run: DryRun = True


@tool("Take out an order for signs", changes=True, operations=True)
def order_signs(c: Call, a: OrderSigns) -> dict:
    """Take out an order for the signs that are marked to be made (most signs hang already: an order is the ones
    to be replaced, marked with `sign` and `to_order`). What each sign says now is kept with the order and
    stays, whatever happens to the routes; the signs are then no longer marked. Dry, it gives the rows the order would hold. For real, the order's
    number and where the list for whoever makes the signs is fetched. Only when the person has asked for it."""
    if a.dry_run:
        rows = sign_ops.order_rows(c.conn, a.area, a.signs)
        return {"would_order": len(rows), "rows": rows}
    done = sign_ops.take_out_order(c.conn, a.area, c.author, a.signs, a.note)
    return {**done, "list": f"/api/v1/signs/orders/{done['id']}.xlsx"}


class Adrift(Args):
    pass


@tool("Decisions about signs that have lost their sign")
def signs_adrift(c: Call, a: Adrift) -> dict:
    """What has been decided about a sign, a post or a sign site that is no longer there to decide about, in
    any area: the routes have been changed since. Each says what it said, where it was, why it is adrift, and
    for a sign which signs at the same junction it could be moved to (`moves`). Nothing is done about them by
    itself. Settle one with `settle_adrift`."""
    found = sign_ops.adrift(c.conn)
    return {"adrift": found, "count": len(found)}


class Settle(Args):
    adrift: str = Field(max_length=40, description="The `id` of what is adrift, from `signs_adrift`")
    move_to: str | None = Field(None, max_length=160, description=(
        "The key of the sign it is about now, one of its `moves`. Left out: it is forgotten"))
    dry_run: DryRun = True


@tool("Settle a decision that has lost its sign", changes=True, operations=True)
def settle_adrift(c: Call, a: Settle) -> dict:
    """Say which sign a decision that is adrift is about now (`move_to`), or forget it. Only when the person has
    said which: which sign a decision belongs to is theirs to say. Gives what is still adrift after."""
    done = (sign_ops.move(c.conn, a.adrift, a.move_to, c.author) if a.move_to
            else sign_ops.forget(c.conn, a.adrift, c.author))
    return {"moved_to" if a.move_to else "forgotten": done, "still_adrift": len(sign_ops.adrift(c.conn))}


class UndoSigns(Args):
    dry_run: DryRun = True


@tool("Take back your own latest decision about signs", changes=True, operations=True)
def undo_signs(c: Call, a: UndoSigns) -> dict:
    """Take back the latest decision about signs made through an assistant in this person's name: a sign
    changed, a post moved, signs put on the order. Not what the person did in the page, and not an order that
    has been taken out, which stands. Dry, it says what it would take back."""
    if a.dry_run:
        return {"would_take_back": sign_undo.last(c.conn, c.author)}
    return sign_undo.undo(c.conn, c.author)


class Undo(Args):
    dry_run: DryRun = True


@tool("Take back your own latest change", changes=True)
def undo_last(c: Call, a: Undo) -> dict:
    """Take back the latest change made through an assistant in this person's name. Not what the person did in
    the map, and not what others did. Dry, it says what it would take back."""
    mine = undo.whose(c.author)
    if a.dry_run:
        return {"would_take_back": undo.last(c.conn, mine)}
    return undo.undo(c.conn, c.author)


@tool("Bring back what you took back last", changes=True)
def redo_last(c: Call, a: Undo) -> dict:
    """Bring back the change that was taken back last through an assistant in this person's name, as long as
    nothing has been changed through an assistant since. Several taken back in a row come back in the order
    they were made. Dry, it says what it would bring back."""
    mine = undo.whose(c.author)
    if a.dry_run:
        return {"would_bring_back": undo.undone_last(c.conn, mine)}
    return undo.redo(c.conn, c.author)


class Back(Args):
    route: RouteRef
    txid: int = Field(description="From `changes`: that change and every later change to the route are taken back")


@tool("Put a route back as it was", changes=True)
def put_back(c: Call, a: Back) -> dict:
    """Put a route back as it was before one of its changes, whoever made them: that change and every later
    one to the route and its line. What was done to other routes in between stays. Look at `changes` first."""
    return undo.back(c.conn, c.author, _route_id(c.conn, a.route), a.txid)


class Gap(Args):
    wanted: str = Field(min_length=1, max_length=2000, description="What the person wanted, in their words")
    tried: str | None = Field(None, max_length=2000, description="What you tried")
    why_not: str | None = Field(None, max_length=2000, description="Why it could not be done, or what looked wrong")
    route: RouteRef | None = None


@tool("Report what could not be done", changes=True)
def report_gap(c: Call, a: Gap) -> dict:
    """Tell whoever looks after the tool that something could not be done, had to be gone round, or looks
    wrong. Call it whenever that happens, with the person's own words."""
    about = None
    if a.route:
        try:
            about = ("route", _route_id(c.conn, a.route))
        except places.NotFound:
            about = None
    return {"kept_as": feedback.record(
        c.conn, "reported", "mcp", "report_gap", a.wanted, who=str(c.author), about=about,
        arguments={k: v for k, v in (("tried", a.tried), ("why_not", a.why_not)) if v})}


# ---- The protocol ----

def _listed(person: access.Person | None = None) -> list[dict]:
    """The tools, as an assistant is told of them: not those of a feature the person does not have."""
    listed = []
    for t in TOOLS.values():
        if t.feature and not (person and person.has(t.feature)):
            continue
        schema = t.args.model_json_schema()
        schema.pop("title", None)
        listed.append({"name": t.name, "title": t.title, "description": t.description, "inputSchema": schema,
                       "annotations": {"title": t.title, "readOnlyHint": not t.changes,
                                       "destructiveHint": t.name in ("set_state", "take_stretch_out", "put_back"),
                                       "openWorldHint": False}})
    return listed


def _text(value, error: bool = False) -> dict:
    said = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return {"content": [{"type": "text", "text": said}], "isError": error}


class Server:
    """Answers MCP requests for one person at a time. Each tool call gets a connection of its own."""

    def __init__(self, read: ConnectionPool, write: ConnectionPool) -> None:
        self.read, self.write = read, write

    def _keep(self, kind: str, name: str, arguments: dict, who: str | None, message: str, trace: str | None = None) -> None:
        """Note a refusal or a failure with the feedback. Never lets the note become a fault."""
        try:
            about = None
            ref = arguments.get("route") if isinstance(arguments, dict) else None
            with self.write.connection() as conn:
                if isinstance(ref, str) and ref:
                    try:
                        about = ("route", _route_id(conn, ref))
                    except REFUSALS:
                        about = None
                feedback.record(conn, kind, "mcp", f"tool {name}", message, who=who, arguments=arguments,
                                about=about, trace=trace)
        except Exception:
            log.exception("could not keep feedback about the tool %s", name)

    def call(self, person: access.Person, name: str, arguments: dict) -> dict:
        t = TOOLS[name]
        if t.feature and not person.has(t.feature):
            return _text(f"that is not switched on for you: {FEATURES[t.feature].ask}", error=True)
        if t.changes and not (person.operates if t.operations else person.edits):
            return _text("you are signed in to look, not to change: ask for the "
                         f"{'operations' if t.operations else 'editor'} role", error=True)
        try:
            args = t.args.model_validate(arguments or {})
        except ValidationError as e:
            said = "; ".join(f"{'.'.join(str(p) for p in d['loc']) or 'arguments'}: {d['msg']}" for d in e.errors())
            self._keep("refused", name, arguments, person.name, f"the arguments were not understood: {said}")
            return _text(f"the arguments were not understood: {said}", error=True)
        author = auth.Author(person.name or "", "mcp", person.email)
        try:
            with (self.write if t.writes else self.read).connection() as conn:
                result = t.run(Call(conn, author, person), args)
                if getattr(args, "dry_run", False) or not t.changes:
                    conn.rollback()         # looked at only: nothing of it is kept
            return _text(result)
        except REFUSALS as e:
            self._keep("refused", name, arguments, person.name, str(e))
            return _text(str(e), error=True)
        except Exception as e:
            self._keep("failed", name, arguments, person.name, f"{type(e).__name__}: {e}", traceback.format_exc())
            log.exception("the tool %s failed", name)
            return _text(f"it failed ({type(e).__name__}), and that has been noted for whoever looks after the tool. "
                         "Nothing was changed.", error=True)

    def answer(self, person: access.Person, message: dict) -> dict | None:
        """The answer to one JSON-RPC message, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                    "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request"}}
        if "id" not in message:
            return None                 # a notification: initialized, cancelled. Nothing to say.
        method, params, mid = message["method"], message.get("params") or {}, message["id"]

        def result(value: dict) -> dict:
            return {"jsonrpc": "2.0", "id": mid, "result": value}

        def error(code: int, said: str) -> dict:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": said}}

        if method == "initialize":
            asked = params.get("protocolVersion")
            return result({"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": SERVER, "instructions": INSTRUCTIONS})
        if method == "ping":
            return result({})
        if method == "tools/list":
            return result({"tools": _listed(person)})
        if method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                return error(-32602, f"no tool is called {name}")
            return result(self.call(person, name, params.get("arguments") or {}))
        return error(-32601, f"this server does not do {method}")


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    server = Server(read, write)

    @app.post(PATH, include_in_schema=False)
    async def mcp(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "not from another site's page"}, status_code=403)
        person = auth.person_of(request)
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}},
                                status_code=400)

        def work():
            if isinstance(body, list):
                return [a for a in (server.answer(person, m) for m in body) if a is not None]
            return server.answer(person, body)

        answered = await to_thread.run_sync(work)
        if answered is None or answered == []:
            return Response(status_code=202)        # only notifications: taken, nothing to answer
        return JSONResponse(answered)

    @app.get(PATH, include_in_schema=False)
    def no_stream() -> Response:
        """There is nothing to listen for: every answer comes with its request."""
        return Response(status_code=405, headers={"Allow": "POST"})
