"""Who may use the tool, and as what.

A person signs in with an e-mail address and has a name and a role. A token
lets a program act as a person. The service looks people and tokens up here;
only the command line, as the database's owner, changes who is listed.

What a person may use is a set of features (`stiflyt2.features`). A role is a
bundle of them, kept in the database, so that a new role needs no new code. A
person may be given a feature their role lacks, or be denied one it has. And a
feature as a whole is off (for nobody), given (to those whose role or own list
has it), or for all who are let in. So there are three things to look at when
somebody has or lacks something, and `why` says which of them it was.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace

from psycopg.rows import dict_row

from stiflyt2 import features as known_features
from stiflyt2.features import STATES

# A reader looks. Operations changes what is kept about signs and work, and not the routes. An
# editor, the route responsible, changes the routes, and may do what operations does.
ROLES = ("reader", "operations", "editor")
THROUGH = ("mcp", "api")                # how what comes in with a token is recorded
PREFIX = "stf2_"


class Refused(Exception):
    pass


ROLES = ("reader", "operations", "editor")      # the three there were at first: each may do what those before it may
# What those three give of themselves, where nobody has looked the role up (and what they were made as).
BUILT_IN = {"reader": frozenset(), "operations": frozenset({"signs"}), "editor": frozenset({"routes", "signs"})}


@dataclass(frozen=True)
class Person:
    name: str | None            # None: nobody in particular, on a server with no editor named
    role: str
    email: str | None = None
    through: str = "viewer"     # how this request came in: the map, an assistant (mcp), a script (api)
    really: str | None = None   # the role this one holds, while looking at the pages as one with a lesser role
    features: frozenset | None = None       # what this one may use. None: not looked up, so what the role gives of itself
    look_as: tuple = ()         # the roles this one may look at the pages as, each with what it may use

    def has(self, feature: str) -> bool:
        """Whether this one may use a feature. Nobody in particular may use none."""
        mine = self.features if self.features is not None else BUILT_IN.get(self.role, frozenset())
        return bool(self.name) and feature in mine

    @property
    def edits(self) -> bool:
        """Whether this is a route responsible: one who changes the routes."""
        return self.has("routes")

    @property
    def operates(self) -> bool:
        """Whether this one may change what is kept about signs. A route responsible may too."""
        return self.has("signs")


def lesser(role: str) -> list[str]:
    """Of the three first roles, those one with this role may look at the pages as: those that may do less."""
    return list(ROLES[:ROLES.index(role)]) if role in ROLES else []


def looking_as(person: Person | None, role: str | None) -> Person | None:
    """The person as one with a lesser role, for as long as they ask to be: to see what such a one sees and
    is refused. Only in the pages, only downwards, and only what they could do anyway: asking for a role one
    does not hold, or holds already, changes nothing. Lesser is a role whose features are some of one's own."""
    if person is None or not person.name or person.through != "viewer":
        return person
    if person.features is None:         # not looked up: the three first roles, as they stand to each other
        return replace(person, role=role, really=person.role) if role in lesser(person.role) else person
    found = dict(person.look_as).get(role)
    return person if found is None else replace(person, role=role, features=found, really=person.really or person.role)


def _bundles(conn) -> tuple[dict[str, str], dict[str, frozenset]]:
    """How each feature stands as a whole, and what each role is a bundle of."""
    said = dict(conn.execute("SELECT key, state FROM access.feature").fetchall())
    states = {key: said.get(key, f.state) for key, f in known_features.load().items()}
    roles = {name: frozenset(features) for name, features in conn.execute("SELECT name, features FROM access.role")}
    return states, roles


def _live(states: dict[str, str], wanted) -> frozenset:
    """What is left of a set of features when those that are off are taken out and those for all put in."""
    return frozenset(k for k, s in states.items() if s == "all" or (s == "given" and k in wanted))


def resolved(conn, who: Person) -> Person:
    """The person with what they may use looked up: their role's bundle, what they are given or denied
    themselves, and how each feature stands. And the roles they may look at the pages as."""
    states, roles = _bundles(conn)
    mine = set(roles.get(who.role, BUILT_IN.get(who.role, frozenset())))
    if who.email:
        for feature, given in conn.execute("SELECT feature, given FROM access.person_feature WHERE email = %s",
                                           (who.email,)).fetchall():
            (mine.add if given else mine.discard)(feature)
    has = _live(states, mine) if who.name else frozenset()
    look = tuple((name, _live(states, bundle)) for name, bundle in sorted(roles.items(), key=lambda r: (len(r[1]), r[0]))
                 if _live(states, bundle) < has)
    return replace(who, features=has, look_as=look)


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def person(conn, email: str) -> Person | None:
    """The listed person who signs in with this address, if there is one."""
    found = _rows(conn, "SELECT email, name, role FROM access.person WHERE email = %(e)s",
                  {"e": (email or "").strip().lower()})
    return resolved(conn, Person(found[0]["name"], found[0]["role"], found[0]["email"])) if found else None


def by_token(conn, token: str) -> Person | None:
    """The person a token acts as, and how what it does is recorded. None for a token unknown or withdrawn."""
    if not token.startswith(PREFIX):
        return None
    found = _rows(conn, """
        SELECT p.email, p.name, p.role, t.through FROM access.token t JOIN access.person p USING (email)
        WHERE t.digest = %(d)s AND t.revoked_at IS NULL""", {"d": _digest(token)})
    return resolved(conn, Person(found[0]["name"], found[0]["role"], found[0]["email"], found[0]["through"])) if found else None


def seen(conn, who: Person, token: str | None = None) -> None:
    """Note that a person was here now, and that a token was used."""
    conn.execute("UPDATE access.person SET last_seen_at = now() WHERE email = %s", (who.email,))
    if token:
        conn.execute("UPDATE access.token SET last_used_at = now() WHERE digest = %s", (_digest(token),))


