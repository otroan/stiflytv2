"""Command line entry point: `stiflyt2 import ...`, `stiflyt2 sources ...`."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from stiflyt2.config import settings
from stiflyt2.importer import core

app = typer.Typer(no_args_is_help=True, help="Stiflyt v2")
imp = typer.Typer(no_args_is_help=True, help="Import source datasets as immutable snapshots")
src = typer.Typer(no_args_is_help=True, help="Inspect imported sources")
idx = typer.Typer(no_args_is_help=True, help="The rebuildable search and map index")
placenames = typer.Typer(no_args_is_help=True, help="Place names (Stedsnavn)")
places = typer.Typer(no_args_is_help=True, help="Owned places: where routes start and end")
routes = typer.Typer(no_args_is_help=True, help="Route candidates from Turrutebasen")
overlay_app = typer.Typer(no_args_is_help=True, help="Everything that is yours, as a whole: copy, remove, put back")
app.add_typer(imp, name="import")
app.add_typer(src, name="sources")
app.add_typer(idx, name="index")
app.add_typer(placenames, name="placenames")
app.add_typer(places, name="places")
app.add_typer(routes, name="routes")
feedback_app = typer.Typer(help="What the tool refused, what failed, and what people say is missing")
app.add_typer(overlay_app, name="overlay")
access_app = typer.Typer(no_args_is_help=True, help="Who may use the tool, as what, and the tokens programs act with")
app.add_typer(feedback_app, name="feedback")
app.add_typer(access_app, name="access")
height_app = typer.Typer(help="How high the ground is along the routes, from Kartverket's height service")
app.add_typer(height_app, name="height")

File = Annotated[Path | None, typer.Option(help="Use a local copy of the delivery")]


def _report(r: core.Result) -> None:
    if r.reused:
        typer.echo(f"unchanged: delivery already imported as snapshot {r.snapshot_id}")
    else:
        typer.echo(f"snapshot {r.snapshot_id}")
        typer.echo(f"  rows: {json.dumps(r.counts)}")
        typer.echo(f"  diff: {json.dumps(r.diff)}")


def _run(fn, *args) -> None:
    try:
        _report(fn(*args))
    except core.ImportRejected as e:
        typer.echo(f"rejected, nothing activated: {e}", err=True)
        raise typer.Exit(1) from e


@imp.command()
def turrutebasen(file: File = None) -> None:
    """Turrutebasen, nationwide."""
    from stiflyt2.importer.adapters import turrutebasen as a
    _run(a.run, file)


@imp.command()
def stedsnavn(file: File = None) -> None:
    """Stedsnavn (SSR), nationwide."""
    from stiflyt2.importer.adapters import stedsnavn as a
    _run(a.run, file)


@imp.command()
def kulturminner(file: File = None) -> None:
    """Kulturminner (Riksantikvaren), nationwide."""
    from stiflyt2.importer.adapters import kulturminner as a
    _run(a.run, file)


@imp.command()
def teig(
    kommune: Annotated[str, typer.Argument(help="Municipality number, e.g. 4644")],
    file: File = None,
) -> None:
    """Property parcels (Matrikkelen Eiendomskart Teig) for one municipality."""
    from stiflyt2.importer.adapters import teig as a
    _run(a.run, kommune, file)


@imp.command()
def vegnett(
    kommune: Annotated[str, typer.Argument(help="Municipality number, e.g. 3434")],
    file: File = None,
) -> None:
    """The road network (NVDB Vegnett Pluss) for one municipality."""
    from stiflyt2.importer.adapters import vegnett as a
    _run(a.run, kommune, file)


@imp.command("fkb-wfs")
def fkb_wfs(
    name: Annotated[str, typer.Argument(help="Scope name, e.g. bre")],
    bbox: Annotated[str, typer.Argument(help="minx,miny,maxx,maxy in EPSG:25833")],
) -> None:
    """Paths and tractor roads from the open WFS, for a bounding box."""
    from stiflyt2.importer.adapters import fkb_wfs as a
    parts = tuple(int(float(v)) for v in bbox.split(","))
    if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise typer.BadParameter("bbox must be minx,miny,maxx,maxy")
    _run(a.run, name, parts)


@imp.command()
def osm(
    name: Annotated[str, typer.Argument(help="Scope name, e.g. jot")],
    bbox: Annotated[str, typer.Argument(help="minx,miny,maxx,maxy in EPSG:25833")],
) -> None:
    """OpenStreetMap paths, tracks and roads for a bounding box (ODbL: to look at, not to copy from)."""
    from stiflyt2.importer.adapters import osm as a
    parts = tuple(int(float(v)) for v in bbox.split(","))
    if len(parts) != 4 or parts[0] >= parts[2] or parts[1] >= parts[3]:
        raise typer.BadParameter("bbox must be minx,miny,maxx,maxy")
    _run(a.run, name, parts)


@imp.command()
def area(
    prefix: Annotated[str, typer.Argument(help="Area prefix, e.g. ron")],
    everything: Annotated[bool, typer.Option("--all", help="Fetch again what is already there too")] = False,
) -> None:
    """The map data an area needs: mapped paths, roads and properties for its municipalities, and OpenStreetMap.

    The area is where Turrutebasen's routes with that prefix lie, with 2 km to
    spare. What is already fetched for it is left alone unless --all is given.
    """
    from stiflyt2.domain import mapdata
    from stiflyt2.importer.adapters import fkb_wfs, teig, vegnett
    from stiflyt2.importer.adapters import osm as osm_adapter
    from stiflyt2.index import build as index

    with psycopg.connect(settings.import_dsn) as conn:
        bbox, have = mapdata.box(conn, prefix), mapdata.status(conn, prefix)
    if bbox is None:
        raise typer.BadParameter(f"Turrutebasen has no routes with the prefix {prefix}")
    typer.echo(f"{prefix}: box {','.join(map(str, bbox))}, "
               f"{len(have['roads']['municipalities'])} municipalities")
    refused = 0

    def fetch(what: str, fn, *args) -> None:
        nonlocal refused
        typer.echo(what)
        try:
            _run(fn, *args)
        except typer.Exit:      # one source refused is no reason to leave the others unfetched
            refused += 1

    if everything or have["paths"]["share"] < 0.999:
        fetch("mapped paths:", fkb_wfs.run, prefix.lower(), bbox)
    for m in have["roads"]["municipalities"]:
        if everything or not m["loaded"]:
            fetch(f"roads for {m['name']} ({m['no']}):", vegnett.run, m["no"])
    for m in have["properties"]["municipalities"]:
        if everything or not m["loaded"]:
            fetch(f"property map for {m['name']} ({m['no']}):", teig.run, m["no"])
    if everything or have["osm"]["share"] < 0.999:
        fetch("OpenStreetMap:", osm_adapter.run, prefix.lower(), bbox)
    for b in index.build():
        typer.echo(f"index: rebuilt {b.source} in {b.seconds:.0f} s")
    if refused:
        raise typer.Exit(1)


@app.command()
def refresh(
    source: Annotated[str | None, typer.Option(help="Only this source")] = None,
) -> None:
    """Check every imported source and scope for a new delivery, and import what changed.

    Afterwards the original files of all but the newest snapshots are removed
    (STIFLYT2_KEEP_ARTIFACTS, default 14 per source and scope; 0 keeps all).
    """
    from stiflyt2.importer import refresh as r

    checks = r.run(source)
    for c in checks:
        snap = c.snapshot_id[:8] if c.snapshot_id else ""
        typer.echo(f"{c.source_key:<14} {c.scope:<14} {c.outcome:<10} {snap}  {c.detail}")
    pruned = r.prune(settings.keep_artifacts, source)
    if pruned:
        typer.echo(f"removed the original files of {pruned} older snapshots")
    _build_index()
    if any(c.outcome in ("rejected", "unreached", "error") for c in checks):
        raise typer.Exit(1)


@app.command("health")
def health_now(
    alert: Annotated[bool, typer.Option(help="Also send it, where a way to send is set up (STIFLYT2_ALERT_*); "
                                              "the same at most once in 20 hours")] = False,
) -> None:
    """What the page warns of now: a nightly check or copy that did not run or went wrong.

    Run after the nightly jobs with --alert, so that it is known without opening the page.
    """
    from stiflyt2 import feedback, health

    with psycopg.connect(settings.api_dsn) as conn:
        warnings = health.status(conn, Path("var/backup"))["warnings"]
    for w in warnings:
        typer.echo(w)
    if not warnings:
        typer.echo("nothing to warn of")
    if alert:
        try:
            typer.echo(health.alert(warnings, Path("var/run/alerted.json")))
        except Exception as e:  # said in the page instead
            typer.echo(f"could not send: {type(e).__name__}: {e}", err=True)
            with psycopg.connect(settings.api_dsn, autocommit=True) as conn:
                feedback.record(conn, "failed", "job", "nightly alert", f"could not send: {type(e).__name__}: {e}")
            raise typer.Exit(1) from e


def _build_index(*, force: bool = False, source: str | None = None) -> None:
    from stiflyt2.index import build

    for b in build.build(force=force, only=source):
        typer.echo(f"index: rebuilt {b.source} in {b.seconds:.0f} s  {json.dumps(b.rows)}")


@idx.command("build")
def index_build(
    force: Annotated[bool, typer.Option(help="Rebuild even what is current")] = False,
    source: Annotated[str | None, typer.Option(help="Only this source")] = None,
) -> None:
    """Rebuild the index tables of every source whose active snapshot has changed."""
    _build_index(force=force, source=source)


@idx.command("status")
def index_status() -> None:
    """Per source: whether its index tables match the active snapshot."""
    from stiflyt2.index import build

    for source, state, built_at, seconds, rows in build.status():
        took = f"{seconds:.0f} s" if seconds is not None else ""
        typer.echo(f"{source:<14} {state:<13} {built_at or ''!s:<20} {took:<6} {json.dumps(rows)}")


@places.command("make")
def places_make(
    area: Annotated[str | None, typer.Option(help="Only where routes of this area end, e.g. jot")] = None,
) -> None:
    """Make a place of every proposed hut and parking place that is not one yet.

    The places are marked as not reviewed and follow their proposals until a
    person reviews or changes them. Runs as the API role, like every change to
    owned data.
    """
    from stiflyt2.domain import places as domain

    with psycopg.connect(settings.api_dsn) as conn:
        done = domain.make_default(conn, area)
    typer.echo(", ".join(f"{n} {what}" for what, n in done.items() if n) or "nothing to do")


@placenames.command("search")
def places_search(
    text: str,
    near: Annotated[str | None, typer.Option(help="east,north in EPSG:25833")] = None,
    limit: int = 15,
) -> None:
    """Find places by name. Misspellings and historical spellings are found too."""
    from stiflyt2.index import query

    point = tuple(float(v) for v in near.split(",")) if near else None
    with psycopg.connect(settings.import_dsn) as conn:
        found = query.search_places(conn, text, limit=limit, near=point)
    for r in found:
        where = ", ".join(v for v in (r["municipality"], r["county"]) if v)
        far = f"  {r['distance_m'] / 1000:.1f} km" if r["distance_m"] is not None else ""
        typer.echo(f"{r['name']:<32} {r['type'] or '':<18} {where:<34} "
                   f"{r['east']:.0f},{r['north']:.0f}{far}")


@routes.command("make")
def routes_make(area: Annotated[str, typer.Argument(help="Area prefix, e.g. jot")]) -> None:
    """Make every clean route candidate of an area yours: one connected line with two ends.

    They are marked as not reviewed. Candidates in pieces or with extra ends
    are left for a person.
    """
    from stiflyt2.domain import routes as domain

    with psycopg.connect(settings.api_dsn) as conn:
        done = domain.make_default(conn, area)
    typer.echo(", ".join(f"{n} {what}" for what, n in done.items() if n) or "nothing to do")


@app.command("report")
def area_report(
    area: Annotated[str | None, typer.Argument(help="Area prefix, e.g. jot. Left out: every area side by side")] = None,
    maintainer: Annotated[str | None, typer.Option(
        help="With no area: only the ground this maintainer looks after, e.g. 'DNT | DNT Oslo og omegn'")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="As JSON")] = False,
) -> None:
    """Where an area stands: routes, lengths, maintainers, the network, and what is left to do.

    Without an area: every area side by side, with the kilometres of ground
    its routes cover and how much of that is marked, shared stretches counted
    once. For Turrutebasen as it is, and for what is yours.
    """
    import json

    from stiflyt2.domain import report

    with psycopg.connect(settings.api_dsn) as conn:
        if area is None:
            found = report.areas(conn, maintainer)
            typer.echo(json.dumps(found, ensure_ascii=False, indent=1) if as_json else report.areas_as_text(found))
            return
        found = report.report(conn, area)
    typer.echo(json.dumps(found, ensure_ascii=False, indent=1, default=str) if as_json else report.as_text(found))


Backups = Annotated[Path, typer.Option("--to", help="Where copies of the overlay are kept")]
Named = Annotated[str, typer.Option("--database", help="The name of the database, to say that this one is meant")]


def _overlay(what: str, named: str, sure: bool, beside: bool = False) -> None:
    """Say what is about to be replaced, and go on only when the database is named and it is confirmed."""
    from stiflyt2 import overlay

    whole = "what is kept beside the overlay" if beside else "the overlay"
    if named != overlay.database():
        raise typer.BadParameter(f"this would {what} {whole} of the database {overlay.database()}: "
                                 f"name it with --database to go on")
    holds = overlay.holds(beside)
    typer.echo(f"{whole.capitalize()} of {named} holds " + ", ".join(f"{n} {name}" for name, n in holds.items()) + ".")
    if not sure:
        typer.confirm(f"A copy is written first. {what.capitalize()} it?", abort=True)


@overlay_app.command("backup")
def overlay_backup(
    to: Backups = Path("var/backup"),
    keep: Annotated[int, typer.Option(help="Keep this many of the newest copies and remove older ones. 0 keeps all")] = 0,
) -> None:
    """Write a copy of everything that is yours (places, routes, the history) to a file.

    And beside it, a copy of who may use the tool and of the feedback kept.
    """
    from stiflyt2 import overlay

    try:
        typer.echo(f"copy written to {overlay.backup(to)}")
        typer.echo(f"and of who uses the tool and the feedback, to {overlay.backup_beside(to)}")
        gone = overlay.prune(to, keep)
        if gone:
            typer.echo(f"{len(gone)} older copies removed")
    except overlay.Failed as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@overlay_app.command("reset")
def overlay_reset(database: Named, to: Backups = Path("var/backup"),
                  yes: Annotated[bool, typer.Option("--yes", help="Do not ask")] = False) -> None:
    """Remove everything that is yours and start from nothing. The imported sources are left as they are.

    A copy is written first; `overlay restore` puts it back. Afterwards the
    places and routes of an area are made again with `places make` and
    `routes make`.
    """
    from stiflyt2 import overlay

    _overlay("remove", database, yes)
    try:
        kept = overlay.reset(to)
    except overlay.Failed as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"the overlay is empty. What was there is kept in {kept}")
    typer.echo("restart the viewer: deploy/viewer.sh restart")


@overlay_app.command("restore")
def overlay_restore(file: Path, database: Named, to: Backups = Path("var/backup"),
                    yes: Annotated[bool, typer.Option("--yes", help="Do not ask")] = False) -> None:
    """Put a copy back, in place of what is there now: of the overlay, or of what is kept beside it.

    Which it is, the file says: an `overlay-…` copy (overlay.dump off the server) is places, routes and their
    history; a `tool-…` copy (tool.dump) is who may use the tool, the feedback, and operations (signs,
    issues, reports, photos as records). Put back both copies of the same night: operations hang on the
    routes by their ids.
    """
    from stiflyt2 import overlay

    try:
        beside = overlay.kind_of(file) == "tool"
    except overlay.Failed as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    _overlay("replace", database, yes, beside)
    try:
        if beside:
            kept, empty = overlay.restore_beside(file, to)
        else:
            kept, empty = overlay.restore(file, to), []
    except overlay.Failed as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    holds = overlay.holds(beside)
    typer.echo(f"put back from {file}: " + ", ".join(f"{n} {name}" for name, n in holds.items()))
    if empty:
        typer.echo(f"the copy has nothing of {', '.join(empty)}: left empty")
    typer.echo(f"what was there before is kept in {kept}")
    typer.echo("restart the viewer: deploy/viewer.sh restart")


def _when(at) -> str:
    return f"{at.astimezone():%Y-%m-%d %H:%M}"


@feedback_app.callback(invoke_without_command=True)
def feedback_list(
    ctx: typer.Context,
    everything: Annotated[bool, typer.Option("--all", help="Also what has been looked at")] = False,
) -> None:
    """What has come in and nobody has looked at yet: a person's words first, then by how often.

    Entries that say the same, numbers and ids aside, are one line, named by
    the number of the latest. `feedback show N` gives them in full.
    """
    if ctx.invoked_subcommand:
        return
    from stiflyt2 import feedback

    with psycopg.connect(settings.api_dsn) as conn:
        groups, undone = feedback.grouped(conn, everything), feedback.taken_back(conn)
    kind = None
    for g in groups:
        if g["kind"] != kind:
            kind = g["kind"]
            typer.echo(f"\n{kind}")
        about = ", ".join(f"{a['called'] or a['type']}" + (f" ×{a['times']}" if a["times"] > 1 else "")
                          for a in g["about"][:6])
        state = f"  fixed in {g['fixed_in']}" if g["fixed_in"] else "" if g["unseen"] else "  seen"
        people = f"{g['people']} people, " if g["people"] > 1 else ""
        typer.echo(f"  #{g['id']:<5} ×{g['times']:<3} {people}latest {_when(g['latest'])}  "
                   f"{'/'.join(g['through'])}  {g['what']}{state}")
        typer.echo(f"         {g['message']}" + (f"   [{about}]" if about else ""))
    if not groups:
        typer.echo("nothing has come in" if everything else "nothing new has come in")
    if undone:
        typer.echo(f"\ntaken back within {feedback.SOON_S // 60} minutes, the last 14 days")
        for u in undone:
            typer.echo(f"  {_when(u['at'])}  {u['who'] or '?'}: {u['what']}  (after {u['after_s']} s)")


@feedback_app.command("show")
def feedback_show(number: Annotated[int, typer.Argument(help="The number of an entry")]) -> None:
    """Every entry that says the same as this one, with what was asked."""
    from stiflyt2 import feedback

    with psycopg.connect(settings.api_dsn) as conn:
        try:
            found = feedback.entries(conn, number)
        except feedback.NotFound as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
    for e in found:
        about = f"  {e['about_type']} {e['about'] or ''} {e['about_id']}" if e["about_id"] else ""
        state = f"  fixed in {e['fixed_in']}" if e["fixed_in"] else "  seen" if e["seen_at"] else ""
        typer.echo(f"#{e['id']}  {_when(e['at'])}  {e['kind']}  {e['who'] or '?'} through {e['through']}  "
                   f"{e['what']}" + (f" -> {e['status']}" if e["status"] else "") + state)
        typer.echo(f"  {e['message']}{about}")
        if e["arguments"]:
            typer.echo(f"  asked with: {json.dumps(e['arguments'], ensure_ascii=False)}")
        if e["trace"]:
            typer.echo("  " + e["trace"].rstrip().replace("\n", "\n  "))


def _marked(number: int, commit: str | None) -> None:
    from stiflyt2 import feedback

    with psycopg.connect(settings.api_dsn) as conn:
        try:
            n = feedback.mark(conn, number, commit)
        except feedback.NotFound as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
    typer.echo(f"{n} marked" if n else "they were marked already")


@feedback_app.command("failed")
def feedback_failed(what: Annotated[str, typer.Argument(help="The job, e.g. 'nightly backup'")],
                    message: Annotated[str, typer.Argument(help="What went wrong")]) -> None:
    """Keep a note that a job failed, for whoever looks after the tool. Called by the nightly scripts."""
    from stiflyt2 import feedback

    with psycopg.connect(settings.api_dsn) as conn:
        number = feedback.record(conn, "failed", "job", what, message)
    typer.echo(f"kept as #{number}")


@feedback_app.command("seen")
def feedback_seen(number: Annotated[int, typer.Argument(help="The number of an entry")]) -> None:
    """Say that an entry, and every one that says the same, has been looked at."""
    _marked(number, None)


@feedback_app.command("fixed")
def feedback_fixed(number: Annotated[int, typer.Argument(help="The number of an entry")],
                   commit: Annotated[str, typer.Argument(help="The commit that put it right")]) -> None:
    """Say that what an entry tells of has been put right, and in what commit."""
    _marked(number, commit)


def _as_owner():
    """A connection as the database's owner: who is listed is not for the service itself to change."""
    import os

    from stiflyt2 import overlay

    conn = psycopg.connect(dbname=overlay.database())
    conn.execute(f"SET ROLE {overlay.OWNER}")
    conn.execute("SELECT set_config('application_name', %s, false)", (f"stiflyt2 access ({os.environ.get('USER', '?')})",))
    return conn


