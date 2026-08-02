"""Structured event ingestion from user-managed website calendars.

The cheap path is deliberately first: iCalendar and schema.org Event JSON-LD
already contain the fields a model would be asked to infer. Common
server-rendered event cards are another cheap source when a venue's CMS omits
those standards. Only pages that still have no events reach a guarded,
text-only nano-model fallback.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import html
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser

from . import config, dedupe, geo, notifications, paths

MAX_BYTES = 10 * 1024 * 1024
MAX_MODEL_CHARS = 300_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
USER_AGENT = "SocialCalendar/0.1 (+local personal calendar)"


@dataclass
class StructuredEvent:
    external_id: str
    title: str | None
    starts_at: str
    start_time_known: bool
    venue_name: str | None
    permalink: str
    category: str | None = None
    price_text: str | None = None
    ends_at: str | None = None
    description: str = ""
    raw: dict | None = None
    confidence: float = 1.0
    city: str | None = None
    region: str | None = None
    address: str | None = None
    lat: float | None = None
    lon: float | None = None
    ticket_url: str | None = None
    ticket_status: str | None = None
    image_url: str | None = None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("enter a website or calendar URL")
    if value.lower().startswith("webcal://"):
        value = "https://" + value[9:]
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("website URLs must use http or https")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/",
                                    parsed.query, ""))


def add_source(conn: sqlite3.Connection, url: str, name: str | None = None,
               linked_handle: str | None = None, source_type: str = "venue",
               radius_miles: float | None = None, notify: bool = False) -> int:
    url = normalize_url(url)
    label = (name or "").strip() or urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    handle = (linked_handle or "").strip().lstrip("@") or None
    if source_type not in ("venue", "performer"):
        raise ValueError("unknown source type")
    if radius_miles is not None and not (1 <= float(radius_miles) <= 1000):
        raise ValueError("radius must be between 1 and 1000 miles")
    existing = conn.execute("SELECT id FROM web_source WHERE url=?", (url,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE web_source SET name=?, linked_handle=?, source_type=?, radius_miles=?, "
            "notify=?, enabled=1 WHERE id=?",
            (label, handle, source_type, radius_miles, int(notify), existing["id"]))
        if source_type == "performer":
            recalculate_performer(conn, existing["id"])
        return existing["id"]
    return conn.execute(
        "INSERT INTO web_source "
        "(name,url,linked_handle,source_type,radius_miles,notify,enabled,added_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (label, url, handle, source_type, radius_miles, int(notify), _now())).lastrowid


class _StructuredHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.calendar_links: list[str] = []
        self._in_jsonld = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "script" and "ld+json" in values.get("type", "").lower():
            self._in_jsonld = True
            self._parts = []
        if tag.lower() in ("link", "a"):
            kind = values.get("type", "").lower()
            href = values.get("href")
            if href and ("text/calendar" in kind
                         or href.lower().split("?", 1)[0].endswith(".ics")
                         or href.lower().startswith("webcal://")):
                self.calendar_links.append(href)

    def handle_data(self, data):
        if self._in_jsonld:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._in_jsonld:
            self.scripts.append("".join(self._parts))
            self._in_jsonld = False
            self._parts = []


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_PATTERN = "|".join(_MONTHS)
_CARD_DATE = re.compile(
    rf"^({_MONTH_PATTERN})\s+(\d{{1,2}})(?:\s+(\d{{4}}))?"
    rf"(?:\s+to\s+({_MONTH_PATTERN})\s+(\d{{1,2}})\s+(\d{{4}}))?$",
    re.IGNORECASE,
)


def _card_dates(value: str) -> tuple[str | None, str | None]:
    """Parse the accessible date labels used by Carbonhouse event listings."""
    label = re.sub(r"[\s,]+", " ", html.unescape(value or "")).strip()
    match = _CARD_DATE.fullmatch(label)
    if not match:
        return None, None
    start_month, start_day, start_year, end_month, end_day, end_year = match.groups()
    if end_month:
        end_year_number = int(end_year)
        start_year_number = int(start_year) if start_year else end_year_number
        if not start_year and _MONTHS[start_month.lower()] > _MONTHS[end_month.lower()]:
            start_year_number -= 1
        end = dt.date(end_year_number, _MONTHS[end_month.lower()], int(end_day))
    else:
        if not start_year:
            return None, None
        start_year_number = int(start_year)
        end = None
    start = dt.date(start_year_number, _MONTHS[start_month.lower()], int(start_day))
    return start.isoformat(), end.isoformat() if end else None


class _EventCardHTML(HTMLParser):
    """Extract Carbonhouse-style server-rendered event cards.

    Carbonhouse is used by a number of performing-arts venues. Its listing
    pages do not always publish schema.org data, but the visible cards use a
    stable structure and an accessible, unambiguous date label.
    """

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.events: list[StructuredEvent] = []
        self._depth = 0
        self._card_depth: int | None = None
        self._card: dict[str, str] | None = None
        self._captures: dict[str, tuple[str, int, list[str]]] = {}
        self._category_labels: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {k.lower(): (v or "") for k, v in attrs}
        classes = set(values.get("class", "").split())

        if (tag == "span" and "event_filter_item" in classes
                and values.get("data-category")):
            name = f"filter:{values['data-category']}"
            self._captures[name] = (tag, self._depth, [])

        if (tag == "div" and self._card is None
                and {"eventItem", "entry"}.issubset(classes)):
            self._card_depth = self._depth
            self._card = {}
            category_class = next((value for value in classes
                                   if re.fullmatch(r"category_\d+", value)), None)
            if category_class:
                self._card["category_id"] = category_class.removeprefix("category_")

        if self._card is not None:
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and "title" in classes:
                self._captures["title"] = (tag, self._depth, [])
            elif tag == "div" and "date" in classes:
                if values.get("aria-label"):
                    self._card["date"] = values["aria-label"]
                self._captures["date_text"] = (tag, self._depth, [])
            elif tag == "div" and "event_venue" in classes:
                self._captures["venue"] = (tag, self._depth, [])

            if tag == "a" and "title" in self._captures and values.get("href"):
                self._card["permalink"] = urllib.parse.urljoin(
                    self.base_url, values["href"])

        if tag not in self._VOID:
            self._depth += 1

    def handle_data(self, data):
        for _, _, parts in self._captures.values():
            parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in self._VOID:
            self._depth = max(0, self._depth - 1)

        for name, (capture_tag, capture_depth, parts) in list(self._captures.items()):
            if tag == capture_tag and self._depth == capture_depth:
                value = " ".join("".join(parts).split())
                if value and name.startswith("filter:"):
                    self._category_labels[name.removeprefix("filter:")] = value
                elif value and self._card is not None:
                    self._card[name] = value
                del self._captures[name]

        if (self._card is not None and tag == "div"
                and self._depth == self._card_depth):
            self._finish_card()

    def _finish_card(self):
        card = self._card or {}
        self._card = None
        self._card_depth = None
        self._captures.clear()
        title = card.get("title")
        permalink = card.get("permalink")
        start, end = _card_dates(card.get("date") or card.get("date_text") or "")
        if not (title and permalink and start):
            return
        source_category = self._category_labels.get(card.get("category_id", ""), "")
        self.events.append(StructuredEvent(
            external_id=permalink,
            title=title,
            starts_at=start,
            start_time_known=False,
            ends_at=end,
            venue_name=card.get("venue"),
            permalink=permalink,
            category=_category(f"{title} {source_category}"),
            raw={"parser": "carbonhouse-card", **card},
        ))


def parse_event_cards(page: str, base_url: str) -> list[StructuredEvent]:
    parser = _EventCardHTML(base_url)
    parser.feed(page)
    return _disambiguate_occurrences(parser.events)


class _LinkHTML(HTMLParser):
    """Small ordered-link reader for Punchup's server-rendered tour cards."""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a" and self._href is None:
            href = dict(attrs).get("href")
            if href:
                self._href = urllib.parse.urljoin(self.base_url, href)
                self._depth = 1
                self._parts = []
                return
        if self._href is not None:
            self._depth += 1

    def handle_data(self, data):
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if self._href is None:
            return
        self._depth -= 1
        if self._depth == 0:
            text = " ".join("".join(self._parts).split())
            self.links.append((self._href, text))
            self._href, self._parts = None, []


