"""poll -> source_post -> gate -> extract -> validate -> dedupe -> event.

Each stage is separately re-runnable. Re-extraction never re-scrapes; dedup is
recomputed from scratch each pass so a better normalizer takes effect without a
migration.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from . import dedupe, prompts, validate
from .sources import IngestionSource, download_images, to_row


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --- stage 1: ingest --------------------------------------------------------

def ingest(conn: sqlite3.Connection, source: IngestionSource, handles: list[str],
           limit: int = 20, fetch_media: bool = True,
           newer_than: str | None = None) -> int:
    try:
        posts = source.fetch_recent(handles, limit, newer_than=newer_than)
    except TypeError:
        posts = source.fetch_recent(handles, limit)   # sources without a window
    new = 0
    for p in posts:
        if conn.execute("SELECT 1 FROM source_post WHERE post_id=?", (p.post_id,)).fetchone():
            continue  # idempotent on provider post id
        if fetch_media and not p.local_images:
            download_images(p)
        row = to_row(p, source.source_name())
        conn.execute(
            f"INSERT INTO source_post ({','.join(row)}) "
            f"VALUES ({','.join('?' * len(row))})", tuple(row.values()))
        name = (p.raw or {}).get("ownerFullName")
        _touch_account(conn, p.polled_handle, polled=True)
        if p.attributed_handle and p.attributed_handle != p.polled_handle:
            _touch_account(conn, p.attributed_handle, polled=False, display_name=name)
        elif name:
            _touch_account(conn, p.polled_handle, polled=True, display_name=name)
        new += 1
        if new % 25 == 0:
            conn.commit()
    conn.commit()
    for h in handles:
        conn.execute("UPDATE account SET last_polled_at=? WHERE handle=?", (_now(), h))
    return new


def _touch_account(conn: sqlite3.Connection, handle: str | None, polled: bool,
                   display_name: str | None = None) -> None:
    if not handle:
        return
    conn.execute(
        "INSERT INTO account (handle, is_polled, seen_count, discovery_source, added_at, "
        "display_name) VALUES (?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET "
        "is_polled = max(is_polled, excluded.is_polled), "
        "seen_count = seen_count + excluded.seen_count, "
        "display_name = COALESCE(excluded.display_name, account.display_name)",
        (handle, int(polled), 0 if polled else 1, "manual" if polled else "tagged",
         _now(), display_name))


# --- stage 2 + 3: gate, extract ---------------------------------------------

def _record(conn: sqlite3.Connection, post_id: str, stage: str, model: str, out: dict) -> int:
    cur = conn.execute(
        "INSERT INTO extraction (post_id, stage, prompt_version, model, raw_output, "
        "is_error, created_at) VALUES (?,?,?,?,?,?,?)",
        (post_id, stage, prompts.version_for(stage), model, json.dumps(out),
         int("_error" in out), _now()))
    return cur.lastrowid


def _post_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["local_images"] = json.loads(d.get("local_images") or "[]")
    return d


def process(conn: sqlite3.Connection, extractor, limit: int | None = None) -> dict:
    """Gate then extract every post without an event row yet."""
    # Select on ANY extraction row, not just 'extract'. A gated-out post never
    # gets an extract row, so keying on that stage alone would re-gate it on
    # every future run -- a cost that grows with corpus age.
    q = ("SELECT * FROM source_post p WHERE NOT EXISTS "
         "(SELECT 1 FROM extraction e WHERE e.post_id=p.post_id) "
         "ORDER BY posted_at DESC")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()

    stats = {"seen": len(rows), "gated_out": 0, "gate_skipped": 0, "events": 0,
             "errors": 0, "flagged": 0}
    for i, row in enumerate(rows, 1):
        post = _post_dict(row)
        # Commit per post. Holding one transaction across hundreds of model calls
        # locks the database for the whole run and takes the web UI down with it.
        conn.commit()
        if i % 10 == 0 or i == len(rows):
            print(f"  ...{i}/{len(rows)} posts", flush=True)

        # The gate reads caption text only. A post with no caption but with
        # images carries all its information in the flyer, so gating it on text
        # would reject every image-only announcement sight unseen. Skip straight
        # to vision instead -- it is the one case where the gate cannot help.
        if not (post.get("caption") or "").strip() and post.get("local_images"):
            stats["gate_skipped"] += 1
        else:
            gate = extractor.gate(post)
            _record(conn, post["post_id"], "gate", extractor.model, gate)
            if "_error" in gate:
                stats["errors"] += 1
                continue
            if not gate.get("is_event_candidate"):
                stats["gated_out"] += 1
                continue

        out = extractor.extract(post)
        eid = _record(conn, post["post_id"], "extract", extractor.model, out)
        if "_error" in out:
            stats["errors"] += 1
            continue
        if not out.get("is_event"):
            continue

        _insert_event(conn, post, eid, out, stats)
    conn.commit()
    return stats


def _insert_event(conn: sqlite3.Connection, post: dict, extraction_id: int,
                  out: dict, stats: dict) -> None:
    flagged, reason = validate.validate({
        "starts_at": out.get("starts_at"),
        "posted_at": post.get("posted_at"),
        "date_reasoning": out.get("date_reasoning"),
    })
    conn.execute(
        "INSERT INTO event (post_id, extraction_id, title, starts_at, start_time_known, "
        "venue_name, venue_key, category, price_text, confidence, date_reasoning, "
        "needs_review, review_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (post["post_id"], extraction_id, out.get("title"), out.get("starts_at"),
         int(bool(out.get("start_time_known"))), out.get("venue_name"),
         dedupe.normalize_venue(out.get("venue_name")), out.get("category"),
         out.get("price_text"), out.get("confidence"), out.get("date_reasoning"),
         int(flagged), reason, _now()))
    stats["events"] += 1
    stats["flagged"] += int(flagged)


# --- stage 4: dedupe --------------------------------------------------------

def rebuild_dedupe(conn: sqlite3.Connection) -> dict:
    """Recompute grouping over all events. Idempotent, safe to re-run after a
    normalizer change."""
    rows = conn.execute(
        "SELECT e.id, e.post_id, e.title, e.starts_at, e.venue_name, e.price_text, "
        "e.start_time_known, p.posted_at FROM event e "
        "JOIN source_post p ON p.post_id = e.post_id").fetchall()
    events = [dict(r) for r in rows]
    dedupe.group_events(events)
    for ev in events:
        # Refresh venue_key too -- an alias added later (e.g. the Petri's/Petra's
        # OCR fix) must reach existing rows, or they never join to `venue`.
        conn.execute("UPDATE event SET dedupe_group=?, is_canonical=?, venue_key=? WHERE id=?",
                     (ev["dedupe_group"], ev["is_canonical"],
                      dedupe.normalize_venue(ev.get("venue_name")), ev["id"]))
    groups = {e["dedupe_group"] for e in events}
    return {"events": len(events), "groups": len(groups), "merged": len(events) - len(groups)}


# --- stage 5: series --------------------------------------------------------

def expand_series(conn: sqlite3.Connection, weeks: int = 8) -> dict:
    """Turn recurring rules into future occurrences, out to the horizon.

    Generation is strictly forward from the post date -- Phase 0 caught the
    model resolving a weekly Open Mic backward to the previous occurrence.
    """
    rows = conn.execute(
        "SELECT e.id, e.title, e.venue_name, e.venue_key, e.category, e.date_reasoning, "
        "e.starts_at, e.start_time_known, e.post_id, p.posted_at, p.caption FROM event e "
        "JOIN source_post p ON p.post_id = e.post_id "
        "WHERE e.occurrence_of IS NULL AND e.starts_at IS NOT NULL").fetchall()

    def _find_series(conn, title, venue_key, rule):
        """Match on venue + rule + fuzzy title -- two posts about the same
        weekly night rarely title it identically."""
        for s in conn.execute(
                "SELECT id, title FROM series WHERE venue_key IS ? AND rule = ?",
                (venue_key, rule)):
            if dedupe.title_similarity(title, s["title"]) >= 0.6:
                return s["id"]
        return None

    made = series_made = 0
    for r in rows:
        rule = dedupe.detect_recurrence(f"{r['date_reasoning'] or ''} {r['caption'] or ''}")
        if not rule:
            continue
        anchor = validate._date_of(r["posted_at"])
        if anchor is None:
            continue

        # Several posts describe the same weekly event. Reuse an existing series
        # rather than minting a second one, or every post spawns a parallel set
        # of occurrences.
        sid = _find_series(conn, r["title"], r["venue_key"], rule["rule"])
        if sid is None:
            cur = conn.execute(
                "INSERT INTO series (title, venue_key, kind, rule, horizon_until, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (r["title"], r["venue_key"], rule["kind"], rule["rule"],
                 (anchor + dt.timedelta(weeks=weeks)).isoformat(), _now()))
            sid = cur.lastrowid
            series_made += 1
        conn.execute("UPDATE event SET occurrence_of=? WHERE id=?", (sid, r["id"]))

        # A weekly jam happens at the same time every week. The source event
        # already knows it ("Doors 7pm / Music 7:30pm"), so carry that forward --
        # otherwise every generated occurrence reads "time TBA" even though the
        # flyer stated the time plainly.
        src_time = ""
        if r["starts_at"] and "T" in r["starts_at"]:
            src_time = r["starts_at"][10:]      # "T19:30:00"
        time_known = 1 if src_time else 0

        for day in dedupe.expand_recurring(rule, anchor, weeks):
            iso = day.isoformat() + src_time
            if conn.execute(
                "SELECT 1 FROM event WHERE occurrence_of=? AND substr(starts_at,1,10)=?",
                (sid, iso[:10])).fetchone():
                continue  # idempotent across polls
            conn.execute(
                "INSERT INTO event (post_id, title, starts_at, start_time_known, venue_name, "
                "venue_key, category, occurrence_of, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (r["post_id"], r["title"], iso, time_known, r["venue_name"],
                 r["venue_key"], r["category"], sid, _now()))
            made += 1
    return {"series": series_made, "occurrences_generated": made}