def _access(what) -> None:
    from stiflyt2 import access

    try:
        with _as_owner() as conn:
            what(conn)
    except access.Refused as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e


@access_app.command("list")
def access_list() -> None:
    """Everybody who may use the tool: their role, when they were last here, and their tokens."""
    from stiflyt2 import access

    def show(conn) -> None:
        found = access.people(conn)
        for p in found:
            last = f"last here {_when(p['last_seen_at'])}" if p["last_seen_at"] else "not been here yet"
            own = "".join(f" {'+' if f['given'] else '-'}{f['feature']}" for f in p["own"])
            typer.echo(f"{p['name']:<24} {p['role'] + own:<16} {p['email']:<34} {last}")
            for t in p["tokens"]:
                typer.echo(f"    token {t['id']} ({t['through']}): {t['label']}"
                           + (f", last used {t['last_used_at'][:16].replace('T', ' ')}" if t["last_used_at"] else ", not used yet"))
        if not found:
            typer.echo("nobody is listed")

    _access(show)


@access_app.command("add")
def access_add(
    email: Annotated[str, typer.Argument(help="The address the person signs in to Google with")],
    name: Annotated[str, typer.Argument(help="The name their changes are recorded under")],
    role: Annotated[str, typer.Option(help="reader looks; operations changes signs and work, not routes; editor, the route responsible, changes both; or a role of your own making (`access roles`)")] = "reader",
) -> None:
    """List a person, or change the name or role of one who is listed."""
    import os

    from stiflyt2 import access

    _access(lambda conn: access.add(conn, email, name, role, os.environ.get("USER", "?")))
    typer.echo(f"{name} <{email.strip().lower()}> may sign in, as {role}")


