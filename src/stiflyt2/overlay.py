"""The overlay as a whole: keep a copy of it, take it away, put a copy back.

The overlay is everything that is yours: the schemas `core` and `history`
(places, routes, stretches, the event log, undo). The imported sources and the
index built from them are not part of it and are never touched here.

Taking the overlay away rolls the owned-side migrations back to nothing and
runs them again, so what is left is exactly a new installation. A copy is
always written first, and `restore` puts a copy back in full, ids and history
included.

Runs as the OS user that runs the migrations (a member of stiflyt2_owner), on
the database named by STIFLYT2_MIGRATE_URL.
"""
from __future__ import annotations

import os
import subprocess
import sys
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


def holds() -> dict[str, int]:
    """How much the overlay holds now."""
    with psycopg.connect(dbname=database()) as conn:
        conn.execute(f"SET ROLE {OWNER}")
        return {name: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for name, table in COUNTED.items()}


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
    _run("pg_dump", *_FORMAT, f"--role={OWNER}", *(f"--schema={s}" for s in BESIDE),
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
    if not file.is_file():
        raise Failed(f"no such file: {file}")
    # Everything in the copy except the schemas themselves, which stay as they are with their grants.
    listed = [line for line in _run("pg_restore", "--list", str(file)).splitlines()
              if " SCHEMA - " not in line and " ACL - SCHEMA " not in line]
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