_PUNCHUP_EVENT = re.compile(r"/e/([0-9a-f-]{20,})$")
_PUNCHUP_DAY = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:,?\s+(\d{4}))?\b", re.I)
_PUNCHUP_CITY = re.compile(r"^(.+?),\s*([A-Z]{2})$")
_PUNCHUP_TIME = re.compile(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*[–-]\s*"
                           r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s*(.*)$", re.I)


def _punchup_date(text: str) -> str | None:
    match = _PUNCHUP_DAY.search(text or "")
    if not match:
        return None
    month, day, year = match.groups()
    now = dt.datetime.now(config.tzinfo()).date()
    candidate = dt.date(int(year or now.year), _MONTHS[month.lower()], int(day))
    if year is None and candidate < now - dt.timedelta(days=30):
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate.isoformat()


def _punchup_time(text: str) -> tuple[str | None, str]:
    match = _PUNCHUP_TIME.search(text or "")
    if not match:
        return None, ""
    try:
        clock = dt.datetime.strptime(match.group(1).upper().replace(" ", ""), "%I:%M%p").time()
    except ValueError:
        return None, match.group(2).strip()
    return clock.strftime("%H:%M:%S"), match.group(2).strip()


def _dedupe_tail(value: str) -> str:
    words = value.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            return " ".join(words[:half])
    return value


def parse_punchup(page: str, base_url: str,
                  performer_name: str | None = None) -> list[StructuredEvent]:
    """Read Punchup's ordered tour links without opening ticket-provider URLs."""
    parser = _LinkHTML(base_url)
    parser.feed(page)
    cards: dict[str, dict] = {}
    order: list[str] = []
    current: str | None = None
    for href, text in parser.links:
        match = _PUNCHUP_EVENT.search(urllib.parse.urlsplit(href).path)
        if match:
            external_id = match.group(1)
            if external_id not in cards:
                cards[external_id] = {"url": href, "parts": []}
                order.append(external_id)
            cards[external_id]["parts"].append(text)
            current = external_id
        elif current and text.casefold() == "buy tickets":
            cards[current]["ticket_url"] = href

    events: list[StructuredEvent] = []
    for external_id in order:
        card = cards[external_id]
        parts = [part for part in card["parts"] if part]
        date_text = next((part for part in parts if _punchup_date(part)), "")
        start_day = _punchup_date(date_text)
        city_text = next((part for part in parts if _PUNCHUP_CITY.match(part)), "")
        city_match = _PUNCHUP_CITY.match(city_text)
        timed = next((part for part in parts if _PUNCHUP_TIME.search(part)), "")
        clock, venue = _punchup_time(timed)
        if not start_day or not city_match or not venue:
            continue
        city, region = city_match.groups()
        starts = f"{start_day}T{clock}" if clock else start_day
        events.append(StructuredEvent(
            external_id=external_id, title=performer_name, starts_at=starts,
            start_time_known=bool(clock), venue_name=_dedupe_tail(venue),
            permalink=card["url"], category="comedy", city=city, region=region,
            ticket_url=card.get("ticket_url"),
            ticket_status="tickets" if card.get("ticket_url") else None,
            raw={"parser": "punchup", "city": city, "region": region},
        ))
    return _disambiguate_occurrences(events)


_PUNCHUP_PROFILE = re.compile(
    r'\\"id\\":\\"([0-9a-f-]{36})\\".*?\\"slug\\":\\"([^\\"]+)\\"', re.S)


def _punchup_profile_id(page: str, url: str) -> str | None:
    slug = urllib.parse.urlsplit(url).path.strip("/").split("/", 1)[0]
    for profile_id, profile_slug in _PUNCHUP_PROFILE.findall(page):
        if profile_slug == slug:
            return profile_id
    return None


def parse_punchup_api(body: str, base_url: str,
                      performer_name: str | None = None) -> list[StructuredEvent]:
    """Turn Punchup's public show JSON into ordinary structured events."""
    try:
        shows = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(shows, list):
        return []
    out = []
    for show in shows:
        if not isinstance(show, dict) or not show.get("id"):
            continue
        starts, known = _local_iso(show.get("datetime"))
        if not starts:
            continue
        location = str(show.get("location") or "").strip()
        city_match = _PUNCHUP_CITY.match(location)
        city, region = city_match.groups() if city_match else (location or None, None)
        ticket_url = _text(show.get("ticket_link"))
        sold_out = bool(show.get("is_sold_out"))
        comedian = show.get("comedian") if isinstance(show.get("comedian"), dict) else {}
        title = (_text(show.get("title")) or _text(comedian.get("display_name"))
                 or performer_name)
        out.append(StructuredEvent(
            external_id=str(show["id"]), title=title, starts_at=starts,
            start_time_known=known, venue_name=_text(show.get("venue")),
            permalink=urllib.parse.urljoin(base_url, f"/e/{show['id']}"),
            category="comedy", city=city, region=region,
            ticket_url=ticket_url,
            ticket_status="sold out" if sold_out else ("tickets" if ticket_url else None),
            raw={"parser": "punchup-api", "show": show},
        ))
    return _disambiguate_occurrences(out)


_BANDSINTOWN_ARTIST_ID = re.compile(r'"artistId"\s*:\s*"([^"\\]+)"')
_SQUARESPACE_IDENTIFIER = re.compile(r'"identifier"\s*:\s*"([a-z0-9-]+)"', re.I)
_BANDSINTOWN_WIDGET_ARTIST = re.compile(
    r'data-artist-name\s*=\s*["\']([^"\']+)["\']', re.I)
_BANDSINTOWN_WIDGET_APP = re.compile(
    r'data-app-id\s*=\s*["\']([^"\']+)["\']', re.I)
_RIVERSIDE_UPCOMING = re.compile(
    r'<span>\s*UPCOMING\s*</span>.*?<div class="event-list">(.*?)</div>\s*</div>.*?'
    r'<h3[^>]*>.*?\bPAST\b', re.I | re.S)
_RIVERSIDE_EVENT = re.compile(
    r'<div class="item-show\s+rs_event_details">(.*?)</div>\s*</li>', re.I | re.S)


def _bandsintown_config(page: str) -> tuple[str, str] | None:
    """Find a Squarespace Tour Dates block and its Bandsintown app id.

    Squarespace renders the public event list in the browser, but leaves the
    artist identifier in the page's ``data-block-json``. Its registered
    Bandsintown application name is derived from the site's identifier, which
    lets us use the same public feed the embedded widget uses.
    """
    decoded = html.unescape(page)
    artist = _BANDSINTOWN_ARTIST_ID.search(decoded)
    site = _SQUARESPACE_IDENTIFIER.search(decoded)
    if not (artist and site):
        return None
    return artist.group(1), f"squarespace-{site.group(1)}"


def _bandsintown_widget_config(page: str, page_url: str) -> tuple[str, str] | None:
    """Read a standard Bandsintown v3 widget embedded by a performer site."""
    if "widgetv3.bandsintown.com" not in page.casefold():
        return None
    decoded = html.unescape(page)
    artist = _BANDSINTOWN_WIDGET_ARTIST.search(decoded)
    if not artist:
        return None
    app = _BANDSINTOWN_WIDGET_APP.search(decoded)
    hostname = urllib.parse.urlsplit(page_url).hostname
    if not (app or hostname):
        return None
    return artist.group(1), (app.group(1) if app else f"js_{hostname}")


def _is_bandsintown_event_list(body: str) -> bool:
    try:
        return isinstance(json.loads(body), list)
    except (TypeError, ValueError):
        return False


def _html_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(value)).split())