@access_app.command("roles")
def access_roles() -> None:
    """The roles there are: what each is a bundle of, and how many people hold it."""
    from stiflyt2 import access

    def show(conn) -> None:
        for r in access.roles(conn):
            typer.echo(f"{r['name']:<16} {', '.join(r['features']) or 'looks only':<32} {r['people']} "
                       f"{'person' if r['people'] == 1 else 'people'}" + (f"   {r['about']}" if r["about"] else ""))

    _access(show)


@access_app.command("role")
def access_role(
    name: Annotated[str, typer.Argument(help="The role: one there is, to change it, or a new name")],
    features: Annotated[str, typer.Option(help="What it is a bundle of, with commas between: see `access features`. Empty: looks only")] = "",
    about: Annotated[str | None, typer.Option(help="What the role is for, in a sentence")] = None,
    drop: Annotated[bool, typer.Option("--drop", help="Take the role away. Nobody may hold it")] = False,
) -> None:
    """Make a role, change what a role is a bundle of, or take one away. Those who hold it follow within half a minute."""
    from stiflyt2 import access

    if drop:
        _access(lambda conn: access.drop_role(conn, name))
        typer.echo(f"the role {name} is gone")
        return
    wanted = [f.strip() for f in features.split(",") if f.strip()]
    _access(lambda conn: access.set_role(conn, name, wanted, about))
    typer.echo(f"{name}: {', '.join(sorted(wanted)) or 'looks only'}")


