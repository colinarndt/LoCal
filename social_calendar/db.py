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

from .paths import DB_PATH  # noqa: F401  (re-exported; callers import from here)

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
    is_hidden     INTEGER NOT NULL DEFAULT 0,
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
    location_city  TEXT,
    location_region TEXT,
    location_lat   REAL,
    location_lon   REAL,
    ticket_url     TEXT,
    ticket_status  TEXT,
    is_confirmed   INTEGER NOT NULL DEFAULT 0,
    is_hidden      INTEGER NOT NULL DEFAULT 0,
    occurrence_of  INTEGER REFERENCES series(id),
    dedupe_group   TEXT,                   -- canonical key; siblings share it
    is_canonical   INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_event_starts ON event(starts_at);
CREATE INDEX IF NOT EXISTS ix_event_group  ON event(dedupe_group);

-- User-managed website calendars. Website entries are mutable snapshots rather
-- than immutable social posts, so their fetch state and stable external IDs
-- live separately even though a compatibility source_post row lets them reuse
-- the existing event, dedupe, geocode, UI, and export paths.
CREATE TABLE IF NOT EXISTS web_source (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    linked_handle   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    format          TEXT,
    etag            TEXT,
    last_modified   TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error      TEXT,
    last_result     TEXT,                    -- events | empty | error
    source_type     TEXT NOT NULL DEFAULT 'venue', -- venue | performer
    radius_miles    REAL,
    notify          INTEGER NOT NULL DEFAULT 0,
    added_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS web_item (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES web_source(id),
    external_id  TEXT NOT NULL,
    post_id      TEXT NOT NULL UNIQUE REFERENCES source_post(post_id),
    event_id     INTEGER NOT NULL REFERENCES event(id),
    content_hash TEXT NOT NULL,
    in_range     INTEGER NOT NULL DEFAULT 1,
    distance_miles REAL,
    series_key   TEXT,                    -- NULL until checked; '' means not series-shaped
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE(source_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_web_item_source ON web_item(source_id);

-- Website calendars often publish each occurrence as an independent Event and
-- omit RRULE/recurrence metadata. Date-shaped permalinks let us propose a
-- relationship, but the user confirms or rejects it before it becomes a series.
CREATE TABLE IF NOT EXISTS web_series_candidate (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER NOT NULL REFERENCES web_source(id),
    series_key       TEXT NOT NULL,
    title            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'proposed', -- proposed | confirmed | rejected
    series_id        INTEGER REFERENCES series(id),
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE(source_id, series_key)
);
CREATE INDEX IF NOT EXISTS ix_web_series_status ON web_series_candidate(status);

CREATE TABLE IF NOT EXISTS location_cache (
    location_key TEXT PRIMARY KEY,
    lat          REAL,
    lon          REAL,
    address      TEXT,
    geocoded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_delivery (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    INTEGER NOT NULL REFERENCES web_source(id),
    external_id  TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    kind         TEXT NOT NULL, -- new | nearby | updated
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    ticket_url   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(source_id, external_id, content_hash, kind)
);
CREATE INDEX IF NOT EXISTS ix_alert_pending ON alert_delivery(delivered_at);

-- Successful arbitrary-page model results are cached by sanitized page hash.
-- Most sites also send ETag/Last-Modified, but this prevents a repeated model
-- call for sites that do not implement conditional requests.
CREATE TABLE IF NOT EXISTS web_parse_cache (
    source_id      INTEGER PRIMARY KEY REFERENCES web_source(id),
    content_hash   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    raw_output     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- One displayed event may be supported by a venue website, the venue's own
-- Instagram post, and several reposts. Keeping those references independently
-- lets dedupe collapse records without losing provenance.
CREATE TABLE IF NOT EXISTS event_source (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL REFERENCES event(id),
    source_kind    TEXT NOT NULL,          -- instagram | website
    source_item_id TEXT NOT NULL,
    permalink      TEXT,
    match_method   TEXT NOT NULL,          -- extracted | structured | caption
    created_at     TEXT NOT NULL,
    UNIQUE(source_kind, source_item_id)
);
CREATE INDEX IF NOT EXISTS ix_event_source_event ON event_source(event_id);

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

-- Money. One row per paid call, appended and never edited.
--
-- Deliberately not derived from `extraction`: that table predates any token
-- accounting, so spend before this table existed cannot be reconstructed. The
-- UI reports "since tracking started" rather than pretending otherwise.
CREATE TABLE IF NOT EXISTS spend (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at        TEXT NOT NULL,        -- ISO 8601 UTC
    provider           TEXT NOT NULL,        -- anthropic | apify
    detail             TEXT,                 -- model id, or apify actor id
    usd                REAL NOT NULL,
    units              REAL,                 -- apify: items scraped. anthropic: null.
    estimated          INTEGER NOT NULL DEFAULT 0,  -- 1 when the provider gave no actual
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_spend_at ON spend(occurred_at);
"""

# Columns added after the first release. sqlite has no "ADD COLUMN IF NOT EXISTS",
# so attempt and ignore the duplicate error.
MIGRATIONS = [
    "ALTER TABLE account ADD COLUMN status TEXT NOT NULL DEFAULT 'candidate'",
    "ALTER TABLE account ADD COLUMN proposed_reason TEXT",
    "ALTER TABLE account ADD COLUMN avatar_file TEXT",
    "ALTER TABLE web_source ADD COLUMN source_type TEXT NOT NULL DEFAULT 'venue'",
    "ALTER TABLE web_source ADD COLUMN radius_miles REAL",
    "ALTER TABLE web_source ADD COLUMN notify INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE web_source ADD COLUMN last_result TEXT",
    "ALTER TABLE web_item ADD COLUMN in_range INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE web_item ADD COLUMN distance_miles REAL",
    "ALTER TABLE web_item ADD COLUMN series_key TEXT",
    "ALTER TABLE series ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE event ADD COLUMN location_city TEXT",
    "ALTER TABLE event ADD COLUMN location_region TEXT",
    "ALTER TABLE event ADD COLUMN location_lat REAL",
    "ALTER TABLE event ADD COLUMN location_lon REAL",
    "ALTER TABLE event ADD COLUMN ticket_url TEXT",
    "ALTER TABLE event ADD COLUMN ticket_status TEXT",
    # Earlier website imports treated CMS all-day spans (00:00:00 through
    # 23:59:59) as literal midnight events. Repair stored schema.org payloads
    # once on open; the condition is idempotent after start_time_known is zero.
    """UPDATE event SET starts_at=substr(starts_at,1,10), ends_at=substr(starts_at,1,10),
       start_time_known=0
       WHERE start_time_known=1 AND starts_at LIKE '%T00:00:00'
       AND EXISTS (
           SELECT 1 FROM source_post p WHERE p.post_id=event.post_id
           AND p.source_name='website'
           AND p.raw_provider_json LIKE '%\"startDate\": \"%T00:00:00%'
           AND p.raw_provider_json LIKE '%\"endDate\": \"%T23:59:59%'
       )""",
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
    # Classify website rows from older installs once. New polls set series_key
    # as they write, while NULL marks only records that predate this feature.
    if conn.execute("SELECT 1 FROM web_item WHERE series_key IS NULL LIMIT 1").fetchone():
        from . import websites
        websites.refresh_series_candidates(conn)
        conn.commit()
    # Existing installs predate event_source. Backfill once so their source
    # counts and future cross-source matches work without re-extraction.
    if (conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 0
            and conn.execute("SELECT 1 FROM event LIMIT 1").fetchone()):
        conn.execute(
            "INSERT OR IGNORE INTO event_source "
            "(event_id, source_kind, source_item_id, permalink, match_method, created_at) "
            "SELECT e.id, CASE WHEN p.source_name='apify' THEN 'instagram' ELSE p.source_name END, "
            "p.post_id, p.permalink, 'extracted', e.created_at "
            "FROM event e JOIN source_post p ON p.post_id=e.post_id "
            "WHERE e.occurrence_of IS NULL")
        conn.commit()
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


@contextmanager
def read_session(path: Path | str = DB_PATH) -> Iterator[sqlite3.Connection]:
    """A connection for reads only, skipping everything `connect` does on open.

    `connect` runs the schema, the migrations, and the stale-account self-heal.
    That last one is a write, and behind a running fetch's write lock it waits
    out `busy_timeout` -- measured at 5.7s against a 6s lock, and up to 30s in
    practice. Fine for a CLI command; on the AppKit main thread it freezes the
    menu bar at exactly the moment someone opens it to watch a fetch.

    Readers never block writers in WAL, so this stays responsive. The short
    timeout is deliberate: a UI path should fail fast and show stale numbers
    rather than beachball.
    """
    conn = sqlite3.connect(path, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 2000")
    try:
        yield conn
    finally:
        conn.close()