def parse_riverside_events(page: str, base_url: str,
                           performer_name: str | None = None) -> tuple[bool, list[StructuredEvent]]:
    """Read Riverside's RSEventsPro Upcoming table, including its empty state."""
    section = _RIVERSIDE_UPCOMING.search(page)
    if not section:
        return False, []
    events: list[StructuredEvent] = []
    for card in _RIVERSIDE_EVENT.findall(section.group(1)):
        def field(name: str) -> str:
            match = re.search(r'<li[^>]*class="[^"]*\b' + name + r'\b[^"]*"[^>]*>(.*?)</li>',
                              card, re.I | re.S)
            return _html_text(match.group(1)) if match else ""

        date_text = field("show-date")
        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", date_text)
        if not date_match:
            continue
        venue = field("show-venue") or None
        tour = field("show-tour")
        ticket_match = re.search(r'<li[^>]*class="[^"]*\bshow-tix\b[^"]*"[^>]*>.*?'
                                 r'href=["\']([^"\']+)', card, re.I | re.S)
        ticket_url = (urllib.parse.urljoin(base_url, html.unescape(ticket_match.group(1)))
                      if ticket_match else None)
        starts = date_match.group(1)
        identity = "|".join((starts, venue or "", tour))
        events.append(StructuredEvent(
            external_id=identity, title=performer_name, starts_at=starts,
            start_time_known=False, venue_name=venue, permalink=base_url,
            category="music", description=tour, ticket_url=ticket_url,
            ticket_status="tickets" if ticket_url else None,
            raw={"parser": "riverside-events", "date": date_text, "tour": tour},
        ))
    return True, _disambiguate_occurrences(events)


def parse_bandsintown_api(body: str, performer_name: str | None = None) -> list[StructuredEvent]:
    """Convert Bandsintown's public artist-events response to calendar events."""
    try:
        records = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    artist_image = next((
        _text(record.get("artist", {}).get("image_url"))
        for record in records if isinstance(record, dict)
        and isinstance(record.get("artist"), dict)
        and _text(record["artist"].get("image_url"))), None)
    out: list[StructuredEvent] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("id"):
            continue
        starts, known = _local_iso(record.get("datetime") or record.get("starts_at"))
        if not starts:
            continue
        venue = record.get("venue") if isinstance(record.get("venue"), dict) else {}
        try:
            lat = float(venue["latitude"]) if venue.get("latitude") is not None else None
            lon = float(venue["longitude"]) if venue.get("longitude") is not None else None
        except (TypeError, ValueError):
            lat = lon = None
        offers = record.get("offers") if isinstance(record.get("offers"), list) else []
        ticket_url = next((_text(offer.get("url")) for offer in offers
                           if isinstance(offer, dict) and _text(offer.get("url"))), None)
        offer_status = next((str(offer.get("status") or "").replace("_", " ").lower()
                             for offer in offers if isinstance(offer, dict)), "")
        sold_out = bool(record.get("sold_out")) or offer_status == "sold out"
        artist = record.get("artist") if isinstance(record.get("artist"), dict) else {}
        title = (_text(record.get("title")) or _text(artist.get("name"))
                 or performer_name)
        permalink = _text(record.get("url")) or "https://www.bandsintown.com"
        out.append(StructuredEvent(
            external_id=f"bandsintown:{record['id']}", title=title, starts_at=starts,
            start_time_known=known, venue_name=_text(venue.get("name")),
            permalink=permalink, category=_category(f"{title or ''} tour"),
            city=_text(venue.get("city")), region=_text(venue.get("region")),
            address=_text(venue.get("street_address")), lat=lat, lon=lon,
            ticket_url=ticket_url,
            ticket_status="sold out" if sold_out else (offer_status or None),
            # Bandsintown does not publish a separate show poster in its event
            # API. Its artist image is the page's event image, so use it rather
            # than guessing from ticket-vendor artwork.
            image_url=_text(artist.get("image_url")) or artist_image,
            raw={"parser": "bandsintown-api", "event": record},
        ))
    return _disambiguate_occurrences(out)


