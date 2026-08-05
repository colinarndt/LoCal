"""Ingestion. SPEC section 5.1 -- one protocol, one adapter for now.

Phase 2 sources (Eventbrite, Resident Advisor, venue RSS) implement the same
protocol and land in the same `source_post` table under a different source_name.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import spend
from .paths import MEDIA_DIR

MAX_IMAGES = 3
ACTOR_ID = "apify/instagram-scraper"


@dataclass
class RawPost:
    post_id: str
    polled_handle: str | None       # the account WE asked for; None if undeterminable
    attributed_handle: str | None   # who the provider says owns it
    posted_at: str
    caption: str
    permalink: str
    media_kind: str
    image_urls: list[str]
    raw: dict
    local_images: list[str] = field(default_factory=list)


class IngestionSource(Protocol):
    def fetch_recent(self, handles: list[str], limit: int) -> list[RawPost]: ...
    def source_name(self) -> str: ...


def read_accounts(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@")
        if line:
            out.append(line)
    return out


def download_images(post: RawPost, media_dir: Path = MEDIA_DIR) -> None:
    """Store images locally. CDN URLs expire, so hotlinking breaks re-extraction."""
    media_dir.mkdir(parents=True, exist_ok=True)
    for i, url in enumerate(post.image_urls[:MAX_IMAGES]):
        dest = media_dir / f"{post.post_id}_{i}.jpg"
        if dest.exists():
            post.local_images.append(dest.name)
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                dest.write_bytes(resp.read())
            post.local_images.append(dest.name)
        except Exception as exc:
            print(f"  ! image {i} failed for {post.post_id}: {exc}", file=sys.stderr)


class ApifySource:
    """apify/instagram-scraper. Posts and reels only -- stories need an
    authenticated session and are Phase 4 (SPEC section 6)."""

    def __init__(self, token: str, meter: spend.Meter | None = None):
        from apify_client import ApifyClient

        self.client = ApifyClient(token)
        # Drained by `pipeline.ingest`, which holds the database connection.
        self.meter = meter if meter is not None else spend.Meter()

    def source_name(self) -> str:
        return "apify"

    def estimate_cost(self, handles: list[str], limit: int) -> float:
        """Pre-flight guess, shown before you click. Only ever an upper bound --
        actual billed cost comes back on the finished run and is what gets
        recorded to the ledger."""
        return len(handles) * limit * 0.002  # ~$2.00 per 1,000 posts

    def fetch_recent(self, handles: list[str], limit: int = 20,
                     newer_than: str | None = None) -> list[RawPost]:
        run_input = {
            "directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
            "resultsType": "posts",
            "resultsLimit": limit,
            # parent data carries the input profile, which is how a collab post
            # gets attributed back to the account we actually polled
            "addParentData": True,
        }
        if newer_than:
            # Bounds what the provider returns, so we stop paying to re-fetch
            # posts we already stored.
            run_input["onlyPostsNewerThan"] = newer_than
        run = self.client.actor(ACTOR_ID).call(run_input=run_input)
        # Metered here rather than in `_harvest`, which `harvest_run` also calls
        # against an already-paid run -- doing it there would bill twice. In a
        # `finally` because the run is charged the moment it completes, and
        # harvesting it is exactly the step already known to fail on its own.
        posts: list[RawPost] = []
        try:
            posts = self._harvest(run, handles)
        finally:
            self.meter.add_apify(ACTOR_ID, run, units=len(posts),
                                 fallback_usd=self.estimate_cost(handles, limit))
        return posts

    def harvest_run(self, run_id: str, handles: list[str]) -> list[RawPost]:
        """Pull a run that already finished. Free -- it is already paid for,
        which is also why this path records no spend.

        Exists because the scrape succeeding while the client fails downstream
        is a failure mode we have already hit once.
        """
        run = self.client.run(run_id).get()
        if run is None:
            raise ValueError(f"no such run: {run_id}")
        return self._harvest(run, handles)

    def _harvest(self, run, handles: list[str]) -> list[RawPost]:
        # `Run` is a pydantic model in apify-client 3.x, not a dict.
        requested = set(handles)
        posts = []
        for raw in self.client.dataset(run.default_dataset_id).iterate_items():
            p = self._normalize(raw, requested)
            if p:
                posts.append(p)
        return posts

    @staticmethod
    def _polled_handle(raw: dict, attributed: str | None, requested: set[str]) -> str | None:
        """Which account we polled to surface this post.

        Prefer the provider's parent/input data. Fall back to the attributed
        handle when it is one we asked for. Otherwise None -- a collab post from
        an account we never requested, where the originating poll is unknowable.
        """
        for key in ("inputUrl", "parentUrl", "ownerUsernameInput"):
            val = raw.get(key)
            if isinstance(val, str):
                slug = val.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
                if slug in requested:
                    return slug
        parent = raw.get("parentData") or {}
        if isinstance(parent, dict):
            cand = parent.get("ownerUsername") or parent.get("username")
            if cand in requested:
                return cand
        return attributed if attributed in requested else None

    @classmethod
    def _normalize(cls, raw: dict, requested: set[str]) -> RawPost | None:
        post_id = raw.get("id") or raw.get("shortCode")
        if not post_id or not raw.get("timestamp"):
            return None

        urls, seen = [], set()
        for u in ([raw.get("displayUrl")]
                  + [c.get("displayUrl") for c in (raw.get("childPosts") or [])]
                  + [x for x in (raw.get("images") or []) if isinstance(x, str)]):
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

        attributed = raw.get("ownerUsername") or raw.get("username")
        return RawPost(
            post_id=str(post_id),
            polled_handle=cls._polled_handle(raw, attributed, requested),
            attributed_handle=attributed,
            posted_at=raw["timestamp"],
            caption=raw.get("caption") or "",
            permalink=raw.get("url") or f"https://www.instagram.com/p/{post_id}/",
            media_kind="reel" if raw.get("type") == "Video" else "post",
            image_urls=urls[:MAX_IMAGES],
            raw=raw,
        )


class LocalSpikeSource:
    """Reads the Phase 0 corpus off disk. Lets the pipeline be exercised
    end-to-end without spending anything."""

    def __init__(self, posts_dir: Path):
        self.posts_dir = Path(posts_dir)

    def source_name(self) -> str:
        return "spike-local"

    def fetch_recent(self, handles: list[str], limit: int = 0) -> list[RawPost]:
        requested = set(handles)
        out = []
        for f in sorted(self.posts_dir.glob("*.json")):
            d = json.loads(f.read_text())
            attributed = d.get("account_handle")
            out.append(RawPost(
                post_id=d["post_id"],
                polled_handle=attributed if attributed in requested else None,
                attributed_handle=attributed,
                posted_at=d["posted_at"],
                caption=d.get("caption") or "",
                permalink=d.get("permalink", ""),
                media_kind=d.get("media_kind", "post"),
                image_urls=d.get("image_urls", []),
                raw=d.get("raw_provider_json", {}),
                local_images=d.get("local_images", []),
            ))
        return out


def to_row(post: RawPost, source_name: str) -> dict:
    return {
        "post_id": post.post_id,
        "polled_handle": post.polled_handle or "",
        "attributed_handle": post.attributed_handle,
        "posted_at": post.posted_at,
        "caption": post.caption,
        "permalink": post.permalink,
        "media_kind": post.media_kind,
        "local_images": json.dumps(post.local_images),
        "raw_provider_json": json.dumps(post.raw),
        "source_name": source_name,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
