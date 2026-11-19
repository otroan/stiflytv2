"""Whether what runs by itself has run: the nightly check of the sources, and the copy of what is yours.

Read by the page, so that whoever works in it gets to know when a night has
gone wrong. Nothing here sends anything anywhere.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg.rows import dict_row

LATE = timedelta(hours=30)      # a nightly job not heard from for this long has missed a night


def _rows(conn, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _ago(then: datetime, now: datetime) -> str:
    hours = (now - then).total_seconds() / 3600
    return f"{hours:.0f} hours" if hours < 72 else f"{hours / 24:.0f} days"


def status(conn, backups: Path, now: datetime | None = None) -> dict:
    """When the sources were last checked and a copy last written, and what there is to warn of, in sentences."""
    now = now or datetime.now(UTC)
    database = conn.info.dbname
    warnings = []
    checked, = _rows(conn, """
        SELECT max(finished_at) AS at,
               count(*) FILTER (WHERE outcome IN ('error', 'rejected')
                                AND finished_at > now() - make_interval(hours => 30))::int AS failed
        FROM import_control.check_log""", {})
    if checked["at"] is None:
        warnings.append("The sources have never been checked for news: the nightly check is not running.")
    elif now - checked["at"] > LATE:
        warnings.append(f"The sources were last checked for news {_ago(checked['at'], now)} ago: "
                        "the nightly check is not running.")
    elif checked["failed"]:
        bad = _rows(conn, """
            SELECT DISTINCT ON (source_key, scope) source_key, scope, outcome, left(detail, 200) AS detail
            FROM import_control.check_log WHERE finished_at > now() - make_interval(hours => 30)
            ORDER BY source_key, scope, finished_at DESC""", {})
        bad = [b for b in bad if b["outcome"] in ("error", "rejected")]
        if bad and bad[0]["outcome"] == "rejected":
            # A delivery that was refused: said as what it means for the one who works here, and without
            # the way round the check, which is for whoever looks after the server to decide on.
            why = (bad[0]["detail"] or "").split("; set STIFLYT2_")[0]
            warnings.append(f"A new delivery of {bad[0]['source_key']} was refused last night, as it looks faulty "
                            f"({why}). The one before it is still in use, and nothing of yours is changed."
                            + (f" {len(bad) - 1} more of the sources could not be checked." if len(bad) > 1 else ""))
        elif bad:
            warnings.append(f"The nightly check failed for {len(bad)} of the sources, first "
                            f"{bad[0]['source_key']} {bad[0]['scope']}: {bad[0]['detail']}")
    copies = sorted(backups.glob(f"overlay-{database}-*.dump")) if backups.is_dir() else []
    copied = datetime.fromtimestamp(copies[-1].stat().st_mtime, UTC) if copies else None
    if copied is None:
        warnings.append("No copy of your places and routes has been written: the nightly copy is not running.")
    elif now - copied > LATE:
        warnings.append(f"The last copy of your places and routes is {_ago(copied, now)} old: "
                        "the nightly copy is not running.")
    for job in _rows(conn, """
            SELECT what, message, at FROM feedback.entry
            WHERE through = 'job' AND kind = 'failed' AND seen_at IS NULL ORDER BY id DESC LIMIT 3""", {}):
        warnings.append(f"The {job['what']} failed on {job['at']:%-d %b at %H:%M}: {job['message'][:200]}")
    return {"checked_at": checked["at"], "copied_at": copied, "copies": len(copies), "warnings": warnings}
