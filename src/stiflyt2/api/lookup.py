"""Look-ups over HTTP: what carries a route number, what lies at a point, the mapped way, the odd things.

All of them only read. They answer what a person otherwise finds by looking
at the map, for an assistant that cannot look.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, Path, Query
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.domain import lookup

V1 = "/api/v1"
Area = Annotated[str, Path(pattern="^[A-Za-z]{3}$")]
Id = Annotated[str, Query(pattern="^[0-9a-fA-F-]{36}$")]


class Between(BaseModel):
    points: list[tuple[Annotated[float, Field(ge=-180, le=180)], Annotated[float, Field(ge=-90, le=90)]]] = Field(
        min_length=2, max_length=20)        # lon, lat
    route: str | None = Field(None, pattern="^[0-9a-fA-F-]{36}$")       # a route of yours to hold the way against
    reach_m: float = Field(lookup.routes.MAPPED_REACH_M, ge=1, le=1000)


def register(app: FastAPI, read: ConnectionPool) -> None:
    @app.get(f"{V1}/turrutebasen/{{code}}")
    def numbered(code: Annotated[str, Path(min_length=1, max_length=40)]) -> dict:
        """Everything in the loaded Turrutebasen that carries a route number, segment by segment."""
        with read.connection() as conn:
            return lookup.turrutebasen(conn, code)

    @app.get(f"{V1}/here")
    def here(lon: Annotated[float, Query(ge=-180, le=180)], lat: Annotated[float, Query(ge=-90, le=90)],
             radius_m: Annotated[float, Query(gt=0, le=lookup.HERE_MAX_M)] = lookup.HERE_M) -> dict:
        """What lies around a point: names, mapped paths and roads, Turrutebasen, and what is yours."""
        with read.connection() as conn:
            return lookup.here(conn, lon, lat, radius_m)

    @app.post(f"{V1}/mapped-way")
    def mapped_way(body: Between) -> dict:
        """The shortest way over mapped paths and roads through some points (lon, lat). Makes nothing."""
        with read.connection() as conn:
            return lookup.mapped_way(conn, body.points, body.route, body.reach_m)

    @app.get(f"{V1}/same-line")
    def shared(a: Id, b: Id) -> dict:
        """What two routes of yours share, and how far each goes alone."""
        with read.connection() as conn:
            return lookup.shared(conn, a, b)

    @app.get(f"{V1}/areas/{{area}}/checks")
    def checks(area: Area) -> dict:
        """The odd things of an area: where to look, not what is wrong."""
        with read.connection() as conn:
            return lookup.checks(conn, area)
