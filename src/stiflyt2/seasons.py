"""Seasons: the routes of the summer, and DNT's winter routes, which are routes of their own.

A winter route has the code of the summer route it goes with and a v (jot11v
goes with jot11). It is staked anew each winter, over frozen lakes and bogs,
and never shares a stretch with a summer route: moving a summer path must not
move it. Places and their arrival points are shared: a hut is where both
arrive. The database holds to that (domain migration d0019): a route, a
stretch and a leg each have a season, and a leg of one season on a stretch of
the other is refused.

What is asked for is of one season: an area's routes, the map's lines, the
checks and reports are those of the season asked for (the summer unless the
winter is). What is only of the summer stays so whatever is asked: what is
handed to Kartverket as foot routes, the walk network the planner and the
trips go by, and (for now) the signs.

This module is where a season is named and known; it reads nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SUMMER = "summer"
WINTER = "winter"
SEASONS = (SUMMER, WINTER)
Season = Literal["summer", "winter"]

# A winter route's code: an area, a number, and a v (jot11v; jot77av where Turrutebasen has letters too).
WINTER_CODE = r"^[a-z]{3}[0-9]+[a-z]*v$"
_WINTER = re.compile(WINTER_CODE)


class Unknown(ValueError):
    """Not a season."""


def known(season: str | None) -> str:
    """The season asked for: the summer when none is."""
    if season is None:
        return SUMMER
    if season not in SEASONS:
        raise Unknown(f"a season is {' or '.join(SEASONS)}")
    return season


def of_code(code: str) -> str:
    """The season a route's code is of: a winter route's ends in v."""
    return WINTER if _WINTER.match((code or "").strip().lower()) else SUMMER


def twin(code: str) -> str:
    """The code of the route of the other season that goes with this one: jot11 for jot11v, and jot11v for jot11."""
    c = (code or "").strip()
    return c[:-1] if of_code(c) == WINTER else f"{c}v"


def code_fits(code: str, season: str) -> str | None:
    """None where a code fits its season; else why not, in a sentence."""
    if of_code(code) == season:
        return None
    if season == WINTER:
        return f"a winter route's code is the summer route's with a v after it ({twin(code)}), not {code}"
    return f"{code} is a winter route's code (a v after the number): a summer route's has none"


@dataclass(frozen=True)
class Candidates:
    """Where Turrutebasen's routes of a season are indexed (source_index), and how they say what they say."""
    route: str                  # the candidates: a code and a group of segments lying together
    route_segment: str          # which segments each candidate is made of
    segment: str                # the segments
    layer: str                  # the layer of Turrutebasen they come from, as kept with what is made of them
    graded: str                 # a candidate's grading and special type, over alias r (none in the winter)
    marked: str                 # whether a segment is marked, over alias t (in the winter: staked for it, SM)


CANDIDATES = {
    SUMMER: Candidates("route", "route_segment", "trail_segment", "fotrute",
                       "r.grading, r.special_type", "t.marked"),
    WINTER: Candidates("winter_route", "winter_route_segment", "winter_segment", "skiloype",
                       "NULL::text AS grading, NULL::text AS special_type", "t.marking IN ('JA', 'SM')"),
}
