"""The overlay as a whole: keep a copy of it, take it away, put a copy back.

The overlay is everything that is yours: the schemas `core` and `history`
(places, routes, stretches, the event log, undo). The imported sources and the
index built from them are not part of it and are never touched here.

Beside it is kept what is about the tool and the work done with it: who may use
it (`access`), the feedback (`feedback`) and operations (`ops`: signs, issues,
reports, photos as records, their undo). It has a copy of its own, and is put
back row for row into the tables as they are (`restore_beside`): its tables
come with the import migrations, which also make the sources, so they are not
taken away and made again as the overlay's are.

Taking the overlay away rolls the owned-side migrations back to nothing and
runs them again, so what is left is exactly a new installation. A copy is
always written first, and `restore` puts a copy back in full, ids and history
included.

Runs as the OS user that runs the migrations (a member of stiflyt2_owner), on
the database named by STIFLYT2_MIGRATE_URL.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ("core", "history")
# About the tool, who uses it, and what is decided about signs and work: not the overlay, copied beside it.
BESIDE = ("feedback", "access", "ops")
OWNER = "stiflyt2_owner"
COUNTED = {"places": "core.place", "routes": "core.route", "stretches": "core.edge",
           "changes": "history.event"}
COUNTED_BESIDE = {"people": "access.person", "feedback entries": "feedback.entry", "issues": "ops.entry",
                  "reports": "ops.report", "photos": "ops.photo", "tracks": "ops.track", "sign sites decided on": "ops.sign_site"}


# Not compressed: the nightly copies also go into a git repository off the server, which keeps only what
# differs from one night to the next and does its own compressing.
_FORMAT = ("--format=custom", "--compress=0")


class Failed(Exception):
    pass


def database() -> str:
    """The database the owned-side migrations run on."""
    return os.environ.get("STIFLYT2_MIGRATE_URL", "postgresql+psycopg:///stiflyt2").rsplit("/", 1)[-1]


def _run(*cmd: str) -> str:
    p = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=ROOT)
    if p.returncode != 0:
        raise Failed(f"{Path(cmd[0]).name} failed: {p.stderr.strip()[-800:]}")
    return p.stdout


def _alembic(*args: str) -> None:
    _run(str(Path(sys.executable).parent / "alembic"), "-n", "domain", *args)


def holds(beside: bool = False) -> dict[str, int]:
    """How much the overlay holds now, or what is kept beside it."""
    with psycopg.connect(dbname=database()) as conn:
        conn.execute(f"SET ROLE {OWNER}")
        return {name: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for name, table in (COUNTED_BESIDE if beside else COUNTED).items()}


def _listed(file: Path) -> list[str]:
    if not file.is_file():
        raise Failed(f"no such file: {file}")
    return _run("pg_restore", "--list", str(file)).splitlines()


def kind_of(file: Path) -> str:
    """Which copy a file is: of the overlay ("overlay"), or of what is kept beside it ("tool")."""
    listed = _listed(file)
    if any(" TABLE DATA core " in line for line in listed):
        return "overlay"
    if any(re.search(rf" TABLE DATA ({'|'.join(BESIDE)}) ", line) for line in listed):
        return "tool"
    raise Failed(f"{file} is not a copy of the overlay, nor of what is kept beside it")


def backup(directory: Path) -> Path:
    """Write a copy of the whole overlay to a file, and return the file."""
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / f"overlay-{database()}-{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}.dump"
    _run("pg_dump", *_FORMAT, f"--role={OWNER}", *(f"--schema={s}" for s in SCHEMAS),
         f"--file={file}", database())
    return file


def backup_beside(directory: Path) -> Path:
    """Write a copy of what is kept beside the overlay: who may use the tool, and what they met using it."""
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / f"tool-{database()}-{datetime.now(UTC):%Y%m%dT%H%M%S.%fZ}.dump"
    # The tracks cleaned are worked out from what came, and are left out: `stiflyt2 tracks clean-again` makes them.
    _run("pg_dump", *_FORMAT, f"--role={OWNER}", *(f"--schema={s}" for s in BESIDE), "--exclude-table-data=ops.track_line",
         f"--file={file}", database())
    return file


def prune(directory: Path, keep: int) -> list[Path]:
    """Remove all but the newest copies of this database, of each kind. Returns what was removed."""
    gone = []
    for kind in ("overlay", "tool"):
        files = sorted(directory.glob(f"{kind}-{database()}-*.dump"))
        for old in files[:-keep] if keep > 0 else []:
            old.unlink()
            gone.append(old)
    return gone


def reset(directory: Path) -> Path:
    """Take the whole overlay away, leaving it as on a new installation. Returns the copy written first."""
    kept = backup(directory)
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    return kept


def restore(file: Path, directory: Path) -> Path:
    """Put a copy of the overlay back in place of what is there now. Returns a copy of what was there."""
    # Everything in the copy except the schemas themselves, which stay as they are with their grants.
    listed = [line for line in _listed(file) if " SCHEMA - " not in line and " ACL - SCHEMA " not in line]
    if not any(" TABLE DATA core " in line for line in listed):
        raise Failed(f"{file} is not a copy of an overlay")
    kept = backup(directory)
    _alembic("downgrade", "base")
    contents = file.with_suffix(".list")
    contents.write_text("\n".join(listed) + "\n")
    try:
        _run("pg_restore", f"--role={OWNER}", "--clean", "--if-exists", "--exit-on-error",
             "--single-transaction", f"--use-list={contents}", f"--dbname={database()}", str(file))
    except Failed as e:
        _alembic("upgrade", "head")  # nothing of the copy went in: leave an empty overlay that works
        raise Failed(f"{e}\nThe overlay is now empty. What was there is kept in {kept}") from e
    finally:
        contents.unlink()
    _alembic("upgrade", "head")      # a copy from before a later migration is brought up to date
    return kept


_DATA = re.compile(rf" (TABLE DATA|SEQUENCE SET) ({'|'.join(BESIDE)}) (\S+) ")


def restore_beside(file: Path, directory: Path) -> tuple[Path, list[str]]:
    """Put a copy of what is kept beside the overlay back, in place of what is there now.

    Row for row into the tables as they are, all in one transaction: every table is emptied and filled
    from the copy, and where the copy does not fit them (a copy from before a migration that changed a
    table), nothing is changed. The undo of operations does not count it as a change. Returns a copy of
    what was there, and the tables the copy has nothing of, which are left empty.
    """
    entries = [(m.group(1), f"{m.group(2)}.{m.group(3)}", line) for line in _listed(file) if (m := _DATA.search(line))]
    in_copy = {table for kind, table, _ in entries if kind == "TABLE DATA"}
    if not in_copy:
        raise Failed(f"{file} is not a copy of what is kept beside the overlay")
    with psycopg.connect(dbname=database()) as conn:
        conn.execute(f"SET ROLE {OWNER}")
        tables = [t for (t,) in conn.execute("""
            SELECT format('%%I.%%I', schemaname, tablename) FROM pg_tables WHERE schemaname = ANY(%s) ORDER BY 1""",
            (list(BESIDE),))]
        refers = {}
        for table, to in conn.execute("""
                SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint
                WHERE contype = 'f' AND connamespace = ANY(SELECT oid FROM pg_namespace WHERE nspname = ANY(%s))""",
                (list(BESIDE),)):
            refers.setdefault(table, set()).add(to)
    unknown = sorted(in_copy - set(tables))
    if unknown:
        raise Failed(f"the copy has tables that are not here: {', '.join(unknown)}. Nothing is changed")
    # A table is filled after those it refers to, as the keys between them are checked row by row.
    order, left = [], sorted(in_copy)
    while left:
        ready = [t for t in left if not (refers.get(t, set()) - {t}) & set(left)] or left[:1]
        order += ready
        left = [t for t in left if t not in ready]
    by_table = {table: line for kind, table, line in entries if kind == "TABLE DATA"}
    sequences = [line for kind, _, line in entries if kind == "SEQUENCE SET"]
    kept = backup_beside(directory)
    with tempfile.TemporaryDirectory(dir=directory) as work:
        contents, script = Path(work) / "contents.list", Path(work) / "rows.sql"
        contents.write_text("\n".join([by_table[t] for t in order] + sequences) + "\n")
        _run("pg_restore", "--data-only", f"--use-list={contents}", f"--file={script}", str(file))
        try:
            _run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction", f"--dbname={database()}",
                 "-c", f"SET ROLE {OWNER}", "-c", "SET LOCAL stiflyt2.undoing = 'on'",
                 "-c", f"TRUNCATE {', '.join(tables)}", "-f", str(script))
        except Failed as e:
            raise Failed(f"{e}\nNothing is changed. A copy of what is there is in {kept}") from e
    return kept, sorted(set(tables) - in_copy)
