"""The menu bar reads the database on the AppKit main thread.

`connect()` does maintenance writes on open -- the stale-account self-heal --
which wait out `busy_timeout` behind a running fetch. Measured at 5.7s against a
6s lock, and `busy_timeout` is 30s. That is a frozen menu bar at exactly the
moment someone opens it to watch a fetch, so the UI path uses `read_session`.
"""

import multiprocessing as mp
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar import db


def _hold_write_lock(db_path, seconds, ready):
    """A fetch mid-run: an open write transaction. Raw sqlite3 on purpose --
    db.connect() would perform the self-heal we are trying to leave pending."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO spend (occurred_at, provider, detail, usd) "
                 "VALUES ('x','y','z',0.0)")
    ready.set()
    time.sleep(seconds)
    conn.rollback()
    conn.close()


@pytest.fixture
def contended_db():
    """A database with a self-heal pending and a writer holding the lock."""
    path = str(Path(tempfile.mkdtemp()) / "contended.db")
    db.connect(path).close()

    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO account (handle, is_polled, seen_count, "
                "discovery_source, added_at, status) "
                "VALUES ('v',1,0,'manual','now','candidate')")
    raw.commit()
    raw.close()

    ready = mp.Event()
    proc = mp.Process(target=_hold_write_lock, args=(path, 4, ready))
    proc.start()
    ready.wait(10)
    time.sleep(0.3)
    yield path
    proc.join()


def test_read_session_does_not_block_behind_a_writer(contended_db):
    start = time.perf_counter()
    with db.read_session(contended_db) as conn:
        conn.execute("SELECT COUNT(*) FROM account").fetchone()
        conn.execute("SELECT COALESCE(SUM(usd), 0) FROM spend").fetchone()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"menu read blocked for {elapsed:.1f}s"


def test_connect_does_block_which_is_why_read_session_exists(contended_db):
    """Pins the reason. If this ever stops blocking, the fix can be simplified;
    while it blocks, the UI path must not use connect()."""
    start = time.perf_counter()
    db.connect(contended_db).close()
    assert time.perf_counter() - start > 1.0


def test_read_session_sees_committed_data():
    path = str(Path(tempfile.mkdtemp()) / "plain.db")
    with db.session(path) as conn:
        conn.execute("INSERT INTO spend (occurred_at, provider, detail, usd) "
                     "VALUES ('2026-07-29T00:00:00+00:00','anthropic','m',0.25)")
    with db.read_session(path) as conn:
        assert conn.execute("SELECT SUM(usd) FROM spend").fetchone()[0] == 0.25
        # Row factory matters: callers index by column name.
        assert conn.execute("SELECT provider FROM spend").fetchone()["provider"] == "anthropic"
