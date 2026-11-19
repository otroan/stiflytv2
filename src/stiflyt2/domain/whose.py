"""Who maintains a route: the names Turrutebasen gives, and which of them are the same.

Turrutebasen spells an association in more than one way ("DNT | DNT Oslo og
omegn", "DNT Oslo og Omegn", "DNT | DNT Oslo og Omegn"). Names are compared
without case, without spaces at the ends or doubled, and without the
"DNT | " some of them begin with.

Many routes say only "DNT", all over the country. Such a route counts as DNT
Oslo og omegn's where it meets a route that names DNT Oslo og omegn and none
that names another association (decided 2026-11-20): in Femundsmarka and
Rondane they lie among its routes, in Hardangervidda some lie among DNT
Telemark's or Bergen og Hordaland Turlag's. Where it meets both, a person
says whose it is.
"""
from __future__ import annotations

import re

HOME = "dnt oslo og omegn"
BARE = "dnt"
NORM_SQL = r"lower(regexp_replace(regexp_replace(btrim({m}), '\s+', ' ', 'g'), '^dnt ?\| ?', '', 'i'))"
MEETS_M = 5         # a route meets another where a segment of the one lies this near a segment of the other

_kept: dict = {}


def norm(name: str | None) -> str:
    """A maintainer's name as it is compared."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    return re.sub(r"^dnt ?\| ?", "", name, flags=re.IGNORECASE).lower()


def kin(name: str | None) -> list[str] | None:
    """The names, as they are compared, that count as this maintainer. None for nobody in particular."""
    return [norm(name)] if name else None


def is_home(name: str | None) -> bool:
    return bool(name) and norm(name) == HOME


def bare(names) -> bool:
    """Whether a route's maintainers say only "DNT"."""
    return {norm(n) for n in names or []} == {BARE}


def home_dnt(conn) -> list[str]:
    """The route candidates that say only "DNT" and are DNT Oslo og omegn's: they meet a route that names it, and
    none that names another association. Worked out once for each build of the index."""
    built = conn.execute("SELECT max(built_at)::text FROM source_index.index_state").fetchone()[0]
    if _kept.get("built") != built:
        norm_m = NORM_SQL.format(m="m")
        _kept.update(built=built, home=sorted(i for (i,) in conn.execute(f"""
            WITH named AS (
                SELECT r.id, ARRAY(SELECT DISTINCT {norm_m} FROM unnest(r.maintainers) m) AS names
                FROM source_index.route r WHERE r.area_prefix IS NOT NULL),
            meets AS (
                SELECT b.id AS bare, %(home)s = ANY(o.names) AS home,
                       EXISTS (SELECT 1 FROM unnest(o.names) n WHERE n NOT IN (%(bare)s, %(home)s)) AS other
                FROM named b
                JOIN source_index.route_segment a ON a.route_id = b.id
                JOIN source_index.trail_segment ta ON ta.id = a.segment_id
                JOIN source_index.trail_segment tb ON ST_DWithin(ta.geom, tb.geom, %(near)s)
                JOIN source_index.route_segment s ON s.segment_id = tb.id AND s.route_id <> b.id
                JOIN named o ON o.id = s.route_id
                WHERE b.names = ARRAY[%(bare)s]::text[])
            SELECT bare FROM meets GROUP BY bare HAVING bool_or(home) AND NOT bool_or(other)""",
            {"home": HOME, "bare": BARE, "near": MEETS_M})))
    return _kept["home"]


def params(conn, name: str | None) -> dict:
    """What `sql` asks for, for one maintainer."""
    return {"kin": kin(name), "home": home_dnt(conn) if is_home(name) else []}


def sql(column: str, route: str | None = None) -> str:
    """SQL for whether an array of names has the maintainer among them (or %(kin)s is NULL: anybody).

    `route`, where given, is the column of the route candidate's id: one that says only "DNT" and is DNT Oslo
    og omegn's (%(home)s, from `params`) counts as theirs.
    """
    named = f"EXISTS (SELECT 1 FROM unnest({column}) m WHERE {NORM_SQL.format(m='m')} = ANY(%(kin)s::text[]))"
    also = f" OR {route} = ANY(%(home)s::text[])" if route else ""
    return f"(%(kin)s::text[] IS NULL OR {named}{also})"
