"""Reports of what was done on the routes over HTTP, and for an assistant: the feature `reports`.

Looking needs the feature; reporting needs it and the editor or operations role too.
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import seasons
from stiflyt2.api import auth, mcp
from stiflyt2.api.issues import Part
from stiflyt2.api.params import Season
from stiflyt2.ops import issues, reports, undo

V1 = "/api/v1/reports"
May = Annotated[str, Depends(auth.may("reports"))]
Id = Annotated[str, Path(max_length=40)]
Kind = Literal["inspection", "work", "note"]
Done = Literal["clearing", "marking", "signs", "bridges", "boards", "drainage", "other"]


def _reporter(who: May, request: Request):
    person = auth.person_of(request)
    if not (person.edits or person.operates):
        raise HTTPException(403, "you may look at reports; reporting needs the editor or operations role")
    return who


Reporter = Annotated[str, Depends(_reporter)]


class Cover(BaseModel):
    route: str = Field(max_length=40, description="The id of the route")
    parts: Annotated[list[Part], Field(max_length=reports.on_route.MOST_PARTS)] | None = None     # none: the whole route


class Report(BaseModel):
    kind: Kind
    done_on: dt.date
    year_only: bool = False                     # only the year of done_on is known
    covers: Annotated[list[Cover], Field(min_length=1, max_length=reports.MOST_ROUTES)]
    text: str | None = Field(None, max_length=4000)
    condition: Literal["good", "fair", "poor"] | None = None
    persons: int | None = Field(None, ge=0, lt=1000)
    hours: float | None = Field(None, ge=0, lt=100000)
    done: list[Done] = []
    link: str | None = Field(None, max_length=1000)
    closes: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=200)] = []
    photos: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=1000)] = []    # photos of it


class Chosen(BaseModel):
    route: str = Field(max_length=40)
    parts: Annotated[list[Part], Field(min_length=1, max_length=reports.on_route.MOST_PARTS)]


class Undo(BaseModel):
    txid: int | None = None


class Change(BaseModel):
    kind: Kind | None = None
    done_on: dt.date | None = None
    year_only: bool | None = None
    covers: Annotated[list[Cover], Field(min_length=1, max_length=reports.MOST_ROUTES)] | None = None
    text: str | None = Field(None, max_length=4000)
    condition: Literal["good", "fair", "poor"] | None = None
    persons: int | None = Field(None, ge=0, lt=1000)
    hours: float | None = Field(None, ge=0, lt=100000)
    done: list[Done] | None = None
    link: str | None = Field(None, max_length=1000)
    photos: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=1000)] | None = None


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(V1)
    def listed(_: May, area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
               route: Annotated[str | None, Query(max_length=40)] = None, season: Season = seasons.SUMMER) -> list[dict]:
        """The reports of an area's routes of a season, or those that cover some of a route: the latest done first."""
        with read.connection() as conn:
            return reports.listing(conn, area, route, season=season)

    @app.get(f"{V1}/status")
    def status(_: May, area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
               maintainer: Annotated[str | None, Query(max_length=120)] = None,
               season: Season = seasons.SUMMER) -> list[dict]:
        """Each route in use: when it was last inspected and last had a work party, whether it is due, and whether the
        maintainer asked for (the home maintainer, when none is) looks after it."""
        with read.connection() as conn:
            return reports.status(conn, area, maintainer, season=season)

    @app.get(f"{V1}/coverage")
    def coverage(_: May, kind: Literal["inspection", "work"],
                 area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
                 season: Season = seasons.SUMMER) -> list[dict]:
        """What the inspections or the work parties covered, as lines with the day: the latest last."""
        with read.connection() as conn:
            return reports.coverage(conn, area, kind, season=season)

    @app.post(f"{V1}/parts")
    def chosen(body: Chosen, _: May) -> dict:
        """What parts of a route would be, drawn and measured, while they are being chosen. Keeps nothing."""
        with read.connection() as conn:
            return issues.parts(conn, body.route, [p.model_dump() for p in body.parts])

    @app.get(f"{V1}/undo")
    def would_undo(author: May) -> dict:
        """What an undo would take back: your own latest decision in operations."""
        with read.connection() as conn:
            return {"last": undo.last(conn, author)}

    @app.post(f"{V1}/undo")
    def take_back(author: May, body: Undo | None = None) -> dict:
        with write.connection() as conn:
            return undo.undo(conn, author, body.txid if body else None)

    @app.get(f"{V1}/{{report}}")
    def single(report: Id, _: May) -> dict:
        with read.connection() as conn:
            return reports.one(conn, report)

    @app.post(V1, status_code=201)
    def add(body: Report, author: Reporter) -> dict:
        """Report what was done on which day, on which routes or parts of them; issues it says are done are done."""
        said = body.model_dump()
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: reports.report(conn, author, **said))

    @app.patch(f"{V1}/{{report}}")
    def change(report: Id, body: Change, author: Reporter) -> dict:
        said = body.model_dump(exclude_unset=True)
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: reports.change(conn, author, report, said))

    @app.delete(f"{V1}/{{report}}", status_code=204)
    def take_away(report: Id, author: Reporter) -> Response:
        with write.connection() as conn:
            undo.kept_as_made(conn, author, lambda: reports.take_away(conn, author, report))
        return Response(status_code=204)


