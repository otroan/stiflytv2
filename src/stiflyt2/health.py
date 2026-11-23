"""Whether what runs by itself has run: the nightly check of the sources, and the copy of what is yours.

Read by the page, so that whoever works in it gets to know when a night has
gone wrong; and sent, where a way to send is set up (`alert`), so that it is
known without opening the page.

A source that could not be reached (a service down or too busy) is asked again
the next night: it is only warned of when it has not caught up by itself.
"""
from __future__ import annotations

import json
import re
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
    if checked["at"] is not None:
        warnings += _not_caught_up(conn, now)
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


def _not_caught_up(conn, now: datetime) -> list[str]:
    """Sources last not reached, that have gone a day past their time without being reached since."""
    from stiflyt2.importer.refresh import EVERY_DAYS

    said = []
    for r in _rows(conn, """
            SELECT DISTINCT ON (source_key, scope) source_key, scope, outcome, left(detail, 200) AS detail,
                   (SELECT max(o.finished_at) FROM import_control.check_log o WHERE o.source_key = c.source_key
                      AND o.scope = c.scope AND o.outcome IN ('unchanged', 'imported')) AS reached
            FROM import_control.check_log c ORDER BY source_key, scope, finished_at DESC""", {}):
        if r["outcome"] != "unreached":
            continue
        if r["reached"] is not None and now - r["reached"] <= timedelta(days=EVERY_DAYS.get(r["source_key"], 1) + 1):
            continue
        since = f"for {_ago(r['reached'], now)}" if r["reached"] else "since it was first loaded"
        said.append(f"{r['source_key']} {r['scope']} has not been reached {since} ({r['detail']}). "
                    "What was fetched before is still in use, and it is asked again every night.")
    return said


def alert(warnings: list[str], state: Path, now: datetime | None = None) -> str:
    """Send what there is to warn of to whoever is set up to hear it: by e-mail, to a web address, or both.

    The same warning is sent at most once in 20 hours (the numbers in it, as hours ago, may differ).
    Says in a sentence what was done. Raises when a way to send is set up and sending fails.
    """
    from stiflyt2.config import settings

    now = now or datetime.now(UTC)
    def key(w: str) -> str:
        return re.sub(r"\d+", "#", w)
    sent = json.loads(state.read_text()) if state.is_file() else {}
    sent = {k: at for k, at in sent.items() if now - datetime.fromisoformat(at) < timedelta(hours=20)}
    new = [w for w in warnings if key(w) not in sent]
    if not new:
        return "nothing to send" if not warnings else "nothing new to send: the same was sent within 20 hours"
    ways = []
    where = f"\n\n{settings.public_url}" if settings.public_url else ""
    text = "\n\n".join(new) + where
    if settings.alert_url:
        import httpx

        httpx.post(settings.alert_url, content=text.encode(), headers={"Title": "Stiflyt v2: something to look at"},
                   timeout=30).raise_for_status()
        ways.append(settings.alert_url.split("//")[-1].split("/")[0])
    if settings.alert_to and settings.smtp_host:
        import smtplib
        from email.message import EmailMessage

        mail = EmailMessage()
        mail["Subject"], mail["From"], mail["To"] = ("Stiflyt v2: something to look at",
                                                     settings.alert_from or settings.smtp_user, settings.alert_to)
        mail.set_content(text)
        with (smtplib.SMTP_SSL if settings.smtp_port == 465 else smtplib.SMTP)(settings.smtp_host, settings.smtp_port,
                                                                                timeout=30) as smtp:
            if settings.smtp_port != 465:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(mail)
        ways.append(settings.alert_to)
    if not ways:
        return (f"{len(new)} to warn of, not sent: no way to send is set up "
                "(STIFLYT2_ALERT_TO with STIFLYT2_SMTP_HOST, or STIFLYT2_ALERT_URL)")
    sent.update({key(w): now.isoformat() for w in new})
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(sent, indent=1))
    return f"sent {len(new)} to warn of to {', '.join(ways)}"
