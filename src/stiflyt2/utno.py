"""What UT.no says about a route: read from the route's own page there, one route at a time.

UT.no has no API that is open to us (its GraphQL API wants a client that
UT.no issues, and we have none). What it shows the public about a route is
on the route's page, `https://ut.no/rutebeskrivelse/<number>`, and the page
carries the same as data: code, name, the places at its ends, a description
for each direction, grading, length. That is what is read here.

One page is fetched when a person asks for one route, and no faster than
one a second. Nothing here goes through routes on its own: there is no
search by code without the API, so the person says which page is the
route's, and the number is kept with the route. The text is DNT's; what is
done with it is the person's to decide, and nothing is written here.
"""
from __future__ import annotations

import json
import re
import threading
import time
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

from stiflyt2.config import settings
from stiflyt2.domain.places import Invalid, NotFound

HOSTS = ("ut.no", "www.ut.no")
PAGE = "https://ut.no/rutebeskrivelse/{number}"
MOST = 2_000_000        # bytes of a page that are read
APART_S = 1.0           # at least this long between two pages fetched
# The page itself, or the same page in UT.no's map (`/kart/rutebeskrivelse/…`), with or without its name after the number.
_ADDRESS = re.compile(r"^(?:https?://)?(?:www\.)?ut\.no/(?:kart/)?rutebeskrivelse/(\d{1,12})(?:[/?#].*)?$", re.IGNORECASE)
_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
_turn, _last = threading.Lock(), [0.0]


class Unavailable(Exception):
    """UT.no did not answer, or its page was not as it used to be."""


def number(address: str | int) -> int:
    """The number of a route on UT.no, from the address of its page there or from the number itself."""
    said = str(address).strip()
    found = _ADDRESS.match(said)
    if found:
        return int(found.group(1))
    if said.isdigit() and len(said) <= 12:
        return int(said)
    raise Invalid("give the address of the route's page on UT.no, like https://ut.no/rutebeskrivelse/13394424, "
                  "or the number in it")


def address(route_number: int) -> str:
    return PAGE.format(number=route_number)


def search(name: str | None, code: str) -> str:
    """A web search for a route's page on UT.no, by the names of its ends, for a person to open in their browser.
    There is no search here: UT.no's own runs in the browser through its API, which wants a client we do not have."""
    words = " ".join(f'"{w.strip()}"' for w in re.split(r"\s+[–-]\s+", name or "") if w.strip()) or code
    return "https://www.google.com/search?q=" + quote(f"site:ut.no rutebeskrivelse {words}")


_BREAKS = {"p": "\n\n", "div": "\n\n", "ul": "\n\n", "ol": "\n\n", "h1": "\n\n", "h2": "\n\n", "h3": "\n\n",
           "h4": "\n\n", "br": "\n", "li": "\n"}


