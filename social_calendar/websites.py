"""Structured event ingestion from user-managed website calendars.

The cheap path is deliberately first: iCalendar and schema.org Event JSON-LD
already contain the fields the vision model would be asked to infer. Common
server-rendered event cards are another cheap source when a venue's CMS omits
those standards. Unsupported pages are reported rather than silently pushed
through an expensive, unreliable arbitrary-page model call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from . import config, dedupe

MAX_BYTES = 10 * 1024 * 1024
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
               linked_handle: str | None = None) -> int:
    url = normalize_url(url)
    label = (name or "").strip() or urllib.parse.urlsplit(url).netloc.removeprefix("www.")
    handle = (linked_handle or "").strip().lstrip("@") or None
    existing = conn.execute("SELECT id FROM web_source WHERE url=?", (url,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE web_source SET name=?, linked_handle=?, enabled=1 WHERE id=?",
            (label, handle, existing["id"]))
        return existing["id"]
    return conn.execute(
        "INSERT INTO web_source (name,url,linked_handle,enabled,added_at) "
        "VALUES (?,?,?,1,?)", (label, url, handle, _now())).lastrowid


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

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {k.lower(): (v or "") for k, v in attrs}
        classes = set(values.get("class", "").split())

        if (tag == "div" and self._card is None
                and {"eventItem", "entry"}.issubset(classes)):
            self._card_depth = self._depth
            self._card = {}

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
                if value and self._card is not None:
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
        self.events.append(StructuredEvent(
            external_id=permalink,
            title=title,
            starts_at=start,
            start_time_known=False,
            ends_at=end,
            venue_name=card.get("venue"),
            permalink=permalink,
            category=_category(title),
            raw={"parser": "carbonhouse-card", **card},
        ))


def parse_event_cards(page: str, base_url: str) -> list[StructuredEvent]:
    parser = _EventCardHTML(base_url)
    parser.feed(page)
    return _disambiguate_occurrences(parser.events)


def _walk_json(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "itemListElement", "item"):
            if key in value:
                yield from _walk_json(value[key])


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


def _text(value) -> str | None:
    if isinstance(value, str):
        return html.unescape(value).strip() or None
    if isinstance(value, dict):
        return _text(value.get("name"))
    if isinstance(value, list):
        return next((text for item in value if (text := _text(item))), None)
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


def _category(text: str) -> str:
    lower = text.lower()
    rules = {
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
            title = _text(item.get("name"))
            location = item.get("location")
            venue = _text(location)
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
                raw=item,
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


def _request(url: str, etag: str | None = None, modified: str | None = None,
             opener=urllib.request.urlopen) -> tuple[bytes, dict, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/calendar,text/html,application/xhtml+xml"}
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
        return data, response_headers, response.geturl()


def fetch_events(source: sqlite3.Row | dict, opener=urllib.request.urlopen):
    try:
        data, headers, final_url = _request(source["url"], source["etag"],
                                            source["last_modified"], opener)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, "unchanged", {}, source["url"]
        raise
    text = data.decode("utf-8-sig", errors="replace")
    content_type = headers.get("content-type", "").lower()
    if "text/calendar" in content_type or "BEGIN:VCALENDAR" in text[:1000]:
        return parse_ics(text, final_url), "ics", headers, final_url

    events = parse_jsonld(text, final_url)
    if events:
        return events, "json-ld", headers, final_url

    parser = _StructuredHTML()
    parser.feed(text)
    if parser.calendar_links:
        link = parser.calendar_links[0]
        if link.lower().startswith("webcal://"):
            link = "https://" + link[9:]
        calendar_url = urllib.parse.urljoin(final_url, link)
        child, _, child_final = _request(calendar_url, opener=opener)
        return parse_ics(child.decode("utf-8-sig", errors="replace"), child_final), \
            "linked-ics", headers, child_final

    events = parse_event_cards(text, final_url)
    if events:
        return events, "html-cards", headers, final_url
    return [], "unsupported", headers, final_url


def _stable_post_id(source_id: int, external_id: str) -> str:
    digest = hashlib.sha256(external_id.encode()).hexdigest()[:24]
    return f"web:{source_id}:{digest}"


def _event_hash(event: StructuredEvent) -> str:
    fields = [event.title, event.starts_at, event.ends_at, event.venue_name,
              event.permalink, event.category, event.price_text, event.description]
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _upsert_event(conn: sqlite3.Connection, source, event: StructuredEvent,
                  seen_at: str) -> tuple[bool, bool]:
    existing = conn.execute(
        "SELECT * FROM web_item WHERE source_id=? AND external_id=?",
        (source["id"], event.external_id)).fetchone()
    post_id = existing["post_id"] if existing else _stable_post_id(source["id"], event.external_id)
    digest = _event_hash(event)
    raw = json.dumps(event.raw or {}, ensure_ascii=False)
    caption = "\n\n".join(x for x in (event.title, event.description) if x)

    if existing:
        conn.execute(
            "UPDATE web_item SET last_seen_at=?, content_hash=? WHERE id=?",
            (seen_at, digest, existing["id"]))
        if existing["content_hash"] == digest:
            return False, False
        conn.execute(
            "UPDATE source_post SET caption=?, permalink=?, raw_provider_json=?, fetched_at=? "
            "WHERE post_id=?", (caption, event.permalink, raw, seen_at, post_id))
        conn.execute(
            "UPDATE event SET title=?, starts_at=?, ends_at=?, start_time_known=?, "
            "venue_name=?, venue_key=?, category=?, price_text=?, confidence=1, "
            "date_reasoning='Structured website calendar data' WHERE id=?",
            (event.title, event.starts_at, event.ends_at, int(event.start_time_known),
             event.venue_name, dedupe.normalize_venue(event.venue_name), event.category,
             event.price_text, existing["event_id"]))
        return False, True

    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,attributed_handle,posted_at,caption,"
        "permalink,media_kind,local_images,raw_provider_json,source_name,fetched_at) "
        "VALUES (?,?,?,?,?,?,'web_event','[]',?,'website',?)",
        (post_id, source["linked_handle"] or "", source["linked_handle"], seen_at,
         caption, event.permalink, raw, seen_at))
    marker = conn.execute(
        "INSERT INTO extraction (post_id,stage,prompt_version,model,raw_output,is_error,created_at) "
        "VALUES (?,'structured','structured-v1','parser',?,0,?)",
        (post_id, raw, seen_at)).lastrowid
    event_id = conn.execute(
        "INSERT INTO event (post_id,extraction_id,title,starts_at,ends_at,start_time_known,"
        "venue_name,venue_key,category,price_text,confidence,date_reasoning,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,'Structured website calendar data',?)",
        (post_id, marker, event.title, event.starts_at, event.ends_at,
         int(event.start_time_known), event.venue_name,
         dedupe.normalize_venue(event.venue_name), event.category, event.price_text,
         seen_at)).lastrowid
    conn.execute(
        "INSERT INTO web_item (source_id,external_id,post_id,event_id,content_hash,"
        "first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?)",
        (source["id"], event.external_id, post_id, event_id, digest, seen_at, seen_at))
    conn.execute(
        "INSERT OR IGNORE INTO event_source (event_id,source_kind,source_item_id,permalink,"
        "match_method,created_at) VALUES (?,'website',?,?,'structured',?)",
        (event_id, post_id, event.permalink, seen_at))
    return True, False


def poll_source(conn: sqlite3.Connection, source_id: int,
                opener=urllib.request.urlopen) -> dict:
    source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
    if source is None:
        raise ValueError("no such website source")
    checked = _now()
    try:
        events, kind, headers, _ = fetch_events(source, opener)
        if events is None:
            conn.execute(
                "UPDATE web_source SET last_checked_at=?, last_success_at=?, last_error=NULL "
                "WHERE id=?", (checked, checked, source_id))
            return {"source": source["name"], "found": 0, "new": 0, "updated": 0,
                    "unchanged": True}
        if not events:
            raise ValueError("no iCalendar, schema.org Event, or supported event cards found")
        made = updated = 0
        for event in events:
            is_new, changed = _upsert_event(conn, source, event, checked)
            made += int(is_new)
            updated += int(changed)
        conn.execute(
            "UPDATE web_source SET format=?,etag=?,last_modified=?,last_checked_at=?,"
            "last_success_at=?,last_error=NULL WHERE id=?",
            (kind, headers.get("etag"), headers.get("last-modified"), checked, checked,
             source_id))
        return {"source": source["name"], "found": len(events), "new": made,
                "updated": updated, "unchanged": False}
    except Exception as exc:
        conn.execute(
            "UPDATE web_source SET last_checked_at=?,last_error=? WHERE id=?",
            (checked, f"{type(exc).__name__}: {exc}", source_id))
        return {"source": source["name"], "found": 0, "new": 0, "updated": 0,
                "error": f"{type(exc).__name__}: {exc}"}


def poll_all(conn: sqlite3.Connection, source_ids: list[int] | None = None,
             log=lambda *_: None, opener=urllib.request.urlopen) -> dict:
    where, params = "enabled=1", []
    if source_ids is not None:
        if not source_ids:
            return {"sources": 0, "found": 0, "new": 0, "updated": 0, "errors": 0}
        where += f" AND id IN ({','.join('?' * len(source_ids))})"
        params.extend(source_ids)
    rows = conn.execute(f"SELECT id,name FROM web_source WHERE {where} ORDER BY name", params).fetchall()
    total = {"sources": len(rows), "found": 0, "new": 0, "updated": 0, "errors": 0}
    for row in rows:
        log(f"checking {row['name']} website")
        result = poll_source(conn, row["id"], opener)
        for key in ("found", "new", "updated"):
            total[key] += result.get(key, 0)
        total["errors"] += int("error" in result)
        conn.commit()
    return total
