"""Tests run against a throwaway database, never the working one: snapshots are
immutable, so fixture data loaded into a real database could not be removed.

Two optional settings, from the environment or .env, for clusters where the OS
user is not a superuser:

  STIFLYT2_TEST_TEMPLATE   create the test database from this template, which
                           already has the PostGIS extension
  STIFLYT2_TEST_ROLE_CONN  extra libpq parameters for logging in as a
                           stiflyt2_* role, e.g. host=127.0.0.1
"""
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

HTTP_CLIENT = httpx.Client  # the real one, before any test replaces it with a canned server

TEST_DB = "stiflyt2_test"
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


class _Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STIFLYT2_TEST_", env_file=ROOT / ".env", extra="ignore")

    template: str = ""
    role_conn: str = ""


_env = _Env()


def role_dsn(role: str) -> str:
    return f"dbname={TEST_DB} user={role} {_env.role_conn}".strip()


os.environ["STIFLYT2_IMPORT_DSN"] = role_dsn("stiflyt2_import")


def _run(*cmd, env=None):
    p = subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr.strip()[-1000:]}")


@pytest.fixture(scope="session", autouse=True)
def database(tmp_path_factory):
    _run("dropdb", "--if-exists", TEST_DB)
    _run("createdb", *(["-T", _env.template] if _env.template else []), TEST_DB)
    _run("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", TEST_DB, "-f", "db/bootstrap.sql")
    for track in ("alembic", "domain"):
        _run(str(ROOT / ".venv/bin/alembic"), "-n", track, "upgrade", "head",
             env={"STIFLYT2_MIGRATE_URL": f"postgresql+psycopg:///{TEST_DB}"})
    from stiflyt2.config import settings
    settings.import_dsn = os.environ["STIFLYT2_IMPORT_DSN"]
    settings.api_dsn = role_dsn("stiflyt2_api")
    settings.editor = ""  # whatever the checkout's .env says; tests name their own
    # The same for sign-in: the tests run the service locally unless one of them says otherwise.
    settings.google_client_id = settings.google_client_secret = settings.session_secret = settings.public_url = ""
    settings.data_dir = tmp_path_factory.mktemp("raw")
    settings.photos_dir = tmp_path_factory.mktemp("photos")
    settings.tracks_dir = tmp_path_factory.mktemp("tracks")
    yield


@pytest.fixture
def small(monkeypatch):
    """Let the fixtures past the guard against implausibly small nationwide deliveries."""
    from dataclasses import replace

    from stiflyt2.importer.adapters import kulturminner, stedsnavn, turrutebasen

    for adapter in (turrutebasen, stedsnavn, kulturminner):
        monkeypatch.setattr(adapter, "SPECS", [replace(s, min_rows=1) for s in adapter.SPECS])


def later_delivery(fixture: Path, tmp_path: Path, *statements: str) -> Path:
    """A copy of a fixture delivery with SQL edits applied to its geodatabase."""
    unpacked = tmp_path / "later"
    with zipfile.ZipFile(fixture) as z:
        z.extractall(unpacked)
    gdb = next(unpacked.glob("*.gdb"))
    for sql in statements:
        subprocess.run(["ogrinfo", "-q", str(gdb), "-dialect", "SQLite", "-sql", sql],
                       check=True, capture_output=True)
    return Path(shutil.make_archive(str(tmp_path / "later"), "zip", unpacked))


def deliver(tmp_path, features, *, key="fixture", scope="T", collect=False, **kw):
    """Import a list of GeoJSON features as one delivery of a synthetic source."""
    import hashlib
    import json

    import psycopg

    from stiflyt2.config import settings
    from stiflyt2.importer import core

    path = tmp_path / "f.geojson"
    path.write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::25833"}},
        "features": features,
    }))
    table = f"fx_{key}"
    spec = [core.LayerSpec("line", table, "s.lokalid", "to_jsonb(s) - 'ogc_fid' - 'geom'",
                           collect=collect)]
    with psycopg.connect(settings.import_dsn) as conn:
        core.register_source(conn, key, key, "test", "test", "allowed")
    try:
        core.ogr_load(path, None, table)
        return core.ingest(core.Delivery(
            source_key=key, scope=scope,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
            artifact_path=path, transform_version="fx-1", layers=spec), **kw)
    finally:
        core.drop_staging([table])


def line(lokalid, name, coords):
    return {"type": "Feature", "properties": {"lokalid": lokalid, "name": name},
            "geometry": {"type": "LineString", "coordinates": coords}}


@pytest.fixture(scope="session")
def have_snapshot(database, tmp_path_factory):
    """A complete snapshot of a separate synthetic source, for guard tests."""
    return deliver(
        tmp_path_factory.mktemp("guard"),
        [line("g1", "G1", [[1e5, 68e5], [1e5 + 50, 68e5]]),
         line("g2", "G2", [[1e5 + 50, 68e5], [1e5 + 90, 68e5]])],
        key="guard",
    )


@pytest.fixture
def loaded(small, monkeypatch):
    """Every source has its base fixture as the active snapshot, and the index is built."""
    from test_fkb_wfs import BOX, Server
    from test_osm import BOX as OSM_BOX
    from test_osm import Geofabrik

    from stiflyt2.importer.adapters import (
        fkb_wfs,
        kulturminner,
        osm,
        stedsnavn,
        teig,
        turrutebasen,
        vegnett,
    )
    from stiflyt2.index import build

    real, server = HTTP_CLIENT, Server()
    monkeypatch.setattr(fkb_wfs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(server), **kw))
    turrutebasen.run(FIXTURES / "turrutebasen.zip")
    stedsnavn.run(FIXTURES / "stedsnavn.zip")
    kulturminner.run(FIXTURES / "kulturminner.zip")
    teig.run("4644", FIXTURES / "teig.zip")
    vegnett.run("4644", FIXTURES / "vegnett.zip")
    fkb_wfs.run("index", BOX)
    geofabrik = Geofabrik()
    monkeypatch.setattr(osm.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(geofabrik), **kw))
    monkeypatch.setattr(osm, "URL", Geofabrik.URL)
    osm.run("index", OSM_BOX)
    build.build()


def scripts_of(client, page: str) -> dict[str, str]:
    """A page's own scripts by address, as the browser would fetch them; what is written in the page itself under ''."""
    html = client.get(page).text
    found = {"": "\n".join(re.findall(r"<script>\n(.*?)</script>", html, re.DOTALL))}
    for src in re.findall(r'<script src="(/static/[^"]+)"', html):
        got = client.get(src)
        assert got.status_code == 200 and got.headers["cache-control"] == "no-cache", src
        found[src] = got.text
    return found
