"""What a person works with (their profile), and what an area is (its routes added from another area), over HTTP.

A profile is one's own: anybody known here reads and says theirs. Adding a route of another area to an area is for
the route responsible.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import access
from stiflyt2.api import auth
from stiflyt2.ops import areas, undo

Editor = Annotated[str, Depends(auth.editor)]
Area = Annotated[str, Path(pattern="^[A-Za-z]{3}$")]


class Profile(BaseModel):
    areas: Annotated[list[Annotated[str, Field(pattern="^[A-Za-z]{3}$")]], Field(max_length=40)] = []
    maintainer: str | None = Field(None, max_length=120)


class Added(BaseModel):
    code: str = Field(min_length=2, max_length=20, description="The route's code, as sun27")


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get("/api/v1/profile")
    def mine(request: Request) -> dict:
        """What you work with: your areas, the first the one the pages open with, and whose routes are yours."""
        with read.connection() as conn:
            return access.profile(conn, auth.person_of(request))

    @app.put("/api/v1/profile")
    def say(body: Profile, request: Request) -> dict:
        who = auth.person_of(request)
        if not who.name:
            raise HTTPException(403, "only one who is known here has a profile")
        try:
            with write.connection() as conn:
                return access.set_profile(conn, who, body.areas, body.maintainer)
        except access.Refused as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/v1/areas/added")
    def added() -> dict:
        """The routes added to each area from another, by area."""
        with read.connection() as conn:
            return areas.listing(conn)

    @app.post("/api/v1/areas/{area}/added", status_code=201)
    def add(area: Area, body: Added, author: Editor) -> dict:
        """Add a route of another area to this one: Breheimen has sun27."""
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: areas.add(conn, author, area, body.code))

    @app.delete("/api/v1/areas/{area}/added/{route}")
    def take_out(area: Area, route: Annotated[str, Path(max_length=40)], author: Editor) -> dict:
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: areas.take_out(conn, author, area, route))
