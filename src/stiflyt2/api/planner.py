"""The route planner over HTTP and for an assistant: the feature `planner` (ADR-005).

Everything here asks for the feature. It reads the walk network and keeps nothing.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import height
from stiflyt2.api import auth, mcp
from stiflyt2.ops import planner

V1 = "/api/v1/planner"
May = Annotated[str, Depends(auth.may("planner"))]
Point = Annotated[list[float], Field(min_length=2, max_length=2, description="[longitude, latitude]")]


class How(BaseModel):
    prefer: float = Field(planner.PREFER, ge=0, le=1, description="How much DNT's marked routes are preferred: "
                          "0 the shortest way, 1 a route of DNT's counts half its length")
    ferries: bool = Field(True, description="Ferries may be taken")
    ice: bool = Field(False, description="Glaciers may be crossed")
    alpine: bool = Field(False, description="Demanding alpine paths and via ferratas may be walked")


class Ask(How):
    points: list[Point] = Field(min_length=2, max_length=planner.MOST_POINTS,
                                description="Where it begins, the places to pass on the way, and where it ends")
    straight: list[bool] | None = Field(None, max_length=planner.MOST_POINTS - 1,
                                        description="For each leg between two points: drawn straight over the ground, "
                                                    "with no path (a sketch), or along paths and roads")


class Part(BaseModel):
    coords: list[Point] = Field(min_length=2, max_length=200_000)
    ferry: bool = False
    rough: bool = Field(False, description="No path here: it takes longer")
    ice: bool = False


class Heights(BaseModel):
    parts: list[Part] = Field(min_length=1, max_length=5000, description="The way's steps, in order")
    hours: float = Field(7.0, ge=3, le=14, description="The walking of a day, with no rests")


def register(app: FastAPI, read: ConnectionPool) -> None:
    @app.post(f"{V1}/plan")
    def plan(body: Ask, who: May) -> dict:
        """A way on foot through the points, along DNT's routes as much as asked for."""
        with read.connection() as conn:
            return planner.plan(conn, body.points, planner.Options(body.prefer, body.ferries, body.ice, body.alpine),
                                body.straight)

    @app.post(f"{V1}/heights")
    def heights(body: Heights, who: May) -> dict:
        """How high a way planned goes, what it climbs, the time it takes, and days that end at huts."""
        if sum(len(p.coords) for p in body.parts) > 200_000:
            raise HTTPException(422, "the way is too long to be asked about in one go")
        with read.connection() as conn:
            try:
                return planner.heights(conn, [p.model_dump() for p in body.parts], body.hours)
            except height.Unavailable as e:
                raise HTTPException(502, str(e)) from e


class PlanWalk(mcp.Args, How):
    points: list[Point] = Field(min_length=2, max_length=planner.MOST_POINTS,
                                description="[lon, lat] of where it begins, of each place to pass, and of where it ends")
    straight: list[bool] | None = Field(None, max_length=planner.MOST_POINTS - 1,
                                        description="For each leg between two points, true to draw it straight over the "
                                                    "ground where nothing is mapped (a sketch). None: every leg along paths")
    hours: float = Field(7.0, ge=3, le=14, description="Hours of walking in a day, with no rests")
    with_heights: bool = Field(True, description="Also the climb, the time with it, and days that end at huts "
                                                 "(asks Kartverket's height service: a little slower)")


@mcp.tool("Plan a walk from anywhere to anywhere in Norway", feature="planner")
def plan_walk(c: mcp.Call, a: PlanWalk) -> dict:
    """A way on foot between points anywhere in Norway, over every path and road in OpenStreetMap, Turrutebasen's
    marked routes and ours, preferring DNT's marked routes as much as `prefer` says (0: the shortest way). Says
    the way in steps (along which route, path, road or ferry, how far), the length of each kind, and with
    `with_heights` the climb, the time by Naismith's rule with no rests, and days that end at huts near the way.
    Glaciers, demanding alpine paths and via ferratas are left out unless asked for. A leg may be drawn straight
    where nothing is mapped (`straight`): it is a sketch over ground you do not see, say so; `legs` says of each
    leg how long it is against the crow's flight, and why one was drawn straight when no way was found. The line itself is not
    given. Name `credits` wherever the plan is passed on (OpenStreetMap's data is under ODbL). Whether a hut is
    open, a bridge is up or a ferry runs is not known here: say so."""
    told = planner.plan(c.conn, a.points, planner.Options(a.prefer, a.ferries, a.ice, a.alpine), a.straight)
    if a.with_heights:
        parts = [{"coords": told["coords"][s["at"][0]:s["at"][1] + 1], "ferry": s["class"] == "ferry",
                  "rough": s["class"] == "off", "ice": s["ice"]} for s in told["steps"]]
        got = planner.heights(c.conn, parts, a.hours)
        got.pop("profile")
        told["heights"] = got
        told["credits"] += got.pop("credits")
    told.pop("coords")
    for step in told["steps"] + told["legs"]:
        step.pop("at")
    return told