class _Words(HTMLParser):
    """The words of a piece of HTML, one paragraph to a block, a list item to a line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.link: str | None = None
        self.shown = ""

    def handle_starttag(self, tag, attrs):
        if tag in _BREAKS:
            self.out.append(_BREAKS[tag])
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            self.link, self.shown = dict(attrs).get("href"), ""

    def handle_endtag(self, tag):
        if tag in _BREAKS and tag not in ("br", "li"):
            self.out.append(_BREAKS[tag])
        if tag == "a":       # where a link leads is said after its words, unless the words are the address
            if self.link and self.link.startswith(("http://", "https://")) and self.link not in self.shown:
                self.out.append(f" ({self.link})")
            self.link = None

    def handle_data(self, data):
        self.out.append(re.sub(r"\s+", " ", data))
        if self.link is not None:
            self.shown += data


def words(html: str | None) -> str:
    """Text out of the HTML that UT.no keeps descriptions as."""
    if not html:
        return ""
    reader = _Words()
    reader.feed(html)
    reader.close()
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", "".join(reader.out)))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=httpx.Timeout(20.0),
                        follow_redirects=True, max_redirects=3)


def fetch(route_number: int, http: httpx.Client | None = None) -> dict:
    """What UT.no's page for a route says about it. Nothing is written."""
    own = http is None
    http = http or client()
    with _turn:
        wait = APART_S - (time.monotonic() - _last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            answer = http.get(address(route_number))
        except httpx.HTTPError as e:
            raise Unavailable(f"UT.no did not answer ({type(e).__name__})") from e
        finally:
            _last[0] = time.monotonic()
            if own:
                http.close()
    if answer.status_code in (404, 410):
        raise NotFound(f"UT.no has no route description with the number {route_number}")
    if answer.status_code != 200 or answer.url.host not in HOSTS or len(answer.content) > MOST:
        raise Unavailable(f"UT.no did not give the page (answered {answer.status_code})")
    try:
        route = json.loads(_DATA.search(answer.text).group(1))["props"]["pageProps"]["route"]
        if route["__typename"] != "Route" or int(route["id"]) != route_number:
            raise KeyError("another thing than the route asked for")
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise Unavailable("UT.no's page for the route is not put together as it used to be: "
                          "the description could not be read out of it") from e

    def said(key: str) -> str | None:
        value = route.get(key)
        return value.strip() or None if isinstance(value, str) else None

    return {"number": route_number, "address": address(route_number), "code": said("code"), "name": said("name"),
            "from": said("placeA"), "via": said("placeVia"), "to": said("placeB"),
            "there": words(route.get("descriptionAb")), "back": words(route.get("descriptionBa")),
            "notes": words(route.get("notes")),
            "grading_there": said("gradingAb"), "grading_back": said("gradingBa"),
            "length_m": route["distance"] if isinstance(route.get("distance"), int) else None,
            "kind": said("type"), "public": route.get("status") == "PUBLIC",
            "changed": (said("updatedAt") or "")[:10] or None}


_PLAIN = re.compile(r"\b(turisthytte|turisthytta|fjellstue|fjellstuer|fjellstova|fjellstugu|fjellhotell|hotell|hotel|"
                    r"høyfjellsstuer|vandrerhjem|turistgard|turistsenter|gård|gard|seter|sæter|setra|sætra|"
                    r"øvre|nedre|sørre|søre|nordre)\b|[^\w ]")


def _plain(name: str | None) -> str:
    """A place name as it is compared: what kind of place it is, which of two it is (øvre, nedre), case and marks, left out."""
    return " ".join(_PLAIN.sub(" ", (name or "").lower()).split())


def _near(x: str, y: str) -> bool:
    """Two words spelt nearly alike (Juvasshytta, Juvvasshytta): at most one letter in six apart."""
    if abs(len(x) - len(y)) > 2 or min(len(x), len(y)) < 5:
        return x == y
    row = list(range(len(y) + 1))
    for i, a in enumerate(x, 1):
        last, row[0] = row[0], i
        for j, b in enumerate(y, 1):
            last, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, last + (a != b))
    return row[-1] <= max(1, min(len(x), len(y)) // 6)


def _same(a: str | None, b: str | None) -> bool:
    x, y = _plain(a), _plain(b)
    return bool(x and y) and (x == y or x.startswith(y + " ") or y.startswith(x + " ") or _near(x.split()[0], y.split()[0]))


def ways(found: dict, walked_from: str | None, walked_to: str | None) -> dict:
    """UT.no's two descriptions as a route of ours has them: `there` the way the route is walked (from its first end
    to its last), `back` the other way. Which of UT.no's is which is said by the names of the ends, when they fit one
    way and not the other; `sure` is False where they do not say it, and UT.no's own order is kept."""
    a, b = found.get("there") or "", found.get("back") or ""
    along = _same(found.get("from"), walked_from) or _same(found.get("to"), walked_to)
    against = _same(found.get("from"), walked_to) or _same(found.get("to"), walked_from)
    if along and not against:
        return {"there": a, "back": b, "sure": True, "turned": False}
    if against and not along:
        return {"there": b, "back": a, "sure": True, "turned": True}
    return {"there": a, "back": b, "sure": False, "turned": False}


def described(found: dict) -> str:
    """UT.no's two descriptions, one for each direction, as one text: each under the way it is walked."""
    there, back = found["there"], found["back"]
    if not there or not back or there == back:
        return there or back
    a, b = found["from"] or "A", found["to"] or "B"
    return f"{a} – {b}\n{there}\n\n{b} – {a}\n{back}"
