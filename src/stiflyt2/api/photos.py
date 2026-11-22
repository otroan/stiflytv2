"""Photos over HTTP, and for an assistant: the feature `photos`.

Looking needs the feature; adding and changing need it and the editor or operations role too. A photo is sent
as the body of a POST, with where it was taken and what it shows in the query: the page sends the file as it is.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.responses import FileResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth, mcp
from stiflyt2.ops import photos, undo

V1 = "/api/v1/photos"
May = Annotated[str, Depends(auth.may("photos"))]
Id = Annotated[str, Path(max_length=40)]


def _adder(who: May, request: Request):
    person = auth.person_of(request)
    if not (person.edits or person.operates):
        raise HTTPException(403, "you may look at photos; adding and changing them needs the editor or operations role")
    return who


Adder = Annotated[str, Depends(_adder)]


class Change(BaseModel):
    caption: str | None = Field(None, max_length=500)
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    entry: str | None = Field(None, max_length=40)      # given as null: it shows no issue


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(V1)
    def listed(_: May, area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
               route: Annotated[str | None, Query(max_length=40)] = None,
               entry: Annotated[str | None, Query(max_length=40)] = None, unplaced: bool = False) -> list[dict]:
        """The photos of an area, of a route (taken within 75 m of it), of an issue, or those waiting to be placed."""
        with read.connection() as conn:
            return photos.listing(conn, area, route, entry, unplaced)

    @app.post(V1, status_code=201)
    async def add(request: Request, author: Adder,
                  lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
                  lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
                  entry: Annotated[str | None, Query(max_length=40)] = None,
                  caption: Annotated[str | None, Query(max_length=500)] = None,
                  area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
                  origin: Annotated[str | None, Query(max_length=200)] = None) -> dict:
        """Add a photo: the file as the body. Placed where the camera says, else at `lon`, `lat`, else it waits."""
        data = await request.body()
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: photos.add(
                conn, author, data, lon=lon, lat=lat, entry=entry, caption=caption, area=area, origin=origin or "upload"))

    @app.get(f"{V1}/{{photo}}")
    def single(photo: Id, _: May) -> dict:
        with read.connection() as conn:
            return photos.one(conn, photo)

    @app.get(f"{V1}/{{photo}}/{{size}}")
    def picture(photo: Id, size: Literal["thumb", "1600", "original"], _: May) -> FileResponse:
        """The photo: its thumbnail, the copy 1600 px long to show, or the file as it came."""
        with read.connection() as conn:
            where, kind = photos.file(conn, photo, size)
        return FileResponse(where, media_type=kind, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.patch(f"{V1}/{{photo}}")
    def change(photo: Id, body: Change, author: Adder) -> dict:
        """Say what a photo shows, where it was taken, or which issue it shows."""
        said = body.model_dump(exclude_unset=True)
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: photos.change(conn, author, photo, said))

    @app.delete(f"{V1}/{{photo}}", status_code=204)
    def take_away(photo: Id, author: Adder) -> Response:
        """Take a photo away. Undo brings it back."""
        with write.connection() as conn:
            undo.kept_as_made(conn, author, lambda: photos.take_away(conn, author, photo))
        return Response(status_code=204)


class Photos(mcp.Args):
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="An area: its three letters")
    route: str | None = Field(None, max_length=40, description="Or the photos taken near a route, by its code or id")
    issue: str | None = Field(None, max_length=40, description="Or those of an issue, by its id")


@mcp.tool("Photos from the routes", feature="photos")
def route_photos(c: mcp.Call, a: Photos) -> dict:
    """The photos of an area, those taken within 75 m of a route, or those of an issue: where each was taken, when,
    what is said of it, and the address of its thumbnail and of the copy to show (`/api/v1/photos/<id>/1600`)."""
    rid = mcp._route_id(c.conn, a.route) if a.route else None
    told = photos.listing(c.conn, a.area, rid, a.issue)
    for p in told:
        p["shown_at"] = f"/api/v1/photos/{p['id']}/1600"
        p.pop("sha256", None)
    return {"photos": told, "count": len(told)}
