"""One pass of the whole thing: poll -> extract -> series -> dedupe -> geocode.

Both `cli run-once` and the /discover "fetch now" button call `poll()`. It lives
here rather than in cli.py so the web UI can reuse it without importing the CLI,
and so the stage ORDER is stated once -- it matters:

  * series before dedupe, or generated occurrences survive as duplicates until
    the next run
  * geocode after extraction, because `venue` rows are built from venue_keys
    that only exist once extraction has written them
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from . import avatars, discovery, geo, pipeline


def fetch_window(conn: sqlite3.Connection, handles: list[str],
                 history_days: int | None = None) -> tuple[str, str]:
    """How far back to fetch, as (newer_than, why).

    A never-polled account needs history; one polled yesterday needs yesterday.
    The 2-day overlap absorbs clock skew and a missed scheduled run -- which is
    what makes a skipped day self-healing rather than a permanent gap.
    """
    if history_days:
        return f"{history_days} days", f"explicit {history_days}-day window"

    marks = [r["last_polled_at"] for r in conn.execute(
        f"SELECT last_polled_at FROM account WHERE handle IN "
        f"({','.join('?' * len(handles))})", handles).fetchall()] if handles else []

    if not marks or any(m is None for m in marks):
        return "30 days", "never-polled account(s) -> 30 days of history"

    age = (dt.datetime.now(dt.timezone.utc)
           - dt.datetime.fromisoformat(min(marks))).days + 2
    days = max(age, 3)
    return f"{days} days", f"incremental -> posts newer than {days} days"


def relabel(conn: sqlite3.Connection) -> None:
    """Canonical venue labels, so the dropdown shows one tidy name per venue."""
    for r in conn.execute("SELECT venue_key, address FROM venue").fetchall():
        conn.execute("UPDATE venue SET display_name=? WHERE venue_key=?",
                     (geo.label_for(r["venue_key"], r["address"]), r["venue_key"]))


def poll(conn: sqlite3.Connection, source, extractor, handles: list[str],
         limit: int = 20, newer_than: str | None = None,
         max_posts: int | None = None, log=lambda *_: None) -> dict:
    """Run every stage for `handles`. Returns per-stage counts."""
    stats: dict = {}

    stats["ingested"] = pipeline.ingest(conn, source, handles, limit,
                                        newer_than=newer_than)
    log(f"ingested {stats['ingested']} new posts")

    stats["processed"] = pipeline.process(conn, extractor, max_posts)
    log(f"processing: {stats['processed']}")

    stats["series"] = pipeline.expand_series(conn)
    stats["dedupe"] = pipeline.rebuild_dedupe(conn)
    log(f"series: {stats['series']}  dedupe: {stats['dedupe']}")

    # New accounts bring new venues. Idempotent -- only venue_keys still missing
    # coordinates are queried, at Nominatim's 1 req/sec.
    stats["geocode"] = geo.geocode_all(conn)
    relabel(conn)
    log(f"geocode: {stats['geocode']}")

    # Surface newly-tagged accounts and give them avatars, so /discover fills in
    # without a separate pass. Idempotent: a steady-state run costs nothing.
    discovery.stage(conn, discovery.rank_tagged(conn))
    client = getattr(source, "client", None)
    stats["avatars"] = avatars.backfill(conn, client) if client else 0
    log(f"avatars: {stats['avatars']}")

    return stats
