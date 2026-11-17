"""Answers that hold as long as the mapped lines do, kept for as long as the process runs.

Laying a stretch against the mapped paths and roads is slow, and the answer
depends only on the stretch's line and on the mapped lines. A revision of a
stretch never changes its line, so an answer kept under the revision's id
holds until the mapped lines are built anew. After a change only the
stretches changed are worked out again. Nothing here is kept between
processes, and nothing is lost by emptying it.
"""
from __future__ import annotations

_built = None
_kept: dict[str, dict] = {}
MOST = 200_000


def answers(conn, kind: str) -> dict:
    """The answers of one kind kept so far: a dict to look in and add to. Empty after the mapped lines were rebuilt."""
    global _built
    built = conn.execute("SELECT max(built_at) FROM source_index.index_state").fetchone()[0]
    if built != _built:
        _kept.clear()
        _built = built
    box = _kept.setdefault(kind, {})
    if len(box) > MOST:
        box.clear()
    return box
