# An MCP server for Stiflyt v2: tools, limits and feedback

Status: proposed 2026-11-16, and built the same day except `picture`: the
collecting of feedback, the look-ups that were missing, a person per author
with undo per person, and the MCP server itself (`POST /mcp`,
`src/stiflyt2/api/mcp.py`; see the README, "An assistant: the MCP server",
and "Connecting a client" below). What follows is the reasoning it was built
from. Written from one long
evening (2026-11-15) of mending Jotunheimen and Breheimen with the route responsible, in which
an assistant did part of the work through the viewer's own API. The list of
tools is what that work actually used; the limits are what it could not do.

## Connecting a client

The server is at `https://stiflyt2.example.org/mcp` (sign-in was switched on
2026-11-16, `deployment.md`), and needs a token. It answers every request with one JSON object and has no
stream, which the protocol allows. Checked on 2026-11-16 against the Model
Context Protocol's own Python client (version 2.3.0): initialise, list the 29
tools, call them, get a refusal back in words. And with Claude Code
(2.1.311, on a Mac, over the SSH tunnel to the local service, so without a
token): it connected, saw the 29 tools, and answered three questions in
Norwegian with `checks`, `route`, `find` and `areas`, in 35 seconds. It was
allowed the tools that look only. After sign-in was switched on the same day,
again over HTTPS at the public address with a token of the route responsible's own. **Not yet
tried: a change made through a Claude client, and Windows.**

Added since, and tried through the tests only: `on_utno` (what UT.no's page
says about a route; `describe_route` takes the description and the page's
number), `route` says what the route climbs, how long it takes each way and
what grading its numbers give, and `route_facts` gives a route as it is
walked, to write a description from (README, "A route in facts"). And signs:
`signs`, `sign_site`, `sign_post`, `sign`, `new_sign_site`,
`add_sign_destination`, `order_signs` (README, "Operations: signs"), for
which the operations role is enough; and `signs_adrift`, `settle_adrift` and
`undo_signs` (ADR-004). 41 tools.

For a second person, on Windows:

1. The route responsible lists the person and makes a token, on the server.
   The token is shown once; hand it over privately.

   ```sh
   .venv/bin/stiflyt2 access add kari@… "Kari Nordmann" --role editor
   .venv/bin/stiflyt2 access token kari@… "Claude Code, Kari's PC" --through mcp
   ```

2. **Claude Code** runs on Windows itself (PowerShell or cmd; no WSL needed).
   The person adds the server once, for themselves and not in any shared file:

   ```
   claude mcp add --transport http stiflyt https://stiflyt2.example.org/mcp --header "Authorization: Bearer stf2_…" --scope user
   ```

   `claude mcp list` shows whether it is connected, and `/mcp` inside a
   session. (Command and scopes as in Claude Code's documentation,
   https://code.claude.com/docs/en/mcp, read 2026-11-16.)
3. They then ask in plain words: "what is odd in Jotunheimen?", "where does
   jot74 go?", "carry jot64 on to Fannaråkhytta, show me first".

**Claude Desktop and claude.ai**, from Anthropic's documentation (read
2026-11-16) and not tried here: a remote server is added as a *custom
connector*, by its address, and the dialog has a "Request headers" section
for a server that takes a fixed credential instead of OAuth. So: add a custom
connector with the address `https://stiflyt2.example.org/mcp` and a request
header `Authorization` with the value `Bearer stf2_…`. A connector belongs to
the account and shows in Claude Desktop, on Windows too; the documentation
says every plan can add one (one only on the free plan). Two things follow
from how it works: the calls come from Anthropic's servers and not from the
person's machine (the service is public over HTTPS, so that is fine), and the
token is kept by Anthropic. No OAuth is needed on the server for this, and
nothing is installed. Sources:
https://claude.com/docs/connectors/custom/add-unlisted and
https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp

The Claude Code desktop app is said to keep its connectors apart from the
command line's (`claude mcp add`), so a server added in the terminal does not
show there.

Whoever a token acts as, it is their role that counts, what it changes is
recorded under their name as through an assistant, and
`stiflyt2 access revoke N` withdraws it.

## What it is for

A person talks to an assistant, and the assistant looks things up, proposes,
and changes routes through the same domain functions as the viewer. The map
stays the place for what has to be seen and drawn. Roughly:

