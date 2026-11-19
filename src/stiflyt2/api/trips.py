"""Trips for work over HTTP and for an assistant: the feature `trips` (ADR-005).

Everything here asks for the feature. It reads the routes and keeps nothing.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth, mcp
from stiflyt2.ops import trips

V1 = "/api/v1/trips"
May = Annotated[str, Depends(auth.may("trips"))]
Code = Annotated[str, Field(min_length=1, max_length=40)]


class Ask(BaseModel):
    routes: list[Code] | None = Field(None, max_length=300, description="The routes to cover, by their codes")
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="Or every route in use of an area")
    start: str | None = Field(None, max_length=120, description="The place the trip begins at, by name or id")
    hours: float = Field(trips.HOURS, ge=3, le=14, description="The walking of a day, with no rests")
    tent: bool = Field(False, description="A day may end anywhere, not only at a hut")
    over_ice: bool = Field(False, description="Stretches over a glacier are walked too")
    back_to_start: bool = Field(False, description="The trip ends where it began")
    mapped: bool = Field(True, description="Between the routes, also along mapped paths and roads")


def register(app: FastAPI, read: ConnectionPool) -> None:
    @app.post(f"{V1}/plan")
    def plan(body: Ask, who: May) -> dict:
        """A trip on foot that covers the routes asked for, with little walked twice, in days that end at huts."""
        with read.connection() as conn:
            return trips.for_routes(conn, routes=body.routes, area=body.area, start=body.start, hours=body.hours,
                                    tent=body.tent, over_ice=body.over_ice, back_to_start=body.back_to_start,
                                    mapped=body.mapped)


class PlanTrip(mcp.Args):
    routes: list[Code] | None = Field(None, max_length=300, description="The routes to cover, by their codes (jot2, jot88)")
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="Or every route in use of an area: its three letters")
    start: str | None = Field(None, max_length=120, description="The place the trip begins at, by its name. None: wherever the least is walked twice")
    hours: float = Field(trips.HOURS, ge=3, le=14, description="Hours of walking in a day, with no rests")
    tent: bool = Field(False, description="True: a day may end anywhere, not only at a hut")
    over_ice: bool = Field(False, description="True: stretches over a glacier are walked too")
    back_to_start: bool = Field(False, description="True: the trip ends where it began")
    mapped: bool = Field(True, description="True: between the routes, and from where it begins, it may also go along "
                                           "other paths and roads: FKB paths, NVDB roads, OpenStreetMap paths. False: only "
                                           "along the routes")


@mcp.tool("Plan a trip that covers some routes", feature="trips")
def plan_trip(c: mcp.Call, a: PlanTrip) -> dict:
    """A trip on foot for whoever is to see to some routes (inspect them, mark them): it walks every stretch of
    them with as little as possible walked twice, and is cut into days that end at a hut. For each day: from
    where to where, how far, the climb, the walking time by Naismith's rule with no rests, which of the routes are
    covered that day, whether the day is longer than asked for because no hut lies nearer, and walking
    instructions (`steps`): along which route, path or road to where, and where a part is walked back the same way.
    Also what was left out: routes that cannot be reached on foot, and what lies over water or ice; `notes`; and
    `credits`, the sources of the paths and roads off the routes, which are to be named wherever the plan is
    passed on (OpenStreetMap's paths under ODbL). Give either `routes` or `area`."""
    told = trips.for_routes(c.conn, routes=a.routes, area=a.area, start=a.start, hours=a.hours, tent=a.tent,
                            over_ice=a.over_ice, back_to_start=a.back_to_start, mapped=a.mapped)
    for day in told["days"]:
        ends = day.pop("coords")
        day["ends_at"] = [round(ends[-1][0], 5), round(ends[-1][1], 5)]     # the line itself is for a map, not for words
        for step in day["steps"]:
            step.pop("at")
    return told