@access_app.command("features")
def access_features() -> None:
    """Every feature: how it stands, which roles have it, and who is given or denied it by name."""
    from stiflyt2 import access

    def show(conn) -> None:
        for f in access.features(conn):
            typer.echo(f"{f['key']:<12} {f['state']:<6} to {f['title']}")
            typer.echo(f"    roles: {', '.join(f['roles']) or 'none'}"
                       + (f"; given to {', '.join(f['given'])}" if f["given"] else "")
                       + (f"; denied {', '.join(f['denied'])}" if f["denied"] else ""))

    _access(show)


@access_app.command("feature")
def access_feature(
    key: Annotated[str, typer.Argument(help="The feature, from `access features`")],
    state: Annotated[str, typer.Argument(help="off: for nobody. given: for those whose role or own list has it. all: for everybody who is let in")],
) -> None:
    """Say how a feature stands as a whole."""
    import os

    from stiflyt2 import access

    _access(lambda conn: access.set_state(conn, key, state, os.environ.get("USER", "?")))
    typer.echo(f"{key} is {state}")


@access_app.command("give")
def access_give(email: str, feature: str) -> None:
    """Give one person a feature, whatever their role says."""
    import os

    from stiflyt2 import access

    _access(lambda conn: access.grant(conn, email, feature, True, os.environ.get("USER", "?")))
    typer.echo(f"{email} has {feature}")


