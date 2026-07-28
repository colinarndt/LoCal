"""Fetch-window grouping. The money stage: a wrong window re-fetches at full price."""

import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar.runner import fetch_windows


def _conn(marks: dict[str, str | None]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE account (handle TEXT PRIMARY KEY, last_polled_at TEXT)")
    conn.executemany("INSERT INTO account VALUES (?,?)", list(marks.items()))
    return conn


def _ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


# --- the case that prompted this: one new account must not widen the rest -----

def test_never_polled_account_does_not_widen_the_others():
    conn = _conn({"newbie": None, "a": _ago(1), "b": _ago(1)})
    groups = dict(fetch_windows(conn, ["newbie", "a", "b"]))
    assert groups["30 days"] == ["newbie"]
    assert sorted(groups["3 days"]) == ["a", "b"]


def test_widest_window_is_fetched_first():
    conn = _conn({"newbie": None, "a": _ago(1)})
    assert [w for w, _ in fetch_windows(conn, ["newbie", "a"])] == ["30 days", "3 days"]


# --- window arithmetic -------------------------------------------------------

def test_window_is_age_plus_two_days_of_overlap():
    conn = _conn({"a": _ago(5)})
    assert fetch_windows(conn, ["a"]) == [("7 days", ["a"])]


def test_freshly_polled_account_still_gets_the_three_day_floor():
    conn = _conn({"a": _ago(0)})
    assert fetch_windows(conn, ["a"]) == [("3 days", ["a"])]


def test_accounts_polled_together_share_one_group():
    conn = _conn({h: _ago(4) for h in "abcd"})
    assert fetch_windows(conn, list("abcd")) == [("6 days", ["a", "b", "c", "d"])]


# --- edges -------------------------------------------------------------------

def test_explicit_history_days_overrides_every_window():
    conn = _conn({"newbie": None, "a": _ago(1)})
    assert fetch_windows(conn, ["newbie", "a"], history_days=90) == \
        [("90 days", ["newbie", "a"])]


def test_handle_with_no_account_row_counts_as_never_polled():
    conn = _conn({"a": _ago(1)})
    assert dict(fetch_windows(conn, ["a", "ghost"]))["30 days"] == ["ghost"]


def test_no_handles_means_no_fetch():
    # An empty rotation must not reach the provider at all -- an empty
    # directUrls list still bills a run.
    assert fetch_windows(_conn({}), []) == []
