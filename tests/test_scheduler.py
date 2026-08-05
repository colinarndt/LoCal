"""Daily-run timing. The case that matters is a laptop that was asleep."""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar import scheduler

NOW = dt.datetime(2026, 7, 29, 9, 0, tzinfo=dt.timezone.utc)


def _conn(marks: dict[str, str | None], polled: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE account (handle TEXT PRIMARY KEY, is_polled INTEGER, "
                 "last_polled_at TEXT)")
    conn.executemany("INSERT INTO account VALUES (?,?,?)",
                     [(h, int(polled), m) for h, m in marks.items()])
    return conn


def _ago(hours: float) -> str:
    return (NOW - dt.timedelta(hours=hours)).isoformat()


def _web_conn(rows: list[tuple]) -> sqlite3.Connection:
    conn = _conn({})
    conn.execute(
        "CREATE TABLE web_source (id INTEGER PRIMARY KEY, enabled INTEGER, "
        "source_type TEXT, last_checked_at TEXT)")
    conn.executemany("INSERT INTO web_source VALUES (?,?,?,?)", rows)
    return conn


# --- the lid-closed case ----------------------------------------------------

def test_a_missed_night_runs_on_wake_rather_than_waiting_for_the_next_window():
    """Asleep through the scheduled hour, opened at 09:00 -- run now, not at 03:00."""
    conn = _conn({"venue": _ago(34)})
    assert scheduler.due(conn, now=NOW) is True


def test_a_recent_poll_is_not_due():
    conn = _conn({"venue": _ago(3)})
    assert scheduler.due(conn, now=NOW) is False


def test_a_recent_website_check_does_not_delay_an_overdue_instagram_poll():
    conn = _conn({"venue": _ago(30)})
    conn.execute(
        "CREATE TABLE web_source (id INTEGER PRIMARY KEY, enabled INTEGER, "
        "source_type TEXT, last_checked_at TEXT)")
    conn.execute("INSERT INTO web_source VALUES (1,1,'venue',?)", (_ago(1),))
    assert scheduler.due(conn, now=NOW) is True


def test_due_exactly_on_the_interval():
    conn = _conn({"venue": _ago(24)})
    assert scheduler.due(conn, now=NOW) is True


# --- what must not trigger a paid run ---------------------------------------

def test_a_never_polled_rotation_does_not_self_start():
    """First run hits every account at full history depth. That stays a decision."""
    conn = _conn({"venue": None})
    assert scheduler.due(conn, now=NOW) is False
    assert scheduler.describe(conn, now=NOW) == "never run"


def test_an_empty_rotation_is_not_due():
    assert scheduler.due(_conn({}), now=NOW) is False


def test_unpolled_accounts_are_ignored():
    """A stale candidate nobody approved must not look like an overdue run."""
    conn = _conn({"candidate": _ago(500)}, polled=False)
    assert scheduler.due(conn, now=NOW) is False


def test_a_new_account_does_not_make_the_rotation_look_overdue():
    """MAX not MIN: one unpolled newcomer alongside a fresh poll is not due."""
    conn = _conn({"newbie": None, "venue": _ago(2)})
    assert scheduler.due(conn, now=NOW) is False


def test_a_malformed_mark_does_not_crash_the_scheduler_thread():
    conn = _conn({"venue": "not a timestamp"})
    assert scheduler.due(conn, now=NOW) is False


def test_a_naive_timestamp_is_read_as_utc():
    conn = _conn({"venue": (NOW - dt.timedelta(hours=30)).replace(tzinfo=None).isoformat()})
    assert scheduler.due(conn, now=NOW) is True


# --- independent website clocks --------------------------------------------

def test_a_new_venue_page_is_due_without_a_manual_first_fetch():
    conn = _web_conn([(1, 1, "venue", None)])
    assert scheduler.due_venue_source_ids(conn, now=NOW) == [1]


def test_a_recent_venue_does_not_mask_an_overdue_or_new_venue():
    conn = _web_conn([
        (1, 1, "venue", _ago(2)),
        (2, 1, "venue", _ago(30)),
        (3, 1, "venue", None),
    ])
    assert scheduler.due_venue_source_ids(conn, now=NOW) == [2, 3]


def test_website_intervals_are_independent_by_source_type():
    conn = _web_conn([
        (1, 1, "venue", _ago(10)),
        (2, 1, "performer", _ago(10)),
        (3, 0, "performer", _ago(50)),
    ])
    assert scheduler.due_venue_source_ids(conn, interval_hours=24, now=NOW) == []
    assert scheduler.due_performer_source_ids(conn, interval_hours=6, now=NOW) == [2]


def test_a_bad_website_timestamp_cannot_strand_the_source():
    conn = _web_conn([(1, 1, "venue", "not a timestamp")])
    assert scheduler.due_venue_source_ids(conn, now=NOW) == [1]


# --- the menu's "last run" line ---------------------------------------------

def test_describe_reads_naturally_at_each_scale():
    assert scheduler.describe(_conn({"v": _ago(0.5)}), now=NOW) == "30m ago"
    assert scheduler.describe(_conn({"v": _ago(5)}), now=NOW) == "5h ago"
    assert scheduler.describe(_conn({"v": _ago(50)}), now=NOW) == "2d ago"