@access_app.command("deny")
def access_deny(email: str, feature: str) -> None:
    """Deny one person a feature, though their role has it."""
    import os

    from stiflyt2 import access

    _access(lambda conn: access.grant(conn, email, feature, False, os.environ.get("USER", "?")))
    typer.echo(f"{email} does not have {feature}")


@access_app.command("as-role")
def access_as_role(email: str, feature: str) -> None:
    """Leave a feature to the person's role again: neither given nor denied by name."""
    from stiflyt2 import access

    _access(lambda conn: access.grant(conn, email, feature, None, "?"))
    typer.echo(f"{feature} follows the role of {email} again")


@access_app.command("why")
def access_why(email: str) -> None:
    """For one person: every feature, whether they have it, and what decided that."""
    from stiflyt2 import access

    def show(conn) -> None:
        for f in access.why(conn, email):
            typer.echo(f"{'has ' if f['has'] else 'lacks'} {f['feature']:<12} {f['because']}")

    _access(show)


@access_app.command("remove")
def access_remove(email: str) -> None:
    """Take a person off the list. They are shut out within half a minute; what they changed stays."""
    from stiflyt2 import access

    _access(lambda conn: access.remove(conn, email))
    typer.echo(f"{email} is off the list")


@access_app.command("token")
def access_token(
    email: Annotated[str, typer.Argument(help="The person the token acts as")],
    label: Annotated[str, typer.Argument(help="What it is for, e.g. 'Claude Code on the laptop'")],
    through: Annotated[str, typer.Option(help="mcp for an assistant, api for a script")] = "mcp",
) -> None:
    """Make a token a program acts as a person with. It is shown once: keep it where the program reads it."""
    from stiflyt2 import access

    made = []
    _access(lambda conn: made.append(access.new_token(conn, email, through, label)))
    typer.echo(made[0])


