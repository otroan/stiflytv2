"""Read-only HTTP API over source_index, and the map page that uses it.

Connects as stiflyt2_api, which can read the index but not source_raw. Every
statement is read-only and limited in time, so a heavy question cannot hold up
the database this service shares.

    uvicorn stiflyt2.api.app:app --host 127.0.0.1 --port 8100
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool

from stiflyt2.api import auth as auth_api
from stiflyt2.api import feedback as feedback_api
from stiflyt2.api import issues as issues_api
from stiflyt2.api import lookup as lookup_api
from stiflyt2.api import mcp as mcp_api
from stiflyt2.api import owned_routes as routes_api
from stiflyt2.api import photos as photos_api
from stiflyt2.api import places as places_api
from stiflyt2.api import presence as presence_api
from stiflyt2.api import profile as profile_api
from stiflyt2.api import reports as reports_api
from stiflyt2.api import signs as signs_api
from stiflyt2.api import trips as trips_api
from stiflyt2.config import settings
from stiflyt2.domain import routes as owned_routes
from stiflyt2.index import query

STATIC = Path(__file__).parent / "static"
V1 = "/api/v1"
Text = Annotated[str, Query(min_length=1, max_length=80)]


class _Fresh(StaticFiles):
    """Scripts and styles that the browser asks about every time: a page and its scripts change together,
    and a page run with yesterday's script fails in ways nobody can make sense of."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Compiling a statement before running it pays off for long reports over millions of rows. Here it
# cost more than it saved: most of a second on a statement that then ran for two.
QUICK = "-c jit=off"


def create_app() -> FastAPI:
    pool = ConnectionPool(
        settings.api_dsn, min_size=1, max_size=4, open=False,
        kwargs={"options": f"-c statement_timeout=15000 -c default_transaction_read_only=on {QUICK}"},
    )

    # Changes to owned data go through their own small pool, which may write.
    writes = ConnectionPool(settings.api_dsn, min_size=0, max_size=2, open=False,
                            kwargs={"options": f"-c statement_timeout=15000 {QUICK}"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open()
        writes.open()
        yield
        writes.close()
        pool.close()

    app = FastAPI(title="Stiflyt v2", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    # What is added first sits innermost: the gate knows who is asking before anything is answered,
    # and what is refused or fails is collected inside the compression, as it was written.
    auth_api.install(app, writes)
    app.add_middleware(feedback_api.Collect, pool=writes)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.exception_handler(psycopg.errors.QueryCanceled)
    def too_slow(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "the question took too long to answer"}, status_code=504)

    @app.get("/", include_in_schema=False)
    def operations() -> FileResponse:
        """The front door: operations. What happens to the routes: signs, and later work and reports."""
        return FileResponse(STATIC / "drift.html", headers={"Cache-Control": "no-cache"})

    @app.get("/ruter", include_in_schema=False)
    def page() -> FileResponse:
        """The route editor: where the routes and the graph themselves are put right."""
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/guide", include_in_schema=False)
    def guide() -> FileResponse:
        """How to use it, for someone new."""
        return FileResponse(STATIC / "guide.html", headers={"Cache-Control": "no-cache"})

    @app.get(f"{V1}/status")
    def status() -> list[dict]:
        """When each source's index tables were built, and how many rows they hold."""
        with pool.connection() as conn:
            return query.index_state(conn)

    @app.get(f"{V1}/placenames")
    def placenames(q: Text, lon: float | None = None, lat: float | None = None,
               limit: Annotated[int, Query(ge=1, le=50)] = 15) -> list[dict]:
        """Place names matching q, misspellings and historical spellings included."""
        near = (lon, lat) if lon is not None and lat is not None else None
        with pool.connection() as conn:
            return query.search_places(conn, q, limit=limit, near=near, near_srid=4326)

    @app.get(f"{V1}/areas")
    def areas() -> list[dict]:
        """The three-letter route code prefixes in use, with route counts."""
        with pool.connection() as conn:
            return query.areas(conn)

    @app.get(f"{V1}/routes")
    def routes(q: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
               area: Annotated[str | None, Query(pattern="^[A-Za-z]{3}$")] = None,
               segment: Annotated[str | None, Query(max_length=64)] = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 30) -> list[dict]:
        """Route candidates: by code or name (q), by area prefix, or by a segment they use."""
        with pool.connection() as conn:
            if segment:
                return query.segment_routes(conn, segment)
            if area:
                return query.area_routes(conn, area)
            if q:
                return query.search_routes(conn, q, limit=limit)
        raise HTTPException(422, "give q, area or segment")

    @app.get(f"{V1}/anchors")
    def anchors(area: Annotated[str, Query(pattern="^[A-Za-z]{3}$")]) -> list[dict]:
        """The huts and parking places where routes of an area end, most scattered first."""
        with pool.connection() as conn:
            return query.area_anchors(conn, area)

    @app.get(f"{V1}/anchors/{{anchor_id}}")
    def anchor(anchor_id: str) -> dict:
        """One anchor: a place routes end at, and the route ends that belong to it."""
        with pool.connection() as conn:
            found = query.anchor_detail(conn, anchor_id)
            if found is None:
                raise HTTPException(404, "no such anchor")
            # The owned place made from this proposal, if it has been adopted.
            adopted = conn.execute(
                """SELECT p.id FROM core.source_reference sr JOIN core.place p ON p.id = sr.subject_id
                   WHERE sr.subject_type = 'place' AND sr.ref_type = 'adopted_from'
                     AND sr.detail->>'anchor_id' = %s AND p.status = 'confirmed'""",
                (anchor_id,)).fetchone()
        found["place_id"] = str(adopted[0]) if adopted else None
        return found

    @app.get(f"{V1}/routes/{{route_id}}")
    def route(route_id: str) -> dict:
        """One route candidate: geometry, and the paths, heritage sites and parcels along it."""
        with pool.connection() as conn:
            found = query.route_detail(conn, route_id)
            if found is None:
                raise HTTPException(404, "no such route")
            # Your route that this candidate has been made into or added to, if any.
            found["owned_route_id"] = owned_routes.owner_of(conn, route_id)
        return found

    @app.get(f"{V1}/tiles/{{layer}}/{{z}}/{{x}}/{{y}}.mvt")
    def tile(layer: str, z: int, x: int, y: int) -> Response:
        if layer not in query.TILE_LAYERS:
            raise HTTPException(404, "no such layer")
        if not (0 <= z <= 22 and 0 <= x < 2**z and 0 <= y < 2**z):
            raise HTTPException(404, "no such tile")
        with pool.connection() as conn:
            body = query.tile(conn, layer, z, x, y)
        return Response(body, media_type="application/vnd.mapbox-vector-tile",
                        headers={"Cache-Control": "max-age=300"})

    # The pages' scripts and styles, behind the same gate as the pages.
    app.mount("/static", _Fresh(directory=STATIC), name="static")
    places_api.register(app, pool, writes)
    routes_api.register(app, pool, writes)
    feedback_api.register(app, pool, writes)
    auth_api.register(app, pool)
    mcp_api.register(app, pool, writes)
    lookup_api.register(app, pool)
    signs_api.register(app, pool, writes)
    trips_api.register(app, pool)
    presence_api.register(app, pool)
    issues_api.register(app, pool, writes)
    photos_api.register(app, pool, writes)
    reports_api.register(app, pool, writes)
    profile_api.register(app, pool, writes)
    return app


app = create_app()
