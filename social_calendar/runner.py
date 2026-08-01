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

from . import avatars, discovery, geo, pipeline, spend, websites


HISTORY_WINDOW = "30 days"


def _window_for(mark: str | None) -> str:
    """One account's window, from its own last_polled_at.

    A never-polled account needs history; one polled yesterday needs yesterday.
    The 2-day overlap absorbs clock skew and a missed scheduled run -- which is
    what makes a skipped day self-healing rather than a permanent gap.
    """
    if mark is None:
        return HISTORY_WINDOW
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(mark)).days + 2
    return f"{max(age, 3)} days"


def fetch_windows(conn: sqlite3.Connection, handles: list[str],
                  history_days: int | None = None) -> list[tuple[str, list[str]]]:
    """Group handles by their own window, widest first.

    Collapsing a batch to one window (the oldest mark in it) meant a single
    never-polled account dragged all 19 back to 30 days of history, re-fetching
    the rest at full price. Grouping means each account asks only for what it
    is actually missing.

    One Apify run per group, not per account: `onlyPostsNewerThan` is run-level,
    applied to every directUrl in the run. Accounts polled together share a mark,
    so this is normally two groups -- new accounts, then everybody else.
    """
    if not handles:
        return []
    if history_days:
        return [(f"{history_days} days", list(handles))]

    marks = {r["handle"]: r["last_polled_at"] for r in conn.execute(
        f"SELECT handle, last_polled_at FROM account WHERE handle IN "
        f"({','.join('?' * len(handles))})", handles).fetchall()}

    groups: dict[str, list[str]] = {}
    for h in handles:
        # A handle with no account row has never been polled by definition.
        groups.setdefault(_window_for(marks.get(h)), []).append(h)

    # Widest window first: new accounts are the ones with nothing on the
    # calendar yet, so they are what you are waiting to see.
    return sorted(groups.items(), key=lambda kv: -int(kv[0].split()[0]))


def relabel(conn: sqlite3.Connection) -> None:
    """Canonical venue labels, so the dropdown shows one tidy name per venue."""
    for r in conn.execute("SELECT venue_key, address FROM venue").fetchall():
        conn.execute("UPDATE venue SET display_name=? WHERE venue_key=?",
                     (geo.label_for(r["venue_key"], r["address"]), r["venue_key"]))


def poll(conn: sqlite3.Connection, source, extractor, handles: list[str],
         limit: int = 20, newer_than: str | None = None,
         max_posts: int | None = None, log=lambda *_: None,
         groups: list[tuple[str, list[str]]] | None = None,
         website_source_ids: list[int] | None = None) -> dict:
    """Run every stage for `handles`. Returns per-stage counts.

    `groups` fetches each set of handles under its own window (see
    `fetch_windows`); `newer_than` puts them all under one. Only the ingest
    stage repeats -- extraction, series, dedupe and geocode see the union once,
    because they work off what is in the database rather than what was fetched.
    """
    stats: dict = {}

    # Structured calendars go first. Their authoritative event records become
    # candidates for the Instagram caption pre-match below, which is how a
    # clear repeat announcement avoids a vision call in the same run.
    stats["websites"] = websites.poll_all(
        conn, source_ids=website_source_ids, log=log, extractor=extractor)
    # Website-only runs do not enter pipeline.process, so their nano call still
    # needs an explicit drain into the spend ledger here.
    spend.drain_into(conn, getattr(extractor, "meter", None))
    conn.commit()
    log(f"websites: {stats['websites']}")

    batches = groups if groups is not None else [(newer_than, list(handles))]
    stats["ingested"] = 0
    for window, batch in (batches if handles else []):
        if len(batches) > 1:
            log(f"fetching {len(batch)} account(s), window {window}")
        stats["ingested"] += pipeline.ingest(conn, source, batch, limit,
                                             newer_than=window)
    log(f"ingested {stats['ingested']} new posts")

    stats["processed"] = (pipeline.process(conn, extractor, max_posts) if handles else
                          {"seen": 0, "gated_out": 0, "gate_skipped": 0,
                           "vision_skipped": 0, "events": 0, "errors": 0, "flagged": 0})
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