@access_app.command("revoke")
def access_revoke(token: Annotated[int, typer.Argument(help="The number of the token, from `access list`")]) -> None:
    """Withdraw a token."""
    from stiflyt2 import access

    _access(lambda conn: access.revoke(conn, token))
    typer.echo(f"token {token} is withdrawn")


@height_app.callback(invoke_without_command=True)
def height_state(ctx: typer.Context) -> None:
    """How much of what is walked has heights."""
    if ctx.invoked_subcommand:
        return
    from stiflyt2 import height

    with psycopg.connect(settings.api_dsn) as conn:
        found = height.state(conn)
    typer.echo(f"{found['stretches'] - found['without']} of {found['stretches']} stretches have heights"
               + (f"; {found['without_m'] / 1000:.1f} of {found['metres'] / 1000:.1f} km wait. "
                  "`stiflyt2 height fill` asks for them." if found["without"] else "."))


@height_app.command("fill")
def height_fill(
    area: Annotated[str | None, typer.Argument(help="Area prefix, e.g. jot. Left out: everything")] = None,
    again: Annotated[bool, typer.Option("--again", help="Also the stretches that have heights")] = False,
) -> None:
    """Ask Kartverket's height service about every stretch that has no heights yet, and keep the answers.

    A stretch is asked about once; after that only when its line has been
    moved. Can be stopped and run again: what has been answered is kept.
    """
    from stiflyt2 import height

    with psycopg.connect(settings.api_dsn, autocommit=True) as conn:
        todo = height.lacking(conn, area=area, again=again)
        if not todo:
            typer.echo("Every stretch has heights.")
            return
        typer.echo(f"{len(todo)} stretches, {sum(len(t['points']) for t in todo)} points to ask about.")
        went = height.fill(todo, lambda got: height.keep(conn, got),
                           each=lambda n, of: n % 50 == 0 and typer.echo(f"  {n} of {of}"))
    typer.echo(f"Kept heights for {went['lines']} stretches.")
    if went["stopped"]:
        typer.echo(f"Stopped with {went['left']} left: {went['stopped']}", err=True)
        raise typer.Exit(1)


