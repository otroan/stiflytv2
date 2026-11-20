"""Who else is at work in the pages now, over HTTP: the feature `presence` (ADR-005)."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2 import presence
from stiflyt2.api import auth
from stiflyt2.domain import undo

May = Annotated[str, Depends(auth.may("presence"))]


class Where(BaseModel):
    page: str | None = Field(None, max_length=20)
    area: str | None = Field(None, max_length=10)
    route: str | None = Field(None, max_length=60)
    site: str | None = Field(None, max_length=200)


def register(app: FastAPI, read: ConnectionPool) -> None:
    @app.post("/api/v1/here")
    def here(body: Where, who: May) -> list[dict]:
        """Say where your page is, and hear who else has a page open now: where, and what they have not saved."""
        if who.through != "viewer":
            raise HTTPException(403, "only a page that is open is somebody at work here")
        presence.BOARD.say(str(who), body.model_dump())
        others = presence.BOARD.others(str(who))
        if others:
            with read.connection() as conn:
                waiting = {i["author"]: i for i in undo.open_items(conn)}
            for o in others:
                w = waiting.get(o["name"])
                o["unsaved"] = w["changes"] if w else 0
        return others
