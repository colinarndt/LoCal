"""SQLite storage. Plain sqlite3 -- single user, single writer, schema visible as DDL.

The two-table split (SPEC section 4) is the point: `source_post` is raw and
immutable, `event` is re-derivable. Re-running extraction with a better prompt
costs zero re-scraping.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).parent.parent / "data" / "calendar.db"

SCHEMA = """
-- Raw provider output. Never edited after insert.
CREATE TABLE IF NOT EXISTS source_post (
    post_id            TEXT PRIMARY KEY,   -- provider id; makes re-scrape idempotent
    polled_handle      TEXT NOT NULL,      -- the account WE asked for
    attributed_handle  TEXT,               -- who the provider says owns it (collab posts differ)
    posted_at          TEXT NOT NULL,      -- ISO 8601. Anchor for relative date resolution.
    caption            TEXT NOT NULL DEFAULT '',
    permalink          TEXT,
    media_kind         TEXT,               -- post | reel | story
    local_images       TEXT NOT NULL DEFAULT '[]',   -- json array of filenames
    raw_provider_json  TEXT,
    source_name        TEXT NOT NULL DEFAULT 'apify',
    fetched_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_post_polled ON source_post(polled_handle);
CREATE INDEX IF NOT EXISTS ix_post_posted ON source_post(posted_at);

-- One row per model call. Kept so prompt iterations stay comparable.
CREATE TABLE IF NOT EXISTS extraction (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        TEXT NOT NULL REFERENCES source_post(post_id),
    stage          TEXT NOT NULL,          -- gate | extract
    prompt_version TEXT NOT NULL,          -- per-stage, not global
    model          TEXT NOT NULL,
    raw_output     TEXT NOT NULL,
    is_error       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_extraction_post ON extraction(post_id, stage);

-- Groups occurrences. A run ("Jul 24-26") or a recurring rule ("every Wednesday").
CREATE TABLE IF NOT EXISTS series (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    venue_key     TEXT,                    -- normalized
    kind          TEXT NOT NULL,           -- run | recurring
    rule          TEXT,                    -- human-readable
    horizon_until TEXT,                    -- occurrences generated through this date
    created_at    TEXT NOT NULL
);

-- ONE ROW PER OCCURRENCE (SPEC section 4). A three-night run is three rows.
CREATE TABLE IF NOT EXISTS event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id        TEXT NOT NULL REFERENCES source_post(post_id),
    extraction_id  INTEGER REFERENCES extraction(id),
    title          TEXT,
    starts_at      TEXT,                   -- ISO; date-only when time unknown
    ends_at        TEXT,                   -- same-day end only; never spans a run
    start_time_known INTEGER NOT NULL DEFAULT 0,
    venue_name     TEXT,
    venue_key      TEXT,                   -- normalized, for dedup
    category       TEXT,
    price_text     TEXT,
    confidence     REAL,                   -- NOT calibrated; do not filter on it
    date_reasoning TEXT,
    needs_review   INTEGER NOT NULL DEFAULT 0,   -- section 5.5 weekday flag
    review_reason  TEXT,
    is_confirmed   INTEGER NOT NULL DEFAULT 0,
    is_hidden      INTEGER NOT NULL DEFAULT 0,
    occurrence_of  INTEGER REFERENCES series(id),
    dedupe_group   TEXT,                   -- canonical key; siblings share it
    is_canonical   INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_starts ON event(starts_at);
CREATE INDEX IF NOT EXISTS ix_event_group  ON event(dedupe_group);

-- Geocoded venues. Small table -- ~15 rows -- populated once via Nominatim.
CREATE TABLE IF NOT EXISTS venue (
    venue_key    TEXT PRIMARY KEY,   -- normalized key from dedupe.normalize_venue
    display_name TEXT,
    lat          REAL,
    lon          REAL,
    neighborhood TEXT,
    address      TEXT,
    geocoded_at  TEXT
);

-- Accounts we poll, plus accounts we have merely seen (collab attribution).
CREATE TABLE IF NOT EXISTS account (
    handle           TEXT PRIMARY KEY,
    display_name     TEXT,
    category_hint    TEXT,
    is_polled        INTEGER NOT NULL DEFAULT 0,   -- in the poll rotation?
    seen_count       INTEGER NOT NULL DEFAULT 0,   -- times seen as attributed handle
    discovery_source TEXT,                          -- manual | tagged | suggested
    last_polled_at   TEXT,
    added_at         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'candidate',  -- candidate | approved | rejected
    proposed_reason  TEXT
);
"""

# Columns added after the first release. sqlite has no "ADD COLUMN IF NOT EXISTS",
# so attempt and ignore the duplicate error.
MIGRATIONS = [
    "ALTER TABLE account ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate'",
    "ALTER TABLE account ADD COLUMN proposed_reason TEXT",
    "ALTER TABLE account ADD COLUMN avatar_file TEXT",
]


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the web UI read while a pipeline run is writing. Without it a
    # long run-once makes every page load fail with "database is locked".
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # another process holds the lock; it will apply next time
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(SCHEMA)  # idempotent
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc):
                raise
    # Anything already in the poll rotation is approved by definition. Check
    # before writing -- this runs on EVERY connection, and an unconditional
    # UPDATE made read-only page loads contend for the write lock.
    stale = conn.execute(
        "SELECT 1 FROM account WHERE is_polled=1 AND status='candidate' LIMIT 1").fetchone()
    if stale:
        conn.execute("UPDATE account SET status='approved' "
                     "WHERE is_polled=1 AND status='candidate'")
        conn.commit()
    return conn


@contextmanager
def session(path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
