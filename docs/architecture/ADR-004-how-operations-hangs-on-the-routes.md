# ADR-004: How operations hangs on the routes

Status: built 2026-11-17 for signs, as the pattern for the rest of operations
(work to do, reports, closures, issues, photos). `src/stiflyt2/ops/anchor.py`,
`ops/undo.py`, `ops/signs.py`; import migration `0014`.

## The problem

Operations is what happens to the routes: which sign is worn, what was done
on a stretch last summer, which bridge is out. All of it is about something
in the routes: a junction, a stretch, a place, a route. And the routes are
not finished. Junctions are joined, a stretch is divided where a new junction
is found, a route end is given a place, a code is changed, a route is taken
into another. Each of those changes which id stands for a thing, or what
there is to hang something on at all.

The first version of signs kept each decision under the ids it was made
about and looked it up by those ids. A sign whose stretch had been divided
since then silently lost what was decided about it: the arrow by hand, that
it was on the order, that it had been ordered and was not up yet. Nothing
said so. And nothing decided about a sign could be taken back.

Every further part of operations would have had the same two faults.

## Decision

1. **Operations never changes the routes.** It lives in its own schema
   (`ops`), outside the overlay, and reads `core`. A sign that is wrong
   because a route is wrong is put right in the route.

2. **What operations keeps says what it is about by id, as the id was when
   it was said.** A junction, a stretch, a place or a route of `core`; or a
   spot of its own on a stretch (a stretch and a position). Changing a
   route never touches a row of operations.

3. **It is never read by its ids as they stand. It is asked what stands for
   them now** (`anchor.stands_for`, which goes by `core.succession`):

   | The thing | What stands for it |
   |---|---|
   | is still itself | itself |
   | was joined to another, or taken into another | that one |
   | was divided | the part the kind of thing says it means: for a sign, the part of the stretch that leaves the sign's junction; for a spot, the part it lies on |
   | has become part of something | that: a junction that is now an arrival point of a place is the place, for a sign site and for where a sign points |
   | has another code | the route that had the code |
   | is gone, and nothing came instead | nothing |

   Reading costs one question per kind of thing for all that are still
   themselves, which is nearly always all of them.

4. **Before something new is said, what is kept is brought to where it
   belongs now** (`_bring_along`): the row is put under the ids that stand
   for its old ones, so that the new is said about the same row and not
   beside it. That is recorded as an event (`sign.followed`).

5. **What cannot be followed is adrift, and a person settles it.** It is
   never dropped and never guessed at. Adrift is also what is followed but
   finds nothing there: the routes no longer give a sign from that junction
   to that place, because a hut was made on the way or the route no longer
   passes. It is listed with what it said, where it was, why it is adrift
   and who said it last. A person says which sign it is about now, or that
   it can be forgotten. If the routes change back, it finds its sign again
   by itself.

6. **Every decision is an event, and can be taken back by the one who made
   it** (`ops.event`, `ops.undo_log`, `ops/undo.py`). One request is one
   action. Undo takes back your own latest, made the way you ask (in the
   page, or through an assistant); what others have done since stays, and
   where that is about the same thing, nothing is done. This is the routes'
   own undo (ADR-003, amendment 16) over again, for `ops`. And what a person
   has decided since they last saved is an item of work, saved or discarded
   as a whole and asked about when they leave what they were at (a sign
   site), as with the routes (ADR-003, amendment 38). Operations has no
   changesets: where each person last saved is a mark of its own
   (`ops.saved`, import migration `0015`).

7. **What has left the house stands.** An order that has been taken out has
   gone to whoever makes the signs: it cannot be undone, and it keeps what
   each sign said then, whatever happens to the routes afterwards. The same
   will go for a report that has been sent.

## What a new part of operations does

- Its tables are in `ops`, each with an `id uuid` and the trigger
  `ops.log_for_undo`, and say what a row is about by kind and id.
- It reads through `anchor`, and says for a divided stretch which part it
  means. Work done on a stretch that is later divided is probably about
  both parts; a sign is about one.
- It records what is decided with `ops.event`, in the transaction that
  decides it, so that undo knows whose it is.
- It lists what is adrift, and gives a person the two ways out: move it,
  or forget it.
- Its changes need the operations role; an assistant gets the same tools,
  dry unless told otherwise.

## Considered and not chosen

- **Rewriting what operations keeps when a route is changed.** The route
  editor would have to know every table of operations, and a mistake in
  the routes, undone a minute later, would already have rewritten them.
  Reading through what stands for an id now leaves operations untouched by
  an undo in the routes: the old id is itself again. (A row is put under
  the new ids only when a person decides something more about it, point
  4.)
- **Foreign keys from `ops` into `core`.** A retired junction is still a
  row, so a key holds and says nothing; and the overlay can be reset and
  put back as a whole, which a key from outside it would stand in the way
  of.
- **Following by position**, as v1 does: the nearest sign site within some
  metres. It follows a junction that is moved, and it follows the wrong
  one where two lie close, which is at the huts, where the signs are.

## Left

- A stretch is divided, something more is decided about a sign along it
  (which puts the decision under the part), and the division is then
  undone in the routes: the part is gone and nothing came instead, so the
  decision is adrift, with its sign among those it can be moved to. One
  click, but it does not find its way back by itself.
- Two decisions that end up about the same sign: the latest stands, the
  earlier is adrift. Nothing merges them.
- A post is kept under one junction of its place. Where the junctions of a
  place lie close and which of them the post stands at changes, what is
  kept follows; where they are further apart than 30 m it does not.
- A sign site that has its code (`BRE-SS-0001`) and goes adrift can be
  forgotten, code and all. The orders that used the code keep it.