@app.command("checks")
def odd_things(area: Annotated[str, typer.Argument(help="Area prefix, e.g. jot")]) -> None:
    """The odd things of an area, each a place to look: one line under two numbers, a route of a few metres,
    a number that looks misspelt, a maintainer that is none, a line that goes out and back, ice not said to be ice."""
    from stiflyt2.domain import lookup as look

    with psycopg.connect(settings.api_dsn) as conn:
        found = look.checks(conn, area)
    for f in found["checks"]:
        at = f"   ({f['lat']:.5f}, {f['lon']:.5f})" if f["lat"] is not None else ""
        typer.echo(f"{f['kind']:<13} {f['text']}{at}")
    if not found["checks"]:
        typer.echo("nothing odd was found")


@app.command("turrutebasen")
def numbered(code: Annotated[str, typer.Argument(help="A route number, e.g. jot48")]) -> None:
    """Everything in the loaded Turrutebasen that carries a route number, segment by segment."""
    from stiflyt2.domain import lookup as look
    from stiflyt2.domain.places import NotFound

    with psycopg.connect(settings.api_dsn) as conn:
        try:
            found = look.turrutebasen(conn, code)
        except NotFound as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(1) from e
    for c in found["candidates"]:
        state = f"yours as {c['yours']['code']}" if c["yours"] else "set aside" if c["set_aside"] else "not yours"
        typer.echo(f"{c['label']}  {c['length_m'] / 1000:.1f} km in {c['segments']} segments, {c['shape']}  "
                   f"{'; '.join(c['maintainers'])}  ({state})")
    for s in found["segments"]:
        by = f"by {s['by_name']} ({s['by_type']}, {s['by_m']} m)" if s["by_name"] else ""
        yours = "yours: " + ", ".join(y["code"] for y in s["yours"]) if s["yours"] else "not in a route of yours"
        typer.echo(f"  {s['id']}  {s['length_m']:>6} m  {', '.join(s['codes']):<24} "
                   f"{s['follows'] or s['rutefolger'] or '?':<12} {'marked' if s['marked'] else 'unmarked':<9}"
                   f"{'; '.join(s['maintainers']):<28} {yours}  {by}")


@app.command("lookup")
def lookup(text: Annotated[str, typer.Argument(help="A code or a name, also one no longer used")]) -> None:
    """Find a route or a place by what it is called, or was called before."""
    from stiflyt2.domain import identity

    with psycopg.connect(settings.api_dsn) as conn:
        found = identity.lookup(conn, text)
    for f in found:
        was = "" if f["known_as"] == "now" else f"  (earlier {f['kind']}: {f['matched']})"
        typer.echo(f"{f['type']:<6} {f['label']:<24} {f['name'] if f['type'] == 'route' and f['name'] else '':<40} "
                   f"{f['id']}{was}")
    if not found:
        typer.echo("nothing is or was called that")


@app.command("corrections")
def to_turrutebasen(
    area: Annotated[str | None, typer.Argument(help="Area prefix, e.g. jot. All areas when left out")] = None,
    geojson: Annotated[Path | None, typer.Option(help="Also write the corrections to this GeoJSON file")] = None,
    srid: Annotated[int, typer.Option(help="Coordinates of the file: 4326 or 25833")] = 4326,
) -> None:
    """What to ask Kartverket to change in Turrutebasen: what is yours against the loaded Turrutebasen.

    Each change names the segment by Turrutebasen's own id (lokalid).
    """
    import json

    from stiflyt2.domain import corrections

    with psycopg.connect(settings.api_dsn) as conn:
        found = corrections.corrections(conn, area, srid)
    if geojson:
        geojson.write_text(json.dumps(corrections.as_geojson(found, srid), ensure_ascii=False))
    typer.echo(corrections.as_text(found))


@routes.command("export")
def routes_export(
    file: Annotated[Path, typer.Argument(help="The GeoJSON file to write")],
    area: Annotated[str | None, typer.Option(help="Area prefix, e.g. jot. All areas when left out")] = None,
    changed: Annotated[bool, typer.Option("--changed", help="Only routes that differ from Turrutebasen")] = False,
    srid: Annotated[int, typer.Option(help="Coordinates of the file: 4326 or 25833")] = 4326,
    discontinued: Annotated[bool | None, typer.Option(
        "--discontinued/--in-use", help="Only the discontinued routes, or only those in use. Both when left out")] = None,
) -> None:
    """Your routes as they are now, one line per route, with what has changed from Turrutebasen."""
    import json

    from stiflyt2.domain import export

    with psycopg.connect(settings.api_dsn) as conn:
        found = export.routes_geojson(conn, area, changed, srid, discontinued)
    file.write_text(json.dumps(found, ensure_ascii=False))
    differ = sum(f["properties"]["changed"] for f in found["features"])
    gone = sum(f["properties"]["state"] == "discontinued" for f in found["features"])
    typer.echo(f"{len(found['features'])} routes written to {file}: {differ} to act on, of them {gone} discontinued")


