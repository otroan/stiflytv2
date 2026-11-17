"""Who gets in: nobody unlisted once people sign in, readers look, editors change, and a token acts as its person."""
import psycopg
import pytest
from conftest import TEST_DB, role_dsn
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stiflyt2 import access
from stiflyt2.api import auth
from stiflyt2.api.app import create_app
from stiflyt2.cli import app as cli
from stiflyt2.config import settings

V1 = "/api/v1"
KARI, PER = "kari@example.org", "per@example.org"


@pytest.fixture
def owner(loaded):
    """As the database's owner, who alone changes who is listed. Whoever a test lists is taken off again."""
    with psycopg.connect(dbname=TEST_DB, autocommit=True) as conn:
        conn.execute("SET ROLE stiflyt2_owner")
        yield conn
        conn.execute("DELETE FROM access.person WHERE email LIKE '%@example.org'")


@pytest.fixture
def signed(owner, monkeypatch):
    """The service as it runs behind the proxy: people sign in."""
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "session_secret", "a secret for the tests only")
    monkeypatch.setattr(settings, "public_url", "https://testserver")
    monkeypatch.setattr(settings, "editor", "Somebody Else")      # not used when people sign in
    monkeypatch.setattr(auth, "REMEMBER_S", 0)
    with TestClient(create_app(), base_url="https://testserver", follow_redirects=False) as client:
        yield client


class Google:
    """Stands in for Google: sends the browser away and back, and says who signed in."""

    def __init__(self, **said):
        self.said = said

    async def authorize_redirect(self, request, redirect_uri):
        return RedirectResponse(f"https://accounts.example/o/oauth2?redirect_uri={redirect_uri}")

    async def authorize_access_token(self, request):
        return {"userinfo": self.said}


def test_run_locally_it_answers_only_what_is_asked_straight_of_localhost(loaded, monkeypatch):
    monkeypatch.setattr(settings, "editor", "Test Editor")
    with TestClient(create_app()) as client:
        assert client.get(f"{V1}/areas").status_code == 200
        assert client.get(f"{V1}/auth/me").json() == {
            "name": "Test Editor", "email": None, "role": "editor", "edits": True, "operates": True, "through": "api",
                "sign_in": False}
        # Passed on by a proxy, or asked for under the public name: this way of running is not for the world.
        for headers in ({"X-Forwarded-For": "203.0.113.9"}, {"Host": "stiflyt2.example.org"}):
            refused = client.get(f"{V1}/areas", headers=headers)
            assert (refused.status_code, refused.json()["detail"]) == (403, "sign-in is not set up on this server")
            assert client.get("/", headers=headers).status_code == 403


def test_nothing_is_given_to_someone_who_is_not_signed_in(signed):
    for path in (f"{V1}/areas", f"{V1}/tiles/routes/8/134/72.mvt", f"{V1}/owned/routes.geojson", f"{V1}/feedback",
                 f"{V1}/auth/me", "/api/openapi.json"):
        answer = signed.get(path)
        assert (answer.status_code, answer.json()) == (401, {"detail": "not signed in"}), path
    assert signed.post(f"{V1}/feedback", json={"message": "anyone?"}).status_code == 401
    # The page itself sends you to sign in, and back to where you were going.
    browser = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    page = signed.get("/", headers=browser)
    assert (page.status_code, page.headers["location"]) == (302, f"{V1}/auth/login?next=/")
    assert signed.get("/guide", headers=browser).headers["location"] == f"{V1}/auth/login?next=/guide"
    # A program is not sent to a sign-in page it would try to read as an answer: an assistant's client
    # that gets 401 from /mcp looks for OAuth on the server, and is told in JSON that there is none.
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration", "/"):
        looked = signed.get(path, headers={"Accept": "application/json"})
        assert (looked.status_code, looked.json()) == (404, {"detail": "not found"}), path
    assert signed.post("/register", json={"client_name": "x"}).status_code == 404
    assert signed.get(f"{V1}/auth/login", params={"next": "//evil.example/"}).status_code in (302, 307)


