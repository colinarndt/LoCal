"""SPEC section 7 -- semi-manual account discovery.

Two signals, in order of quality:

1. Accounts already appearing on posts we scrape. Free (no extra fetches) and
   high precision -- venues tag artists, artists tag venues, promoters tag both.
   The live run showed collab attribution is the normal case, not an edge case:
   6 of 10 posts came back attributed to an account we never asked for.
2. LLM proposal from an interest description, for cold-starting a scene we have
   no foothold in.

Nothing is auto-added. Both signals feed a review queue.
"""

from __future__ import annotations

import json

TAGGED_SQL = """
SELECT p.attributed_handle                       AS handle,
       COUNT(DISTINCT e.id)                      AS events,
       COUNT(DISTINCT p.post_id)                 AS posts,
       MAX(p.posted_at)                          AS last_seen,
       GROUP_CONCAT(DISTINCT p.polled_handle)    AS seen_via
FROM source_post p
JOIN event e ON e.post_id = p.post_id
WHERE p.attributed_handle IS NOT NULL
  AND p.attributed_handle != ''
  AND p.attributed_handle NOT IN (SELECT handle FROM account WHERE is_polled = 1)
  AND p.attributed_handle NOT IN (SELECT handle FROM account WHERE status = 'rejected')
GROUP BY p.attributed_handle
ORDER BY events DESC, posts DESC
"""


def rank_tagged(conn, min_events: int = 1) -> list[dict]:
    """Candidates ranked by how many real EVENTS their posts produced.

    Ranking on events rather than raw post count matters: an account that
    appears often but never yields an event is noise, not a lead.
    """
    out = []
    for r in conn.execute(TAGGED_SQL):
        if r["events"] < min_events:
            continue
        out.append({
            "handle": r["handle"],
            "events": r["events"],
            "posts": r["posts"],
            "last_seen": (r["last_seen"] or "")[:10],
            "seen_via": [h for h in (r["seen_via"] or "").split(",") if h],
            "source": "tagged",
        })
    return out


PROPOSE_SYSTEM = """\
You suggest Instagram accounts worth following for local event discovery in a \
specific city. You are given the user's interests and the accounts they already \
follow.

Propose accounts that regularly ANNOUNCE dated, attendable events -- venues, \
promoters, booking agencies, recurring series, market organizers. Do not propose \
personal accounts, news outlets, or accounts that only post recaps.

Only propose handles you have real reason to believe exist. It is far better to \
return three you are confident about than fifteen guesses -- every dead handle \
costs a paid request to discover. Say so in `confidence` when unsure.

Never propose a handle already in the provided list.\
"""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "accounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "no leading @"},
                    "why": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["handle", "why", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["accounts"],
    "additionalProperties": False,
}


def propose(extractor, description: str, existing: list[str], city: str | None = None,
            limit: int = 10) -> list[dict]:
    """Ask the model for candidate accounts. Returns [] on any failure."""
    if city is None:
        from . import config
        city = config.load()["city"]
    prompt = (
        f"City: {city}\n"
        f"Interests: {description}\n\n"
        f"Already following ({len(existing)}): {', '.join(sorted(existing)) or '(none)'}\n\n"
        f"Propose up to {limit} accounts."
    )
    # Same request shape as the gate and extract stages, so metering, refusal
    # handling and JSON parsing stay in one place rather than being repeated
    # here and drifting apart.
    data = extractor._respond(
        extractor.model, PROPOSE_SYSTEM,
        [{"type": "input_text", "text": prompt}], PROPOSE_SCHEMA, "propose",
    )
    if "_error" in data:
        return []

    known = {h.lower() for h in existing}
    out = []
    for a in data.get("accounts", []):
        h = (a.get("handle") or "").lstrip("@").strip()
        if h and h.lower() not in known:
            out.append({"handle": h, "events": 0, "posts": 0, "last_seen": "",
                        "seen_via": [], "source": "suggested",
                        "why": a.get("why", ""), "confidence": a.get("confidence")})
    return out


def stage(conn, candidates: list[dict]) -> int:
    """Write candidates into `account` as pending review. Never auto-approves."""
    n = 0
    for c in candidates:
        row = conn.execute("SELECT status FROM account WHERE handle=?", (c["handle"],)).fetchone()
        if row and row["status"] in ("approved", "rejected"):
            continue  # already decided; do not re-surface
        reason = c.get("why") or (
            f"appeared on {c['posts']} post(s) yielding {c['events']} event(s)"
            f"{' via @' + ', @'.join(c['seen_via']) if c['seen_via'] else ''}")
        if row:
            conn.execute("UPDATE account SET proposed_reason=?, discovery_source=? WHERE handle=?",
                         (reason, c["source"], c["handle"]))
        else:
            conn.execute(
                "INSERT INTO account (handle, is_polled, seen_count, discovery_source, "
                "added_at, status, proposed_reason) VALUES (?,0,?,?,datetime('now'),"
                "'candidate',?)", (c["handle"], c["posts"], c["source"], reason))
        n += 1
    return n


def decide(conn, handle: str, approve: bool) -> None:
    """Approve puts the account into the poll rotation; reject keeps it out and
    stops it being proposed again."""
    conn.execute(
        "UPDATE account SET status=?, is_polled=? WHERE handle=?",
        ("approved" if approve else "rejected", 1 if approve else 0, handle))


def approved_handles(conn) -> list[str]:
    return [r["handle"] for r in conn.execute(
        "SELECT handle FROM account WHERE is_polled=1 ORDER BY handle")]


def pending(conn) -> list[dict]:
    """Only accounts a discovery pass deliberately surfaced.

    Every attributed handle gets an `account` row at ingest time, so filtering on
    status alone would bury the real leads under dozens of accounts that were
    merely seen once and never produced an event.
    """
    return [dict(r) for r in conn.execute(
        "SELECT a.handle, a.display_name, a.avatar_file, a.seen_count, a.discovery_source, "
        "a.proposed_reason, "
        "(SELECT COUNT(DISTINCT e.id) FROM source_post p JOIN event e ON e.post_id=p.post_id "
        " WHERE p.attributed_handle = a.handle) AS events "
        "FROM account a WHERE a.status='candidate' AND a.proposed_reason IS NOT NULL "
        "ORDER BY events DESC, a.seen_count DESC, a.handle")]
