# ADR-006: Seasons: winter routes are routes of their own

Status: the foundation built 2026-11-26 (`src/stiflyt2/seasons.py`; domain
migration `d0019`; Turrutebasen's ski trails taken in the same day, import
transform `turrutebasen-2`). What is left is listed at the end.

## The problem

DNT's winter routes are staked anew each winter with branches over frozen lakes
and bogs. Turrutebasen keeps them among its ski trails, with the code of the
summer route they go with and a v (jot11v goes with jot11), and a line drawn
once (mostly in 2003) that stands for where they usually go. Each winter the
people who stake them hand in a GPX track of where they went that year; and a
route is open from a day to a day, which the area contact keeps.

Everything the tool had was made for the summer: a route was a list of
stretches, and a stretch could be shared by any routes. A winter line laid on
the same stretches as a summer path would move when the path is mended; and a
winter stretch over a lake, mixed into the summer's network, would send the
planner and the trips over the ice, and Kartverket would be told of it as a foot
route.

## Decision

1. **A route has a season, summer or winter, and so has a stretch.** A winter
   route never runs over a summer stretch, nor a summer route over a winter one.
   Junctions and places have no season: a hut is where both arrive, and its
   arrival points are the same.

2. **The database holds to it, whatever the code that asks.** A leg (a stretch
   of a route) carries its route's season, set by a trigger, and two foreign
   keys refuse a leg whose stretch is of the other season (`d0019`). The domain
   says why before the database has to (`routes._same_season`), and every
   stretch made is made with the season of the route it is made for
   (`routes._new_edge(..., season=)`, required: nothing is made of a season by
   default).

3. **One module names a season and knows it** (`stiflyt2.seasons`): the two
   seasons, a code's season (`of_code`), its twin (`twin`), whether a code fits
   a season (`code_fits`), and where Turrutebasen's candidates of each season
   are indexed and how they say what they say (`CANDIDATES`). It reads nothing.

4. **What is asked for is of one season.** An area's routes, the map's lines,
   the checks, the reports, the issues, photos, reports and tracks of an area:
   each takes `season` (the summer unless the winter is asked for), over HTTP
   (`?season=winter`), for an assistant (`season`), and on the command line
   (`--season`). One that acts on a route or a stretch by its id keeps to that
   route's season where it walks on to other routes (what meets it, what it
   could be laid over).

5. **What is only of the summer stays so, whatever is asked**: what is handed
   to Kartverket as foot routes (`corrections`), the UT.no export, the walk
   network the planner and the work trips go by, and (until winter signs are
   built) the signs.

6. **Turrutebasen's winter routes are candidates of their own**, indexed apart
   from the summer's (`winter_route`, `winter_segment`, `winter_route_segment`)
   so that nothing made for the summer meets them; made ours, they become
   winter routes of winter stretches, and say which layer they came from
   (`skiloype`).

7. **A code says its season.** A winter route's code is the summer code with
   a v; a summer route's has none. Making, laying out and renaming a route
   refuse a code of the other season.

## What is left

- The two seasons side by side in the pages: a summer/winter switch on the
  front page and in the route editor.
- A winter route's twin: shown with it, its code following when the summer
  code changes, a check where the two do not run between the same places.
- Each winter's track from the people who stake the routes, kept with the
  route and the season, and the normal line laid on it where it has moved.
- When a winter route is open, by the area contact.
- Signs for the winter routes.