@routes.command("fill-metadata")
def routes_fill_metadata() -> None:
    """Give routes that say nothing about who maintains them what Turrutebasen has for them."""
    from stiflyt2.domain import routes as domain

    with psycopg.connect(settings.api_dsn) as conn:
        typer.echo(f"{domain.fill_metadata(conn)} routes filled")


@routes.command("search")
def routes_search(text: str, limit: int = 30) -> None:
    """Find route candidates by code prefix (bre, bre18) or by part of the name."""
    from stiflyt2.index import query

    with psycopg.connect(settings.import_dsn) as conn:
        found = query.search_routes(conn, text, limit=limit)
    for r in found:
        typer.echo(f"{r['label']:<12} {r['name'] or '':<42} {r['length_km']:>7} km "
                   f"{r['segment_count']:>4} seg  {r['shape'] or '':<9} {r['maintainer']}")


@src.command("list")
def list_sources() -> None:
    """Active snapshot per source and scope, and the latest refresh check."""
    with psycopg.connect(settings.import_dsn) as conn:
        rows = conn.execute(
            """SELECT s.source_key, s.scope, s.fetched_at::timestamp(0), s.source_updated_at::date,
                      s.counts::text, left(s.id::text, 8),
                      coalesce(c.finished_at::timestamp(0)::text || ' ' || c.outcome, 'never')
               FROM import_control.active_snapshot a
               JOIN source_raw.snapshot s ON s.id = a.snapshot_id
               LEFT JOIN LATERAL (
                   SELECT finished_at, outcome FROM import_control.check_log l
                   WHERE l.source_key = a.source_key AND l.scope = a.scope
                   ORDER BY finished_at DESC LIMIT 1) c ON true
               ORDER BY 1, 2"""
        ).fetchall()
    for r in rows:
        typer.echo(f"{r[0]:<14} {r[1]:<14} fetched {r[2]}  source date {r[3]}  {r[5]}  "
                   f"checked {r[6]}  {r[4]}")


@src.command()
def verify(
    source: Annotated[str | None, typer.Option(help="Only this source")] = None,
) -> None:
    """Check every kept original file against the checksum stored with its snapshot."""
    from stiflyt2.importer import delivery
    from stiflyt2.importer.adapters import fkb_wfs

    with psycopg.connect(settings.import_dsn) as conn:
        rows = conn.execute(
            """SELECT left(s.id::text, 8), s.source_key, s.scope, s.checksum, s.artifact_path,
                      s.manifest, p.pruned_at::date
               FROM source_raw.snapshot s
               LEFT JOIN import_control.pruned_artifact p ON p.snapshot_id = s.id
               WHERE s.state = 'complete' AND (%s::text IS NULL OR s.source_key = %s)
               ORDER BY s.fetched_at""",
            (source, source),
        ).fetchall()
    failed = False
    for snap, source_key, scope, checksum, artifact, manifest, pruned in rows:
        path = Path(artifact)
        if pruned:
            status = f"file removed by retention {pruned}"
        elif not path.exists():
            status = "MISSING"
        elif path.is_file():
            status = "ok" if delivery.sha256(path) == checksum else "CHANGED"
        elif "files" not in manifest:
            status = "not checkable (made before file order was recorded)"
        else:
            files = [path / name for name in manifest["files"]]
            same = (all(f.exists() for f in files)
                    and fkb_wfs.checksum(path / manifest["schema"], files) == checksum)
            status = "ok" if same else "CHANGED"
        failed = failed or status in ("MISSING", "CHANGED")
        typer.echo(f"{source_key:<14} {scope:<14} {snap}  {status}  {artifact}")
    if failed:
        raise typer.Exit(1)


@src.command()
def history(source: str) -> None:
    """Every snapshot of a source, with what changed."""
    with psycopg.connect(settings.import_dsn) as conn:
        rows = conn.execute(
            """SELECT s.fetched_at::timestamp(0), s.scope, s.state, left(s.id::text, 8),
                      coalesce(d.counts::text, ''), coalesce(left(s.error, 100), '')
               FROM source_raw.snapshot s
               LEFT JOIN import_control.diff d ON d.to_snapshot = s.id
               WHERE s.source_key = %s ORDER BY s.fetched_at""",
            (source,),
        ).fetchall()
    for r in rows:
        typer.echo(f"{r[0]}  {r[1]:<10} {r[2]:<9} {r[3]}  {r[4]} {r[5]}")


if __name__ == "__main__":
    app()
