#!/usr/bin/env python3
"""Phase 0, step 1: pull recent posts into local storage. Costs Apify money.

Run this ONCE. Everything downstream (score.py) reads from disk, so escalating
a model rung never re-hits the scraper. Images are downloaded rather than
hotlinked because Instagram CDN URLs expire and would break a later rerun.

Posts and reels only. Stories need an authenticated session and are Phase 4 --
deliberately no flag for them here.

    python3 spike/fetch.py --accounts spike/accounts.txt --limit 12

Untested against the live Apify API -- see the VERIFY note on ACTOR_INPUT below.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

SPIKE_DIR = Path(__file__).parent
REPO_ROOT = SPIKE_DIR.parent
POSTS_DIR = SPIKE_DIR / "posts"
MEDIA_DIR = POSTS_DIR / "media"


def load_env() -> None:
    """.env.local wins over .env; absolute paths so cwd doesn't matter."""
    load_dotenv(REPO_ROOT / ".env.local")
    load_dotenv(REPO_ROOT / ".env")

ACTOR_ID = "apify/instagram-scraper"
MAX_IMAGES_PER_POST = 3


def read_accounts(path: Path) -> list[str]:
    handles = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@")
        if line:
            handles.append(line)
    if not handles:
        sys.exit(f"No handles found in {path}")
    return handles


def normalize(raw: dict) -> dict | None:
    """Map one Apify record onto our RawPost shape. Returns None for stories."""
    post_id = raw.get("id") or raw.get("shortCode")
    if not post_id:
        return None

    # Collect image URLs: the main display image plus any carousel children.
    urls = []
    if raw.get("displayUrl"):
        urls.append(raw["displayUrl"])
    for child in raw.get("childPosts") or []:
        if child.get("displayUrl"):
            urls.append(child["displayUrl"])
    for extra in raw.get("images") or []:
        if isinstance(extra, str):
            urls.append(extra)

    seen, ordered = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return {
        "post_id": str(post_id),
        "account_handle": raw.get("ownerUsername") or raw.get("username"),
        "posted_at": raw.get("timestamp"),
        "caption": raw.get("caption") or "",
        "permalink": raw.get("url") or f"https://www.instagram.com/p/{post_id}/",
        "media_kind": "reel" if raw.get("type") == "Video" else "post",
        "image_urls": ordered[:MAX_IMAGES_PER_POST],
        "local_images": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw_provider_json": raw,
    }


def download_images(post: dict) -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(post["image_urls"]):
        dest = MEDIA_DIR / f"{post['post_id']}_{i}.jpg"
        if dest.exists():
            post["local_images"].append(dest.name)
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            post["local_images"].append(dest.name)
        except Exception as exc:  # a dead image shouldn't kill the whole run
            print(f"  ! image {i} failed for {post['post_id']}: {exc}", file=sys.stderr)


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accounts", type=Path, default=SPIKE_DIR / "accounts.txt")
    ap.add_argument("--limit", type=int, default=12, help="posts per account")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument(
        "--from-run",
        metavar="RUN_ID",
        help="harvest an Apify run that already completed instead of starting a "
             "new scrape. Free -- the run is already paid for. Use this when the "
             "actor succeeded but this script failed downstream of it.",
    )
    args = ap.parse_args()

    token = os.getenv("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN is not set. Copy .env.example to .env and fill it in.")

    try:
        from apify_client import ApifyClient
    except ImportError:
        sys.exit("apify-client not installed. Run: pip install -r requirements.txt")

    handles = read_accounts(args.accounts)

    if not args.from_run:
        total = len(handles) * args.limit
        print(f"Accounts:  {len(handles)}")
        print(f"Per account: {args.limit}")
        print(f"Total posts: ~{total}")
        print(f"Est. cost:   ~${total * 0.002:.2f} (at ~$2.00 per 1,000 posts)")
        print()
        print("Confirm every handle in accounts.txt is real and active before spending.")

        if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")

    # VERIFY: this input shape matches apify/instagram-scraper as documented,
    # but has not been run. If the actor errors on startup, check its current
    # input schema on the Apify console and fix here.
    actor_input = {
        "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
        "resultsType": "posts",
        "resultsLimit": args.limit,
        "addParentData": False,
    }

    client = ApifyClient(token)

    if args.from_run:
        print(f"\nHarvesting existing run {args.from_run} -- no new scrape, no cost.")
        run = client.run(args.from_run).get()
        if run is None:
            sys.exit(f"No such run: {args.from_run}")
        if run.status != "SUCCEEDED":
            print(f"  ! run status is {run.status}; harvesting whatever it produced.",
                  file=sys.stderr)
    else:
        print(f"\nRunning {ACTOR_ID} ...")
        run = client.actor(ACTOR_ID).call(run_input=actor_input)

    # `Run` is a pydantic model in apify-client 3.x, not a dict -- attribute access.
    print(f"Run {run.id}: {run.status}, dataset {run.default_dataset_id}")

    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    saved = skipped = 0

    for raw in client.dataset(run.default_dataset_id).iterate_items():
        post = normalize(raw)
        if post is None:
            continue

        dest = POSTS_DIR / f"{post['post_id']}.json"
        if dest.exists():  # idempotent on provider post id
            skipped += 1
            continue

        download_images(post)
        dest.write_text(json.dumps(post, indent=2))
        saved += 1
        print(f"  + {post['account_handle']}/{post['post_id']} "
              f"({len(post['local_images'])} images)")

    print(f"\nSaved {saved} new posts, skipped {skipped} already on disk.")
    print(f"Location: {POSTS_DIR}")
    print("\nNext: python3 spike/score.py --rung 1 --stage gate")


if __name__ == "__main__":
    main()