def _walk_json(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    elif isinstance(value, dict):
        yield value
        # Event objects are often nested under non-standard keys. For example,
        # Comedy Zone publishes a Place whose events live in capital-E
        # ``Events``. Walking every JSON value is safe and lets the @type test
        # below decide what is actually an event.
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk_json(child)


def _types(value) -> set[str]:
    raw = value.get("@type") if isinstance(value, dict) else None
    vals = raw if isinstance(raw, list) else [raw]
    return {str(v).lower() for v in vals if v}


def _local_iso(value) -> tuple[str | None, bool]:
    if not value:
        return None, False
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text, False
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if parsed.tzinfo:
        parsed = parsed.astimezone(config.tzinfo()).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds"), True


def _schema_all_day_span(start_value, end_value, start: str, end: str | None) -> bool:
    """Recognize a date-only event encoded as an inclusive full-day span.

    Some CMSes serialize all-day entries as 00:00:00 through 23:59:59 instead
    of schema.org's simpler date form. That is an availability convention, not
    a claim that the event starts at midnight.
    """
    start_text, end_text = str(start_value or ""), str(end_value or "")
    midnight = r"\d{4}-\d{2}-\d{2}T00:00:00(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    last_second = r"\d{4}-\d{2}-\d{2}T23:59:59(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    return bool(re.fullmatch(midnight, start_text) and re.fullmatch(last_second, end_text)
                and end and start[:10] == end[:10])


def _text(value) -> str | None:
    if isinstance(value, str):
        return html.unescape(value).strip() or None
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list):
        return next((text for item in value if (text := _text(item))), None)
    return None


def _image_url(value) -> str | None:
    """Return a directly usable image URL from schema.org's flexible shape."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return next((url for item in value if (url := _image_url(item))), None)
    if isinstance(value, dict):
        return _image_url(value.get("url") or value.get("contentUrl"))
    return None


def _price(offers) -> str | None:
    if isinstance(offers, list):
        vals = [p for p in (_price(x) for x in offers) if p]
        return " / ".join(dict.fromkeys(vals)) or None
    if not isinstance(offers, dict):
        return None
    price = offers.get("price")
    if price is None:
        price = offers.get("lowPrice")
    if price is None:
        return None
    currency = str(offers.get("priceCurrency") or "").upper()
    prefix = "$" if currency == "USD" else (currency + " " if currency else "")
    return f"{prefix}{price}"


def _offer_url(offers) -> str | None:
    if isinstance(offers, list):
        return next((url for item in offers if (url := _offer_url(item))), None)
    if not isinstance(offers, dict):
        return None
    return _text(offers.get("url"))


def _location_fields(location) -> dict:
    if not isinstance(location, dict):
        return {"venue_name": _text(location), "city": None, "region": None,
                "address": None, "lat": None, "lon": None}
    address = location.get("address")
    addr = address if isinstance(address, dict) else {}
    geo_data = location.get("geo") if isinstance(location.get("geo"), dict) else {}
    try:
        lat = float(geo_data.get("latitude")) if geo_data.get("latitude") is not None else None
        lon = float(geo_data.get("longitude")) if geo_data.get("longitude") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    pieces = [addr.get("streetAddress"), addr.get("addressLocality"),
              addr.get("addressRegion"), addr.get("postalCode")]
    return {
        "venue_name": _text(location.get("name")) or _text(location),
        "city": _text(addr.get("addressLocality")),
        "region": _text(addr.get("addressRegion")),
        "address": ", ".join(str(part) for part in pieces if part) or _text(address),
        "lat": lat, "lon": lon,
    }


def _category(text: str) -> str:
    lower = text.lower()
    if re.search(r"\b(?:play|plays|drama)\b", lower):
        return "theater"
    rules = {
        "theater": ("theater", "theatre", "broadway", "musical", "stage play",
                    "stage production", "dramatic production"),
        "music": ("concert", "music", "band", "dj", "singer", "tour"),
        "comedy": ("comedy", "comedian", "stand-up", "standup"),
        "food": ("dinner", "tasting", "brunch", "food", "beer", "wine"),
        "market": ("market", "bazaar", "vendor", "flea"),
        "art": ("gallery", "exhibition", "artist", "art "),
        "opening": ("grand opening", "opening reception"),
    }
    for category, words in rules.items():
        if any(word in lower for word in words):
            return category
    return "other"


def _disambiguate_occurrences(events: list[StructuredEvent]) -> list[StructuredEvent]:
    """Some series reuse one URL/UID for several separately dated entries."""
    counts: dict[str, int] = {}
    for event in events:
        counts[event.external_id] = counts.get(event.external_id, 0) + 1
    for event in events:
        if counts[event.external_id] > 1:
            event.external_id = f"{event.external_id}#{event.starts_at}"
    return events


def parse_jsonld(page: str, base_url: str) -> list[StructuredEvent]:
    parser = _StructuredHTML()
    parser.feed(page)
    out: list[StructuredEvent] = []
    for script in parser.scripts:
        try:
            data = json.loads(script.strip())
        except (ValueError, TypeError):
            continue
        for item in _walk_json(data):
            if not any(kind.endswith("event") for kind in _types(item)):
                continue
            start, known = _local_iso(item.get("startDate"))
            if not start:
                continue
            end, _ = _local_iso(item.get("endDate"))
            if _schema_all_day_span(item.get("startDate"), item.get("endDate"), start, end):
                start, end, known = start[:10], end[:10], False
            title = _text(item.get("name"))
            location = _location_fields(item.get("location"))
            venue = location["venue_name"]
            url = urllib.parse.urljoin(base_url, str(item.get("url") or item.get("@id") or base_url))
            description = _text(item.get("description")) or ""
            identity = str(item.get("@id") or item.get("url") or "")
            if not identity:
                identity = "|".join((title or "", start, venue or ""))
            out.append(StructuredEvent(
                external_id=identity, title=title, starts_at=start,
                start_time_known=known, ends_at=end, venue_name=venue,
                permalink=url,
                category=_category(f"{title or ''} {description} {' '.join(_types(item))}"),
                price_text=_price(item.get("offers")), description=description,
                raw=item, city=location["city"], region=location["region"],
                address=location["address"], lat=location["lat"], lon=location["lon"],
                ticket_url=_offer_url(item.get("offers")),
                image_url=_image_url(item.get("image")),
            ))
    return _disambiguate_occurrences(out)


def _unescape_ics(value: str) -> str:
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def _ics_date(value: str) -> tuple[str | None, bool]:
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}", False
    match = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not match:
        return _local_iso(value)
    day, clock, zulu = match.groups()
    parsed = dt.datetime.strptime(day + clock, "%Y%m%d%H%M%S")
    if zulu:
        parsed = parsed.replace(tzinfo=dt.timezone.utc).astimezone(config.tzinfo()).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds"), True


def parse_ics(body: str, base_url: str) -> list[StructuredEvent]:
    unfolded: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line.rstrip("\r"))
    blocks, current = [], None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            blocks.append(current)
            current = None
        elif current is not None and ":" in line:
            lhs, value = line.split(":", 1)
            current[lhs.split(";", 1)[0].upper()] = _unescape_ics(value)
    out = []
    for item in blocks:
        start, known = _ics_date(item.get("DTSTART", ""))
        if not start:
            continue
        end, _ = _ics_date(item.get("DTEND", ""))
        title = item.get("SUMMARY") or None
        venue = item.get("LOCATION") or None
        url = urllib.parse.urljoin(base_url, item.get("URL") or base_url)
        identity = item.get("UID") or "|".join((title or "", start, venue or ""))
        if item.get("RECURRENCE-ID"):
            identity += "#" + item["RECURRENCE-ID"]
        description = item.get("DESCRIPTION") or ""
        out.append(StructuredEvent(
            external_id=identity, title=title, starts_at=start,
            start_time_known=known, ends_at=end, venue_name=venue,
            permalink=url, category=_category(f"{title or ''} {description}"),
            description=description, raw=item,
        ))
    return _disambiguate_occurrences(out)


class _VisibleHTML(HTMLParser):
    """Reduce arbitrary HTML to visible text and explicit absolute links.

    Scripts, styles, navigation, forms, and images are deliberately excluded:
    they add tokens but no calendar facts, and source-page instructions must
    never become instructions to the model.
    """

    _SKIP = {"script", "style", "svg", "noscript", "template", "nav", "header",
             "footer", "form"}
    _BLOCK = {"address", "article", "aside", "blockquote", "br", "dd", "div",
              "dl", "dt", "figcaption", "figure", "h1", "h2", "h3", "h4",
              "h5", "h6", "hr", "li", "main", "p", "section", "table", "td",
              "th", "tr", "ul", "ol"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.links: set[str] = set()
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._skip_depth:
            if tag not in self._VOID:
                self._skip_depth += 1
            return
        if tag in self._SKIP:
            self._skip_depth = 1
            return
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and values.get("href"):
            url = urllib.parse.urljoin(self.base_url, values["href"])
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                               parsed.query, ""))
                self.links.add(url)
                self.parts.append(f"\nLINK {url}\n")
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text + " ")


def model_page_text(page: str, base_url: str) -> tuple[str, set[str]]:
    """Return bounded visible page text plus the exact links it contains."""
    parser = _VisibleHTML(base_url)
    parser.feed(page)
    lines: list[str] = []
    previous = None
    for raw in "".join(parser.parts).splitlines():
        line = " ".join(raw.split())
        if line and line != previous:
            lines.append(line)
            previous = line
    text = "\n".join(lines)
    if len(text) > MAX_MODEL_CHARS:
        text = text[:MAX_MODEL_CHARS] + "\n[page text truncated]"
    return text, parser.links


def _events_from_model(output: dict, page_text: str, links: set[str],
                       base_url: str, model: str | None = None) -> list[StructuredEvent]:
    """Validate model output against the page before it can reach storage."""
    if "_error" in output:
        raise ValueError(f"AI website extraction failed: {output['_error']}")
    visible = " ".join(page_text.casefold().split())
    base_parts = urllib.parse.urlsplit(base_url)
    page_url = urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc,
                                       base_parts.path, base_parts.query, ""))
    allowed_links = links | {page_url}
    events: list[StructuredEvent] = []
    for item in output.get("events") or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split())
        if not title or " ".join(title.casefold().split()) not in visible:
            continue
        start, parsed_time_known = _local_iso(item.get("starts_at"))
        if not start:
            continue
        raw_url = str(item.get("permalink") or "").strip()
        url = urllib.parse.urljoin(base_url, raw_url)
        parsed_url = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc,
                                      parsed_url.path, parsed_url.query, ""))
        if parsed_url.scheme not in ("http", "https") or url not in allowed_links:
            continue
        time_known = bool(item.get("start_time_known")) and parsed_time_known
        if not time_known:
            start = start[:10]
        end, _ = _local_iso(item.get("ends_at"))
        category = str(item.get("category") or "other")
        if category not in {"music", "theater", "comedy", "food", "market",
                            "art", "opening", "other"}:
            category = "other"
        venue = _text(item.get("venue_name"))
        description = _text(item.get("description")) or ""
        price = _text(item.get("price_text"))
        events.append(StructuredEvent(
            external_id="|".join((url, start, title.casefold())),
            title=title,
            starts_at=start,
            start_time_known=time_known,
            ends_at=end,
            venue_name=venue,
            permalink=url,
            category=category,
            price_text=price,
            description=description,
            raw={"parser": "nano-html", "model": model or "nano", **item},
            confidence=0.8,
        ))
    return _disambiguate_occurrences(events)


@dataclass
class FetchResult:
    events: list[StructuredEvent] | None
    kind: str
    headers: dict
    final_url: str
    content_hash: str | None = None
    model_output: str | None = None
    model: str | None = None

    def __iter__(self):
        """Keep the original four-value unpacking API for callers and tests."""
        yield self.events
        yield self.kind
        yield self.headers
        yield self.final_url


def _request(url: str, etag: str | None = None, modified: str | None = None,
             opener=urllib.request.urlopen, fresh: bool = False) -> tuple[bytes, dict, str]:
    headers = {"User-Agent": USER_AGENT,
               # Some normal web pages (including Shopify event pages) treat
               # a calendar-first Accept header as a request for a different
               # route and respond 404. Calendar feeds identify themselves by
               # content type/body, so prefer ordinary HTML here.
               "Accept": "text/html,application/xhtml+xml,text/calendar",
               "Accept-Encoding": "gzip, deflate"}
    if fresh:
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("calendar response is larger than 10 MB")
        response_headers = {k.lower(): v for k, v in response.headers.items()}
        encoding = response_headers.get("content-encoding", "").lower()
        if "gzip" in encoding:
            data = gzip.decompress(data)
        elif "deflate" in encoding:
            data = zlib.decompress(data)
        if len(data) > MAX_BYTES:
            raise ValueError("decompressed calendar response is larger than 10 MB")
        return data, response_headers, response.geturl()


def _parse_response(data: bytes, headers: dict, final_url: str,
                    opener, source=None) -> tuple[list[StructuredEvent], str, str]:
    text = data.decode("utf-8-sig", errors="replace")
    content_type = headers.get("content-type", "").lower()
    if "text/calendar" in content_type or "BEGIN:VCALENDAR" in text[:1000]:
        return parse_ics(text, final_url), "ics", final_url

    source_type = source["source_type"] if source is not None and "source_type" in source.keys() else "venue"
    if source_type == "performer" and urllib.parse.urlsplit(final_url).netloc.endswith("punchup.live"):
        events = parse_punchup(text, final_url, source["name"])
        if events:
            return events, "punchup", final_url

    events = parse_jsonld(text, final_url)
    if events:
        return events, "json-ld", final_url

    parser = _StructuredHTML()
    parser.feed(text)
    if parser.calendar_links:
        link = parser.calendar_links[0]
        if link.lower().startswith("webcal://"):
            link = "https://" + link[9:]
        calendar_url = urllib.parse.urljoin(final_url, link)
        child, _, child_final = _request(calendar_url, opener=opener)
        return parse_ics(child.decode("utf-8-sig", errors="replace"), child_final), \
            "linked-ics", child_final

    events = parse_event_cards(text, final_url)
    if events:
        return events, "html-cards", final_url
    if urllib.parse.urlsplit(final_url).netloc.endswith("riversideband.pl"):
        recognized, events = parse_riverside_events(
            text, final_url, source["name"] if source is not None else None)
        if recognized:
            return events, "riverside-events", final_url
    visible, _ = model_page_text(text, final_url)
    if re.search(r"\bno upcoming (?:tour )?(?:dates|shows|events)\b.{0,80}"
                 r"\bcheck back soon\b", visible, re.I | re.S):
        # A clear no-dates notice is a successful check, not an extraction
        # failure. It remains visible as zero tour dates in the source list.
        return [], "empty-tour-page", final_url
    return [], "unsupported", final_url


def fetch_events(source: sqlite3.Row | dict, opener=urllib.request.urlopen,
                 extractor=None, model_cache: sqlite3.Row | dict | None = None):
    request_url = source["url"]
    last_headers: dict = {}
    last_final_url = request_url
    last_text = ""
    for attempt in range(4):
        try:
            data, headers, final_url = _request(
                request_url,
                source["etag"] if attempt == 0 else None,
                source["last_modified"] if attempt == 0 else None,
                opener,
                fresh=attempt > 0,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResult(None, "unchanged", {}, source["url"])
            raise
        events, kind, parsed_url = _parse_response(data, headers, final_url, opener, source)
        if events or kind != "unsupported":
            return FetchResult(events, kind, headers, parsed_url)

        text = data.decode("utf-8-sig", errors="replace")
        source_type = source["source_type"] if "source_type" in source.keys() else "venue"
        if (source_type == "performer"
                and urllib.parse.urlsplit(final_url).netloc.endswith("punchup.live")):
            performer_id = _punchup_profile_id(text, final_url)
            if performer_id:
                api_url = (urllib.parse.urljoin(final_url, "/api/shows") + "?"
                           + urllib.parse.urlencode({"comedianId": performer_id}))
                api_data, _, _ = _request(api_url, opener=opener)
                events = parse_punchup_api(
                    api_data.decode("utf-8-sig", errors="replace"), final_url, source["name"])
                if events:
                    return FetchResult(events, "punchup-api", headers, final_url)
        widget = _bandsintown_widget_config(text, final_url)
        if widget:
            artist, app_id = widget
            api_url = "https://rest.bandsintown.com/V3.1/artists/" + urllib.parse.quote(
                artist, safe="") + "/events?" + urllib.parse.urlencode({"app_id": app_id})
            try:
                api_data, _, _ = _request(api_url, opener=opener)
            except (urllib.error.URLError, ValueError):
                api_data = None
            if api_data is not None:
                body = api_data.decode("utf-8-sig", errors="replace")
                if _is_bandsintown_event_list(body):
                    name = source.get("name") if isinstance(source, dict) else source["name"]
                    return FetchResult(parse_bandsintown_api(body, name),
                                       "bandsintown-api", headers, final_url)
        bandsintown = _bandsintown_config(text)
        if bandsintown:
            artist, app_id = bandsintown
            api_url = "https://rest.bandsintown.com/artists/" + urllib.parse.quote(
                artist, safe="") + "/events?" + urllib.parse.urlencode({"app_id": app_id})
            try:
                api_data, _, _ = _request(api_url, opener=opener)
            except (urllib.error.URLError, ValueError):
                # A widget can be configured with an old or private app id;
                # retain the normal fallback path instead of failing the source.
                api_data = None
            if api_data is not None:
                events = parse_bandsintown_api(
                    api_data.decode("utf-8-sig", errors="replace"), source.get("name")
                    if isinstance(source, dict) else source["name"])
                if events:
                    return FetchResult(events, "bandsintown-api", headers, final_url)
        last_headers, last_final_url, last_text = headers, final_url, text
        redirected = final_url.rstrip("/") != request_url.rstrip("/")
        incomplete = "<title" not in text.lower() or "</html>" not in text.lower()
        if attempt == 3 or not (redirected or incomplete):
            break
        # Retry a genuinely incomplete response at the canonical target, but
        # never retry a complete page that is simply unsupported.
        request_url = final_url

    page_text, links = model_page_text(last_text, last_final_url)
    digest = hashlib.sha256(page_text.encode()).hexdigest()
    from .extract import WEBSITE_PROMPT_VERSION

    def cached(name, default=None):
        if model_cache is None:
            return default
        try:
            return model_cache[name]
        except (KeyError, IndexError):
            return default

    raw_output = None
    model = None
    kind = "unsupported"
    if (cached("content_hash") == digest
            and cached("prompt_version") == WEBSITE_PROMPT_VERSION
            and cached("raw_output")):
        try:
            output = json.loads(cached("raw_output"))
        except (TypeError, ValueError):
            output = None
        if isinstance(output, dict):
            raw_output = cached("raw_output")
            model = cached("model")
            kind = "model-cache"
    elif extractor is not None and page_text:
        output = extractor.website(page_text, last_final_url)
        raw_output = json.dumps(output, ensure_ascii=False)
        model = (extractor.model_for("website") if hasattr(extractor, "model_for")
                 else getattr(extractor, "model", "nano"))
        kind = "model-html"
    else:
        output = None

    if isinstance(output, dict):
        events = _events_from_model(output, page_text, links, last_final_url, model)
        return FetchResult(events, kind, last_headers, last_final_url,
                           digest, raw_output, model)
    return FetchResult([], "unsupported", last_headers, last_final_url,
                       digest)


def _stable_post_id(source_id: int, external_id: str) -> str:
    digest = hashlib.sha256(external_id.encode()).hexdigest()[:24]
    return f"web:{source_id}:{digest}"


def _event_hash(event: StructuredEvent) -> str:
    fields = [event.title, event.starts_at, event.ends_at, event.venue_name,
              event.permalink, event.category, event.price_text, event.description,
              event.confidence, event.city, event.region, event.address, event.lat,
              event.lon, event.ticket_url, event.ticket_status]
    fields.append(event.image_url)
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _event_images(event: StructuredEvent) -> list[str]:
    """Cache a trusted event image locally, without making image failure fatal."""
    url = (event.image_url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    paths.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    existing = next(paths.MEDIA_DIR.glob(f"web-{digest}.*"), None)
    if existing:
        return [existing.name]
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES or not content_type.startswith("image/"):
            return []
    except (OSError, urllib.error.URLError, ValueError):
        return []
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
              "image/gif": ".gif", "image/avif": ".avif"}.get(content_type, ".img")
    dest = paths.MEDIA_DIR / f"web-{digest}{suffix}"
    try:
        dest.write_bytes(data)
    except OSError:
        return []
    return [dest.name]


def _location_key(event: StructuredEvent) -> str:
    return "|".join(" ".join((value or "").casefold().split()) for value in (
        event.venue_name, event.city, event.region, event.address))


def _resolve_performer_location(conn: sqlite3.Connection, event: StructuredEvent) -> None:
    if event.lat is not None and event.lon is not None:
        return
    key = _location_key(event)
    if not key:
        return
    cached = conn.execute("SELECT lat,lon,address FROM location_cache WHERE location_key=?", (key,)).fetchone()
    if cached:
        event.lat, event.lon = cached["lat"], cached["lon"]
        event.address = event.address or cached["address"]
        return
    query = ", ".join(part for part in (event.venue_name, event.address, event.city, event.region) if part)
    # Nominatim deliberately pauses between calls. Release the SQLite writer
    # while it waits so a long national tour does not freeze the calendar UI.
    conn.commit()
    hit = geo.geocode_place(query)
    if hit:
        event.lat, event.lon = hit["lat"], hit["lon"]
        event.address = event.address or hit["address"]
    conn.execute(
        "INSERT INTO location_cache (location_key,lat,lon,address,geocoded_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(location_key) DO UPDATE SET lat=excluded.lat,lon=excluded.lon,"
        "address=excluded.address,geocoded_at=excluded.geocoded_at",
        (key, event.lat, event.lon, event.address, _now()))


def _qualify_performer(conn: sqlite3.Connection, source, event: StructuredEvent) -> tuple[bool, float | None]:
    _resolve_performer_location(conn, event)
    cfg = config.load()
    lat0, lon0 = cfg["lat"], cfg["lon"]
    if event.lat is None or event.lon is None:
        return False, None
    distance = geo.haversine_miles(float(lat0), float(lon0), event.lat, event.lon)
    return distance <= float(source["radius_miles"] or 250), distance


def recalculate_performer(conn: sqlite3.Connection, source_id: int) -> int:
    """Reapply a performer's radius to cached tour dates after an edit."""
    source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
    if source is None or source["source_type"] != "performer":
        return 0
    rows = conn.execute(
        "SELECT wi.id,wi.event_id,e.location_lat,e.location_lon FROM web_item wi "
        "JOIN event e ON e.id=wi.event_id WHERE wi.source_id=?", (source_id,)).fetchall()
    cfg = config.load()
    lat0, lon0 = cfg["lat"], cfg["lon"]
    changed = 0
    for row in rows:
        if row["location_lat"] is None or row["location_lon"] is None:
            in_range, distance = 0, None
        else:
            distance = geo.haversine_miles(float(lat0), float(lon0),
                                           row["location_lat"], row["location_lon"])
            in_range = int(distance <= float(source["radius_miles"] or 250))
        cur = conn.execute("UPDATE web_item SET in_range=?,distance_miles=? WHERE id=? "
                           "AND (in_range!=? OR distance_miles IS NOT ?)",
                           (in_range, distance, row["id"], in_range, distance))
        changed += int(cur.rowcount)
    return changed


def _upsert_event(conn: sqlite3.Connection, source, event: StructuredEvent,
                  seen_at: str, in_range: bool = True,
                  distance_miles: float | None = None) -> tuple[bool, bool]:
    existing = conn.execute(
        "SELECT * FROM web_item WHERE source_id=? AND external_id=?",
        (source["id"], event.external_id)).fetchone()
    post_id = existing["post_id"] if existing else _stable_post_id(source["id"], event.external_id)
    digest = _event_hash(event)
    raw = json.dumps(event.raw or {}, ensure_ascii=False)
    caption = "\n\n".join(x for x in (event.title, event.description) if x)
    is_model = (event.raw or {}).get("parser") == "nano-html"
    reasoning = ("AI extraction from visible website text" if is_model
                 else "Structured website calendar data")
    # Performer sources cache nationwide dates, but only nearby shows are
    # displayed. Do not download artwork for invisible events.
    local_images = _event_images(event) if in_range else []
    local_images_json = json.dumps(local_images)

    if existing:
        conn.execute(
            "UPDATE web_item SET last_seen_at=?, content_hash=?, in_range=?, distance_miles=? WHERE id=?",
            (seen_at, digest, int(in_range), distance_miles, existing["id"]))
        if existing["content_hash"] == digest:
            return False, False
        conn.execute(
            "UPDATE source_post SET caption=?, permalink=?, local_images=CASE WHEN ?!='[]' THEN ? "
            "ELSE local_images END, raw_provider_json=?, fetched_at=? "
            "WHERE post_id=?", (caption, event.permalink, local_images_json,
                                  local_images_json, raw, seen_at, post_id))
        conn.execute(
            "UPDATE event SET title=?, starts_at=?, ends_at=?, start_time_known=?, "
            "venue_name=?, venue_key=?, category=?, price_text=?, confidence=?, "
            "date_reasoning=?, location_city=?, location_region=?, location_lat=?, "
            "location_lon=?, ticket_url=?, ticket_status=? WHERE id=?",
            (event.title, event.starts_at, event.ends_at, int(event.start_time_known),
             event.venue_name, dedupe.normalize_venue(event.venue_name), event.category,
             event.price_text, event.confidence, reasoning, event.city, event.region,
             event.lat, event.lon, event.ticket_url, event.ticket_status, existing["event_id"]))
        return False, True

    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,attributed_handle,posted_at,caption,"
        "permalink,media_kind,local_images,raw_provider_json,source_name,fetched_at) "
        "VALUES (?,?,?,?,?,?,'web_event',?,?,'website',?)",
        (post_id, source["linked_handle"] or "", source["linked_handle"], seen_at,
         caption, event.permalink, local_images_json, raw, seen_at))
    stage = "website" if is_model else "structured"
    prompt_version = "website-v1" if is_model else "structured-v1"
    model = (event.raw or {}).get("model", "nano") if is_model else "parser"
    marker = conn.execute(
        "INSERT INTO extraction (post_id,stage,prompt_version,model,raw_output,is_error,created_at) "
        "VALUES (?,?,?,?,?,0,?)",
        (post_id, stage, prompt_version, model, raw, seen_at)).lastrowid
    event_id = conn.execute(
        "INSERT INTO event (post_id,extraction_id,title,starts_at,ends_at,start_time_known,"
        "venue_name,venue_key,category,price_text,confidence,date_reasoning,location_city,"
        "location_region,location_lat,location_lon,ticket_url,ticket_status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (post_id, marker, event.title, event.starts_at, event.ends_at,
         int(event.start_time_known), event.venue_name,
         dedupe.normalize_venue(event.venue_name), event.category, event.price_text,
         event.confidence, reasoning, event.city, event.region, event.lat, event.lon,
         event.ticket_url, event.ticket_status, seen_at)).lastrowid
    conn.execute(
        "INSERT INTO web_item (source_id,external_id,post_id,event_id,content_hash,in_range,"
        "distance_miles,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (source["id"], event.external_id, post_id, event_id, digest, int(in_range),
         distance_miles, seen_at, seen_at))
    conn.execute(
        "INSERT OR IGNORE INTO event_source (event_id,source_kind,source_item_id,permalink,"
        "match_method,created_at) VALUES (?,'website',?,?,?,?)",
        (event_id, post_id, event.permalink,
         "extracted" if is_model else "structured", seen_at))
    return True, False


