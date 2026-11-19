"""Owned places over HTTP: adopt a proposed anchor and edit the place (ADR-002).

Changes are accepted only when an editor name is configured (STIFLYT2_EDITOR),
and only from the page this service serves itself.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth
from stiflyt2.domain import places

V1 = "/api/v1"


class Adopt(BaseModel):
    anchor_id: str = Field(max_length=80)
    name: str | None = Field(None, max_length=120)


class AtJunction(BaseModel):
    node_id: str = Field(max_length=40)
    name: str | None = Field(None, max_length=120)


class Change(BaseModel):
    version: int
    name: str | None = Field(None, max_length=120)
    kind: Literal["hut", "parking", "glacier_edge", "other"] | None = None
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    primary_node_id: str | None = None


class Version(BaseModel):
    version: int


class Position(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


Editor = Annotated[str, Depends(auth.editor)]      # the name a change is recorded under


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.exception_handler(places.NotFound)
    def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(places.Conflict)
    def conflict(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(places.Invalid)
    def invalid(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get(f"{V1}/editing")
    def editing(request: Request) -> dict:
        """Whether your changes are accepted, as whom, and your open changeset if there is one."""
        who = auth.person_of(request)
        if not who.edits:
            return {"editor": None, "changeset": None,
                    **({"reader": who.name, "sign_in": True} if auth.signed_in() else {})}
        with read.connection() as conn:
            return {"editor": who.name, "changeset": places.changeset_status(conn, who.name),
                    **({"sign_in": True} if auth.signed_in() else {})}

    @app.post(f"{V1}/changeset/close", status_code=204)
    def close(author: Editor) -> Response:
        with write.connection() as conn:
            places.close_changeset(conn, author)
        return Response(status_code=204)

    @app.get(f"{V1}/places.geojson")
    def collection() -> dict:
        """Every owned place with its access points and connectors."""
        with read.connection() as conn:
            return places.collection(conn)

    @app.get(f"{V1}/places/{{place_id}}")
    def place(place_id: str) -> dict:
        with read.connection() as conn:
            return places.detail(conn, place_id)

    @app.post(f"{V1}/places", status_code=201)
    def adopt(body: Adopt, author: Editor) -> dict:
        """Make a proposed anchor an owned place."""
        with write.connection() as conn:
            return places.detail(conn, places.adopt(conn, body.anchor_id, author, body.name))

    @app.post(f"{V1}/places/at-junction", status_code=201)
    def at_junction(body: AtJunction, author: Editor) -> dict:
        """Make a place where a route ends, with that junction as its arrival point."""
        with write.connection() as conn:
            return places.detail(conn, places.at_node(conn, body.node_id, author, body.name))

    @app.patch(f"{V1}/places/{{place_id}}")
    def change(place_id: str, body: Change, author: Editor) -> dict:
        """Rename, change kind, move the reference point or choose the primary access point."""
        with write.connection() as conn:
            places.update(conn, place_id, body.version, author, name=body.name, kind=body.kind,
                          lon=body.lon, lat=body.lat, primary_node_id=body.primary_node_id)
            return places.detail(conn, place_id)

    @app.post(f"{V1}/places/{{place_id}}/review")
    def review(place_id: str, body: Version, author: Editor) -> dict:
        """Mark a place made by default as looked at and right."""
        with write.connection() as conn:
            places.review(conn, place_id, body.version, author)
            return places.detail(conn, place_id)

    @app.post(f"{V1}/places/{{place_id}}/access-points", status_code=201)
    def add_access_point(place_id: str, body: Position, author: Editor) -> dict:
        with write.connection() as conn:
            places.add_access_point(conn, place_id, body.lon, body.lat, author)
            return places.detail(conn, place_id)

    @app.post(f"{V1}/places/{{place_id}}/access-points/{{node_id}}", status_code=201)
    def take_in(place_id: str, node_id: str, author: Editor) -> dict:
        """Make a junction that is there already an arrival point of the place."""
        with write.connection() as conn:
            places.take_in(conn, place_id, node_id, author)
            return places.detail(conn, place_id)

    @app.get(f"{V1}/places/{{place_id}}/around")
    def around(place_id: str) -> dict:
        """What lies round a place and may belong to it: route ends that are not its arrival points, other places."""
        with read.connection() as conn:
            return places.around(conn, place_id)

    @app.post(f"{V1}/places/{{place_id}}/merge/{{other_id}}")
    def merge(place_id: str, other_id: str, body: Version, author: Editor) -> dict:
        """Make two places one: this one stays, and takes over the other's arrival points."""
        with write.connection() as conn:
            places.merge(conn, place_id, body.version, other_id, author)
            return places.detail(conn, place_id)

    @app.delete(f"{V1}/places/{{place_id}}/access-points/{{node_id}}")
    def remove_access_point(place_id: str, node_id: str, author: Editor) -> dict:
        with write.connection() as conn:
            places.remove_access_point(conn, place_id, node_id, author)
            return places.detail(conn, place_id)

    @app.delete(f"{V1}/places/{{place_id}}", status_code=204)
    def retire(place_id: str, author: Editor) -> Response:
        """Withdraw a place. Its anchor goes back to being a proposal."""
        with write.connection() as conn:
            places.retire(conn, place_id, author)
        return Response(status_code=204)
