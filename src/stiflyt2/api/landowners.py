"""Who owns the ground, over HTTP: the feature `landowners` (ADR-005).

Everything here asks for the feature, the looking too. The properties come from
the property map; their owners are asked of Kartverket's register only when
`owners` is asked for, and each time that is kept (who, when, which units),
not what the register said.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from psycopg_pool import ConnectionPool

from stiflyt2.api import auth
from stiflyt2.domain.places import NotFound
from stiflyt2.ops import landowners

V1 = "/api/v1/landowners"
May = Annotated[auth.Author, Depends(auth.may("landowners"))]
Owners = Annotated[bool, Query(description="Also who owns each property, asked of Kartverket's register")]


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    def asked(who, what: str, units: list[dict]) -> None:
        with write.connection() as conn:
            landowners.asked_by(conn, who, what, [u["id"] for u in units if u.get("id")])

    @app.get(f"{V1}/at")
    def at(lon: Annotated[float, Query(ge=-180, le=180)], lat: Annotated[float, Query(ge=-90, le=90)],
           who: May, owners: Owners = False) -> dict:
        """The property at a spot: its parcel's outline, its number and name, and, when asked for, its owners."""
        with read.connection() as conn:
            found = landowners.at(conn, lon, lat)
        if owners and found["units"]:
            asked(who, f"a spot at {lat:.5f}, {lon:.5f}", found["units"])
            found = landowners.with_owners(found, found["units"])
        return found

    def _along(route: str, who, owners: bool) -> dict:
        with read.connection() as conn:
            try:
                found = landowners.along(conn, route)
            except NotFound as e:
                raise HTTPException(404, str(e)) from e
        if owners and found["properties"]:
            asked(who, found["route"]["code"], found["properties"])
            found = landowners.with_owners(found, found["properties"])
        return found

    @app.get(f"{V1}/along")
    def along(route: Annotated[str, Query(max_length=40)], who: May, owners: Owners = False) -> dict:
        """The properties a route crosses, in the order it crosses them, with where along it; when asked for,
        who owns each."""
        return _along(route, who, owners)

    @app.get(f"{V1}/along.xlsx")
    def along_list(route: Annotated[str, Query(max_length=40)], who: May) -> Response:
        """The properties along a route and their owners, as an Excel list."""
        found = _along(route, who, True)
        return Response(landowners.workbook(found),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="grunneiere-{found["route"]["code"]}.xlsx"'})
