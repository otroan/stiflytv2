"""The route planner over HTTP and for an assistant: the feature `planner` (ADR-005).

Everything here asks for the feature. It reads the walk network and keeps nothing.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

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
    mountains: float = Field(0.0, ge=-1, le=1, description="How much it keeps up in the mountains (1) or down in the "
                             "valleys (-1), by how high the ground lies against the tree line; 0 neither")
    across: bool = Field(False, description="Across country where there is no path: over open ground, not over "
                         "water or rivers nor where it is steeper than 35 degrees (a sketch)")
    wade: bool = Field(False, description="Across country, rivers drawn as a line may be waded (each counts as 1 km "
                       "more); rivers drawn as an area, too wide, never are")
    fords: bool = Field(True, description="Fords on paths are taken as they come; false: gone round by a bridge "
                        "where there is one")

    def options(self) -> planner.Options:
        return planner.Options(self.prefer, self.ferries, self.ice, self.alpine, self.mountains, self.across,
                               self.wade, self.fords)


class KeptStep(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    what: str = Field(alias="class", pattern="^(dnt|marked|path|road|ferry|field|off)$")
    name: str | None = Field(None, max_length=300)
    ice: bool = False
    metres: float = Field(ge=0)
    at: list[int] = Field(min_length=2, max_length=2)


class Crossing(BaseModel):
    at: int = Field(ge=0)
    how: Literal["ford", "bridge", "wade"]
    river: str | None = Field(None, max_length=300)


class Kept(BaseModel):
    """A leg as it was found before, to be taken again as it is."""
    coords: list[Point] = Field(min_length=2, max_length=200_000)
    steps: list[KeptStep] = Field(min_length=1, max_length=20_000)
    joints: list[int] = Field(default_factory=list, max_length=200_000)
    origins: list[str] = Field(default_factory=list, max_length=5)
    crossings: list[Crossing] = Field(default_factory=list, max_length=10_000)
    unfound: str | None = Field(None, max_length=500)
    roundabout: bool = False


class Ask(How):
    points: list[Point] = Field(min_length=2, max_length=planner.MOST_POINTS,
                                description="Where it begins, the places to pass on the way, and where it ends")
    straight: list[bool] | None = Field(None, max_length=planner.MOST_POINTS - 1,
                                        description="For each leg between two points: drawn straight over the ground, "
                                                    "with no path (a sketch), or along paths and roads")
    snap: list[bool] | None = Field(None, max_length=planner.MOST_POINTS,
                                    description="For each point: passed on the nearest stretch, not walked out to")
    modes: list[Literal["paths", "across", "straight"]] | None = Field(
        None, max_length=planner.MOST_POINTS - 1, description="For each leg: along paths and roads (across country "
        "where there are none, when `across`), across country alone even where there are paths, or straight")
    kept: list[Kept | None] | None = Field(None, max_length=planner.MOST_POINTS - 1,
                                           description="For each leg, as it was found before (to keep it), or none")

    def kept_legs(self) -> list[dict | None] | None:
        if self.kept is None:
            return None
        return [None if k is None else {**k.model_dump(exclude={"steps"}),
                                        "steps": [{**st.model_dump(exclude={"what"}), "class": st.what} for st in k.steps]}
                for k in self.kept]


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
            return planner.plan(conn, body.points, body.options(), body.straight, body.snap, body.kept_legs(), body.modes)

    @app.post(f"{V1}/estimate")
    def estimate(body: Ask, who: May) -> dict:
        """About how long a plan would take, before it is asked for."""
        with read.connection() as conn:
            return planner.estimate(conn, body.points, body.options(), body.straight,
                                    None if body.kept is None else [k is not None for k in body.kept], body.modes)

    @app.get(f"{V1}/near")
    def near(lon: Annotated[float, Query(ge=-180, le=180)], lat: Annotated[float, Query(ge=-90, le=90)], who: May) -> dict:
        """How far a point is from the nearest path or road, and where on it."""
        with read.connection() as conn:
            return planner.near(conn, lon, lat)

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
    modes: list[Literal["paths", "across", "straight"]] | None = Field(
        None, max_length=planner.MOST_POINTS - 1, description="For each leg: paths (along paths and roads), across "
        "(across country alone, even where there are paths: a sketch over the ground the height model shows) or "
        "straight. Overrides `straight`")
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
    `crossings` says where it crosses a river: by a bridge, by a ford (on a path), or wading across country, with
    the river's name where it has one; whether a river can be waded today is not known, say so.
    Glaciers, demanding alpine paths and via ferratas are left out unless asked for. A leg may be drawn straight
    where nothing is mapped (`straight`): it is a sketch over ground you do not see, say so; `legs` says of each
    leg how long it is against the crow's flight, and why one was drawn straight when no way was found. The line itself is not
    given. Name `credits` wherever the plan is passed on (OpenStreetMap's data is under ODbL). Whether a hut is
    open, a bridge is up or a ferry runs is not known here: say so."""
    told = planner.plan(c.conn, a.points, a.options(), a.straight, modes=a.modes)
    if a.with_heights:
        parts = [{"coords": told["coords"][s["at"][0]:s["at"][1] + 1], "ferry": s["class"] == "ferry",
                  "rough": s["class"] in ("off", "field"), "ice": s["ice"]} for s in told["steps"]]
        got = planner.heights(c.conn, parts, a.hours)
        got.pop("profile")
        told["heights"] = got
        told["credits"] += got.pop("credits")
    told.pop("coords")
    for step in told["steps"] + told["legs"] + told["crossings"]:
        step.pop("at")
    return told
