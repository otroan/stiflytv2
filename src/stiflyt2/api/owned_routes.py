"""Owned routes over HTTP: make a candidate yours, add pieces, join them with crossings (ADR-003)."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import height, utno
from stiflyt2.api import auth
from stiflyt2.api.places import Editor
from stiflyt2.domain import (
    corrections,
    export,
    factsheet,
    identity,
    overview,
    places,
    proposals,
    report,
    routes,
    undo,
    worklist,
)

V1 = "/api/v1/owned/routes"
HEIGHT_POINTS = 1500    # as many points as are asked of the height service while a person waits (some 8 seconds)


class Make(BaseModel):
    candidate_id: str = Field(max_length=40)
    code: str | None = Field(None, max_length=40)


class Change(BaseModel):
    version: int
    code: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=160)
    code_provisional: bool | None = None
    maintainers: list[Annotated[str, Field(max_length=120)]] | None = Field(None, max_length=10)
    everywhere: bool = False
    grading: Literal["", "G", "B", "R", "S", "I"] | None = None
    description: str | None = Field(None, max_length=12000)
    utno_id: int | None = Field(None, ge=0, le=10**12)      # the number of its page on UT.no; 0: it has none


class Leg(BaseModel):
    version: int
    maintainers: list[Annotated[str, Field(max_length=120)]] | None = Field(None, max_length=10)


class Piece(BaseModel):
    version: int
    candidate_id: str = Field(max_length=40)


class Crossing(BaseModel):
    version: int
    from_node: str
    to_node: str
    kind: Literal["water", "glacier", "other", "connector"]


class Version(BaseModel):
    version: int
    done: bool = True


class Stretch(BaseModel):
    kind: Literal["trail", "tractor_road", "road", "glacier", "water", "off_trail", "connector",
                  "other"] | None = None
    marked: bool | None = None
    signed: bool | None = None


class Snap(BaseModel):
    dry_run: bool = False


class Line(BaseModel):
    points: list[tuple[Annotated[float, Field(ge=-180, le=180)],
                       Annotated[float, Field(ge=-90, le=90)]]] = Field(max_length=2000)
    follow: bool = False
    dry_run: bool = False


class Position(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    snap_m: float | None = Field(None, ge=0, le=50)
    dry_run: bool = False


class Revision(BaseModel):
    rev_no: int = Field(ge=1)


Spot = tuple[Annotated[float, Field(ge=-180, le=180)], Annotated[float, Field(ge=-90, le=90)]]


class Pull(BaseModel):
    grab: Spot
    drop: Spot
    corner_m: float = Field(25.0, ge=1, le=1000)      # as near a corner of a hand-drawn line as counts as the corner


class Along(BaseModel):
    version: int
    edge_id: str | None = None
    segment_id: str | None = Field(None, max_length=60)


Points = list[tuple[Annotated[float, Field(ge=-180, le=180)], Annotated[float, Field(ge=-90, le=90)]]]


class Discontinued(BaseModel):
    version: int
    why: str = Field(min_length=1, max_length=400)
    since: date | None = None


class Unpublished(BaseModel):
    version: int
    why: str = Field(min_length=1, max_length=400)


class Way(BaseModel):
    points: Points = Field(min_length=2, max_length=50)
    reach_m: float | None = Field(None, ge=1, le=2000)
    code: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=160)
    version: int | None = None


class Moved(BaseModel):
    version: int
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    end: str | None = Field(None, max_length=40)        # the end that is moved
    edge_id: str | None = Field(None, max_length=40)    # or the stretch that was grabbed
    reach_m: float | None = Field(None, ge=1, le=500)
    dry_run: bool = False


class Finding(BaseModel):
    key: str = Field(max_length=400)
    kind: str = Field(max_length=40)
    label: str = Field(max_length=200)
    text: str = Field(max_length=400)


class Undo(BaseModel):
    txid: int | None = None


class Action(BaseModel):
    kind: Literal["twig", "gap", "loop", "double", "snap", "reach", "place", "arrival", "passed"]
    node: str | None = Field(None, max_length=40)
    candidate: str | None = Field(None, max_length=40)
    route: str | None = Field(None, max_length=40)
    edge: str | None = Field(None, max_length=40)
    place: str | None = Field(None, max_length=40)


class Approved(BaseModel):
    actions: list[Action] = Field(max_length=1000)


# The revision of a stretch that the one asking has on their map: a change is refused if it is no longer that.
Seen = Annotated[int | None, Header(alias="X-Stiflyt-Seen-Rev")]
Area = Annotated[str, Path(pattern="^[A-Za-z]{3}$")]
AreaOrAll = Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")]


def utno_says(route: dict, address: str | None) -> dict:
    """What UT.no's page says about a route of ours, and whether it looks like the same route."""
    if not address and not route["utno_id"]:
        raise places.Invalid(f"which page on UT.no is {route['code']}? Give the address of the route's page there, "
                             "like https://ut.no/rutebeskrivelse/13394424: it is asked for once, and kept")
    found = utno.fetch(utno.number(address or route["utno_id"]))
    ours = {route["code"].lower(), *(s["code"].lower() for s in route["sources"] if s["code"]),
            *(e["value"].lower() for e in route["earlier"] if e["kind"] == "code")}
    return {**found, "text": utno.described(found), "same_code": bool(found["code"]) and found["code"].lower() in ours,
            "ours": {"code": route["code"], "length_m": route["length"]["on_foot_m"],
                     "has_description": bool(route["description"]), "utno_id": route["utno_id"]}}


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get("/api/v1/areas/{area}/worklist")
    def todo(area: Area) -> dict:
        """What needs attention in an area: shapes, geometry, names, places."""
        with read.connection() as conn:
            return worklist.worklist(conn, area)

    @app.get("/api/v1/report")
    def areas_report(maintainer: Annotated[str | None, Query(max_length=120)] = None) -> dict:
        """Every area side by side: ground covered and how much of it is marked, shared stretches counted once."""
        with read.connection() as conn:
            return report.areas(conn, maintainer)

    @app.get("/api/v1/owned/areas")
    def areas_in_use() -> list[dict]:
        """The areas that have routes of yours in use: how many, and how far they go."""
        with read.connection() as conn:
            return overview.areas(conn)

    @app.get("/api/v1/owned/areas/{area}")
    def area_in_use(area: Area) -> dict:
        """One area: its routes in use with length, climb, time and grading, and what they add up to."""
        with read.connection() as conn:
            return overview.area(conn, area)

    @app.get("/api/v1/maintainers")
    def who_maintains() -> dict:
        """Who maintains routes, by the ground they look after; and whose routes the page starts with."""
        from stiflyt2.config import settings

        with read.connection() as conn:
            return {"home": settings.home_maintainer or None, "maintainers": report.maintainers(conn)}

    @app.get("/api/v1/areas/{area}/report")
    def area_report(area: Area) -> dict:
        """Where an area stands: routes, lengths, maintainers, the network, and what is left."""
        with read.connection() as conn:
            return report.report(conn, area)

    @app.get("/api/v1/current/{subject}/{subject_id}")
    def stands_for(subject: Literal["route", "node", "edge", "place"],
                   subject_id: Annotated[str, Path(max_length=40)]) -> dict:
        """What an id stands for now: itself, what came instead of it, or nothing."""
        with read.connection() as conn:
            return identity.current(conn, subject, subject_id)

    @app.get("/api/v1/lookup")
    def known_as(q: Annotated[str, Query(min_length=1, max_length=160)]) -> list[dict]:
        """Routes and places that are, or once were, known by this code or name."""
        with read.connection() as conn:
            return identity.lookup(conn, q)

    @app.get("/api/v1/areas/{area}/mapped")
    def against_mapped(area: Area) -> dict:
        """Every route of the area against the mapped paths and roads: what laying each on them would give."""
        with read.connection() as conn:
            return proposals.against_mapped(conn, area)

    @app.post("/api/v1/areas/{area}/mapped")
    def lay_all_beside(area: Area, author: Editor) -> dict:
        """Lay every route of the area that is not done on the mapped paths and roads beside it. One action."""
        with write.connection() as conn:
            return proposals.lay_all_beside(conn, area, author)

    @app.get("/api/v1/health")
    def health() -> dict:
        """Whether the nightly check of the sources and the nightly copy have run, and what to warn of."""
        from pathlib import Path as Folder

        from stiflyt2 import health as nightly

        with read.connection() as conn:
            return nightly.status(conn, Folder("var/backup"))

    @app.get("/api/v1/latest")
    def latest() -> dict | None:
        """The latest thing anybody changed. A page asks now and then, to notice what others have done."""
        with read.connection() as conn:
            return routes.latest(conn)

    @app.get("/api/v1/undo")
    def undoable(request: Request) -> dict | None:
        """Your latest change that has not been undone, made the way you are asking: what an undo would take back."""
        who = auth.person_of(request)
        if not who.edits:
            return None
        with read.connection() as conn:
            return undo.last(conn, (who.name, who.through))

    @app.post("/api/v1/undo")
    def take_back(body: Undo, author: Editor) -> dict:
        """Take the latest change back, whatever it was."""
        with write.connection() as conn:
            return undo.undo(conn, author, body.txid)

    @app.post("/api/v1/findings/accepted", status_code=204)
    def accept(body: Finding, author: Editor) -> Response:
        """A person has looked at a network finding and it is right as it is: do not list it again."""
        with write.connection() as conn:
            conn.execute("""INSERT INTO core.accepted_finding (key, kind, label, text, author)
                            VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING""",
                         (body.key, body.kind, body.label, body.text, author))
        return Response(status_code=204)

    @app.delete("/api/v1/findings/accepted", status_code=204)
    def bring_back(key: Annotated[str, Query(max_length=400)], author: Editor) -> Response:
        """List the finding again."""
        with write.connection() as conn:
            conn.execute("DELETE FROM core.accepted_finding WHERE key = %s", (key,))
        return Response(status_code=204)

    @app.get("/api/v1/areas/{area}/proposals")
    def proposed(area: Area) -> list[dict]:
        """Repairs simple enough to approve at a glance. Each is tried out first; nothing is kept."""
        with write.connection() as conn:
            try:
                return proposals.proposals(conn, area)
            finally:
                conn.rollback()

    @app.post("/api/v1/proposals/approve")
    def approve(body: Approved, author: Editor) -> dict:
        """Carry out the proposals a person has approved. One that no longer applies is skipped."""
        done, failed = 0, []
        with write.connection() as conn:
            for action in body.actions:
                what = action.model_dump(exclude_none=True)
                try:
                    with conn.transaction():
                        proposals.approve(conn, author, what)
                    done += 1
                except (places.Invalid, places.Conflict, places.NotFound) as err:
                    failed.append({"action": what, "why": str(err)})
        return {"done": done, "failed": failed}

    @app.get("/api/v1/corrections")
    def to_turrutebasen(area: AreaOrAll = None) -> list[dict]:
        """What to ask Kartverket to change in Turrutebasen, by its own segment ids."""
        with read.connection() as conn:
            found = corrections.corrections(conn, area)
        return [{k: v for k, v in c.items() if k not in ("geometry", "was_geometry")} for c in found]

    @app.get("/api/v1/corrections.geojson")
    def to_turrutebasen_file(area: AreaOrAll = None,
                             srid: Annotated[Literal[4326, 25833], Query()] = 4326) -> JSONResponse:
        """The same as a GeoJSON file to hand over: each line as proposed, and as it is now."""
        with read.connection() as conn:
            found = corrections.corrections(conn, area, srid)
        name = f"turrutebasen-corrections-{area.lower() if area else 'all'}.geojson"
        return JSONResponse(corrections.as_geojson(found, srid), media_type="application/geo+json",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/v1/routes.geojson")
    def routes_file(area: AreaOrAll = None, changed: bool = False, discontinued: bool | None = None,
                    srid: Annotated[Literal[4326, 25833], Query()] = 4326) -> JSONResponse:
        """Your routes as they are now, one feature per route, with what has changed from Turrutebasen.

        In use and discontinued, each saying which; `discontinued=false` gives only
        those in use, `discontinued=true` only the discontinued ones.
        """
        with read.connection() as conn:
            found = export.routes_geojson(conn, area, changed, srid, discontinued)
        name = f"{found['name']}{'-changed' if changed else ''}.geojson"
        return JSONResponse(found, media_type="application/geo+json",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/v1/areas/{area}/routes", status_code=201)
    def make_all(area: Area, author: Editor) -> dict:
        """Make every clean candidate of the area yours, marked as not reviewed."""
        with write.connection() as conn:
            return routes.make_default(conn, area)

    @app.post(f"{V1}/{{route_id}}/review")
    def review(route_id: str, body: Version, author: Editor) -> dict:
        with write.connection() as conn:
            routes.review(conn, route_id, body.version, author, body.done)
            return routes.detail(conn, route_id)

    @app.patch("/api/v1/owned/stretches/{edge_id}", status_code=204)
    def change_stretch(edge_id: str, body: Stretch, author: Editor, seen: Seen = None) -> Response:
        """Say what a stretch is, and whether it is marked and signed."""
        with write.connection() as conn:
            routes.as_seen(conn, edge_id, seen)
            routes.set_stretch(conn, edge_id, author, **body.model_dump(exclude_unset=True))
        return Response(status_code=204)

    @app.post("/api/v1/owned/stretches/{edge_id}/snap")
    def snap(edge_id: str, body: Snap, author: Editor, seen: Seen = None) -> dict:
        """Replace a stretch's line with the mapped path between its ends, or preview that."""
        if body.dry_run:
            with read.connection() as conn:
                return routes.snap_preview(conn, edge_id)
        with write.connection() as conn:
            routes.as_seen(conn, edge_id, seen)
            return routes.snap(conn, edge_id, author)

    @app.post("/api/v1/owned/stretches/{edge_id}/beside")
    def lay_beside(edge_id: str, body: Snap, author: Editor, seen: Seen = None) -> dict:
        """Lay a stretch on the mapped paths and roads where one runs beside it, or preview that."""
        with (read if body.dry_run else write).connection() as conn:
            routes.as_seen(conn, edge_id, None if body.dry_run else seen)
            return routes.lay_beside(conn, edge_id, author, body.dry_run)

    @app.get("/api/v1/owned/routes/{route_id}/mapped")
    def compare_with_mapped(route_id: str) -> dict:
        """A route against the mapped paths and roads: how much runs beside one, and its length laid there."""
        with read.connection() as conn:
            return routes.compare_with_mapped(conn, route_id, "")

    @app.post("/api/v1/owned/routes/{route_id}/mapped")
    def lay_on_mapped(route_id: str, body: Version, author: Editor) -> dict:
        """Lay every stretch of the route on the mapped path or road beside it, where one runs. One action."""
        with write.connection() as conn:
            return routes.compare_with_mapped(conn, route_id, author, body.version, dry_run=False)

    @app.put("/api/v1/owned/stretches/{edge_id}/line")
    def redraw(edge_id: str, body: Line, author: Editor, seen: Seen = None) -> dict:
        """Replace a stretch's line with one through these points, between the same ends.

        With follow it takes the mapped paths between the points; a dry run only shows the line.
        """
        with (read if body.dry_run else write).connection() as conn:
            routes.as_seen(conn, edge_id, None if body.dry_run else seen)
            return routes.redraw(conn, edge_id, author, body.points, follow=body.follow,
                                 dry_run=body.dry_run)

    @app.post("/api/v1/owned/nodes/{node_id}/move")
    def move_node(node_id: str, body: Position, author: Editor) -> dict:
        """Move a junction or an end; the stretches that meet there follow.

        Dropped on another junction the two are joined; on a mapped path or road it lands there.
        """
        with (read if body.dry_run else write).connection() as conn:
            return routes.move_node(conn, node_id, author, body.lon, body.lat, body.snap_m,
                                    body.dry_run)

    @app.post("/api/v1/owned/stretches/{edge_id}/split")
    def split(edge_id: str, body: Position, author: Editor, seen: Seen = None) -> dict:
        """Divide a stretch in two at the spot on it nearest the point."""
        with write.connection() as conn:
            routes.as_seen(conn, edge_id, seen)
            return {"node": routes.split(conn, edge_id, author, body.lon, body.lat)}

    @app.post("/api/v1/owned/stretches/{edge_id}/bend")
    def bend(edge_id: str, body: Pull, author: Editor, seen: Seen = None) -> dict:
        """Pull a stretch, where it was grabbed, over to another spot; it follows paths and roads there."""
        with write.connection() as conn:
            routes.as_seen(conn, edge_id, seen)
            return routes.bend(conn, edge_id, author, body.grab, body.drop, corner_m=body.corner_m)

    @app.post("/api/v1/owned/stretches/{edge_id}/restore", status_code=204)
    def restore(edge_id: str, body: Revision, author: Editor) -> Response:
        """Give a stretch back the line it had at an earlier revision: undo."""
        with write.connection() as conn:
            routes.restore(conn, edge_id, body.rev_no, author)
        return Response(status_code=204)

    @app.post(f"{V1}/{{route_id}}/stretches", status_code=201)
    def add_stretch(route_id: str, body: Along, author: Editor) -> dict:
        """Carry the route on along a stretch of yours or a Turrutebasen segment."""
        if (body.edge_id is None) == (body.segment_id is None):
            raise routes.Invalid("name one stretch: yours, or a Turrutebasen segment")
        with write.connection() as conn:
            routes.add_stretch(conn, route_id, body.version, author, edge_id=body.edge_id,
                               segment_id=body.segment_id)
            return routes.detail(conn, route_id)

    @app.post(f"{V1}/{{route_id}}/end")
    def move_end(route_id: str, body: Moved, author: Editor) -> dict:
        """Move one end of the route to another spot. Only which stretches it uses changes."""
        with write.connection() as conn:
            try:
                told = routes.move_end(conn, route_id, body.version, author, body.end or "", body.lon,
                                       body.lat, body.reach_m, body.dry_run)
                return told if body.dry_run else {**routes.detail(conn, route_id), "told": told}
            finally:
                if body.dry_run:
                    conn.rollback()

    @app.post(f"{V1}/{{route_id}}/via")
    def send_via(route_id: str, body: Moved, author: Editor) -> dict:
        """Send the route by way of another spot, in place of the stretch that was grabbed."""
        with write.connection() as conn:
            try:
                told = routes.send_via(conn, route_id, body.version, author, body.edge_id or "", body.lon,
                                       body.lat, body.reach_m, body.dry_run)
                return told if body.dry_run else {**routes.detail(conn, route_id), "told": told}
            finally:
                if body.dry_run:
                    conn.rollback()

    @app.post(f"{V1}/plan")
    def plan(body: Way) -> dict:
        """The shortest way through the points over what exists, as a route would be laid. Keeps nothing."""
        with write.connection() as conn:
            try:
                return routes.plan(conn, body.points, body.reach_m)
            finally:
                conn.rollback()

    @app.post(f"{V1}/{{route_id}}/discontinued")
    def discontinue(route_id: str, body: Discontinued, author: Editor) -> dict:
        """Say that the route is discontinued, since when and why. It is kept, with its history."""
        with write.connection() as conn:
            routes.discontinue(conn, route_id, body.version, author, body.why, body.since)
            return routes.detail(conn, route_id)

    @app.delete(f"{V1}/{{route_id}}/discontinued")
    def take_up_again(route_id: str, version: int, author: Editor) -> dict:
        """The route is in use after all."""
        with write.connection() as conn:
            routes.take_up_again(conn, route_id, version, author)
            return routes.detail(conn, route_id)

    @app.get(f"{V1}/{{route_id}}/changes")
    def changes(route_id: str) -> list[dict]:
        """The changes to a route and its line that can be taken back, newest first."""
        with read.connection() as conn:
            return undo.of_route(conn, route_id)

    @app.post(f"{V1}/{{route_id}}/back")
    def put_back(route_id: str, body: Undo, author: Editor) -> dict:
        """Put the route back as it was before one of its changes: that change and every later one to it."""
        if body.txid is None:
            raise routes.Invalid("say before which change")
        with write.connection() as conn:
            return undo.back(conn, author, route_id, body.txid)

    @app.post(f"{V1}/{{route_id}}/unpublished")
    def unpublish(route_id: str, body: Unpublished, author: Editor) -> dict:
        """Say that the route is not to be published, and why. It is kept, and not handed on."""
        with write.connection() as conn:
            routes.unpublish(conn, route_id, body.version, author, body.why)
            return routes.detail(conn, route_id)

    @app.delete(f"{V1}/{{route_id}}/unpublished")
    def publish(route_id: str, version: int, author: Editor) -> dict:
        """The route is to be published after all."""
        with write.connection() as conn:
            routes.publish(conn, route_id, version, author)
            return routes.detail(conn, route_id)

    @app.post(f"{V1}/between", status_code=201)
    def lay_out(body: Way, author: Editor) -> dict:
        """Make a new route through the points."""
        with write.connection() as conn:
            return routes.detail(conn, routes.lay_out(conn, author, body.points, body.code or "",
                                                      body.name, body.reach_m))

    @app.post(f"{V1}/{{route_id}}/between")
    def lay_out_again(route_id: str, body: Way, author: Editor) -> dict:
        """Replace the route's stretches by the shortest way through the points."""
        if body.version is None:
            raise routes.Invalid("say which version of the route this replaces")
        with write.connection() as conn:
            routes.lay_out_again(conn, route_id, body.version, author, body.points, body.reach_m)
            return routes.detail(conn, route_id)

    @app.get(f"{V1}.geojson")
    def collection() -> dict:
        """Every stretch used by one of your routes, with its kind and whether it is marked."""
        with read.connection() as conn:
            return routes.collection(conn)

    @app.get(f"{V1}/{{route_id}}")
    def route(route_id: str) -> dict:
        with read.connection() as conn:
            return routes.detail(conn, route_id)

    @app.get(f"{V1}/{{route_id}}/height")
    def how_high(route_id: str) -> dict:
        """The route's height profile, walked in the order of its stretches, and what it climbs.

        A stretch whose line is new or has been moved has no heights yet. They
        are asked of Kartverket's height service here, as far as a short wait
        allows; `waiting` says what is still left, and why.
        """
        with read.connection() as conn:
            routes.detail(conn, route_id)       # refuses a route that is not there
            todo = height.lacking(conn, route_id=route_id)
        waiting = None
        if todo:
            def store(got: dict) -> None:
                with write.connection() as conn:
                    height.keep(conn, got)

            with height.client(timeout_s=10.0) as http:
                went = height.fill(todo, store, http=http, points=HEIGHT_POINTS, tries=2)
            if went["left"]:
                waiting = {"stretches": went["left"], "why": went["stopped"]}
        with read.connection() as conn:
            return {**height.of_route(conn, route_id, samples=True), "waiting": waiting}

    @app.get(f"{V1}/{{route_id}}/facts")
    def facts(route_id: str) -> dict:
        """The route as it is walked, in facts: sections, places and junctions passed, climb, time, ground, names.

        What a description can be written from, and what it cannot tell (`not_known`).
        """
        with read.connection() as conn:
            return factsheet.of_route(conn, route_id)

    @app.exception_handler(utno.Unavailable)
    def utno_is_away(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get(f"{V1}/{{route_id}}/utno")
    def on_utno(route_id: str, address: Annotated[str | None, Query(max_length=300)] = None) -> dict:
        """What UT.no says about the route: read from the route's page there. Nothing is changed.

        `address` is the address of that page, or the number in it; left out,
        the page the route is known to have. `text` is UT.no's description as
        it would be taken: to take it, change the route's description to it
        and give `utno_id` with it.
        """
        with read.connection() as conn:
            route = routes.detail(conn, route_id)
        return utno_says(route, address)

    @app.post(V1, status_code=201)
    def make(body: Make, author: Editor) -> dict:
        """Make a route candidate yours, under a code of your choosing."""
        with write.connection() as conn:
            return routes.detail(conn, routes.make(conn, body.candidate_id, author, body.code))

    @app.patch(f"{V1}/{{route_id}}")
    def change(route_id: str, body: Change, author: Editor) -> dict:
        with write.connection() as conn:
            routes.update(conn, route_id, body.version, author, code=body.code, name=body.name,
                          code_provisional=body.code_provisional, maintainers=body.maintainers,
                          everywhere=body.everywhere, grading=body.grading, description=body.description,
                          utno_id=body.utno_id)
            return routes.detail(conn, route_id)

    @app.patch(f"{V1}/{{route_id}}/legs/{{edge_id}}")
    def change_leg(route_id: str, edge_id: str, body: Leg, author: Editor) -> dict:
        """Say who maintains one stretch of the route, or (with none given) that the route's word holds."""
        with write.connection() as conn:
            routes.set_leg(conn, route_id, body.version, edge_id, author, body.maintainers)
            return routes.detail(conn, route_id)

    @app.post(f"{V1}/{{route_id}}/pieces", status_code=201)
    def add_piece(route_id: str, body: Piece, author: Editor) -> dict:
        """Add another candidate to the route, as a further piece of it."""
        with write.connection() as conn:
            routes.add_piece(conn, route_id, body.version, body.candidate_id, author)
            return routes.detail(conn, route_id)

    @app.post(f"{V1}/{{route_id}}/crossings", status_code=201)
    def add_crossing(route_id: str, body: Crossing, author: Editor) -> dict:
        """Join two points of the route over water, over a glacier, or by a short walk."""
        with write.connection() as conn:
            routes.add_crossing(conn, route_id, body.version, body.from_node, body.to_node,
                                body.kind, author)
            return routes.detail(conn, route_id)

    @app.delete(f"{V1}/{{route_id}}/legs/{{edge_id}}")
    def remove_leg(route_id: str, edge_id: str, version: int, author: Editor) -> dict:
        with write.connection() as conn:
            routes.remove_leg(conn, route_id, version, edge_id, author)
            return routes.detail(conn, route_id)

    @app.delete(f"{V1}/{{route_id}}", status_code=204)
    def retire(route_id: str, author: Editor) -> Response:
        """Withdraw the route. Its candidates go back to being candidates."""
        with write.connection() as conn:
            routes.retire(conn, route_id, author)
        return Response(status_code=204)
