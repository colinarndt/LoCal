"""Fetch-window grouping. The money stage: a wrong window re-fetches at full price."""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar.runner import fetch_windows


def _conn(marks: dict[str, str | None]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE account (handle TEXT PRIMARY KEY, last_polled_at TEXT)")
    conn.executemany("INSERT INTO account VALUES (?,?)", list(marks.items()))
    return conn


def _mark(day: int, hour: int = 12, microsecond: int = 0) -> str:
    return dt.datetime(2026, 9, day, hour, 0, 0, microsecond,
                       tzinfo=dt.timezone.utc).isoformat()


# --- the case that prompted this: one new account must not widen the rest -----

def test_never_polled_account_does_not_widen_the_others():
    conn = _conn({"newbie": None, "a": _mark(4), "b": _mark(4)})
    groups = dict(fetch_windows(conn, ["newbie", "a", "b"]))
    assert groups["30 days"] == ["newbie"]
    assert sorted(groups["2026-09-04T06:00:00Z"]) == ["a", "b"]


def test_widest_window_is_fetched_first():
    conn = _conn({"newbie": None, "a": _mark(4)})
    assert [w for w, _ in fetch_windows(conn, ["newbie", "a"])] == \
        ["30 days", "2026-09-04T06:00:00Z"]


# --- window arithmetic -------------------------------------------------------

def test_cutoff_is_six_hours_before_last_successful_poll():
    conn = _conn({"a": _mark(1, 15)})
    assert fetch_windows(conn, ["a"]) == [("2026-09-01T09:00:00Z", ["a"])]


def test_cutoff_has_no_three_day_floor():
    conn = _conn({"a": _mark(5, 8)})
    assert fetch_windows(conn, ["a"]) == [("2026-09-05T02:00:00Z", ["a"])]


def test_accounts_polled_together_share_one_group():
    # Old versions wrote one _now() value per account. Ignore those harmless
    # microsecond differences so an upgrade does not create four Apify runs.
    conn = _conn({h: _mark(4, microsecond=i) for i, h in enumerate("abcd")})
    assert fetch_windows(conn, list("abcd")) == \
        [("2026-09-04T06:00:00Z", ["a", "b", "c", "d"])]


def test_missed_runs_catch_up_from_the_old_successful_mark():
    conn = _conn({"old": _mark(1), "recent": _mark(4)})
    assert fetch_windows(conn, ["recent", "old"]) == [
        ("2026-09-01T06:00:00Z", ["old"]),
        ("2026-09-04T06:00:00Z", ["recent"]),
    ]


def test_zulu_mark_is_accepted_and_normalized():
    conn = _conn({"a": "2026-09-05T12:34:56Z"})
    assert fetch_windows(conn, ["a"]) == [("2026-09-05T06:34:56Z", ["a"])]


# --- edges -------------------------------------------------------------------

def test_explicit_history_days_overrides_every_window():
    conn = _conn({"newbie": None, "a": _mark(4)})
    assert fetch_windows(conn, ["newbie", "a"], history_days=90) == \
        [("90 days", ["newbie", "a"])]


def test_handle_with_no_account_row_counts_as_never_polled():
    conn = _conn({"a": _mark(4)})
    assert dict(fetch_windows(conn, ["a", "ghost"]))["30 days"] == ["ghost"]


def test_no_handles_means_no_fetch():
    # An empty rotation must not reach the provider at all -- an empty
    # directUrls list still bills a run.
    assert fetch_windows(_conn({}), []) == []