def test_signing_in_with_google_lets_in_only_who_is_listed(signed, owner, monkeypatch):
    access.add(owner, KARI, "Kari Nordmann", "reader", "test")
    monkeypatch.setattr(auth, "google", lambda: Google(email="Kari@Example.org", email_verified=True, name="K"))
    away = signed.get(f"{V1}/auth/login", params={"next": "/?area=jot"})
    assert away.headers["location"].startswith("https://accounts.example/") and "testserver/api/v1/auth/callback" in away.headers["location"]
    back = signed.get(f"{V1}/auth/callback")
    assert (back.status_code, back.headers["location"]) == (302, "/?area=jot")
    assert signed.get(f"{V1}/auth/me").json() == {
        "name": "Kari Nordmann", "email": KARI, "role": "reader", "edits": False, "operates": False, "through": "viewer",
        "sign_in": True}
    assert signed.get("/").status_code == 200 and signed.get(f"{V1}/areas").status_code == 200
    assert "How to use it" in signed.get("/guide").text

    # A reader looks. Changing is refused in words, whatever the request claims about how it came in.
    assert signed.get(f"{V1}/editing").json() == {"editor": None, "changeset": None, "reader": "Kari Nordmann", "sign_in": True}
    no = signed.post(f"{V1}/feedback", json={"message": "let me in"}, headers={"X-Stiflyt-Through": "mcp"})
    assert (no.status_code, no.json()["detail"]) == (403, "you are signed in to look, not to change: ask for the editor role")
    # Made an editor, she changes at once, under her own name.
    access.add(owner, KARI, "Kari Nordmann", "editor", "test")
    assert signed.get(f"{V1}/editing").json()["editor"] == "Kari Nordmann"
    said = signed.post(f"{V1}/feedback", json={"message": "Kari was here"}, headers={"X-Stiflyt-Through": "mcp"})
    assert said.status_code == 201
    assert owner.execute("SELECT who, through FROM feedback.entry WHERE id = %s", (said.json()["id"],)).fetchone() == (
        "Kari Nordmann", "viewer")
    assert owner.execute("SELECT last_seen_at IS NOT NULL FROM access.person WHERE email = %s", (KARI,)).fetchone()[0]
    # A change sent by another site's page in her browser is not hers.
    assert signed.post(f"{V1}/feedback", json={"message": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403

    # Taken off the list, she is out, signed in or not. Signing out does the same for one browser.
    access.remove(owner, KARI)
    assert signed.get(f"{V1}/areas").status_code == 401
    access.add(owner, KARI, "Kari Nordmann", "reader", "test")
    assert signed.get(f"{V1}/areas").status_code == 200
    assert signed.post(f"{V1}/auth/logout").json() == {"ok": True}
    assert signed.get(f"{V1}/areas").status_code == 401

    # Not listed, or an address Google has not confirmed: told why, and not let in.
    for said_by_google, why, words in (
            ({"email": PER, "email_verified": True}, "not_listed", "not on the list of people"),
            ({"email": KARI, "email_verified": False}, "email_unverified", "has not confirmed")):
        monkeypatch.setattr(auth, "google", lambda said=said_by_google: Google(**said))
        signed.get(f"{V1}/auth/login")
        back = signed.get(f"{V1}/auth/callback")
        assert back.headers["location"] == f"{V1}/auth/refused?why={why}"
        told = signed.get(back.headers["location"])
        assert told.status_code == 403 and words in told.text and signed.get(f"{V1}/areas").status_code == 401


def test_a_token_acts_as_its_person_and_what_it_does_is_recorded_as_having_come_that_way(signed, owner):
    access.add(owner, KARI, "Kari Nordmann", "editor", "test")
    access.add(owner, PER, "Per Hansen", "reader", "test")
    hers = access.new_token(owner, KARI, "mcp", "her assistant")
    his = access.new_token(owner, PER, "api", "a script that reads")
    assert hers.startswith("stf2_") and hers != his
    assert owner.execute("SELECT count(*) FROM access.token WHERE digest = %s", (hers,)).fetchone() == (0,)   # not kept

    as_her, as_him = {"Authorization": f"Bearer {hers}"}, {"Authorization": f"Bearer {his}"}
    assert signed.get(f"{V1}/auth/me", headers=as_her).json() == {
        "name": "Kari Nordmann", "email": KARI, "role": "editor", "edits": True, "operates": True, "through": "mcp",
        "sign_in": True}
    said = signed.post(f"{V1}/feedback", headers=as_her, json={"what": "report_gap", "message": "No way to do that"})
    assert owner.execute("SELECT who, through, what FROM feedback.entry WHERE id = %s", (said.json()["id"],)).fetchone() == (
        "Kari Nordmann", "mcp", "report_gap")
    assert signed.get(f"{V1}/areas", headers=as_him).status_code == 200
    assert signed.post(f"{V1}/feedback", headers=as_him, json={"message": "x"}).status_code == 403
    assert owner.execute("SELECT count(*) FROM access.token WHERE last_used_at IS NOT NULL").fetchone() == (2,)

    # A token that is unknown, or withdrawn, is nobody.
    assert signed.get(f"{V1}/areas", headers={"Authorization": "Bearer stf2_made-up"}).status_code == 401
    assert signed.get(f"{V1}/areas", headers={"Authorization": "Bearer something-else"}).status_code == 401
    listed = {p["email"]: p for p in access.people(owner)}
    access.revoke(owner, listed[KARI]["tokens"][0]["id"])
    assert signed.get(f"{V1}/areas", headers=as_her).status_code == 401
    with pytest.raises(access.Refused, match="no token"):
        access.revoke(owner, listed[KARI]["tokens"][0]["id"])


def test_people_are_listed_from_the_command_line_and_not_by_the_service(owner, monkeypatch):
    monkeypatch.setenv("STIFLYT2_MIGRATE_URL", f"postgresql+psycopg:///{TEST_DB}")
    run = CliRunner()
    assert "may sign in, as editor" in run.invoke(cli, ["access", "add", KARI, " Kari   Nordmann ", "--role", "editor"]).output
    assert run.invoke(cli, ["access", "add", PER, "Kari Nordmann"]).exit_code == 1       # two people, one name
    assert "a role is one of" in run.invoke(cli, ["access", "add", PER, "Per Hansen", "--role", "boss"]).output
    token = run.invoke(cli, ["access", "token", KARI, "Claude on the laptop"]).output.strip()
    assert token.startswith("stf2_")
    listed = run.invoke(cli, ["access", "list"]).output
    assert "Kari Nordmann" in listed and "editor" in listed and "(mcp): Claude on the laptop, not used yet" in listed
    number = owner.execute("SELECT id FROM access.token").fetchone()[0]
    assert f"token {number} is withdrawn" in run.invoke(cli, ["access", "revoke", str(number)]).output
    assert "off the list" in run.invoke(cli, ["access", "remove", KARI]).output
    assert run.invoke(cli, ["access", "remove", KARI]).exit_code == 1
    # The service's own database role reads the list and cannot add itself to it.
    with psycopg.connect(role_dsn("stiflyt2_api")) as api, pytest.raises(psycopg.errors.InsufficientPrivilege):
        api.execute("INSERT INTO access.person (email, name, role, added_by) VALUES ('me@example.org', 'Me', 'editor', 'me')")