def poll_source(conn: sqlite3.Connection, source_id: int,
                opener=urllib.request.urlopen, extractor=None) -> dict:
    source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise ValueError("no such website source")
    checked = _now()
    try:
        cache = conn.execute(
            "SELECT * FROM web_parse_cache WHERE source_id=?", (source_id,)).fetchone()
        result = fetch_events(source, opener, extractor=extractor, model_cache=cache)
        events, kind, headers, _ = result
        if events is None:
            conn.execute(
                "UPDATE web_source SET last_checked_at=?, last_success_at=?, last_error=NULL "
                "WHERE id=?", (checked, checked, source_id))
            return {"source": source["name"], "found": 0, "new": 0, "updated": 0,
                    "unchanged": True}
        if result.model_output is not None:
            from .extract import WEBSITE_PROMPT_VERSION
            conn.execute(
                "INSERT INTO web_parse_cache "
                "(source_id,content_hash,prompt_version,model,raw_output,created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
                "content_hash=excluded.content_hash,prompt_version=excluded.prompt_version,"
                "model=excluded.model,raw_output=excluded.raw_output,created_at=excluded.created_at",
                (source_id, result.content_hash, WEBSITE_PROMPT_VERSION,
                 result.model or "nano", result.model_output, checked))
        if not events:
            suffix = ("; AI fallback is unavailable because OPENAI_API_KEY is not set"
                      if extractor is None else "; AI fallback found no verifiable events")
            raise ValueError(
                "no iCalendar, schema.org Event, or supported event cards found" + suffix)
        made = updated = alerts = 0
        for event in events:
            performer = source["source_type"] == "performer"
            in_range, distance = (_qualify_performer(conn, source, event) if performer
                                  else (True, None))
            before = conn.execute(
                "SELECT in_range FROM web_item WHERE source_id=? AND external_id=?",
                (source_id, event.external_id)).fetchone()
            is_new, changed = _upsert_event(conn, source, event, checked, in_range, distance)
            made += int(is_new)
            updated += int(changed)
            if performer and source["notify"] and in_range and (is_new or changed or not before or not before["in_range"]):
                item = conn.execute(
                    "SELECT content_hash FROM web_item WHERE source_id=? AND external_id=?",
                    (source_id, event.external_id)).fetchone()
                alert_kind = "new" if is_new else ("nearby" if before and not before["in_range"] else "updated")
                where = ", ".join(part for part in (event.venue_name, event.city, event.region) if part)
                when = event.starts_at[:10]
                alerts += int(notifications.enqueue(
                    conn, source_id, event.external_id, item["content_hash"], alert_kind,
                    event.title or source["name"], f"{when} · {where}", event.ticket_url or event.permalink))
        conn.execute(
            "UPDATE web_source SET format=?,etag=?,last_modified=?,last_checked_at=?,"
            "last_success_at=?,last_error=NULL WHERE id=?",
            (kind, headers.get("etag"), headers.get("last-modified"), checked, checked,
             source_id))
        return {"source": source["name"], "found": len(events), "new": made,
                "updated": updated, "alerts": alerts, "unchanged": False}
    except Exception as exc:
        conn.execute(
            "UPDATE web_source SET last_checked_at=?,last_error=? WHERE id=?",
            (checked, f"{type(exc).__name__}: {exc}", source_id))
        return {"source": source["name"], "found": 0, "new": 0, "updated": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def poll_all(conn: sqlite3.Connection, source_ids: list[int] | None = None,
             log=lambda *_: None, opener=urllib.request.urlopen, extractor=None) -> dict:
    where, params = "enabled=1", []
    if source_ids is not None:
        if not source_ids:
            return {"sources": 0, "found": 0, "new": 0, "updated": 0,
                    "errors": 0, "error_messages": []}
        where += f" AND id IN ({','.join('?' * len(source_ids))})"
        params.extend(source_ids)
    rows = conn.execute(f"SELECT id,name FROM web_source WHERE {where} ORDER BY name", params).fetchall()
    total = {"sources": len(rows), "found": 0, "new": 0, "updated": 0, "alerts": 0,
             "errors": 0, "error_messages": []}
    for row in rows:
        log(f"checking {row['name']} website")
        result = poll_source(conn, row["id"], opener, extractor=extractor)
        for key in ("found", "new", "updated", "alerts"):
            total[key] += result.get(key, 0)
        total["errors"] += int("error" in result)
        if "error" in result:
            message = f"{result['source']}: {result['error']}"
            total["error_messages"].append(message)
            log(message)
        conn.commit()
    return total