# ---- Changing who is listed: from the command line, as the database's owner. ----

def people(conn) -> list[dict]:
    return _rows(conn, """
        SELECT p.email, p.name, p.role, p.added_at, p.added_by, p.last_seen_at,
               (SELECT coalesce(json_agg(json_build_object('feature', f.feature, 'given', f.given) ORDER BY f.feature), '[]')
                FROM access.person_feature f WHERE f.email = p.email) AS own,
               (SELECT coalesce(json_agg(json_build_object('id', t.id, 'through', t.through, 'label', t.label,
                       'last_used_at', t.last_used_at) ORDER BY t.id), '[]')
                FROM access.token t WHERE t.email = p.email AND t.revoked_at IS NULL) AS tokens
        FROM access.person p ORDER BY p.name""", {})


def add(conn, email: str, name: str, role: str, by: str) -> None:
    """List a person, or change the name or the role of one who is listed."""
    email, name = (email or "").strip().lower(), " ".join((name or "").split())
    known = [r["name"] for r in roles(conn)]
    if role not in known:
        raise Refused(f"a role is one of: {', '.join(known)}")
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


def roles(conn) -> list[dict]:
    """The roles there are: what each is a bundle of, and how many hold it."""
    return _rows(conn, """SELECT r.name, r.features, r.about,
                                 (SELECT count(*) FROM access.person p WHERE p.role = r.name)::int AS people
                          FROM access.role r ORDER BY cardinality(r.features), r.name""", {})


def _known(features: list[str]) -> list[str]:
    unknown = sorted(set(features) - set(known_features.load()))
    if unknown:
        raise Refused(f"no such feature: {', '.join(unknown)}. There are: {', '.join(sorted(known_features.load()))}")
    return sorted(set(features))


def set_role(conn, name: str, features: list[str], about: str | None = None) -> None:
    """Make a role, or change what one is a bundle of. Those who hold it have what it has from then on."""
    name = (name or "").strip().lower()
    conn.execute("""INSERT INTO access.role (name, features, about) VALUES (%s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET features = EXCLUDED.features,
                                                     about = coalesce(EXCLUDED.about, access.role.about)""",
                 (name, _known(features), about))


def drop_role(conn, name: str) -> None:
    held = conn.execute("SELECT count(*) FROM access.person WHERE role = %s", (name,)).fetchone()[0]
    if held:
        raise Refused(f"{held} {'person holds' if held == 1 else 'people hold'} the role {name}: give them another first")
    if name in ROLES:
        raise Refused(f"{name} is one of the three roles the tool counts on being there")
    if not conn.execute("DELETE FROM access.role WHERE name = %s", (name,)).rowcount:
        raise Refused(f"there is no role {name}")


def features(conn) -> list[dict]:
    """Every feature: what it lets one do, how it stands, which roles have it, and who has it apart from their role."""
    states, bundles = _bundles(conn)
    apart = _rows(conn, """SELECT f.feature, f.given, p.name FROM access.person_feature f JOIN access.person p USING (email)
                           ORDER BY p.name""", {})
    return [{"key": key, "title": f.title, "state": states[key],
             "roles": sorted(name for name, bundle in bundles.items() if key in bundle),
             "given": [a["name"] for a in apart if a["feature"] == key and a["given"]],
             "denied": [a["name"] for a in apart if a["feature"] == key and not a["given"]]}
            for key, f in sorted(known_features.load().items())]


def set_state(conn, key: str, state: str, by: str) -> None:
    """Say how a feature stands as a whole: off, given, or for all who are let in."""
    _known([key])
    if state not in STATES:
        raise Refused(f"a feature is one of: {', '.join(STATES)}")
    conn.execute("""INSERT INTO access.feature (key, state, by) VALUES (%s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state, by = EXCLUDED.by, at = now()""",
                 (key, state, by))


def grant(conn, email: str, key: str, given: bool | None, by: str) -> None:
    """Give one person a feature whatever their role says, deny them it, or (None) leave it to their role again."""
    email = (email or "").strip().lower()
    _known([key])
    if not conn.execute("SELECT 1 FROM access.person WHERE email = %s", (email,)).fetchone():
        raise Refused(f"{email} is not listed")
    if given is None:
        conn.execute("DELETE FROM access.person_feature WHERE email = %s AND feature = %s", (email, key))
    else:
        conn.execute("""INSERT INTO access.person_feature (email, feature, given, by) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (email, feature) DO UPDATE SET given = EXCLUDED.given, by = EXCLUDED.by, at = now()""",
                     (email, key, given, by))


def why(conn, email: str) -> list[dict]:
    """For one person and every feature: whether they have it, and which of the three things decided that."""
    email = (email or "").strip().lower()
    found = _rows(conn, "SELECT name, role FROM access.person WHERE email = %(e)s", {"e": email})
    if not found:
        raise Refused(f"{email} is not listed")
    states, bundles = _bundles(conn)
    own = dict(conn.execute("SELECT feature, given FROM access.person_feature WHERE email = %s", (email,)).fetchall())
    role = found[0]["role"]
    told = []
    for key in sorted(known_features.load()):
        if states[key] == "off":
            has, because = False, "the feature is off for everybody"
        elif states[key] == "all":
            has, because = True, "the feature is on for everybody who is let in"
        elif key in own:
            has, because = own[key], ("given to them by name" if own[key] else "denied them by name") + (
                f", though the role {role} has it" if not own[key] and key in bundles.get(role, ()) else "")
        else:
            has = key in bundles.get(role, ())
            because = f"the role {role} {'has' if has else 'does not have'} it"
        told.append({"feature": key, "has": has, "because": because})
    return told


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
