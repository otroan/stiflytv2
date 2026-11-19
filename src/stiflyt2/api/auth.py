"""Who is asking: sign-in with Google, tokens for programs, and the gate every request passes.

Two ways of running:

- **Signed in** (a Google client id is set): the service is reached from
  outside, through the proxy. Nobody gets anything, the page and the map tiles
  included, without being signed in as a person listed in `access.person`, or
  carrying a token that acts as one. A reader looks; an editor changes.
- **Local** (no client id): the service answers only requests made straight
  to localhost, as over an SSH tunnel, and whoever reaches it is the one
  editor named in the settings. A request that has come through a proxy is
  refused, so that this way of running is never open to the world by mistake.

Sessions are a signed cookie, as in v1 (Starlette's SessionMiddleware). The
cookie holds the e-mail address only: name and role are read from the list at
each request, so taking someone off the list shuts them out at once.
"""
from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import quote, urlparse

from anyio import to_thread
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from psycopg_pool import ConnectionPool
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection

from stiflyt2 import access
from stiflyt2.config import settings
from stiflyt2.features import FEATURES

log = logging.getLogger("stiflyt2.auth")

LOGIN, CALLBACK, LOGOUT, ME, REFUSED = (f"/api/v1/auth/{p}" for p in ("login", "callback", "logout", "me", "refused"))
OPEN = (LOGIN, CALLBACK, REFUSED)                   # reached without being signed in
LOCAL_HOSTS = ("localhost", "127.0.0.1", "testserver")
REMEMBER_S = 20                                     # how long a look-up of a person is trusted
SESSION_DAYS = 30
WHY = {"not_listed": "That address is not on the list of people who use Stiflyt. Ask to be added.",
       "email_unverified": "Google has not confirmed that address.",
       "oauth_error": "Signing in with Google did not go through. Try again."}


class Author(str):
    """The name a change is recorded under, which also knows how the change came in."""
    through: str = "viewer"
    email: str | None = None

    def __new__(cls, name: str, through: str = "viewer", email: str | None = None):
        made = super().__new__(cls, name)
        made.through, made.email = through, email
        return made


def signed_in() -> bool:
    """Whether people sign in. Otherwise the service is local, with the one editor of the settings."""
    return bool(settings.google_client_id)


def _safe_next(raw: str | None) -> str:
    """Only a path on this site: `next` must not send anyone elsewhere."""
    if not raw or not raw.startswith("/") or raw.startswith(("//", "/\\")):
        return "/"
    return raw


_oauth: OAuth | None = None


def google():
    """The Google client, made when first needed."""
    global _oauth
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            name="google", client_id=settings.google_client_id, client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"})
    return _oauth.google


def callback_url() -> str:
    return settings.public_url.rstrip("/") + CALLBACK


class Gate:
    """Lets a request through only when it is known who is asking. Pure ASGI, inside the session middleware."""

    def __init__(self, app, pool: ConnectionPool) -> None:
        self.app, self.pool = app, pool
        self.known: dict[tuple[str, str], tuple[float, access.Person | None]] = {}

    def _find(self, kind: str, key: str) -> access.Person | None:
        """A person by e-mail address or by token, remembered for a short while."""
        hit = self.known.get((kind, key))
        if hit and hit[0] > time.monotonic():
            return hit[1]
        with self.pool.connection() as conn:
            who = access.person(conn, key) if kind == "email" else access.by_token(conn, key)
            if who:
                access.seen(conn, who, key if kind == "token" else None)
        if len(self.known) > 500:
            self.known.clear()
        self.known[(kind, key)] = (time.monotonic() + REMEMBER_S, who)
        return who

    def _resolve(self, who: access.Person) -> access.Person:
        """What the one editor of a local server may use: looked up each time, as it is cheap and seldom."""
        with self.pool.connection() as conn:
            return access.resolved(conn, who)

    async def who(self, conn: HTTPConnection) -> access.Person | None:
        """Who is asking, or None when that is not known."""
        if not signed_in():
            said = conn.headers.get("x-stiflyt-through", "")
            local = access.Person(settings.editor or None, "editor" if settings.editor else "reader",
                                  through=said if said in ("viewer", "mcp") else "api")
            return await to_thread.run_sync(self._resolve, local)
        bearer = conn.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            return await to_thread.run_sync(self._find, "token", bearer[7:].strip())
        email = (conn.session.get("user") or {}).get("email")
        return await to_thread.run_sync(self._find, "email", email) if email else None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if not signed_in():
            # Local: only what is asked straight of localhost. Never what a proxy has passed on.
            host = (conn.headers.get("host") or "").rsplit(":", 1)[0]
            if host not in LOCAL_HOSTS or any(h.startswith("x-forwarded-") or h == "forwarded" for h in conn.headers):
                await JSONResponse({"detail": "sign-in is not set up on this server"}, status_code=403)(
                    scope, receive, send)
                return
        # A page may ask to be answered as one with a lesser role than its person holds (the role switch).
        person = access.looking_as(await self.who(conn), conn.headers.get("x-stiflyt-as"))
        scope["stiflyt2_person"] = person
        if person is None and scope["path"] not in OPEN:
            if scope["path"].startswith("/api/") or scope["path"] == "/mcp":
                # A program is asking, not a browser: told so, and how to come back (a token that acts as a person).
                answer = JSONResponse({"detail": "not signed in"}, status_code=401,
                                      headers={"WWW-Authenticate": 'Bearer realm="stiflyt2"'})
            elif scope["method"] == "GET" and "text/html" in conn.headers.get("accept", ""):
                # A person in a browser: to the sign-in, and back to where they were going.
                answer = RedirectResponse(f"{LOGIN}?next={quote(_safe_next(scope['path']), safe='/')}", status_code=302)
            else:
                # A program looking for something else, such as an assistant's client asking whether
                # this server has OAuth of its own (/.well-known/…, /register). It has not: said so in
                # JSON, and not with a sign-in page that the program would try to read as an answer.
                answer = JSONResponse({"detail": "not found"}, status_code=404)
            await answer(scope, receive, send)
            return
        await self.app(scope, receive, send)


