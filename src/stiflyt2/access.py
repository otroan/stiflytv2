"""Who may use the tool, and as what.

A person signs in with an e-mail address and has a name and a role. A token
lets a program act as a person. The service looks people and tokens up here;
only the command line, as the database's owner, changes who is listed.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace

from psycopg.rows import dict_row

# A reader looks. Operations changes what is kept about signs and work, and not the routes. An
# editor, the route responsible, changes the routes, and may do what operations does.
ROLES = ("reader", "operations", "editor")
THROUGH = ("mcp", "api")                # how what comes in with a token is recorded
PREFIX = "stf2_"


class Refused(Exception):
    pass


ROLES = ("reader", "operations", "editor")      # each may do what those before it may


@dataclass(frozen=True)
class Person:
    name: str | None            # None: nobody in particular, on a server with no editor named
    role: str
    email: str | None = None
    through: str = "viewer"     # how this request came in: the map, an assistant (mcp), a script (api)
    really: str | None = None   # the role this one holds, while looking at the pages as one with a lesser role

    @property
    def edits(self) -> bool:
        """Whether this is a route responsible: one who changes the routes."""
        return self.role == "editor" and bool(self.name)

    @property
    def operates(self) -> bool:
        """Whether this one may change what is kept about operations: signs, work. A route responsible may too."""
        return self.role in ("operations", "editor") and bool(self.name)


def lesser(role: str) -> list[str]:
    """The roles one with this role may look at the pages as: those that may do less."""
    return list(ROLES[:ROLES.index(role)]) if role in ROLES else []


def looking_as(person: Person | None, role: str | None) -> Person | None:
    """The person as one with a lesser role, for as long as they ask to be: to see what such a one sees and
    is refused. Only in the pages, only downwards, and only what they could do anyway: asking for a role one
    does not hold, or holds already, changes nothing."""
    if person is None or not person.name or person.through != "viewer" or role not in lesser(person.role):
        return person
    return replace(person, role=role, really=person.role)


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def person(conn, email: str) -> Person | None:
    """The listed person who signs in with this address, if there is one."""
    found = _rows(conn, "SELECT email, name, role FROM access.person WHERE email = %(e)s",
                  {"e": (email or "").strip().lower()})
    return Person(found[0]["name"], found[0]["role"], found[0]["email"]) if found else None


def by_token(conn, token: str) -> Person | None:
    """The person a token acts as, and how what it does is recorded. None for a token unknown or withdrawn."""
    if not token.startswith(PREFIX):
        return None
    found = _rows(conn, """
        SELECT p.email, p.name, p.role, t.through FROM access.token t JOIN access.person p USING (email)
        WHERE t.digest = %(d)s AND t.revoked_at IS NULL""", {"d": _digest(token)})
    return Person(found[0]["name"], found[0]["role"], found[0]["email"], found[0]["through"]) if found else None


def seen(conn, who: Person, token: str | None = None) -> None:
    """Note that a person was here now, and that a token was used."""
    conn.execute("UPDATE access.person SET last_seen_at = now() WHERE email = %s", (who.email,))
    if token:
        conn.execute("UPDATE access.token SET last_used_at = now() WHERE digest = %s", (_digest(token),))


# ---- Changing who is listed: from the command line, as the database's owner. ----

def people(conn) -> list[dict]:
    return _rows(conn, """
        SELECT p.email, p.name, p.role, p.added_at, p.added_by, p.last_seen_at,
               (SELECT coalesce(json_agg(json_build_object('id', t.id, 'through', t.through, 'label', t.label,
                       'last_used_at', t.last_used_at) ORDER BY t.id), '[]')
                FROM access.token t WHERE t.email = p.email AND t.revoked_at IS NULL) AS tokens
        FROM access.person p ORDER BY p.name""", {})


def add(conn, email: str, name: str, role: str, by: str) -> None:
    """List a person, or change the name or the role of one who is listed."""
    email, name = (email or "").strip().lower(), " ".join((name or "").split())
    if role not in ROLES:
        raise Refused(f"a role is one of: {', '.join(ROLES)}")
    if "@" not in email or not name:
        raise Refused("give the e-mail address the person signs in with, and their name")
    taken = _rows(conn, "SELECT email FROM access.person WHERE name = %(n)s AND email <> %(e)s",
                  {"n": name, "e": email})
    if taken:
        raise Refused(f"{name} is the name of {taken[0]['email']} already: changes are recorded by name, "
                      "so two people cannot share one")
    conn.execute("""INSERT INTO access.person (email, name, role, added_by) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role""",
                 (email, name, role, by))


def remove(conn, email: str) -> None:
    """Take a person off the list, and their tokens with them. What they changed stays under their name."""
    if not conn.execute("DELETE FROM access.person WHERE email = %s", ((email or "").strip().lower(),)).rowcount:
        raise Refused(f"{email} is not listed")


def new_token(conn, email: str, through: str, label: str) -> str:
    """Make a token that acts as a person. It is returned once, and only its digest is kept."""
    who = person(conn, email)
    if not who:
        raise Refused(f"{email} is not listed")
    if through not in THROUGH:
        raise Refused(f"a token is for one of: {', '.join(THROUGH)}")
    if not (label or "").strip():
        raise Refused("say what the token is for")
    token = PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO access.token (digest, email, through, label) VALUES (%s, %s, %s, %s)",
                 (_digest(token), who.email, through, label.strip()))
    return token


def revoke(conn, token_id: int) -> None:
    if not conn.execute("UPDATE access.token SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                        (token_id,)).rowcount:
        raise Refused(f"no token {token_id} is in use")
