"""Events you type in yourself, for planning around the ones we found.

Everything else in this database is re-derivable: delete an extraction and it
can be run again, delete an event and the next poll rebuilds it from its source
post. **A manual event is the only row that exists nowhere else.** That single
fact drives the whole module:

- `event.is_manual` exempts it from `rebuild_dedupe`, which is otherwise free to
  merge a row into another and move its provenance. Merging something the user
  typed into a scraped listing would silently rewrite their itinerary.
- It is always canonical, in its own dedupe group.
- It carries `trip_id` directly. Every other row infers its trip from the source
  that produced it, and this one has no source.

It still gets a `source_post` row, the same compatibility trick website imports
use (see `db.SCHEMA`), so it inherits the list, the month grid, the filters,
CSV, and the ICS feed without touching any of them.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import urllib.parse
import uuid

from . import config, dedupe, geo

CATEGORIES = ["music", "theater", "comedy", "food", "market", "art", "opening", "other"]

TITLE_MAX = 200
NOTES_MAX = 4000

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _clean(value: str | None, limit: int, multiline: bool = False) -> str | None:
    """Collapse whitespace and cap the length. Empty becomes None, not ''."""
    text = (value or "").strip() if multiline else " ".join((value or "").split())
    return text[:limit] if text else None


def _valid_link(value: str | None) -> str | None:
    """Accept an ordinary http(s) link, drop anything else silently.

    A `javascript:` URL typed into this form would be rendered as an anchor on
    the event card, so this is a filter and not just tidiness.
    """
    link = (value or "").strip()
    if not link:
        return None
    # Test for a scheme, not for "://" -- `javascript:steal()` contains no "//",
    # so a naive check would prepend https:// and hand back a link that parses
    # as valid with the payload sitting in the host.
    if _SCHEME_RE.match(link):
        if not link.lower().startswith(("http://", "https://")):
            return None
    else:
        link = "https://" + link
    parsed = urllib.parse.urlsplit(link)
    return link if parsed.scheme in ("http", "https") and parsed.netloc else None


def validate(form: dict) -> dict:
    """Normalize submitted fields or raise ValueError with something showable.

    Only a title and a date are required. A date-only entry is a first-class
    result, not a degraded one: "dinner somewhere Thursday" is exactly the kind
    of thing worth writing down before the time is settled.
    """
    title = _clean(form.get("title"), TITLE_MAX)
    if not title:
        raise ValueError("give the event a title")
    try:
        day = dt.date.fromisoformat((form.get("date") or "").strip())
    except ValueError:
        raise ValueError("pick a date for the event")

    starts_at, time_known = day.isoformat(), 0
    raw_time = (form.get("time") or "").strip()
    if raw_time:
        try:
            clock = dt.time.fromisoformat(raw_time)
        except ValueError:
            raise ValueError("the time should look like 19:30")
        starts_at, time_known = f"{day.isoformat()}T{clock:%H:%M:%S}", 1

    category = (form.get("category") or "").strip().lower() or None
    if category and category not in CATEGORIES:
        raise ValueError("unknown category")

    venue = _clean(form.get("venue"), 200)
    return {
        "title": title,
        "starts_at": starts_at,
        "start_time_known": time_known,
        "venue_name": venue,
        "venue_key": dedupe.normalize_venue(venue) if venue else None,
        "category": category,
        "price_text": _clean(form.get("price_text"), 80),
        "ticket_url": _valid_link(form.get("ticket_url")),
        "notes": _clean(form.get("notes"), NOTES_MAX, multiline=True),
    }


def _check_window(fields: dict, trip) -> None:
    """A trip event must fall inside the trip, because the trip view is bounded.

    Without this the default date -- today -- puts a new event outside the
    window it was created in, where the home calendar excludes it for being
    trip-scoped and the trip excludes it for being out of range. The row exists,
    no view can show it, and there is no delete button to reach it.
    """
    from . import trips

    if trip is None:
        return
    if not (trip["starts_on"] <= fields["starts_at"][:10] <= trip["ends_on"]):
        raise ValueError(f"pick a date inside {trips.label(trip)}, "
                         f"or change the trip's dates first")


def _locate(conn: sqlite3.Connection, fields: dict, trip=None) -> tuple:
    """Best-effort coordinates for a typed venue. Never fatal.

    A trip venue is in another metro by definition, so it skips the local-bounds
    guard and anchors on the trip's own city -- the same split `geo.geocode_all`
    makes. "Sam's place" resolves to nothing and the event is simply unlocated.
    """
    venue = fields.get("venue_name")
    if not venue:
        return None, None
    # Nominatim sleeps for its rate limit; do not hold the write lock across it.
    conn.commit()
    if trip is not None:
        hit = geo.geocode_place(f"{venue}, {trip['city']}")
        return (hit["lat"], hit["lon"]) if hit else (None, None)
    hit = geo.geocode_venue(venue, config.load()["city"])
    return (hit["lat"], hit["lon"]) if hit else (None, None)


def add(conn: sqlite3.Connection, form: dict, trip=None, locate=_locate) -> int:
    """Create a hand-entered event, scoped to `trip` when one is being viewed."""
    fields = validate(form)
    _check_window(fields, trip)
    lat, lon = locate(conn, fields, trip)
    post_id = f"manual:{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,attributed_handle,posted_at,"
        "caption,local_images,raw_provider_json,source_name,fetched_at) "
        "VALUES (?,'',NULL,?,?,'[]',?,'manual',?)",
        (post_id, now, fields["notes"] or "", json.dumps({"manual": True}), now))
    return conn.execute(
        "INSERT INTO event (post_id,title,starts_at,start_time_known,venue_name,venue_key,"
        "category,price_text,ticket_url,notes,location_city,location_lat,location_lon,"
        "is_manual,trip_id,dedupe_group,is_canonical,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,1,?)",
        (post_id, fields["title"], fields["starts_at"], fields["start_time_known"],
         fields["venue_name"], fields["venue_key"], fields["category"],
         fields["price_text"], fields["ticket_url"], fields["notes"],
         trip["city"].split(",")[0].strip() if trip else None, lat, lon,
         trip["id"] if trip else None, post_id, now)).lastrowid


def get(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM event WHERE id=? AND is_manual=1",
                        (event_id,)).fetchone()


def update(conn: sqlite3.Connection, event_id: int, form: dict,
           locate=_locate) -> bool:
    """Edit a hand-entered event. Its trip scope does not change here."""
    from . import trips

    existing = get(conn, event_id)
    if existing is None:
        return False
    fields = validate(form)
    trip = trips.get(conn, existing["trip_id"])
    # An edit can walk an event out of its trip just as easily as an add can
    # create it there.
    _check_window(fields, trip)
    lat, lon = existing["location_lat"], existing["location_lon"]
    if fields["venue_name"] != existing["venue_name"]:
        lat, lon = locate(conn, fields, trip)
    conn.execute(
        "UPDATE event SET title=?,starts_at=?,start_time_known=?,venue_name=?,venue_key=?,"
        "category=?,price_text=?,ticket_url=?,notes=?,location_lat=?,location_lon=? "
        "WHERE id=? AND is_manual=1",
        (fields["title"], fields["starts_at"], fields["start_time_known"],
         fields["venue_name"], fields["venue_key"], fields["category"],
         fields["price_text"], fields["ticket_url"], fields["notes"], lat, lon,
         event_id))
    # The caption mirrors the note so search finds it the way it finds any other
    # event's text, rather than needing a special case in the query.
    conn.execute("UPDATE source_post SET caption=? WHERE post_id=?",
                 (fields["notes"] or "", existing["post_id"]))
    return True


def delete(conn: sqlite3.Connection, event_id: int) -> bool:
    """Remove a hand-entered event and its compatibility post row.

    Guarded on `is_manual`: this is the only delete in the app the user can
    reach, and it must not be able to reach a scraped event.
    """
    existing = get(conn, event_id)
    if existing is None:
        return False
    conn.execute("DELETE FROM event_source WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM event WHERE id=? AND is_manual=1", (event_id,))
    conn.execute("DELETE FROM source_post WHERE post_id=?", (existing["post_id"],))
    return True