# ---- For an assistant ----

class Upkeep(mcp.Args):
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="An area: its three letters")
    route: str | None = Field(None, max_length=40, description="Or one route, by its code or id: its reports")
    season: mcp.Season = seasons.SUMMER


@mcp.tool("When routes were last inspected and worked on", feature="reports")
def route_upkeep(c: mcp.Call, a: Upkeep) -> dict:
    """For each route in use of an area: when it was last inspected (and the state found) and last had a work party
    (dugnad), whether that covered all of it, and whether it is due (inspection every 3 years, a work party every
    10), those due longest first. For one route, its reports instead."""
    if a.route:
        rid = mcp._route_id(c.conn, a.route)
        told = reports.listing(c.conn, route=rid)
        for t in told:
            t.pop("lines", None)
            t.pop("edges", None)
        return {"reports": told}
    return {"routes": reports.status(c.conn, a.area, season=a.season), "due_every_years": reports.DUE_YEARS}


class Covered(mcp.Args):
    route: str = Field(max_length=40, description="The route, by its code or id")
    from_km: float | None = Field(None, ge=0, le=1000, description="Only part of it: from here, km from its start")
    to_km: float | None = Field(None, ge=0, le=1000, description="to here")


class ReportWork(mcp.Args):
    kind: Kind = Field(description="inspection, work (a work party, dugnad) or note (a diary note)")
    done_on: dt.date
    year_only: bool = Field(False, description="True: only the year of done_on is known")
    routes: list[Covered] = Field(min_length=1, max_length=reports.MOST_ROUTES)
    text: str | None = Field(None, max_length=4000)
    condition: Literal["good", "fair", "poor"] | None = Field(None, description="An inspection: the state found")
    persons: int | None = Field(None, ge=0, lt=1000)
    hours: float | None = Field(None, ge=0, lt=100000)
    done: list[Done] = Field(default_factory=list)
    link: str | None = Field(None, max_length=1000, description="Where the report itself is kept (SharePoint)")
    dry_run: mcp.DryRun = True


@mcp.tool("Report an inspection, a work party or a diary note", changes=True, operations=True, feature="reports")
def report_work(c: mcp.Call, a: ReportWork) -> dict:
    """Report what was done on which day and where: whole routes, or a part of one in km from its start. Only what
    the person has said. Dry, it says what would be kept."""
    covers = [{"route": mcp._route_id(c.conn, r.route),
               "parts": [{"from_m": r.from_km * 1000, "to_m": r.to_km * 1000}] if r.from_km is not None and r.to_km is not None else None}
              for r in a.routes]
    told = reports.report(c.conn, c.author, kind=a.kind, done_on=a.done_on, year_only=a.year_only, covers=covers, text=a.text, condition=a.condition,
                          persons=a.persons, hours=a.hours, done=a.done, link=a.link)
    told.pop("lines", None)
    return told
