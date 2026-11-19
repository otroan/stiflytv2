"""Signs over HTTP: the sites, posts and signs of an area, what a person decides about them, and orders.

Looking is for everyone who is signed in. Changing is for operations and for the route responsible
(`auth.operator`). Nothing here changes a route.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Query, Response
from fastapi.responses import HTMLResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from stiflyt2.api import auth
from stiflyt2.ops import sheet, signs, undo

V1 = "/api/v1/signs"
Operator = Annotated[str, Depends(auth.operator)]
Area = Annotated[str, Query(pattern="^[A-Za-z]{3}$")]
Text = Annotated[str, Field(max_length=400)]


class Site(BaseModel):
    area: str | None = Field(None, pattern="^[A-Za-z]{3}$")
    no_signs: bool | None = None                # there are to be no signs here: nothing of it is ordered
    name: Annotated[str, Field(max_length=120)] | None = None
    back_text: Text | None = None
    send_to_name: Annotated[str, Field(max_length=160)] | None = None
    send_to_address: Text | None = None


class Spot(BaseModel):
    area: str = Field(pattern="^[A-Za-z]{3}$")
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    name: Annotated[str, Field(max_length=120)] | None = None


class Post(BaseModel):
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    assumed: bool = False                       # true: back to where it is assumed to stand
    taken_out: bool | None = None
    name_sign: bool | None = None               # a sign with the place's name on top of the post
    name_text: Annotated[str, Field(max_length=120)] | None = None      # what it says, if not the site's name
    name_to_order: bool | None = None


class Further(BaseModel):
    node: str = Field(max_length=40)
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class Sign(BaseModel):
    key: str = Field(max_length=160)
    direction: Literal["", "venstre", "rett fram", "høyre"] | None = None
    colour: Literal["", "trehvit", "grønn"] | None = None
    text: Annotated[str, Field(max_length=120)] | None = None
    distance_m: float | None = Field(None, ge=0, le=500_000)       # 0: back to what is worked out
    post_no: int | None = Field(None, ge=0, le=20)
    taken_out: bool | None = None
    to_order: bool | None = None                # it is to be made: on the order that is taken out next
    why: Annotated[str, Field(max_length=200)] | None = None


class Destination(BaseModel):
    post: str = Field(max_length=60)
    place: str | None = Field(None, max_length=40)
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    name: Annotated[str, Field(max_length=120)] | None = None


class Marked(BaseModel):
    keys: list[Annotated[str, Field(max_length=180)]] = Field(min_length=1, max_length=2000)
    to_order: bool = True
    why: Annotated[str, Field(max_length=200)] | None = None


class Up(BaseModel):
    keys: list[Annotated[str, Field(max_length=180)]] = Field(min_length=1, max_length=2000)
    up: bool = True


class Order(BaseModel):
    area: str = Field(pattern="^[A-Za-z]{3}$")
    keys: list[Annotated[str, Field(max_length=160)]] | None = Field(None, max_length=5000)
    note: Text | None = None


class MoveTo(BaseModel):
    key: Annotated[str, Field(max_length=160)]


class Undo(BaseModel):
    txid: int | None = None


def register(app: FastAPI, read: ConnectionPool, write: ConnectionPool) -> None:
    @app.get(f"{V1}/areas")
    def areas() -> list[dict]:
        """The areas that have routes of ours in use, with how many."""
        with read.connection() as conn:
            return signs.areas(conn)

    @app.get(V1)
    def sites(area: Area) -> dict:
        """The sign sites of an area: each with its posts, and what the signs on them say."""
        with read.connection() as conn:
            return signs.listing(conn, area)

    @app.patch(f"{V1}/sites/{{key}}", status_code=204)
    def site(key: str, body: Site, author: Operator) -> Response:
        """Say that a site is to have no signs, or say its name, the text on the back and who its signs go to."""
        with write.connection() as conn:
            signs.set_site(conn, key, author, **body.model_dump())
        return Response(status_code=204)

    @app.post(f"{V1}/sites", status_code=201)
    def at_a_spot(body: Spot, author: Operator) -> dict:
        """Put a sign site at a spot on a route that is no junction."""
        with write.connection() as conn:
            return {"key": signs.add_site(conn, author, body.area, body.lon, body.lat, body.name)}

    @app.delete(f"{V1}/sites/{{key}}", status_code=204)
    def away(key: str, author: Operator) -> Response:
        """Take away a sign site that a person put at a spot."""
        with write.connection() as conn:
            signs.remove_site(conn, key, author)
        return Response(status_code=204)

    @app.patch(f"{V1}/posts/{{key}}", status_code=204)
    def post(key: str, body: Post, author: Operator) -> Response:
        """Say where a post stands, which turns its arrows; put it back; or take it out."""
        with write.connection() as conn:
            signs.set_post(conn, key, author, **body.model_dump())
        return Response(status_code=204)

    @app.post(f"{V1}/posts", status_code=201)
    def further(body: Further, author: Operator) -> dict:
        """A further post at a junction."""
        with write.connection() as conn:
            return {"key": signs.add_post(conn, body.node, author, body.lon, body.lat)}

    @app.patch(V1, status_code=204)
    def sign(body: Sign, author: Operator) -> Response:
        """Say something else of one sign than what is worked out. An empty value goes back to that."""
        with write.connection() as conn:
            signs.set_sign(conn, body.key, author, **body.model_dump(exclude={"key"}, exclude_none=True))
        return Response(status_code=204)

    @app.post(f"{V1}/destinations")
    def destination(body: Destination, author: Operator) -> dict:
        """Add a destination to a post that the routes do not give it: a place of ours, or a spot on a route.

        Answers with the sign's `key`; or, for a spot that is no place and has
        been given no name, with `needs: name`, and nothing is done.
        """
        with write.connection() as conn:
            return signs.add_destination(conn, author, body.post, place=body.place, lon=body.lon, lat=body.lat,
                                         name=body.name)

    @app.post(f"{V1}/to-order")
    def to_order(body: Marked, author: Operator) -> dict:
        """Put signs on the order that is to be taken out next, or take them off it."""
        with write.connection() as conn:
            return {"signs": signs.mark(conn, body.keys, author, body.to_order, body.why)}

    @app.get(f"{V1}/sheet", response_class=HTMLResponse)
    def field_sheet(area: Area, site: Annotated[str | None, Query(max_length=60)] = None,
                    order: Annotated[int | None, Query(ge=1)] = None) -> HTMLResponse:
        """The field sheets of an area, to print or save as PDF: one page per sign site, with a map, the posts
        and what hangs on each. Of one site, of the sites an order has signs at, or of every site."""
        with read.connection() as conn:
            return HTMLResponse(sheet.page(conn, area, site, order), headers={"Cache-Control": "no-cache"})

    @app.post(f"{V1}/up")
    def up(body: Up, author: Operator) -> dict:
        """Say that signs that were ordered are up, or not after all."""
        with write.connection() as conn:
            return {"signs": signs.mark_up(conn, body.keys, author, body.up)}

    @app.get(f"{V1}/adrift")
    def left_adrift() -> list[dict]:
        """What was decided about a sign, a post or a sign site that is no longer there to decide about: each
        with what it said, where it was, why it is adrift, and for a sign which signs it could be moved to."""
        with read.connection() as conn:
            return signs.adrift(conn)

    @app.post(f"{V1}/adrift/{{some_id}}/forget")
    def forget(some_id: Annotated[str, Path(max_length=40)], author: Operator) -> dict:
        """Forget what was decided about something that is gone. It can be taken back with undo."""
        with write.connection() as conn:
            return signs.forget(conn, some_id, author)

    @app.post(f"{V1}/adrift/{{some_id}}/move")
    def move(some_id: Annotated[str, Path(max_length=40)], body: MoveTo, author: Operator) -> dict:
        """Say which sign a decision that is adrift is about now."""
        with write.connection() as conn:
            return signs.move(conn, some_id, body.key, author)

    @app.get(f"{V1}/undo")
    def would_undo(author: Operator) -> dict:
        """What an undo would take back: your own latest decision about signs, made the way you ask."""
        with read.connection() as conn:
            return {"last": undo.last(conn, author)}

    @app.post(f"{V1}/undo")
    def take_back(author: Operator, body: Undo | None = None) -> dict:
        """Take your latest decision about signs back. An order that has been taken out stands."""
        with write.connection() as conn:
            return undo.undo(conn, author, body.txid if body else None)

    @app.get(f"{V1}/work")
    def item_of_work(author: Operator) -> dict:
        """Your item of work among the signs: what you have decided since you last saved."""
        with read.connection() as conn:
            return undo.item(conn, author)

    @app.post(f"{V1}/work/save")
    def save_work(author: Operator) -> dict:
        """Say that what you have decided since you last saved is to stay."""
        with write.connection() as conn:
            return undo.save(conn, author)

    @app.post(f"{V1}/work/discard")
    def discard_work(author: Operator) -> dict:
        """Take back all that you have decided since you last saved, in one go."""
        with write.connection() as conn:
            return undo.discard(conn, author)

    @app.get(f"{V1}/orders")
    def earlier(area: Area) -> list[dict]:
        with read.connection() as conn:
            return signs.orders(conn, area)

    @app.post(f"{V1}/orders", status_code=201)
    def take_out(body: Order, author: Operator) -> dict:
        """Take out an order for the signs that are marked to be made, or for those named. What they say is kept."""
        with write.connection() as conn:
            return signs.take_out_order(conn, body.area, author, body.keys, body.note)

    @app.get(f"{V1}/orders/{{order_id}}.xlsx")
    def workbook(order_id: int) -> Response:
        """An order as the list for whoever makes the signs: one row per sign, as it was when ordered."""
        with read.connection() as conn:
            found = signs.order(conn, order_id)
        name = f"skilt-{found['area']}-{found['id']}.xlsx"
        return Response(signs.as_workbook(found["rows"], f"Skilt {found['area']}"),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get(f"{V1}/orders/{{order_id}}")
    def one(order_id: int) -> dict:
        with read.connection() as conn:
            return signs.order(conn, order_id)
