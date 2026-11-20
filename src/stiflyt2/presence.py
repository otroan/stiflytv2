"""Who else is at work in the pages now, and where: the feature `presence` (ADR-005).

Signing in lasts for weeks, so who is signed in says little. What is known is
who has a page open and where: each page tells the service every 20 seconds,
and somebody whose page has not told it for FRESH_S is gone. It is kept in
the service's memory and nowhere else: a restart empties it, and the pages
fill it again within a minute.

Only those who have the feature are seen, and they see the others who have
it: being shown to the others comes with seeing them. Only the pages count:
an assistant or a script acting for somebody is not somebody at work here.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from stiflyt2 import features

features.feature("presence", "see who else is at work in the pages now, and be seen by them",
                 "ask to be given the feature `presence`")

FRESH_S = 90            # a page that has not said where it is for this long is closed
PAGES = ("editor", "routes", "signs", "trips")
_TEXT = re.compile(r"[^\w\s/().,'’–-]", re.UNICODE)


def _short(text, most: int = 60) -> str | None:
    """What a page says, kept short and plain: it is shown to others."""
    text = _TEXT.sub("", str(text or "")).strip()[:most]
    return text or None


@dataclass
class Board:
    seen: dict[str, dict] = field(default_factory=dict)       # by name

    def say(self, name: str, where: dict, now: float | None = None) -> None:
        """Somebody's page is open, here."""
        page = where.get("page") if where.get("page") in PAGES else None
        self.seen[name] = {"name": name, "page": page, "area": _short(where.get("area"), 3),
                           "route": _short(where.get("route"), 40), "site": _short(where.get("site")),
                           "at": time.time() if now is None else now}

    def others(self, name: str, now: float | None = None) -> list[dict]:
        """The others whose pages are open now, by name."""
        now = time.time() if now is None else now
        for gone in [n for n, s in self.seen.items() if now - s["at"] > FRESH_S]:
            del self.seen[gone]
        return [{k: v for k, v in s.items() if k != "at"} | {"seconds": round(now - s["at"])}
                for n, s in sorted(self.seen.items()) if n != name]


BOARD = Board()
