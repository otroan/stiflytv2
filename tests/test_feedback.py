"""What the tool refuses, where it fails and what people report is kept, counted together and marked."""
import uuid

import psycopg
import pytest
from conftest import role_dsn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stiflyt2 import feedback
from stiflyt2.api.app import create_app
from stiflyt2.cli import app as cli
from stiflyt2.config import settings
from stiflyt2.domain import routes, undo
from stiflyt2.index import query

V1 = "/api/v1"
VIEWER = {"X-Stiflyt-Through": "viewer"}


@pytest.fixture
def client(loaded, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def conn(loaded):
    with psycopg.connect(role_dsn("stiflyt2_api"), autocommit=True) as c:
        yield c


def since(conn, start):
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute("SELECT * FROM feedback.entry WHERE id > %s ORDER BY id", (start,)).fetchall()


def latest(conn):
    return conn.execute("SELECT coalesce(max(id), 0) FROM feedback.entry").fetchone()[0]


def test_what_the_tool_refuses_is_kept_with_what_was_asked_and_about_which_route(client, conn):
    start, nobody = latest(conn), str(uuid.uuid4())
    # A change to a route that is not there: refused in a sentence, and that sentence is what is kept.
    refused = client.patch(f"{V1}/owned/routes/{nobody}", json={"version": 1, "name": "Nowhere"}, headers=VIEWER)
    assert refused.status_code == 404
    # What was sent did not have the shape asked for: a fault of the page or the assistant, kept too.
    assert client.post(f"{V1}/owned/routes/plan", json={"nothing": 1}).status_code == 422
    # Looking at something that is gone is what a page does after an undo: not kept.
    assert client.get(f"{V1}/owned/routes/{nobody}").status_code == 404
    assert client.get(f"{V1}/areas").status_code == 200
    # What the page asks while something is dragged, to show what letting go would give: nobody met that refusal.
    assert client.post(f"{V1}/owned/routes/{nobody}/end", json={"version": 1, "end": nobody, "lon": 7.0, "lat": 61.5,
                       "dry_run": True}, headers={**VIEWER, "X-Stiflyt-Preview": "1"}).status_code == 404

    first, second = since(conn, start)
    assert (first["kind"], first["through"], first["who"], first["status"]) == (
        "refused", "viewer", "Test Editor", 404)
    assert first["what"] == "PATCH /api/v1/owned/routes/{route_id}"
    assert first["message"] == refused.json()["detail"]
    assert (first["about_type"], first["about_id"]) == ("route", nobody)
    assert first["arguments"] == {"path": {"route_id": nobody}, "body": {"version": 1, "name": "Nowhere"}}
    assert first["seen_at"] is None
    assert (second["kind"], second["through"], second["what"]) == ("refused", "api", "POST /api/v1/owned/routes/plan")
    assert second["message"].startswith("the request was not understood: body.")
    assert second["about_id"] is None


def test_an_error_and_a_question_that_took_too_long_are_kept_as_failures(client, conn, monkeypatch):
    start = latest(conn)

    def broken(conn):
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr(query, "areas", broken)
    assert client.get(f"{V1}/areas").status_code == 500

    def slow(conn):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(query, "areas", slow)
    assert client.get(f"{V1}/areas").status_code == 504

    error, timeout = since(conn, start)
    assert (error["kind"], error["status"], error["what"]) == ("failed", 500, "GET /api/v1/areas")
    assert error["message"].startswith("RuntimeError: the index is on fire (broken, line ")
    assert "Traceback" in error["trace"] and "the index is on fire" in error["trace"]
    assert (timeout["kind"], timeout["status"]) == ("failed", 504)
    assert timeout["message"] == "the question took too long to answer"


def test_a_person_or_an_assistant_can_say_what_is_wrong_or_missing(client, conn, monkeypatch):
    start, route = latest(conn), str(uuid.uuid4())
    said = client.post(f"{V1}/feedback", headers=VIEWER, json={
        "message": "There is no way to say that bre59 is closed for the summer",
        "about_type": "route", "about_id": route, "context": {"area": "bre", "zoom": 12.5}})
    assert said.status_code == 201
    gap = client.post(f"{V1}/feedback", headers={"X-Stiflyt-Through": "mcp"}, json={
        "what": "report_gap", "message": "Lay jot38 over the glacier where the guide route goes",
        "tried": "plan between the place names", "why_not": "nothing is mapped on the ice"})
    assert gap.status_code == 201
    page = client.post(f"{V1}/feedback", headers=VIEWER, json={
        "kind": "failed", "what": "page", "message": "TypeError: route is null", "trace": "at draw (page.js:1:1)"})
    assert page.status_code == 201

    note, told, error = since(conn, start)
    assert note["id"] == said.json()["id"] and note["kind"] == "reported" and note["what"] == "note"
    assert (note["through"], note["who"], note["about_id"]) == ("viewer", "Test Editor", route)
    assert note["arguments"] == {"context": {"area": "bre", "zoom": 12.5}}
    assert (told["through"], told["what"]) == ("mcp", "report_gap")
    assert told["arguments"] == {"tried": "plan between the place names", "why_not": "nothing is mapped on the ice"}
    assert (error["kind"], error["what"], error["trace"]) == ("failed", "page", "at draw (page.js:1:1)")

    # What people say comes first in what is handed to whoever looks after the tool.
    listed = client.get(f"{V1}/feedback").json()
    assert listed["groups"][0]["kind"] == "reported"
    assert {note["id"], told["id"]} <= {g["id"] for g in listed["groups"] if g["kind"] == "reported"}

    # Only on a server where changes are made, and that refusal is itself kept.
    monkeypatch.setattr(settings, "editor", "")
    assert client.post(f"{V1}/feedback", json={"message": "anyone there?"}).status_code == 403
    assert since(conn, error["id"])[0]["message"] == "editing is not enabled on this server"


def test_entries_that_say_the_same_are_counted_together_and_marked_together(conn):
    with conn.transaction(force_rollback=True):
        conn.execute("UPDATE feedback.entry SET seen_at = now() WHERE seen_at IS NULL")
        route = routes.make(conn, conn.execute(
            "SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0], "Test Editor")
        via = "POST /api/v1/owned/routes/{route_id}/via"
        for who, metres in (("Ola", 150), ("Ola", 150), ("Kari", 90.5)):
            last = feedback.record(
                conn, "refused", "viewer", via, who=who, status=422, about=("route", route),
                message=f"no stretch leads there, and no mapped path or road lies within {metres} m of the spot")
        other = feedback.record(conn, "refused", "mcp", via, "that is the way it goes already", who="Kari")
        said = feedback.record(conn, "reported", "viewer", "note", "The line jumps back when I let go", who="Ola")

        groups = feedback.grouped(conn)
        assert [(g["id"], g["kind"], g["times"], g["people"]) for g in groups] == [
            (said, "reported", 1, 1), (last, "refused", 3, 2), (other, "refused", 1, 1)]
        same = groups[1]
        assert same["message"].endswith("within 90.5 m of the spot") and same["through"] == ["viewer"]
        assert same["about"] == [{"type": "route", "id": route, "called": "bre18", "times": 3}]
        assert [e["who"] for e in feedback.entries(conn, last)] == ["Kari", "Ola", "Ola"]
        assert feedback.entries(conn, last)[0]["about"] == "bre18"

        # Looked at: all three go, and what is left is what nobody has looked at.
        assert feedback.mark(conn, last) == 3 and feedback.mark(conn, last) == 0
        assert [g["id"] for g in feedback.grouped(conn)] == [said, other]
        # Put right, with the commit: said of every one of them, also those already looked at.
        assert feedback.mark(conn, last, "c0ffee1") == 3
        fixed = next(g for g in feedback.grouped(conn, everything=True) if g["id"] == last)
        assert (fixed["fixed_in"], fixed["unseen"], fixed["times"]) == ("c0ffee1", 0, 3)
        with pytest.raises(feedback.NotFound):
            feedback.mark(conn, 10**9)


def test_the_command_line_lists_shows_and_marks(client, conn):
    conn.execute("UPDATE feedback.entry SET seen_at = now() WHERE seen_at IS NULL")
    said = client.post(f"{V1}/feedback", headers=VIEWER, json={"message": "The spike at Keisarpasset"}).json()["id"]
    run = CliRunner().invoke(cli, ["feedback"])
    assert run.exit_code == 0 and f"#{said}" in run.output and "The spike at Keisarpasset" in run.output
    assert run.output.lstrip().startswith("reported")
    shown = CliRunner().invoke(cli, ["feedback", "show", str(said)])
    assert "Test Editor through viewer" in shown.output
    assert CliRunner().invoke(cli, ["feedback", "fixed", str(said), "abc1234"]).output.strip() == "1 marked"
    assert "nothing new has come in" in CliRunner().invoke(cli, ["feedback"]).output
    assert "fixed in abc1234" in CliRunner().invoke(cli, ["feedback", "--all"]).output
    assert CliRunner().invoke(cli, ["feedback", "show", str(10**9)]).exit_code == 1


def test_a_change_taken_back_at_once_is_read_out_of_the_undo_log(conn):
    start = conn.execute("SELECT coalesce(max(txid), 0) FROM core.undo_log").fetchone()[0]
    with conn.transaction():
        routes.make(conn, conn.execute(
            "SELECT id FROM source_index.route WHERE label = 'bre18'").fetchone()[0], "Test Editor")
    assert not [t for t in feedback.taken_back(conn) if t["txid"] > start]
    with conn.transaction():
        undo.undo(conn, "Test Editor")
    back = [t for t in feedback.taken_back(conn) if t["txid"] > start]
    assert [(t["who"], t["what"]) for t in back] == [("Test Editor", "bre18 made yours")]
    assert back[0]["after_s"] <= feedback.SOON_S
    assert feedback.taken_back(conn, soon_s=-1) == []
