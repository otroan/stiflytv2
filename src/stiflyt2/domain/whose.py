"""Who maintains a route: the names Turrutebasen gives, and which of them are the same.

Turrutebasen spells an association in more than one way ("DNT | DNT Oslo og
omegn", "DNT Oslo og Omegn", "DNT | DNT Oslo og Omegn"), and many stretches
say only "DNT". Names are compared without case, without spaces at the ends
or doubled, and without the "DNT | " some of them begin with. And "DNT"
alone counts as DNT Oslo og omegn's (decided 2026-11-20): where a route of
its areas says only DNT, it is DNT Oslo og omegn's to look after.
"""
from __future__ import annotations

import re

HOME = "dnt oslo og omegn"


def norm(name: str | None) -> str:
    """A maintainer's name as it is compared."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    return re.sub(r"^dnt ?\| ?", "", name, flags=re.IGNORECASE).lower()


def kin(name: str | None) -> list[str] | None:
    """The names, as they are compared, that count as this maintainer. None for nobody in particular."""
    if not name:
        return None
    said = norm(name)
    return [said, "dnt"] if said == HOME else [said]


def sql(column: str) -> str:
    """SQL for whether an array of names has one of %(kin)s among them (or %(kin)s is NULL: anybody)."""
    one = r"lower(regexp_replace(regexp_replace(btrim(m), '\s+', ' ', 'g'), '^dnt ?\| ?', '', 'i'))"
    return f"(%(kin)s::text[] IS NULL OR EXISTS (SELECT 1 FROM unnest({column}) m WHERE {one} = ANY(%(kin)s::text[])))"
