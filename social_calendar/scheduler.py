"""When the daily run should happen, for a process that is not always running.

The menubar app replaces cron, which means inheriting cron's hardest problem
without inheriting its solution: a laptop asleep at 03:00 never fires a 03:00
timer, and nothing catches up afterwards.

So this does not schedule against a clock. It asks "how long since anything was
actually polled?" and runs when that exceeds the interval. Waking at 09:00 after
a closed lid answers "34 hours" and triggers immediately; the alternative fires
at 03:00 the following morning and quietly loses a day.

`last_polled_at` is the same mark `runner._window_for` reads to size the fetch
window, so a late run also asks the provider for exactly the stretch it missed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

DEFAULT_INTERVAL_HOURS = 24

# How often the app wakes to re-evaluate. Short enough that a laptop opened
# mid-morning starts its run promptly, long enough to be free.
CHECK_EVERY_SECONDS = 600


def last_poll(conn: sqlite3.Connection) -> dt.datetime | None:
    """Most recent poll across the rotation, or None if nothing has run.

    MAX, not MIN: the question is "did we poll at all recently", not "is every
    account current". A newly approved account with no mark of its own must not
    look like an overdue run for everyone.
    """
    row = conn.execute(
        "SELECT MAX(last_polled_at) FROM account "
        "WHERE is_polled = 1 AND last_polled_at IS NOT NULL").fetchone()
    marks = [row[0]] if row and row[0] else []
    # Older unit-test schemas and pre-migration databases may not have website
    # sources yet, so the Instagram clock remains a valid fallback.
    try:
        web = conn.execute(
            "SELECT MAX(last_checked_at) FROM web_source "
            "WHERE enabled=1 AND last_checked_at IS NOT NULL").fetchone()
        if web and web[0]:
            marks.append(web[0])
    except sqlite3.OperationalError:
        pass
    if not marks:
        return None
    try:
        parsed = [dt.datetime.fromisoformat(value) for value in marks]
        parsed = [value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
                  for value in parsed]
        stamp = max(parsed)
    except ValueError:
        return None
    # Marks are written in UTC, but tolerate a naive value rather than crashing
    # the scheduler thread over one malformed row.
    return stamp


def hours_since_poll(conn: sqlite3.Connection, now: dt.datetime | None = None) -> float | None:
    mark = last_poll(conn)
    if mark is None:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - mark).total_seconds() / 3600.0


def due(conn: sqlite3.Connection, interval_hours: float = DEFAULT_INTERVAL_HOURS,
        now: dt.datetime | None = None) -> bool:
    """Is a run owed?

    A rotation that has never been polled is *not* due. The first run costs real
    money against every approved account at full history depth, so it stays an
    explicit decision -- the app does not spend on its own the first time it is
    launched.
    """
    elapsed = hours_since_poll(conn, now)
    if elapsed is None:
        return False
    return elapsed >= interval_hours


def describe(conn: sqlite3.Connection, now: dt.datetime | None = None) -> str:
    """Human phrasing for the menu's "last run" line."""
    elapsed = hours_since_poll(conn, now)
    if elapsed is None:
        return "never run"
    if elapsed < 1:
        return f"{int(elapsed * 60)}m ago"
    if elapsed < 24:
        return f"{int(elapsed)}h ago"
    return f"{int(elapsed / 24)}d ago"
