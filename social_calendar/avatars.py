"""Profile pictures for the discovery queue.

Post payloads carry `ownerFullName` but no avatar, so this needs a separate
profile scrape. Cheap (~$2.60/1000 profiles) and one-shot per account -- an
account with an avatar on disk is never re-fetched.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

AVATAR_DIR = Path(__file__).parent.parent / "data" / "avatars"
ACTOR_ID = "apify/instagram-profile-scraper"


def fetch(client, handles: list[str]) -> dict[str, dict]:
    """handle -> {full_name, pic_url}. Empty dict on failure; never raises."""
    if not handles:
        return {}
    try:
        run = client.actor(ACTOR_ID).call(run_input={"usernames": handles})
        out = {}
        for item in client.dataset(run.default_dataset_id).iterate_items():
            h = item.get("username")
            if not h:
                continue
            out[h] = {
                "full_name": item.get("fullName") or item.get("full_name"),
                "pic_url": (item.get("profilePicUrlHD") or item.get("profilePicUrl")
                            or item.get("profile_pic_url")),
            }
        return out
    except Exception as exc:
        print(f"profile scrape failed: {exc}", file=sys.stderr)
        return {}


def download(handle: str, url: str) -> str | None:
    """Store locally -- Instagram CDN URLs expire."""
    if not url:
        return None
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    dest = AVATAR_DIR / f"{handle}.jpg"
    if dest.exists():
        return dest.name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return dest.name
    except Exception as exc:
        print(f"  ! avatar failed for @{handle}: {exc}", file=sys.stderr)
        return None


def backfill(conn, client, limit: int = 60) -> dict:
    """Fetch avatars for accounts in the queue or the rotation that lack one."""
    rows = conn.execute(
        "SELECT handle FROM account WHERE avatar_file IS NULL "
        "AND (is_polled = 1 OR (status='candidate' AND proposed_reason IS NOT NULL)) "
        f"LIMIT {int(limit)}").fetchall()
    handles = [r["handle"] for r in rows]
    if not handles:
        return {"requested": 0, "saved": 0}

    profiles = fetch(client, handles)
    saved = 0
    for h in handles:
        p = profiles.get(h) or {}
        fname = download(h, p.get("pic_url"))
        if fname:
            conn.execute("UPDATE account SET avatar_file=? WHERE handle=?", (fname, h))
            saved += 1
        if p.get("full_name"):
            conn.execute(
                "UPDATE account SET display_name=COALESCE(display_name, ?) WHERE handle=?",
                (p["full_name"], h))
    return {"requested": len(handles), "saved": saved}
