"""GPX tracks over HTTP, and for an assistant: the feature `tracks`.

Looking needs the feature; adding and changing need it and the editor or operations role too. A track is sent as
the body of a POST (the GPX file as it is), with its name and area in the query.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth, mcp
from stiflyt2.ops import tracks, undo

V1 = "/api/v1/tracks"
May = Annotated[str, Depends(auth.may("tracks"))]
Id = Annotated[str, Path(max_length=40)]
Area = Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")]


def _adder(who: May, request: Request):
    person = auth.person_of(request)
    if not (person.edits or person.operates):
        raise HTTPException(403, "you may look at tracks; adding and changing them needs the editor or operations role")
    return who


Adder = Annotated[str, Depends(_adder)]


class Change(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$")


class Undo(BaseModel):
    txid: int | None = None


class Cut(BaseModel):
    from_point: mcp.Point = Field(description="Where on the track the part to cut out begins, [lon, lat]")
    to_point: mcp.Point = Field(description="And where it ends")


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(V1)
    def listed(_: May, area: Area = None, route: Annotated[str | None, Query(max_length=40)] = None) -> list[dict]:
        """The tracks of an area (or that come within 100 m of one of its routes), or near a route: the latest first."""
        with read.connection() as conn:
            return tracks.listing(conn, area, route)

    @app.get(f"{V1}.geojson")
    def drawn(_: May, area: Annotated[str, Query(pattern="^[A-Za-z]{3}$")]) -> dict:
        """The tracks of an area as lines to draw, simplified to a few metres."""
        with read.connection() as conn:
            return tracks.lines(conn, area)

    @app.post(V1, status_code=201)
    async def add(request: Request, author: Adder, area: Area = None,
                  name: Annotated[str | None, Query(max_length=200)] = None,
                  filename: Annotated[str | None, Query(max_length=300)] = None) -> dict:
        """Add a track: the GPX file as the body. The same file again is the track already kept (`again`)."""
        data = await request.body()
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: tracks.add(conn, author, data, name=name, area=area,
                                                                      filename=filename))

    @app.get(f"{V1}/undo")
    def would_undo(author: May) -> dict:
        """What an undo would take back: your own latest decision in operations."""
        with read.connection() as conn:
            return {"last": undo.last(conn, author)}

    @app.post(f"{V1}/undo")
    def take_back(author: May, body: Undo | None = None) -> dict:
        with write.connection() as conn:
            return undo.undo(conn, author, body.txid if body else None)

    @app.get(f"{V1}/{{track}}")
    def single(track: Id, _: May, drawn: bool = True) -> dict:
        """A track: what it comes to, and (`drawn`) its lines, cleaned and as it came, its stops and what was cut out."""
        with read.connection() as conn:
            return tracks.one(conn, track, drawn)

    @app.get(f"{V1}/{{track}}/gpx")
    def as_gpx(track: Id, _: May, cleaned: bool = False) -> Response:
        """The file the track came as; `cleaned`, the track cleaned, as GPX."""
        with read.connection() as conn:
            if cleaned:
                body, name = tracks.gpx(conn, track)
                return Response(body, media_type="application/gpx+xml",
                                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quoted(name)}"})
            where, name = tracks.file(conn, track)
        return FileResponse(where, media_type="application/gpx+xml", filename=name)

    @app.patch(f"{V1}/{{track}}")
    def change(track: Id, body: Change, author: Adder) -> dict:
        """Say what a track is called, or which area it is of."""
        said = body.model_dump(exclude_unset=True)
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: tracks.change(conn, author, track, said))

    @app.post(f"{V1}/{{track}}/cuts")
    def cut(track: Id, body: Cut, author: Adder) -> dict:
        """Cut out of a track the part between two points on it: what came stays, and Undo puts it back."""
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: tracks.cut(conn, author, track, body.from_point, body.to_point))

    @app.delete(f"{V1}/{{track}}/cuts/{{n}}")
    def put_back(track: Id, n: Annotated[int, Path(ge=0, le=1000)], author: Adder) -> dict:
        """Put back a part that was cut out, by its place in the track's list of cuts."""
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: tracks.put_back(conn, author, track, n))

    @app.delete(f"{V1}/{{track}}", status_code=204)
    def take_away(track: Id, author: Adder) -> Response:
        """Take a track away. Undo brings it back."""
        with write.connection() as conn:
            undo.kept_as_made(conn, author, lambda: tracks.take_away(conn, author, track))
        return Response(status_code=204)


def _quoted(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


class Tracks(mcp.Args):
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="An area: its three letters")
    route: str | None = Field(None, max_length=40, description="Or the tracks that come within 100 m of a route, by "
                                                               "its code or id")


@mcp.tool("GPX tracks walked on the routes", feature="tracks")
def route_tracks(c: mcp.Call, a: Tracks) -> dict:
    """The GPX tracks of an area, or those that come within 100 m of a route: who added each and when it was walked,
    its length, its time walking (without stops of two minutes or more) and in all, and the climb by the device's
    heights, as cleaned (jumps and stops taken out, parts cut out by hand left out: `cleaned` says how many). Tracks
    from v1 are lines alone, with no times or heights. A track is somebody's way that day: not the route, and not
    proof that a path is there."""
    rid = mcp._route_id(c.conn, a.route) if a.route else None
    told = tracks.listing(c.conn, a.area, rid)
    return {"tracks": told, "count": len(told)}
