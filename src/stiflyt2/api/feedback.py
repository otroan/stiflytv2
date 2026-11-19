"""Collecting what the tool refuses and where it fails, and taking in what people report.

Every answer under /api that says no, and every error, is kept with what was
asked, by whom and about which route (`stiflyt2.feedback`). It is written
after the answer has gone out, on a connection of its own: what a refused
change tried is rolled back, the note that it was refused is not. Failing to
keep an entry never changes the answer.
"""
from __future__ import annotations

import json
import logging
import traceback
from typing import Annotated, Literal
from urllib.parse import parse_qsl

from anyio import to_thread
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import feedback
from stiflyt2.api.places import Editor

log = logging.getLogger("stiflyt2.feedback")

KEEP = 8192                 # bytes kept of what was asked with, and of the answer
ABOUT = (("route_id", "route"), ("place_id", "place"), ("edge_id", "edge"), ("node_id", "node"))


def kind_of(method: str, status: int) -> str | None:
    """Whether an answer is worth keeping, and as what. A page asking for something gone is not."""
    if status >= 500:
        return "failed"
    if status in (400, 403, 409, 422) or (status == 404 and method != "GET"):
        return "refused"
    return None


def asking(scope) -> tuple[str | None, str]:
    """Who asked and how it came in, as the gate found it."""
    person = scope.get("stiflyt2_person")
    return (person.name, person.through) if person else (None, "api")


def told(status: int, answer: bytes) -> str:
    """The sentence the person was given."""
    try:
        detail = json.loads(answer)["detail"]
    except (ValueError, KeyError, TypeError):
        return answer.decode("utf-8", "replace")[:500] or f"answered {status}"
    if isinstance(detail, str):
        return detail
    try:        # what was sent did not have the shape asked for: a fault in the page or the assistant
        return "the request was not understood: " + "; ".join(
            f"{'.'.join(str(p) for p in d['loc'])}: {d['msg']}" for d in detail)
    except (KeyError, TypeError):
        return json.dumps(detail)[:500]


class Collect:
    """Keeps every refusal and failure under /api. Pure ASGI: it sees what was asked and what was answered."""

    def __init__(self, app, pool: ConnectionPool) -> None:
        self.app, self.pool = app, pool

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        asked, answer, status = bytearray(), bytearray(), None

        async def receiving():
            message = await receive()
            if message["type"] == "http.request" and len(asked) < KEEP:
                asked.extend(message.get("body", b"")[:KEEP - len(asked)])
            return message

        async def sending(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and (status or 0) >= 400 and len(answer) < KEEP:
                answer.extend(message.get("body", b"")[:KEEP - len(answer)])
            await send(message)

        try:
            await self.app(scope, receiving, sending)
        except Exception as e:
            last = traceback.extract_tb(e.__traceback__)[-1]
            await to_thread.run_sync(self.keep, scope, bytes(asked), "failed", 500,
                                     f"{type(e).__name__}: {e} ({last.name}, line {last.lineno})",
                                     "".join(traceback.format_exception(e)))
            raise
        kind = kind_of(scope["method"], status) if status else None
        # What a look ahead while something is dragged is refused, the person never asked for: not kept.
        if kind == "refused" and any(k == b"x-stiflyt-preview" for k, _ in scope.get("headers") or ()):
            kind = None
        if kind:
            await to_thread.run_sync(self.keep, scope, bytes(asked), kind, status,
                                     told(status, bytes(answer)), None)

    def keep(self, scope, asked: bytes, kind: str, status: int, message: str, trace: str | None) -> None:
        try:
            given = {k: str(v) for k, v in (scope.get("path_params") or {}).items()}
            what = scope["path"]
            for name, value in given.items():
                what = what.replace(f"/{value}", f"/{{{name}}}", 1)
            try:
                body = json.loads(asked) if asked else None
            except ValueError:
                body = asked.decode("utf-8", "replace")
            arguments = {k: v for k, v in (("path", given), ("body", body),
                                           ("query", dict(parse_qsl(scope["query_string"].decode("latin-1")))))
                         if v}
            about = next(((kind_, given[name]) for name, kind_ in ABOUT if name in given), None)
            who, through = asking(scope)
            with self.pool.connection() as conn:
                feedback.record(conn, kind, through, f"{scope['method']} {what}", message,
                                who=who, status=status, arguments=arguments, about=about, trace=trace)
        except Exception:        # never let the note of a fault become a fault
            log.exception("could not keep feedback about %s %s", scope.get("method"), scope.get("path"))


class Told(BaseModel):
    """What a person, the page or an assistant says is wrong or cannot be done."""
    kind: Literal["reported", "failed"] = "reported"
    message: str = Field(min_length=1, max_length=feedback.MESSAGE_MAX)      # what was wanted, or what went wrong
    what: str = Field("note", min_length=1, max_length=80)                   # 'note', 'page', 'report_gap'
    tried: str | None = Field(None, max_length=2000)
    why_not: str | None = Field(None, max_length=2000)
    about_type: Literal["route", "place", "edge", "node"] | None = None
    about_id: str | None = Field(None, min_length=1, max_length=40)
    context: dict | None = None
    trace: str | None = Field(None, max_length=feedback.TRACE_MAX)


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.exception_handler(feedback.NotFound)
    def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.post("/api/v1/feedback", status_code=201)
    def tell(body: Told, request: Request, author: Editor) -> dict:
        """Say that something is wrong, is missing or cannot be done. Kept for whoever looks after the tool."""
        arguments = {k: v for k, v in (("tried", body.tried), ("why_not", body.why_not),
                                       ("context", body.context)) if v}
        about = (body.about_type, body.about_id) if body.about_type and body.about_id else None
        with write.connection() as conn:
            return {"id": feedback.record(conn, body.kind, asking(request.scope)[1], body.what,
                                          body.message, who=author, arguments=arguments, about=about,
                                          trace=body.trace)}

    @app.get("/api/v1/feedback")
    def collected(everything: Annotated[bool, Query(alias="all")] = False) -> dict:
        """What has come in and nobody has looked at yet (or all of it), and the changes taken back at once."""
        with read.connection() as conn:
            return {"groups": feedback.grouped(conn, everything), "taken_back": feedback.taken_back(conn)}
