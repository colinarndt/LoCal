"""Bounded local-browser rendering for website calendars.

This is deliberately optional: importing the module does not require
Playwright, and a missing browser simply leaves the normal text-only fallback
in place. When available, rendering supplies compact visible event-card
evidence rather than arbitrary page markup to the extractor.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from dataclasses import dataclass


RENDER_TIMEOUT_MS = 12_000
RENDER_SETTLE_MS = 5_000
MAX_CARDS = 100
MAX_CARD_CHARS = 2_000
MAX_EVIDENCE_CHARS = 120_000


@dataclass(frozen=True)
class RenderedPage:
    """Sanitized evidence from a browser-rendered calendar page."""
    url: str
    evidence: str
    links: set[str]


_CARD_SCRIPT = r"""body => {
  const month = "(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)";
  const weekday = "(?:mon|tues?|wednes?|thurs?|fri|satur?|sun)?";
  const date = new RegExp("\\b" + weekday + "\\s*,?\\s*(?:" + month + "\\.?\\s+\\d{1,2}|\\d{1,2}\\s+" + month + ")\\b|\\b\\d{1,2}[/-]\\d{1,2}(?:[/-]\\d{2,4})?\\b", "i");
  const selectors = ["article", "[role=listitem]", "[data-event-id]", "[class*=event]", "[class*=Event]"];
  const seen = new Set();
  const cards = [];
  for (const selector of selectors) {
    for (const node of body.querySelectorAll(selector)) {
      const text = (node.innerText || "").replace(/\s+/g, " ").trim();
      if (text.length < 12 || !date.test(text)) continue;
      const links = [...node.querySelectorAll("a[href]")].map(link => link.href)
        .filter(href => /^https?:\/\//i.test(href));
      const key = text.slice(0, 500) + "|" + links.join("|");
      if (seen.has(key)) continue;
      seen.add(key);
      cards.push({text, links});
    }
  }
  if (cards.length) return cards;
  const text = (body.innerText || "").replace(/\s+/g, " ").trim();
  const links = [...body.querySelectorAll("a[href]")].map(link => link.href)
    .filter(href => /^https?:\/\//i.test(href));
  return text && date.test(text) ? [{text, links}] : [];
}"""


def _safe_link(value: str) -> str | None:
    parsed = urllib.parse.urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                   parsed.query, ""))


def event_card_evidence(cards: list[dict], fetched_at: dt.date | None = None) -> tuple[str, set[str]]:
    """Turn browser-visible event cards into bounded model evidence."""
    fetched_at = fetched_at or dt.datetime.now().date()
    parts = [f"Trusted retrieval date: {fetched_at.isoformat()}."]
    links: set[str] = set()
    for card in cards[:MAX_CARDS]:
        text = " ".join(str(card.get("text") or "").split())[:MAX_CARD_CHARS]
        if not text:
            continue
        parts.extend(("EVENT CARD", text))
        for raw_url in card.get("links") or []:
            if url := _safe_link(raw_url):
                links.add(url)
                parts.append(f"LINK {url}")
    evidence = "\n".join(parts)
    return evidence[:MAX_EVIDENCE_CHARS], links


class PlaywrightRenderer:
    """Render one page locally, blocking heavyweight non-essential assets."""
    def render(self, url: str) -> RenderedPage | None:
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError:
            return None

        def route_request(route):
            if route.request.resource_type in {"font", "image", "media"}:
                route.abort()
            else:
                route.continue_()

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = None
                try:
                    context = browser.new_context(user_agent=(
                        "Mozilla/5.0 (Macintosh; ARM Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/151.0.0.0 Safari/537.36"))
                    page = context.new_page()
                    page.route("**/*", route_request)
                    page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
                    page.wait_for_timeout(RENDER_SETTLE_MS)
                    cards: list[dict] = []
                    for frame in page.frames:
                        try:
                            found = frame.locator("body").evaluate(_CARD_SCRIPT,
                                                                   timeout=RENDER_TIMEOUT_MS)
                        except (Error, TimeoutError):
                            continue
                        if isinstance(found, list):
                            cards.extend(item for item in found if isinstance(item, dict))
                finally:
                    if context is not None:
                        context.close()
                    browser.close()
        except (Error, TimeoutError, OSError):
            return None

        evidence, links = event_card_evidence(cards)
        if not cards:
            return None
        return RenderedPage(url=url, evidence=evidence, links=links)


def configured_renderer() -> PlaywrightRenderer | None:
    """Return the local renderer unless a user explicitly opts out."""
    if os.getenv("LOCAL_CALENDAR_RENDER_WEBSITES", "1").casefold() in {"0", "false", "no"}:
        return None
    return PlaywrightRenderer()
