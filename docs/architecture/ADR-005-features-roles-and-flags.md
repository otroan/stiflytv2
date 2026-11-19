# ADR-005: Features, roles as bundles of them, and flags

Status: built 2026-11-19 (`src/stiflyt2/features.py`, `access.py`,
`api/auth.py`; import migration `0016`). The two features there were, changing
the routes and deciding about signs, are the first two. Every later part is
built as one.

## The problem

The tool grows by parts: signs first, then what happens on a route, photos,
tracks, who owns the ground, a planner. Who may use what was a ladder of three
roles (reader, operations, editor), each allowed what the one before it was.
That does not stretch: a work party leader, somebody who only orders signs,
and whoever may look up landowners are not steps on one ladder. And a part
that is half built, or meant for a few people first, had nowhere to be but in
front of everybody.

## Decision

1. **A feature is a part that stands by itself, and has a name.** Its own
   module (`src/stiflyt2/ops/<feature>…`), its own endpoints file with a
   `register(app, read, write)`, its own assistant tools, its own script and
   part of a page, its own tables in `ops` with their own migration, its own
   tests. It names itself once (`features.feature("signs", …)`), and
   everything that belongs to it asks for it by that name
   (`Depends(auth.may("signs"))`). Nothing else reaches into it; what it
   needs of the routes it reads through what the routes offer (ADR-004).

2. **A role is a bundle of features, kept in the database** (`access.role`).
   A new role needs no new code: `stiflyt2 access role sign-orderer
   --features signs`. The three first roles are bundles of what they could
   do, so nobody's access changed.

3. **A person may be given a feature their role lacks, or be denied one it
   has** (`access.person_feature`). That is the flag per person.

4. **A feature as a whole is off, given, or for all** (`access.feature`):
   for nobody; for those whose role or own list has it; for everybody who is
   let in. A feature is born `given`, in no role: nobody has it until it is
   put in a role or given by name. That is the flag per feature.

5. **The server decides, and the page follows.** An endpoint or tool of a
   feature refuses whoever lacks it; `/auth/me` says which features one has,
   and a page shows only those. Hidden is also refused.

6. **Three things decide, and no more**: how the feature stands, the role,
   and what the person is given or denied by name. `stiflyt2 access why
   <e-mail>` says for each feature which of the three it was. No
   percentages, no dates, no flags in the browser.

7. **Looking is not a feature.** Whoever is let in may look at the routes
   and the signs, as before. A part that not everybody may even look at (who
   owns the ground) is a feature for the looking too.

8. **"Show the pages as"** offers every role whose features are some of
   one's own, also roles of one's own making.

## What a flag does not do

- It hides and refuses; it does not take anything away. A feature's tables
  are made when its migration runs, whoever may use it, and what is kept in
  them stays when it is switched off.
- It suits what is added, not what is changed. How the route editor itself
  behaves cannot sit behind a flag without two versions of the code.
- It is not at once: the service remembers who somebody is for 20 seconds.

## What a new feature does

- Names itself in its module: `features.feature(key, what it lets one do,
  what one who lacks it is told to ask for)`.
- Puts `auth.may(key)` on every endpoint that belongs to it (on those that
  change, or on all of them where looking is restricted too), and the key on
  its assistant tools.
- Shows its part of a page only where `me.features` has the key.
- Keeps its tables in `ops`, hung on the routes as ADR-004 says, with undo
  and an item of work as the signs have.
- Has a test that it is refused without the feature.
- Is put in the roles that are to have it by hand, when it is ready for them.

## Considered and not chosen

- **More steps on the ladder.** Every new part would need a place on it, and
  most do not have one.
- **Flags in settings or in the browser.** Not per person, and not something
  the server can refuse by.
- **Features as separate services.** One database, one sign-in and one undo
  are worth more here than separate deployment.
