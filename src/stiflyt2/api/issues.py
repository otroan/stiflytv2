"""Issues and notices on the routes over HTTP, and for an assistant: the feature `issues` (ADR-004, ADR-005).

Looking and changing both need the feature. An issue is saved as it is made, unless the person has other
decisions in operations that are not saved yet, which it then joins; Undo takes it back either way.
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Query, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth, mcp
from stiflyt2.ops import issues, undo

V1 = "/api/v1/issues"
May = Annotated[str, Depends(auth.may("issues"))]
Id = Annotated[str, Path(max_length=40)]
Kind = Literal["closed", "warning", "clearing", "marking", "sign", "bridge", "wet", "other"]
State = Literal["reported", "planned", "done", "dismissed"]
About = Literal["spot", "part", "route"]
Text = Annotated[str, Field(min_length=1, max_length=2000)]


class Spot(BaseModel):
    point: mcp.Point | None = Field(None, description="Clicked on the route's line, [lon, lat]")
    m: float | None = Field(None, ge=0, le=1_000_000, description="Or metres from the route's start")


class Part(BaseModel):
    from_point: mcp.Point | None = None
    to_point: mcp.Point | None = None
    from_m: float | None = Field(None, ge=0, le=1_000_000)
    to_m: float | None = Field(None, ge=0, le=1_000_000)


Parts = Annotated[list[Part], Field(min_length=1, max_length=issues.MOST_PARTS)]


class Report(BaseModel):
    route: str = Field(max_length=40, description="The id of the route")
    kind: Kind
    text: Text
    about: About = "spot"
    spot: Spot | None = None
    parts: Parts | None = None
    state: State = "reported"
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None


class Change(BaseModel):
    kind: Kind | None = None
    text: Text | None = None
    about: About | None = None
    state: State | None = None
    valid_from: dt.date | None = None           # given as null: taken away
    valid_until: dt.date | None = None
    spot: Spot | None = None                    # where it is now, said anew
    parts: Parts | None = None


class Chosen(BaseModel):
    route: str = Field(max_length=40)
    parts: Parts


class Undo(BaseModel):
    txid: int | None = None


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(V1)
    def listed(_: May, area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
               route: Annotated[str | None, Query(max_length=40)] = None) -> list[dict]:
        """The issues and notices of an area or a route: open first, notices in force first among them."""
        with read.connection() as conn:
            return issues.listing(conn, area, route)

    @app.get(f"{V1}/undo")
    def would_undo(author: May) -> dict:
        """What an undo would take back: your own latest decision in operations, an issue or about signs."""
        with read.connection() as conn:
            return {"last": undo.last(conn, author)}

    @app.post(f"{V1}/undo")
    def take_back(author: May, body: Undo | None = None) -> dict:
        """Take your latest decision in operations back."""
        with write.connection() as conn:
            return undo.undo(conn, author, body.txid if body else None)

    @app.post(f"{V1}/parts")
    def chosen(body: Chosen, _: May) -> dict:
        """What parts of a route would be, drawn and measured, while they are being chosen. Keeps nothing."""
        with read.connection() as conn:
            return issues.parts(conn, body.route, [p.model_dump() for p in body.parts])

    @app.get(f"{V1}/{{entry}}")
    def single(entry: Id, _: May) -> dict:
        """One issue or notice, with who said what of it and when."""
        with read.connection() as conn:
            return issues.one(conn, entry)

    @app.post(V1, status_code=201)
    def report(body: Report, author: May) -> dict:
        """Report an issue or put up a notice: at a spot on a route, along one of its stretches, or of the whole route."""
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: issues.report(conn, author, **body.model_dump()))

    @app.patch(f"{V1}/{{entry}}")
    def change(entry: Id, body: Change, author: May) -> dict:
        """Say something else of an issue: its state, text, kind, dates, or where it is."""
        said = body.model_dump(exclude_unset=True)
        with write.connection() as conn:
            return undo.kept_as_made(conn, author, lambda: issues.change(conn, author, entry, said))

    @app.delete(f"{V1}/{{entry}}", status_code=204)
    def take_away(entry: Id, author: May) -> Response:
        """Take an issue away. Undo brings it back."""
        with write.connection() as conn:
            undo.kept_as_made(conn, author, lambda: issues.take_away(conn, author, entry))
        return Response(status_code=204)


# ---- For an assistant ----

class Issues(mcp.Args):
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$", description="An area: its three letters")
    route: str | None = Field(None, max_length=40, description="Or one route, by its code or id")
    all: bool = Field(False, description="True: also what is done or dismissed")


@mcp.tool("Issues and notices on the routes", feature="issues")
def route_issues(c: mcp.Call, a: Issues) -> dict:
    """What is reported on the routes of an area or on one route: issues (to be cleared, marking to be mended,
    a sign or post, a bridge, wet ground, something else) and notices to walkers (closed, warning, from a date
    until a date). Each says what it is about (a spot, a stretch or the whole route), which routes it lies on now,
    where it is (`lon`, `lat`), its state (reported, planned, done, dismissed), whether a notice is in force
    (`active`), and whether it has lost what it hung on (`adrift`). Open ones only, unless `all`."""
    rid = mcp._route_id(c.conn, a.route) if a.route else None
    told = [e for e in issues.listing(c.conn, a.area, rid) if a.all or e["open"]]
    for e in told:
        e.pop("lines", None)
    return {"issues": told, "count": len(told)}


class Along(mcp.Args):
    from_km: float | None = Field(None, ge=0, le=1000, description="Where it begins, in km from the route's start")
    to_km: float | None = Field(None, ge=0, le=1000, description="Where it ends, in km from the route's start")
    from_at: mcp.Point | None = Field(None, description="Or where it begins, [lon, lat] on the route's line")
    to_at: mcp.Point | None = Field(None, description="Or where it ends, [lon, lat] on the route's line")


def _parts(parts: list[Along] | None) -> list[dict] | None:
    return [{"from_point": p.from_at, "to_point": p.to_at, "from_m": p.from_km * 1000 if p.from_km is not None else None,
             "to_m": p.to_km * 1000 if p.to_km is not None else None} for p in parts] if parts else None


class ReportIssue(mcp.Args):
    route: str = Field(max_length=40, description="The route, by its code or id")
    kind: Kind
    text: str = Field(min_length=1, max_length=2000, description="What it is, as the person said it")
    about: About = Field("spot", description="A spot on the route, one or more parts of it, or the whole route")
    at: mcp.Point | None = Field(None, description="A spot: where on the route, [lon, lat]")
    at_km: float | None = Field(None, ge=0, le=1000, description="Or a spot in km from the route's start")
    parts: list[Along] | None = Field(None, max_length=issues.MOST_PARTS, description="Parts: where each begins and ends")
    state: State = "reported"
    valid_from: dt.date | None = Field(None, description="A notice (closed, warning): from this day")
    valid_until: dt.date | None = Field(None, description="A notice: until this day. None: until further notice")
    dry_run: mcp.DryRun = True


@mcp.tool("Report an issue or put up a notice", changes=True, operations=True, feature="issues")
def report_issue(c: mcp.Call, a: ReportIssue) -> dict:
    """Report something on a route that is wrong or to be done, or say to walkers that it is closed or that
    they are warned, from a date until a date. At a spot (`at`, within 50 m of the route's line, or `at_km` from
    its start), along parts of it (`parts`: from and to, in km from the start or as points: clearing from one
    bend to another), or of the whole route. Only what the person has said. Dry, it says what would be kept."""
    rid = mcp._route_id(c.conn, a.route)
    spot = {"point": a.at, "m": a.at_km * 1000 if a.at_km is not None else None}
    return issues.report(c.conn, c.author, route=rid, kind=a.kind, text=a.text, about=a.about, spot=spot,
                         parts=_parts(a.parts), state=a.state, valid_from=a.valid_from, valid_until=a.valid_until)


class ChangeIssue(mcp.Args):
    issue: str = Field(max_length=40, description="The `id` of the issue, from `route_issues`")
    state: State | None = None
    text: str | None = Field(None, min_length=1, max_length=2000)
    kind: Kind | None = None
    valid_from: dt.date | None = None
    valid_until: dt.date | None = None
    dry_run: mcp.DryRun = True


@mcp.tool("Say something else of an issue", changes=True, operations=True, feature="issues")
def change_issue(c: mcp.Call, a: ChangeIssue) -> dict:
    """Say that an issue is planned, done or dismissed, or change its text, kind or a notice's dates. Only what
    the person has said. Dry, it says what it would become."""
    said = a.model_dump(exclude={"issue", "dry_run"}, exclude_unset=True)
    return issues.change(c.conn, c.author, a.issue, said)
