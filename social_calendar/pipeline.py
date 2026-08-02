"""poll -> source_post -> gate -> extract -> validate -> dedupe -> event.

Each stage is separately re-runnable. Re-extraction never re-scrapes; dedup is
recomputed from scratch each pass so a better normalizer takes effect without a
migration.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

from . import dedupe, prompts, spend, validate
from .sources import IngestionSource, download_images, to_row


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --- stage 1: ingest --------------------------------------------------------

def ingest(conn: sqlite3.Connection, source: IngestionSource, handles: list[str],
           limit: int = 20, fetch_media: bool = True,
           newer_than: str | None = None) -> int:
    try:
        try:
            posts = source.fetch_recent(handles, limit, newer_than=newer_than)
        except TypeError:
            posts = source.fetch_recent(handles, limit)   # sources without a window
    finally:
        # The scrape is billed whether or not anything downstream succeeds, so
        # the ledger is written before any of it can raise.
        if spend.drain_into(conn, getattr(source, "meter", None)):
            conn.commit()
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
        # Commit per post, not per batch: download_images() does network I/O on
        # the next iteration, and an open transaction across it holds the write
        # lock long enough to 500 the web UI.
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

    stats = {"seen": len(rows), "gated_out": 0, "gate_skipped": 0,
             "vision_skipped": 0, "events": 0, "errors": 0, "flagged": 0}
    for i, row in enumerate(rows, 1):
        post = _post_dict(row)
        # Commit per post. Holding one transaction across hundreds of model calls
        # locks the database for the whole run and takes the web UI down with it.
        # Draining here rather than at the end of the body means the previous
        # post's spend lands even though every branch below can `continue`.
        spend.drain_into(conn, extractor.meter)
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
            gate_model = (extractor.model_for("gate") if hasattr(extractor, "model_for")
                          else extractor.model)
            _record(conn, post["post_id"], "gate", gate_model, gate)
            if "_error" in gate:
                stats["errors"] += 1
                continue
            if not gate.get("is_event_candidate"):
                stats["gated_out"] += 1
                continue

            # Structured calendars and earlier posts may already identify this
            # event. Only explicit caption hints can skip the flyer; ambiguous
            # announcements continue to vision exactly as before.
            match = _caption_match(conn, gate)
            if match is not None:
                _record(conn, post["post_id"], "prematch", extractor.model, {
                    "event_id": match["id"], "reason": match["reason"],
                    "title": match["title"], "starts_at": match["starts_at"],
                })
                _attach_source(conn, match["id"], post, "caption")
                stats["vision_skipped"] += 1
                continue

        out = extractor.extract(post)
        eid = _record(conn, post["post_id"], "extract", extractor.model, out)
        if "_error" in out:
            stats["errors"] += 1
            continue
        if not out.get("is_event"):
            continue

        _insert_event(conn, post, eid, out, stats)
    # The last post's calls have not been drained by the loop head yet.
    spend.drain_into(conn, extractor.meter)
    conn.commit()
    return stats


def _insert_event(conn: sqlite3.Connection, post: dict, extraction_id: int,
                  out: dict, stats: dict) -> None:
    flagged, reason = validate.validate({
        "starts_at": out.get("starts_at"),
        "posted_at": post.get("posted_at"),
        "date_reasoning": out.get("date_reasoning"),
    })
    event_id = conn.execute(
        "INSERT INTO event (post_id, extraction_id, title, starts_at, start_time_known, "
        "venue_name, venue_key, category, price_text, confidence, date_reasoning, "
        "needs_review, review_reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (post["post_id"], extraction_id, out.get("title"), out.get("starts_at"),
         int(bool(out.get("start_time_known"))), out.get("venue_name"),
         dedupe.normalize_venue(out.get("venue_name")), out.get("category"),
         out.get("price_text"), out.get("confidence"), out.get("date_reasoning"),
         int(flagged), reason, _now())).lastrowid
    _attach_source(conn, event_id, post, "extracted")
    stats["events"] += 1
    stats["flagged"] += int(flagged)


def _attach_source(conn: sqlite3.Connection, event_id: int, post: dict,
                   method: str) -> None:
    kind = "website" if post.get("source_name") == "website" else "instagram"
    conn.execute(
        "INSERT OR IGNORE INTO event_source (event_id,source_kind,source_item_id,"
        "permalink,match_method,created_at) VALUES (?,?,?,?,?,?)",
        (event_id, kind, post["post_id"], post.get("permalink"), method, _now()))


def _caption_match(conn: sqlite3.Connection, gate: dict) -> dict | None:
    """Find an existing event only when the caption makes identity explicit.

    False matches silently discard flyer information, so this is intentionally
    stricter than final dedupe. An exact source URL is sufficient. Otherwise an
    exact day, a strong title, and a complete stored venue are all required.
    """
    hinted_url = (gate.get("event_url_hint") or "").strip().rstrip("/")
    if hinted_url:
        row = conn.execute(
            "SELECT e.id,e.title,e.starts_at,'exact event URL' AS reason FROM event e "
            "JOIN event_source es ON es.event_id=e.id "
            "WHERE e.is_canonical=1 AND rtrim(es.permalink,'/')=? LIMIT 1",
            (hinted_url,)).fetchone()
        if row:
            return dict(row)

    title = (gate.get("title_hint") or "").strip()
    day = _iso_day(gate.get("starts_at_hint"))
    if not title or not day:
        return None
    venue = dedupe.normalize_venue(gate.get("venue_hint"))
    rows = conn.execute(
        "SELECT id,title,starts_at,venue_name,venue_key FROM event "
        "WHERE is_canonical=1 AND substr(starts_at,1,10)=? AND title IS NOT NULL",
        (day,)).fetchall()
    matches = []
    for row in rows:
        score = dedupe.title_similarity(title, row["title"])
        venue_agrees = bool(venue and venue == row["venue_key"])
        threshold = 0.75 if venue_agrees else 0.90
        if row["venue_key"] and score >= threshold:
            matches.append((score, row, venue_agrees))
    if not matches:
        return None
    matches.sort(key=lambda match: match[0], reverse=True)
    # Same title on the same date at multiple venues is not hypothetical for
    # touring festivals and common recurring names. Without explicit venue
    # agreement, keep the flyer in the loop rather than selecting arbitrarily.
    if len(matches) > 1 and not matches[0][2]:
        return None
    score, row, venue_agrees = matches[0]
    return {"id": row["id"], "title": row["title"], "starts_at": row["starts_at"],
            "reason": (f"same date, {'same venue, ' if venue_agrees else ''}"
                       f"title similarity {score:.2f}")}


def _iso_day(value: str | None) -> str | None:
    if not value or len(value) < 10:
        return None
    try:
        return dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# --- stage 4: dedupe --------------------------------------------------------

def rebuild_dedupe(conn: sqlite3.Connection) -> dict:
    """Recompute grouping over all events. Idempotent, safe to re-run after a
    normalizer change."""
    rows = conn.execute(
        "SELECT e.id, e.post_id, e.title, e.starts_at, e.venue_name, e.price_text, "
        "e.start_time_known, p.posted_at, p.source_name FROM event e "
        "JOIN source_post p ON p.post_id = e.post_id").fetchall()
    events = [dict(r) for r in rows]
    dedupe.group_events(events)
    for ev in events:
        # Refresh venue_key too -- an alias added later (e.g. the Petri's/Petra's
        # OCR fix) must reach existing rows, or they never join to `venue`.
        conn.execute("UPDATE event SET dedupe_group=?, is_canonical=?, venue_key=? WHERE id=?",
                     (ev["dedupe_group"], ev["is_canonical"],
                      dedupe.normalize_venue(ev.get("venue_name")), ev["id"]))
    # Supporting links follow the canonical event so the one displayed record
    # retains every website and Instagram source in its group.
    canonical = {e["dedupe_group"]: e["id"] for e in events if e["is_canonical"]}
    for ev in events:
        target = canonical.get(ev["dedupe_group"])
        if target and target != ev["id"]:
            conn.execute("UPDATE event_source SET event_id=? WHERE event_id=?",
                         (target, ev["id"]))
    # Website snapshots retain their original event id between polls. Point them
    # at the canonical row after each rebuild so performer visibility and future
    # source updates follow the one event the calendar displays.
    try:
        conn.execute(
            "UPDATE web_item SET event_id=(SELECT es.event_id FROM event_source es "
            "WHERE es.source_kind='website' AND es.source_item_id=web_item.post_id) "
            "WHERE EXISTS (SELECT 1 FROM event_source es WHERE es.source_kind='website' "
            "AND es.source_item_id=web_item.post_id)")
    except sqlite3.OperationalError:
        pass  # pre-website schemas used by a few focused unit tests
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
        "WHERE e.occurrence_of IS NULL AND e.starts_at IS NOT NULL "
        "AND p.source_name != 'website'").fetchall()

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