- **MCP**: look-ups across sources, finding what is odd, proposals, changes
  that are described in words ("carry jot61 on to Fannaråkhytta", "jot1 is
  discontinued"), and the same change for many routes.
- **The map**: where a line runs when nothing is mapped, whether something
  looks right, and saying that a route is done.

## What the assistant could not do, with or without MCP

1. **Lines where nothing is mapped.** jot38 over the glaciers was laid as a
   sketch through place-name points and ran over rock by Storebjørn. The
   assistant does not see terrain.
2. **Facts that are not in the data.** That jot3 goes over Hestlægerhøe and
   jot4 by Tjønnholet, that jot61 ends at Fannaråkhytta, that jot1, jot4 and
   jot38 are discontinued. The data showed that something was odd (two routes
   on one line, a route of 6 m), not what was right.
3. **Seeing that something looks wrong.** The fan of links at Gjendesheim,
   the spike at Keisarpasset and the line jumping back after a drag all came
   from the route responsible's screenshots. In numbers, none of them was an error.

Some of 3 can be made checks, so that it does not take eyes: a route that
goes out and back, two routes that are the same line, a route of a few
metres, a maintainer called "Test turlag", an unmarked segment by a glacier's
name.

## Rules for every tool

- **A change can be tried first.** Every changing tool takes `dry_run` and
  then returns what it would do: lengths before and after, what is added and
  left out, the line. This was used before every change to the real data.
- **A refusal is a sentence to pass on.** The domain already refuses in words
  ("that is the only stretch of jot03: withdraw the route instead"). The
  assistant relays it and does not look for a way round.
- **Every change says how to take it back**, and `undo` exists.
- **Ids go through `identity.current`**, codes and names through `lookup`, so
  that an old code still finds its route.
- **A person says done.** `review` takes a flag that the person has said so
  in this conversation. The same for discontinuing, withdrawing and approving
  proposals in bulk.
- **The author is the person**, not the assistant and not one name in the
  server's settings.

## Tools for looking

Most of the evening was looking. Eight of these existed as endpoints (under
`/api/v1`); the rest was done with queries written on the spot, which an MCP
user cannot do, and all of those but `picture` have since been built
(`domain/lookup.py`).

| Tool | What it answers | Today |
|---|---|---|
| `find` | A route or place by code or name, also an earlier one | `GET /lookup`, `GET /routes?q=`, `GET /placenames?q=` |
| `route` | One route: stretches, ends, places, lengths, state, history | `GET /owned/routes/{id}` |
| `place` | One place: arrival points, links, history | `GET /places/{id}` |
| `area` | Where an area stands: the list, the to-do items, the network | `GET /areas/{a}/report`, `/worklist` |
| `proposals` | Repairs simple enough to approve | `GET /areas/{a}/proposals` |
| `against_mapped` | A route or an area against the mapped paths | `GET /owned/routes/{id}/mapped`, `/areas/{a}/mapped` |
| `corrections` | What Kartverket is asked, per segment | `GET /corrections` |
| `plan` | The way between points, without making it | `POST /owned/routes/plan` |
| `turrutebasen` | Everything carrying a route number: its candidates, and each segment with its id, length, the numbers on it, rutefølger, merking, maintainers, where it lies, and which routes of yours run over it | built 2026-11-16: `GET /turrutebasen/{code}` |
| `what_is_here` | Around a point: names with kind and distance, mapped paths and roads, Turrutebasen segments, stretches and routes of yours, places and arrival points | built 2026-11-16: `GET /here` |
| `mapped_way` | The way over mapped paths and roads between points: length, sources, how much of it lies on a given route | built 2026-11-16: `POST /mapped-way` |
| `same_line` | Routes that share (nearly) all their stretches; what two given routes share | built 2026-11-16: `GET /same-line`, and in `checks` |
| `checks` | The odd things listed above, per area | built 2026-11-16: `GET /areas/{a}/checks` |
| `picture` | **To build.** An image of a route or a place on the map, to look at before and after | screenshots by headless browser |

Where the hand queries were needed: jot3 and jot4 (does a path run by
Tjønnholet), jot48, jot61 and jot49 (which segments carry which number), jot50
and jot03 (segments of a few metres), bre8 (in terrain, unmarked, by a
glacier's name), Gjendesheim (which arrival points routes end at), jot1 and
bre28 (the mapped way where Turrutebasen has nothing).

`picture` can be modest: Kartverket's topographic WMS for the background and
the lines drawn on top on the server. No browser is needed for that.

## Tools for changing

All of these exist in the domain and, except where noted, as endpoints.

- **Routes**: make a candidate yours; lay a route out between points, or
  again; move an end; send it another way; add a part (taking a route of
  yours in); join pieces; take a stretch out; carry on along a stretch;
  change code, name, maintainer, grading, description; who maintains one
  stretch; done and not done; discontinued and in use again; withdraw; lay
  it on the mapped paths beside it.
- **Stretches**: what it is, marked, signed; divide; lay on the mapped way
  between its ends; lay beside; pull (a hand-drawn line by its corners);
  draw through points; give back an earlier line.
- **Junctions**: move, and join to another.
- **Places**: make from a proposal or at a junction, with a name; rename,
  kind, primary arrival point; add and take out arrival points; reviewed;
  withdraw.
- **In bulk**: approve proposals; make an area's clean candidates yours; lay
  an area's routes on the mapped paths; accept a finding as right.
- **Undo**: what the latest change was, and take it back.

To add when this is built: `dry_run` where it is missing (today it is on
snap, draw, move a junction, move an end, send another way, lay beside and
lay on the mapped paths; not on pull, take a stretch out, join, discontinue
or the changes to what is said about a route); taking a hand-laid route into
another (`absorb` has no endpoint of its own); making an existing junction an
arrival point (today only through a proposal); `places make` for an area
(today only the command line).

Not for MCP: imports, the index, and the overlay as a whole (`overlay
reset`, `restore`). Those stay with the operator, on the command line.

## Collecting what is missing and what fails

Yes: the server can collect it itself, and should, for the viewer as well as
for MCP. The viewer's log on 2026-11-16 holds 22 refused plans between
points, 8 refused "send it another way", 5 refused moved ends, one refused
snap and two timeouts of the proposals for Jotunheimen. Nobody read it. Those
that the route responsible happened to mention became changes to the tool the same evening
(the mapped-line fallbacks, the straight hop, the rubber band). The log keeps
the status code only: not the sentence the person was told, not who, not
which route.

One table, outside `core` and `history` (it is about the tool, not about the
routes): when, who, through what (viewer, MCP, API), what kind, the tool or
endpoint, its arguments, the message, the route or place it was about, and
whether it has been looked at.

Four kinds come in:

1. **Refused.** Every refusal by the domain, with the tool and its
   arguments. "This cannot be done" in the tool's own words. Counted by
   message, it is a ranked list of where people get stuck.
2. **Failed.** Every error and timeout, with the trace. The proposals for
   Jotunheimen timing out was found by chance.
3. **Reported.** A tool `report_gap(wanted, tried, why_not, about)` that the
   assistant is told to call whenever it cannot do what was asked, has to go
   round, or the person says something is wrong or missing. This catches
   what never reaches the backend as a call: "there is no way to say a route
   is closed". The person's own words go in as they were said.
4. **Taken back.** A change undone within a minute, the same call repeated,
   a dry run never followed by the real thing. Nobody reports these; they
   are read out of the log. "Lay it on them" pressed twice in a row was the
   sign that it did not settle.

The viewer sends the same: refused changes ("That change was not saved: …"),
errors in the page, and a short "something is wrong here" that a person can
write with the route open.

Read with `stiflyt2 feedback` (grouped by message, a person's words first,
then by how often, with how many people), and as a section of the handoff
note. An entry is marked seen or fixed, with the commit that fixed it.

Built 2026-11-16: the table (`feedback.entry`), refusals and failures from
every request under `/api`, `POST /api/v1/feedback` for what is reported
(the future `report_gap` posts there), the note and the page's own faults
from the viewer, changes undone within two minutes, and the command. Not
built of the above: a call repeated and a dry run never followed up as
signals of their own (a repeated refusal shows as its count per route), and
the section in the handoff note.

What it cannot collect: what a person wanted and never tried, and what
looks wrong but is accepted. For that the person has to say it; `report_gap`
and the note in the viewer make that one sentence.

## Before a second person uses it

Built 2026-11-16, at the route responsible's word: sign-in with Google
behind the proxy, people with a role, tokens that act as a person and mark
what they do as come through an assistant, undo per person and per way in,
the page noticing others' changes, a nightly copy, and a guide. See the
README, "More than one person". Switching sign-in on waits for the route responsible (a Google
client and the Caddy block; `deployment.md`). What stood here before:

- **Each person is their own author.** Today the editor is one name in the
  server's settings. Tokens per person, and roles: route responsible (may
  change `core`), reader.
- **Undo is one stack for everybody.** It takes back the latest change,
  whoever made it. With two people it must take back one's own latest, or
  refuse when someone else has changed the same things since.
  This happened on 2026-11-16 with one person and an assistant: the route responsible pressed
  Undo four times for their own changes to jot88, and it took back four
  changes the assistant had just made to jot53 for them through the API. An
  author per person would not have told the two apart: the assistant acts
  as the person. So undo has to know how a change came in (the map, the
  assistant) as well as from whom. Built the same day as a way round:
  a route can be put back as it was, whatever lies on top
  (`POST /owned/routes/{id}/back`, ADR-003 amendment 29).
- **The viewer is reached over an SSH tunnel.** MCP needs an address and a
  token, and the same for the viewer if the second person is to see the map.

## Order

1. The feedback table, fed by refusals and errors from the API and the
   viewer. Small, and useful before any MCP exists.
2. The look tools that are missing, as endpoints: `turrutebasen`,
   `what_is_here`, `same_line`, `checks`. The viewer gains from them too.
3. A person per author, and undo per person.
4. The MCP server over the endpoints, with `report_gap` and the rules above
   as its instructions.
5. `picture`.