def person_of(request: Request) -> access.Person:
    return request.scope.get("stiflyt2_person") or access.Person(None, "reader")


def may(feature: str):
    """What an endpoint asks for, to belong to a feature: the one whose request this is, who must have the
    feature. Refuses those who lack it, and requests made by another site's page in the browser."""
    said = FEATURES[feature]

    def allowed(request: Request) -> Author:
        who = person_of(request)
        if not who.has(feature):
            raise HTTPException(403, (f"you are signed in to look, not to {said.title}: {said.ask}"
                                      if feature not in ("routes", "signs")
                                      else f"you are signed in to look, not to change: {said.ask}")
                                if signed_in() else "editing is not enabled on this server")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "changes are only accepted from this site's own page")
        return Author(who.name, who.through, who.email)
    return allowed


editor = may("routes")          # the one whose change to the routes this is: a route responsible
operator = may("signs")         # the one whose decision about signs this is: operations, or a route responsible


def install(app: FastAPI, pool: ConnectionPool) -> None:
    """The gate and the sessions. Added before the other middleware, so that it sits inside them."""
    app.add_middleware(Gate, pool=pool)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or secrets.token_urlsafe(48),
                       session_cookie="stiflyt2", max_age=SESSION_DAYS * 86400, same_site="lax",
                       https_only=settings.public_url.startswith("https://"))


def register(app: FastAPI, pool: ConnectionPool) -> None:
    @app.get(LOGIN, include_in_schema=False)
    async def login(request: Request, next: str | None = None):
        if not signed_in():
            return RedirectResponse("/", status_code=302)
        request.session["next"] = _safe_next(next)
        return await google().authorize_redirect(request, callback_url())

    @app.get(CALLBACK, include_in_schema=False)
    async def callback(request: Request):
        target = _safe_next(request.session.pop("next", None))

        def back(why: str) -> RedirectResponse:
            request.session.pop("user", None)
            return RedirectResponse(f"{REFUSED}?why={why}", status_code=302)

        try:
            token = await google().authorize_access_token(request)
        except OAuthError as e:
            log.warning("sign-in refused by Google: %s", e)
            return back("oauth_error")
        said = token.get("userinfo") or {}
        email = (said.get("email") or "").strip().lower()
        if not email or not said.get("email_verified"):
            return back("email_unverified")

        def listed() -> access.Person | None:
            with pool.connection() as conn:
                return access.person(conn, email)

        if not await to_thread.run_sync(listed):
            log.info("sign-in refused: %s is not listed", email)
            return back("not_listed")
        request.session["user"] = {"email": email}
        return RedirectResponse(target, status_code=302)

    @app.get(REFUSED, include_in_schema=False)
    def refused(why: str = "") -> HTMLResponse:
        """Why signing in did not let you in, with a way to try again."""
        return HTMLResponse(
            "<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Stiflyt v2: not signed in</title>"
            "<body style='font:16px/1.5 system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 20px'>"
            f"<h1 style='font-size:22px'>You are not signed in</h1><p>{WHY.get(why, WHY['oauth_error'])}</p>"
            f"<p><a href='{LOGIN}'>Sign in with another Google account</a></p></body></html>", status_code=403)

    @app.post(LOGOUT)
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get(ME)
    def me(request: Request) -> dict:
        """Who you are here, and what you may do. `really` is the role you hold while you look at the pages
        as one with a lesser role, and `may_look_as` the roles you can look as: in the pages only."""
        who = person_of(request)
        holds = who.really or who.role
        looks = [name for name, _ in who.look_as] if who.features is not None else access.lesser(holds)
        return {"name": who.name, "email": who.email, "role": who.role, "edits": who.edits, "operates": who.operates,
                "features": sorted(f for f in FEATURES if who.has(f)),
                "through": who.through, "sign_in": signed_in(), "really": who.really,
                "may_look_as": looks if who.name and who.through == "viewer" else []}
