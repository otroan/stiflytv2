"""The route card over HTTP: a page to print, of the parts asked for. Whoever is let in may make one.

A part that is a feature of its own is on it only for one who has that feature. Asking the register who owns the
ground is kept as any other lookup is.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from psycopg_pool import ConnectionPool

from stiflyt2.api import auth
from stiflyt2.domain.places import NotFound
from stiflyt2.ops import landowners, routecard

V1 = "/api/v1/routecard"


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(f"{V1}/{{route}}", response_class=HTMLResponse)
    def card(route: str, request: Request,
             parts: Annotated[str | None, Query(alias="with", max_length=300,
                                                description="The parts on it, with commas between: " + ", ".join(routecard.PARTS))] = None) -> HTMLResponse:
        """A route on one page: its facts, and the parts asked for (by default those chosen by default)."""
        person = auth.person_of(request)
        chosen = routecard.chosen(parts, person.has)
        with read.connection() as conn:
            try:
                found = routecard.gather(conn, route, chosen, notices=person.has("issues"))
            except NotFound as e:
                raise HTTPException(404, str(e)) from e
        owners = None
        if "owners" in chosen:
            with read.connection() as conn:
                owners = landowners.along(conn, route)
            units = [p["id"] for p in owners["properties"] if p["id"]]
            if units:
                with write.connection() as conn:
                    landowners.asked_by(conn, auth.Author(person.name or "", person.through, person.email),
                                        f"{found['code']} (route card)", units)
                owners = landowners.with_owners(owners, owners["properties"])
        return HTMLResponse(routecard.render(found, owners, person.name))
