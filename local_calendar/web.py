"""Phase 2: local web UI + ICS feed. SPEC section 9.

Server-rendered HTML, no SPA. Responsive from the start because the point is to
pull it up on a phone (over Tailscale/LAN -- no auth, single user).

    python -m local_calendar.web
    open http://localhost:8730
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import json
import os
import re
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from dotenv import load_dotenv
from flask import (Flask, Response, abort, redirect, render_template, request,
                   send_from_directory, url_for)
from markupsafe import Markup, escape

from . import (config, db, discovery, geo, manual, notifications, paths, runner, spend,
               trips, websites)

# Only so /settings can report whether a key is present. Values are never
# rendered, logged, or accepted over HTTP -- see the note above that route.
load_dotenv(paths.ENV_LOCAL_PATH)
load_dotenv(paths.ENV_PATH)

# Templates ship *with* the package (read-only, and bundled into the .app);
# everything writable comes from `paths`. Keeping the two straight is the whole
# point of that module.
app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
app.config["DB"] = str(db.DB_PATH)
# Single-user local app: pick up template edits without a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

MEDIA_DIRS = [paths.MEDIA_DIR]
AVATAR_DIR = paths.AVATAR_DIR


@app.route("/avatar/<path:name>")
def avatar(name: str):
    safe = Path(name).name
    if (AVATAR_DIR / safe).exists():
        return send_from_directory(AVATAR_DIR, safe, max_age=604800)
    abort(404)


@app.route("/media/<path:name>")
def media(name: str):
    """Serve a stored flyer. Basename-only: the name comes from the DB, but
    treating it as a path would still be a traversal risk."""
    safe = Path(name).name
    for d in MEDIA_DIRS:
        if (d / safe).exists():
            return send_from_directory(d, safe, max_age=86400)
    abort(404)


def _local_datetime(value: str | None) -> dt.datetime | None:
    """Parse a stored ISO value and convert aware timestamps to the app zone.

    Event extraction intentionally stores naive datetimes as already-local wall
    time. Operational timestamps are UTC-aware. Keeping those two conventions
    here prevents either kind from being shifted incorrectly for display.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            day = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
        return dt.datetime.combine(day, dt.time())
    return parsed.astimezone(config.tzinfo()) if parsed.tzinfo else parsed


@app.template_filter("short_date")
def short_date(value: str | None) -> str:
    """A local, compact date such as ``Aug 1``."""
    parsed = _local_datetime(value)
    if parsed is None:
        return str(value or "")
    return parsed.strftime("%b %-d")


@app.template_filter("local_stamp")
def local_stamp(value: str | None) -> str:
    """A UTC-safe UI timestamp such as ``Aug 1, 1:30pm``."""
    parsed = _local_datetime(value)
    if parsed is None:
        return str(value or "")
    date = short_date(value)
    hour = parsed.hour % 12 or 12
    suffix = "am" if parsed.hour < 12 else "pm"
    time = (f"{hour}:{parsed.minute:02d}{suffix}" if parsed.minute
            else f"{hour}{suffix}")
    return f"{date}, {time}"


def day_label(iso: str, today: dt.date) -> str:
    """List heading: weekday in the current Sunday-to-Saturday week, else date."""
    try:
        day = dt.date.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return short_date(iso)

    # Match the calendar grid's Sunday-first week, so a Sunday through the
    # following Saturday reads naturally as "this week" in the event list.
    week_start = today - dt.timedelta(days=(today.weekday() + 1) % 7)
    if week_start <= day < week_start + dt.timedelta(days=7):
        return day.strftime("%A")
    return short_date(iso)


@app.template_filter("clock")
def clock(starts_at: str | None) -> str:
    """19:30 -> 7:30pm, 20:00 -> 8pm. Drops ':00' because nobody says 'eight oh clock'."""
    if not starts_at or "T" not in str(starts_at):
        return ""
    t = _local_datetime(starts_at)
    if t is None:
        return ""
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d}{suffix}" if t.minute else f"{hour}{suffix}"


# Shared with the manual-entry form, which validates against the same list.
CATEGORIES = manual.CATEGORIES


class _CaptionHTMLSanitizer(HTMLParser):
    """Keep description formatting without trusting website-provided HTML."""

    allowed = {
        "a", "b", "blockquote", "br", "code", "div", "em", "h2", "h3", "h4",
        "hr", "i", "li", "ol", "p", "pre", "s", "small", "span", "strong",
        "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
        "u", "ul",
    }
    void = {"br", "hr"}
    suppressed = {"embed", "iframe", "math", "object", "script", "style", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.suppressed_depth:
            if tag in self.suppressed:
                self.suppressed_depth += 1
            return
        if tag in self.suppressed:
            self.suppressed_depth = 1
            return
        if tag not in self.allowed:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href"), "")
            href = str(href or "").strip()
            parsed = urlsplit(href)
            if parsed.scheme.lower() in {"http", "https", "mailto"}:
                self.parts.append(
                    f'<a href="{escape(href)}" target="_blank" rel="noopener">')
                return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in self.allowed and tag.lower() not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.suppressed_depth:
            if tag in self.suppressed:
                self.suppressed_depth -= 1
            return
        if tag in self.allowed and tag not in self.void:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.suppressed_depth:
            self.parts.append(str(escape(data)))


@app.template_filter("caption_html")
def caption_html(value: str | None) -> Markup:
    """Render benign event-description HTML while stripping executable markup."""
    parser = _CaptionHTMLSanitizer()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except (ValueError, TypeError):
        return Markup(escape(value or ""))
    return Markup("".join(parser.parts))


@app.template_filter("source_link_label")
def source_link_label(value: str | None) -> str:
    """Name the preview link for the destination it really opens."""
    try:
        host = (urlsplit(value or "").hostname or "").lower()
    except ValueError:
        host = ""
    return "instagram post" if host == "instagram.com" or host.endswith(".instagram.com") else "event page"


# --- month grid -----------------------------------------------------------

def parse_month(value: str | None) -> dt.date | None:
    """'2026-07' -> date(2026, 7, 1). None for anything unparseable."""
    try:
        return dt.date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError):
        return None


def month_weeks(first: dt.date) -> list[list[dt.date]]:
    """Whole Sunday-start weeks covering `first`'s month, spill days included."""
    return calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)


def shift_month(first: dt.date, delta: int) -> dt.date:
    m = first.month - 1 + delta
    return dt.date(first.year + m // 12, m % 12 + 1, 1)


def query_with(args, **over) -> str:
    """Rebuild the query string with `over` keys replaced, not appended -- the
    month/view links are followed repeatedly, so appending would pile up."""
    items = [(k, v) for k, v in args.items(multi=True) if k not in over]
    items += [(k, v) for k, v in over.items() if v is not None]
    return urlencode(items)


app.jinja_env.globals["query_with"] = query_with


# An event belongs to a trip's area when one of its sources was added for that
# trip. Website sources carry the scope directly; Instagram carries it on the
# polled account -- never the attributed one, which differs on the majority of
# collab posts (SPEC section 11) and would scope events by who reposted them.
#
# The last branch reads `e.post_id` directly instead of going through
# `event_source`, and it is not redundant: generated recurring occurrences
# (`pipeline.expand_series`) inherit their source event's post_id and never get
# an `event_source` row of their own. Without it, a trip account's weekly night
# leaks into the home calendar AND goes missing from the trip -- the one case
# that fails in both directions at once.
_TRIP_SCOPE_EXISTS = """(EXISTS (SELECT 1 FROM event_source tes
        JOIN web_item twi ON twi.post_id=tes.source_item_id
        JOIN web_source tws ON tws.id=twi.source_id
        WHERE tes.event_id=e.id AND tws.trip_id IS {test}) OR
    EXISTS (SELECT 1 FROM event_source tas
        JOIN source_post tap ON tap.post_id=tas.source_item_id
        JOIN account ta ON ta.handle=tap.polled_handle
        WHERE tas.event_id=e.id AND ta.trip_id IS {test}) OR
    EXISTS (SELECT 1 FROM source_post dp JOIN account da ON da.handle=dp.polled_handle
        WHERE dp.post_id=e.post_id AND da.trip_id IS {test}) OR
    e.trip_id IS {test})"""


def _active_trip(conn, args):
    """The trip named by ?trip=N, or None for the home calendar.

    An unknown or malformed id falls back to home rather than erroring: these
    links get bookmarked and shared into a phone calendar, and a deleted trip
    should degrade to the ordinary view.
    """
    try:
        trip_id = int(args.get("trip") or 0)
    except (TypeError, ValueError):
        return None
    return trips.get(conn, trip_id) if trip_id > 0 else None


def _trip_scope_clause(trip_id: int | None) -> tuple[str, list]:
    """Scoped to this specific trip, or to any trip at all when `trip_id` is None."""
    if trip_id is None:
        return _TRIP_SCOPE_EXISTS.format(test="NOT NULL"), []
    clause = _TRIP_SCOPE_EXISTS.format(test="?")
    return clause, [trip_id] * clause.count("?")


def _source_visibility_clause(trip=None) -> tuple[str, list]:
    """SQL condition for events that are allowed into the calendar being shown.

    Home calendar: performer pages retain their complete tour history so
    changing a watch's radius can take effect without fetching again. An event
    sourced only from a performer page is therefore visible only when that
    source marks it in range. Events corroborated by Instagram or a venue
    calendar stay visible. Sources belonging to a trip are excluded outright --
    an Austin venue page is a venue page, and without this it would pour Austin
    into the home list through the ordinary website branch.

    Trip view: the home range is exactly the wrong test, since the whole point
    is tour dates too far away to qualify at home. An event is in the trip when
    a source added for that trip produced it, or when it happens near the trip's
    centre. The distance here is a bounding box; `_rows` measures the circle.
    """
    home = """(NOT EXISTS (SELECT 1 FROM event_source pes JOIN web_item pwi
        ON pwi.post_id=pes.source_item_id JOIN web_source pws ON pws.id=pwi.source_id
        WHERE pes.event_id=e.id AND pws.source_type='performer') OR
        EXISTS (SELECT 1 FROM event_source pes JOIN web_item pwi
        ON pwi.post_id=pes.source_item_id JOIN web_source pws ON pws.id=pwi.source_id
        WHERE pes.event_id=e.id AND pws.source_type='performer' AND pwi.in_range=1) OR
        EXISTS (SELECT 1 FROM event_source ies WHERE ies.event_id=e.id
        AND ies.source_kind='instagram') OR
        EXISTS (SELECT 1 FROM event_source wes JOIN web_item wwi
        ON wwi.post_id=wes.source_item_id JOIN web_source wws ON wws.id=wwi.source_id
        WHERE wes.event_id=e.id AND wws.source_type!='performer'))"""
    if trip is None:
        any_trip, _ = _trip_scope_clause(None)
        return f"{home} AND NOT {any_trip}", []

    scoped, params = _trip_scope_clause(trip["id"])
    near = ["0"]        # no centre yet: only the trip's own sources qualify
    if trip["lat"] is not None and trip["lon"] is not None:
        lat0, lon0, radius = trip["lat"], trip["lon"], float(trip["radius_miles"])
        min_lat, max_lat, min_lon, max_lon = geo.bounding_box(lat0, lon0, radius)
        near = ["""(COALESCE(e.location_lat, v.lat) BETWEEN ? AND ?
            AND COALESCE(e.location_lon, v.lon) BETWEEN ? AND ?)"""]
        params += [min_lat, max_lat, min_lon, max_lon]
    # A tour stop whose venue would not geocode has no coordinates at all --
    # negative results are cached, so it never gets a second chance. Its city
    # name is still the one the source published, and dropping the event is the
    # exact miss this feature exists to prevent.
    if city := geo.city_key(trip["city"]):
        near.append("(COALESCE(e.location_lat, v.lat) IS NULL "
                    "AND (LOWER(TRIM(e.location_city)) = ? "
                    "OR LOWER(e.location_city) LIKE ?))")
        params += [city, f"{city},%"]
    return f"({scoped} OR {' OR '.join(near)})", params


def _date_window_clause(args, trip=None) -> tuple[str | None, list]:
    """Return the date condition shared by event results and filter options."""
    # A trip is a fixed window, so it outranks the "upcoming" floor -- otherwise
    # a trip you already took shows nothing. The month grid still narrows it,
    # or the header count would promise events the visible cells cannot show.
    if trip is not None:
        low, high = trip["starts_on"], trip["ends_on"]
        if month := parse_month(args.get("month")):
            weeks = month_weeks(month)
            low = max(low, weeks[0][0].isoformat())
            high = min(high, weeks[-1][-1].isoformat())
        return "substr(e.starts_at,1,10) BETWEEN ? AND ?", [low, high]
    # A month bounds the window on both sides, so the "upcoming" floor would
    # only blank out the days of the current month that have already passed.
    # Bounds cover the whole grid, spill days included, so those cells are real.
    month = parse_month(args.get("month"))
    if month:
        weeks = month_weeks(month)
        return "substr(e.starts_at,1,10) BETWEEN ? AND ?", [
            weeks[0][0].isoformat(), weeks[-1][-1].isoformat()]
    if args.get("when", "upcoming") == "upcoming":
        return "substr(e.starts_at,1,10) >= ?", [
            dt.datetime.now(config.tzinfo()).date().isoformat()]
    return None, []


def _filters(args, trip=None) -> tuple[str, list]:
    """Build the WHERE clause shared by the HTML view and the ICS feed."""
    where = ["e.is_canonical = 1", "e.is_hidden = 0", "e.starts_at IS NOT NULL",
             "NOT EXISTS (SELECT 1 FROM series hs WHERE hs.id=e.occurrence_of "
             "AND hs.is_hidden=1)"]
    params: list = []
    # Tour dates stay cached so a radius edit can take effect without another
    # fetch, but they enter the calendar only when at least one performer watch
    # qualifies them. Local sources and Instagram retain their normal behavior.
    visibility, visibility_params = _source_visibility_clause(trip)
    where.append(visibility)
    params += visibility_params

    if args.getlist("category"):
        cats = args.getlist("category")
        where.append(f"e.category IN ({','.join('?' * len(cats))})")
        params += cats
    if hood := args.get("hood"):
        where.append("v.neighborhood = ?")
        params.append(hood)
    if venue := args.get("venue"):
        where.append("e.venue_key = ?")
        params.append(venue)
    if account := args.get("account"):
        where.append(
            "((p.polled_handle=? OR p.attributed_handle=?) OR "
            "EXISTS (SELECT 1 FROM event_source aes "
            "JOIN source_post ap ON ap.post_id=aes.source_item_id "
            "WHERE aes.event_id=e.id AND (ap.polled_handle=? OR ap.attributed_handle=?)))")
        params += [account, account, account, account]
    if search := (args.get("q") or "").strip():
        # LIKE wildcards in a user's query should be ordinary characters. The
        # correlated source lookup covers captions attached during dedupe too,
        # not only the source_post chosen as the canonical event's display row.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where.append(
            "(e.title LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
            "COALESCE(p.caption,'') LIKE ? ESCAPE '\\' COLLATE NOCASE OR "
            "EXISTS (SELECT 1 FROM event_source ses "
            "JOIN source_post sp ON sp.post_id=ses.source_item_id "
            "WHERE ses.event_id=e.id AND "
            "COALESCE(sp.caption,'') LIKE ? ESCAPE '\\' COLLATE NOCASE))")
        params += [pattern, pattern, pattern]
    if args.get("confirmed") == "1":
        where.append("e.is_confirmed = 1")
    if args.get("review") == "1":
        where.append("e.needs_review = 1")

    date_window, date_params = _date_window_clause(args, trip)
    if date_window:
        where.append(date_window)
        params += date_params
    return " AND ".join(where), params


_SELECT_COLUMNS = """
SELECT e.id, e.title, e.starts_at, e.start_time_known, e.venue_name, e.venue_key,
       e.category, e.price_text, e.needs_review, e.review_reason, e.is_confirmed,
       e.location_city, e.location_region, e.location_lat, e.location_lon,
       e.ticket_url, e.ticket_status, e.notes, e.is_manual,
       e.occurrence_of, e.dedupe_group, p.permalink, p.polled_handle,
       p.attributed_handle,
       (SELECT wsc.id FROM web_item swi
        JOIN web_series_candidate wsc ON wsc.source_id=swi.source_id
          AND wsc.series_key=swi.series_key
        WHERE swi.event_id=e.id AND wsc.status='proposed' LIMIT 1)
         AS series_candidate_id,
       COALESCE(NULLIF(p.local_images,'[]'),
         (SELECT ip.local_images FROM event_source ies
          JOIN source_post ip ON ip.post_id=ies.source_item_id
          WHERE ies.event_id=e.id AND ip.local_images!='[]' LIMIT 1), '[]') AS local_images,
       p.caption, p.posted_at,
       v.neighborhood, v.lat, v.lon, v.address,
       (SELECT MIN(pwi.distance_miles) FROM event_source pes
        JOIN web_item pwi ON pwi.post_id=pes.source_item_id
        JOIN web_source pws ON pws.id=pwi.source_id
        WHERE pes.event_id=e.id AND pws.source_type='performer' AND pwi.in_range=1)
         AS performer_distance,
       (SELECT COUNT(*) FROM event_source es WHERE es.event_id = e.id) AS source_count"""

_SELECT_FROM = """
FROM event e LEFT JOIN source_post p ON p.post_id = e.post_id
              LEFT JOIN venue v ON v.venue_key = e.venue_key
"""

BASE_SELECT = _SELECT_COLUMNS + _SELECT_FROM


def _base_select(trip=None) -> tuple[str, list]:
    """The row query, plus a `trip_scoped` flag when a trip is being shown.

    `_rows` needs to tell "added for this trip" from "happens to be nearby":
    the first is in regardless of distance, the second has to be measured.
    """
    if trip is None:
        return BASE_SELECT, []
    scoped, params = _trip_scope_clause(trip["id"])
    return f"{_SELECT_COLUMNS},\n       {scoped} AS trip_scoped{_SELECT_FROM}", params


_ZIP_CACHE: dict[str, tuple[float, float] | None] = {}


def _zip_center(zipcode: str):
    """Cached -- Nominatim is rate limited and a zip does not move."""
    if zipcode not in _ZIP_CACHE:
        _ZIP_CACHE[zipcode] = geo.geocode_zip(zipcode)
    return _ZIP_CACHE[zipcode]


def _trip_rows(rows, trip) -> list[dict]:
    """Turn the bounding box back into a circle.

    Sources added for the trip are in on their scope alone -- an Austin venue
    page is about Austin whether or not its address geocoded. Everything else
    has to be within the trip's radius of its centre.
    """
    lat0, lon0 = trip["lat"], trip["lon"]
    radius = float(trip["radius_miles"])
    city = geo.city_key(trip["city"])
    out = []
    for r in rows:
        d = dict(r)
        if d.get("trip_scoped"):
            out.append(d)
            continue
        point_lat = d["location_lat"] if d["location_lat"] is not None else d["lat"]
        point_lon = d["location_lon"] if d["location_lon"] is not None else d["lon"]
        if point_lat is None or point_lon is None:
            if city and geo.city_key(d["location_city"]) == city:
                out.append(d)       # no coordinates, but it says it is there
            continue
        if lat0 is None or lon0 is None:
            continue
        d["distance"] = geo.haversine_miles(lat0, lon0, point_lat, point_lon)
        if d["distance"] <= radius:
            out.append(d)
    return out


def _rows(conn, args, trip=None):
    where, params = _filters(args, trip)
    select, select_params = _base_select(trip)
    rows = conn.execute(
        f"{select} WHERE {where} ORDER BY e.starts_at LIMIT 500",
        select_params + params).fetchall()

    if trip is not None:
        # A trip already answers "where", so the zip filter is not applied on
        # top of it -- a home zip intersected with another city returns nothing.
        return _trip_rows(rows, trip)

    zipcode, radius = (args.get("zip") or "").strip(), args.get("radius")
    if not (zipcode and radius):
        return [dict(r) for r in rows]

    center = _zip_center(zipcode)
    if center is None:
        return [dict(r) for r in rows]   # unresolvable zip: do not silently empty the page

    lat0, lon0 = center
    miles = float(radius)
    out = []
    for r in rows:
        d = dict(r)
        point_lat = d["location_lat"] if d["location_lat"] is not None else d["lat"]
        point_lon = d["location_lon"] if d["location_lon"] is not None else d["lon"]
        if point_lat is None or point_lon is None:
            continue
        d["distance"] = geo.haversine_miles(lat0, lon0, point_lat, point_lon)
        if d["distance"] <= miles:
            out.append(d)
    return out


@app.route("/")
def index():
    today = dt.datetime.now(config.tzinfo()).date()

    # The grid needs a month even when the URL omits one, and _filters() reads
    # it off args -- so resolve it up front and hand the filters a copy.
    view = "calendar" if request.args.get("view") == "calendar" else "list"
    args = request.args
    month = None

    with db.session(app.config["DB"]) as conn:
        trip = _active_trip(conn, args)
        if view == "calendar":
            # Opening a trip's grid on today's month would show an empty August
            # for a September trip. The trip's own first month is the answer.
            default = (dt.date.fromisoformat(trip["starts_on"]).replace(day=1)
                       if trip else today.replace(day=1))
            month = parse_month(args.get("month")) or default
            args = request.args.copy()
            args["month"] = f"{month:%Y-%m}"
        rows = _rows(conn, args, trip)
        source_visible, visible_params = _source_visibility_clause(trip)
        date_window, date_params = _date_window_clause(args, trip)
        option_date_where = f" AND {date_window}" if date_window else ""
        option_params = visible_params + date_params

        # Group by calendar day, with a human label per group
        grouped: dict[str, list] = {}
        for r in rows:
            d = dict(r)
            imgs = json.loads(d.get("local_images") or "[]")
            d["thumb"] = imgs[0] if imgs else None
            local = _local_datetime(d["starts_at"])
            day = local.date().isoformat() if local else d["starts_at"][:10]
            grouped.setdefault(day, []).append(d)
        days = [{"date": k, "label": day_label(k, today), "events": v}
                for k, v in grouped.items()]

        weeks = [[{"date": d.isoformat(), "day": d.day,
                   "in_month": d.month == month.month, "is_today": d == today,
                   "events": grouped.get(d.isoformat(), [])}
                  for d in wk]
                 for wk in month_weeks(month)] if month else []

        hoods = conn.execute(
            "SELECT v.neighborhood, COUNT(*) n FROM event e JOIN venue v ON v.venue_key=e.venue_key "
            f"WHERE e.is_canonical=1 AND {source_visible}{option_date_where} "
            "AND v.neighborhood IS NOT NULL GROUP BY 1 ORDER BY v.neighborhood",
            option_params).fetchall()
        # One row per venue_key -- the raw venue_name has many spellings per
        # venue ("PETRAS BAR", "Petra's", "Petras Bar") and listing them all
        # made the dropdown look broken even though the filter worked.
        venues = conn.execute(
            "SELECT e.venue_key, COALESCE(v.display_name, MIN(e.venue_name)) AS label, "
            "COUNT(*) AS n FROM event e LEFT JOIN venue v ON v.venue_key = e.venue_key "
            f"WHERE e.venue_key != '' AND e.is_canonical = 1 AND {source_visible}"
            f"{option_date_where} GROUP BY e.venue_key ORDER BY label",
            option_params).fetchall()
        # Only accounts that can appear in the calendar being shown: a trip's
        # accounts never post home events, and home accounts never post trip ones.
        # `IS` rather than `=` so the home case matches NULL without a second query.
        accounts = conn.execute(
            "SELECT handle FROM account WHERE is_polled = 1 AND trip_id IS ? "
            "ORDER BY handle", (trip["id"] if trip else None,)).fetchall()
        series_candidates = conn.execute(
            "SELECT c.id,c.title,ws.name AS source_name,"
            "COUNT(DISTINCT substr(e.starts_at,1,10)) AS occurrence_count,"
            "MIN(substr(e.starts_at,1,10)) AS first_date,"
            "MAX(substr(e.starts_at,1,10)) AS last_date,"
            "SUM(e.is_hidden) AS hidden_count "
            "FROM web_series_candidate c JOIN web_source ws ON ws.id=c.source_id "
            "JOIN web_item wi ON wi.source_id=c.source_id AND wi.series_key=c.series_key "
            "JOIN event e ON e.id=wi.event_id WHERE c.status='proposed' "
            "GROUP BY c.id,c.title,ws.name "
            "HAVING COUNT(DISTINCT substr(e.starts_at,1,10)) >= 2 "
            "ORDER BY c.updated_at DESC").fetchall()

        # Keep the header count honest: the review link preserves the current
        # date, source, category, location, and distance filters, so count the
        # same rows that its destination will actually render.  The old global
        # database count included past events even when the UI said upcoming.
        review_args = args.copy()
        review_args["review"] = "1"
        review_rows = rows if args.get("review") == "1" else _rows(conn, review_args, trip)
        review_count = len(review_rows) + len(series_candidates)

        pending_count = len(discovery.pending(conn))
        trip_list = [dict(t, label=trips.label(t), short_label=trips.short_label(t),
                          status=trips.status(t, today))
                     for t in trips.listing(conn)]
        trip = dict(trip, label=trips.label(trip)) if trip else None

    return render_template(
        "index.html", days=days, total=len(rows), venues=venues, accounts=accounts,
        pending_count=pending_count, hoods=hoods, trips=trip_list, trip=trip,
        zip_failed=bool((request.args.get("zip") or "").strip()
                        and request.args.get("radius")
                        and _zip_center(request.args["zip"].strip()) is None),
        categories=CATEGORIES, review_count=review_count,
        series_candidates=series_candidates, args=args,
        today=today.isoformat(),
        cfg=config.load(), configured=config.exists(),
        view=view, weeks=weeks, month=month,
        month_label=f"{month:%B %Y}" if month else "",
        prev_month=f"{shift_month(month, -1):%Y-%m}" if month else "",
        next_month=f"{shift_month(month, 1):%Y-%m}" if month else "",
        weekday_names=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    )


@app.post("/event/<int:event_id>/<action>")
def act(event_id: int, action: str):
    field = {"confirm": "is_confirmed", "hide": "is_hidden", "flag": "needs_review"}.get(action)
    if not field:
        return ("unknown action", 400)
    with db.session(app.config["DB"]) as conn:
        # Recurring hides are intentionally series-wide. Confirmation and flags
        # remain occurrence-level editorial decisions.
        cur = conn.execute(
            f"SELECT {field},occurrence_of FROM event WHERE id=?", (event_id,)).fetchone()
        if cur is None:
            return ("no such event", 404)
        if field == "is_hidden" and cur["occurrence_of"] is not None:
            series = conn.execute(
                "SELECT is_hidden FROM series WHERE id=?", (cur["occurrence_of"],)).fetchone()
            if series is not None:
                new = 0 if series["is_hidden"] else 1
                conn.execute("UPDATE series SET is_hidden=? WHERE id=?",
                             (new, cur["occurrence_of"]))
                return redirect(request.referrer or url_for("index"))
        new = 0 if cur[0] else 1
        reason = "manually flagged" if (field == "needs_review" and new) else None
        if field == "needs_review":
            conn.execute("UPDATE event SET needs_review=?, review_reason=? WHERE id=?",
                         (new, reason, event_id))
        else:
            conn.execute(f"UPDATE event SET {field}=? WHERE id=?", (new, event_id))
    if action == "confirm" and request.headers.get("X-Requested-With") == "fetch":
        return {"confirmed": bool(new)}
    destination = request.referrer or url_for("index")
    if action == "confirm":
        destination = f"{destination.split('#', 1)[0]}#event-{event_id}"
    return redirect(destination)


@app.post("/series-candidate/<int:candidate_id>/<decision>")
def review_series(candidate_id: int, decision: str):
    choices = {
        "confirm": ("confirm", False),
        "confirm-hide": ("confirm", True),
        "reject": ("reject", False),
    }
    if decision not in choices:
        return ("unknown series decision", 400)
    with db.session(app.config["DB"]) as conn:
        choice, hide = choices[decision]
        if websites.decide_series_candidate(conn, candidate_id, choice, hide) is None:
            return ("no such series suggestion", 404)
    return redirect(request.referrer or url_for("index"))


# --- settings -------------------------------------------------------------
# Non-secret configuration only. API keys are set by `cli init` and never
# accepted here: this app binds 0.0.0.0 with no auth so a phone on the LAN can
# reach it, which makes any key field on it a key field for the whole network.
# Showing whether a key is *present* is fine; showing or accepting one is not.

@app.route("/settings", methods=["GET", "POST"])
def settings():
    import os

    cfg = config.load()
    error = saved = None
    origin_changed = False

    if request.method == "POST":
        form = request.form
        new = {"radius_miles": cfg["radius_miles"], "timezone": cfg["timezone"],
               # Unchecked boxes are simply absent from the form, so presence is
               # the value. Only the Mac app reads it.
               "show_in_dock": form.get("show_in_dock") == "on"}

        refresh_fields = (
            ("instagram_refresh_hours", "Instagram"),
            ("performer_refresh_hours", "Performer webpage"),
            ("venue_refresh_hours", "Venue webpage"),
        )
        for key, label in refresh_fields:
            try:
                hours = int(form.get(key))
                if not config.REFRESH_HOURS_MIN <= hours <= config.REFRESH_HOURS_MAX:
                    raise ValueError
                new[key] = hours
            except (TypeError, ValueError):
                error = (f"{label} refresh must be between "
                         f"{config.REFRESH_HOURS_MIN} and {config.REFRESH_HOURS_MAX} hours.")
                break

        try:
            new["radius_miles"] = max(1.0, min(500.0, float(form.get("radius_miles"))))
        except (TypeError, ValueError):
            pass

        tz = (form.get("timezone") or "").strip()
        if tz and not config.is_valid_timezone(tz):
            error = f"'{tz}' is not an IANA timezone name (try 'America/Denver', not 'MDT')."
        elif tz:
            new["timezone"] = tz

        city = (form.get("city") or "").strip()
        if not error and city and city != cfg["city"]:
            center = geo.geocode_city(city)
            if center is None:
                error = f"Could not find '{city}'. Add a state or country and try again."
            else:
                new["city"], (new["lat"], new["lon"]) = city, center
                origin_changed = True

        if not error:
            cfg = config.save(new)
            _ZIP_CACHE.clear()   # radius/centre changed -- stale hits would lie
            if origin_changed:
                with db.session(app.config["DB"]) as conn:
                    for row in conn.execute(
                            "SELECT id FROM web_source WHERE source_type='performer'").fetchall():
                        websites.recalculate_performer(conn, row["id"])
            saved = True

    return render_template(
        "settings.html", cfg=cfg, error=error, saved=saved,
        configured=config.exists(),
        keys=[(name, label, bool(os.getenv(name)), url)
              for name, label, url in config.API_KEYS],
    )


# --- fetch-now jobs -------------------------------------------------------
# Polling takes minutes -- an Apify scrape plus a vision call per surviving post,
# for one account or for the whole rotation -- so it cannot happen inside the
# request. A worker thread does the work, the page polls /jobs, and single-flight
# keeps two expensive runs (and two SQLite writers) from overlapping.
#
# Single-flight is per process: cron's `run-once` is a separate process and is not
# covered by it. WAL plus busy_timeout keep that safe, just not free.
#
# This is the one place the web UI spends money. It is cost-capped by `limit`
# per account, and the estimate is shown before you click.

JOB: dict = {"state": "idle", "handle": None, "label": "", "message": "", "stats": None}
_JOB_LOCK = threading.Lock()
FETCH_LIMIT = 20
POST_COST = 0.002   # ~$2.00 per 1,000 posts, same figure ApifySource quotes


def _fetch_worker(handles: list[str], website_source_ids: list[int],
                  db_path: str, limit: int) -> None:

    def progress(msg):
        JOB["message"] = str(msg)

    try:
        source = extractor = None
        if handles:
            from .sources import ApifySource

            source = ApifySource(os.environ["APIFY_TOKEN"])
        if handles or (website_source_ids and os.getenv("OPENAI_API_KEY")):
            from .extract import Extractor

            extractor = Extractor()
        with db.session(db_path) as conn:
            # Same grouping cron uses: each account asks only for what it has
            # missed, so one new account cannot drag the rest back to 30 days.
            groups = runner.fetch_windows(conn, handles)
            stats = runner.poll(conn, source, extractor, handles, limit,
                                groups=groups, log=progress,
                                website_source_ids=website_source_ids)
            delivered = notifications.deliver_pending(conn)
        website_stats = stats["websites"]
        message = (f"{website_stats['new']} website events, "
                   f"{stats['ingested']} new posts, "
                   f"{stats['processed'].get('events', 0)} extracted events, "
                   f"{stats['processed'].get('vision_skipped', 0)} flyers skipped"
                   + (f", {delivered} alert{'s' if delivered != 1 else ''}" if delivered else ""))
        if website_stats["errors"]:
            details = "; ".join(website_stats.get("error_messages", []))
            if not handles and website_stats["errors"] == website_stats["sources"]:
                JOB.update(state="error", stats=stats,
                           message=details or "website fetch failed")
            else:
                JOB.update(state="done", stats=stats,
                           message=f"{message}; {website_stats['errors']} website failed: {details}")
        else:
            JOB.update(state="done", stats=stats, message=message)
    except Exception as exc:
        # Surfaced in the UI rather than only in the server log -- a fetch that
        # silently did nothing is worse than one that says why.
        JOB.update(state="error", message=f"{type(exc).__name__}: {exc}")
    finally:
        if JOB["state"] == "running":
            JOB.update(state="error", message="worker exited without a result")


def start_fetch(handles: list[str], label: str,
                website_source_ids: list[int] | None = None) -> str | None:
    """Claim the single job slot and hand the batch to a worker thread.

    Returns None on success or an error slug. Deliberately free of any request
    context: the menubar app's timer and its "Fetch Now" item call this directly,
    which is what keeps the scheduled run and a click in the UI sharing one lock
    instead of racing each other into two concurrent scrapes.
    """
    website_source_ids = list(website_source_ids or [])
    missing = [k for k, _, _ in config.API_KEYS if handles and not os.getenv(k)]
    if missing:
        return "no-keys"

    with _JOB_LOCK:
        if JOB["state"] == "running":
            return "busy"
        JOB.update(state="running", handle=handles[0] if len(handles) == 1 else None,
                   label=label, message="starting...", stats=None)

    threading.Thread(target=_fetch_worker, daemon=True,
                     args=(handles, website_source_ids, app.config["DB"], FETCH_LIMIT)).start()
    return None


def _start_fetch(handles: list[str], label: str,
                 website_source_ids: list[int] | None = None):
    """Route-facing wrapper: same work, but answers with a redirect."""
    err = start_fetch(handles, label, website_source_ids)
    return redirect(url_for("discover", **({"err": err} if err else {})))


@app.post("/discover/<handle>/fetch")
def fetch_now(handle: str):
    if not HANDLE_RE.match(handle):
        return redirect(url_for("discover", err="bad-handle"))
    with db.session(app.config["DB"]) as conn:
        website_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM web_source WHERE enabled=1 AND linked_handle=?", (handle,))]
    return _start_fetch([handle], f"@{handle}", website_ids)


@app.post("/discover/fetch-all")
def fetch_all():
    """Refresh the whole rotation -- the same work cron's `run-once` does.

    Deliberately not routed as /discover/all/fetch: HANDLE_RE accepts "all",
    so that URL would be ambiguous with a real account named @all.
    """
    with db.session(app.config["DB"]) as conn:
        handles = discovery.approved_handles(conn)
        website_ids = trips.pollable_source_ids(conn)
    if not handles and not website_ids:
        return redirect(url_for("discover", err="nothing-polled"))
    label = f"{len(handles)} accounts, {len(website_ids)} websites"
    return _start_fetch(handles, label, website_ids)


@app.post("/discover/website/<int:source_id>/fetch")
def fetch_website(source_id: int):
    with db.session(app.config["DB"]) as conn:
        row = conn.execute("SELECT name FROM web_source WHERE id=? AND enabled=1",
                           (source_id,)).fetchone()
    if row is None:
        return redirect(url_for("discover", err="bad-website"))
    return _start_fetch([], row["name"], [source_id])


@app.route("/jobs")
def jobs():
    """Polled by /discover so the button can report progress without a reload."""
    return JOB


@app.route("/spend.json")
def spend_json():
    """What this install has actually cost. Read by the menu bar.

    `since` is null on a fresh ledger and is the first recorded call otherwise --
    never an install date. Spend predating the ledger cannot be reconstructed,
    so the caller must label this "since tracking started" rather than "total".
    """
    with db.session(app.config["DB"]) as conn:
        return spend.totals(conn)


@app.route("/discover")
def discover():
    """Review queue. Nothing enters the poll rotation without a click here."""
    # A finished job is reported once and then cleared, so its banner does not
    # follow you around on every later visit to this page.
    job = _claim_job_banner()

    with db.session(app.config["DB"]) as conn:
        discovery.stage(conn, discovery.rank_tagged(conn))
        # Home sources only. A trip's accounts and pages are managed on the
        # trip's own page, and mixing them here would suggest they feed the
        # home calendar -- which is exactly what they must not do.
        polled = conn.execute(
            "SELECT handle, display_name, avatar_file FROM account WHERE is_polled=1 "
            "AND trip_id IS NULL ORDER BY handle").fetchall()
        website_sources = conn.execute(
            "SELECT * FROM web_source WHERE enabled=1 AND source_type='venue' "
            "AND trip_id IS NULL ORDER BY name").fetchall()
        performer_sources = conn.execute(
            "SELECT ws.*, COUNT(wi.id) AS tour_dates, "
            "SUM(CASE WHEN wi.in_range=1 THEN 1 ELSE 0 END) AS nearby_dates "
            "FROM web_source ws LEFT JOIN web_item wi ON wi.source_id=ws.id "
            "WHERE ws.enabled=1 AND ws.source_type='performer' GROUP BY ws.id ORDER BY ws.name").fetchall()
        return render_template("discover.html", queue=discovery.pending(conn),
                               polled=polled, added=request.args.get("added"),
                               website_sources=website_sources,
                               performer_sources=performer_sources,
                               website_added=request.args.get("website_added"),
                               performer_added=request.args.get("performer_added"),
                               removed=request.args.get("removed"),
                               approved=request.args.get("approved"),
                               err=request.args.get("err"), job=job,
                               fetch_limit=FETCH_LIMIT,
                               fetch_cost=FETCH_LIMIT * POST_COST,
                               fetch_all_cost=len(polled) * FETCH_LIMIT * POST_COST)


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _form_trip_id() -> int | None:
    """The trip a source form belongs to. Absent means the home rotation."""
    try:
        trip_id = int(request.form.get("trip_id") or 0)
    except (TypeError, ValueError):
        return None
    return trip_id or None


def _source_redirect(trip_id: int | None, **kwargs):
    """Send the user back to whichever page owns the source they just touched."""
    if trip_id:
        return redirect(url_for("trip_detail", trip_id=trip_id, **kwargs))
    return redirect(url_for("discover", **kwargs))


@app.post("/discover/add")
def add_account():
    """Add a handle by hand. Goes straight into the rotation -- an explicit add
    is its own approval."""
    trip_id = _form_trip_id()
    raw = (request.form.get("handle") or "").strip().lstrip("@")
    # Accept a pasted profile URL as well as a bare handle
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", raw)
    if m:
        raw = m.group(1)
    if not HANDLE_RE.match(raw):
        return _source_redirect(trip_id, err="bad-handle")

    with db.session(app.config["DB"]) as conn:
        if trip_id and trips.get(conn, trip_id) is None:
            return redirect(url_for("trips_page", err="no-trip"))
        # Adding a handle already in the home rotation to a trip would move it
        # out of the home calendar entirely, and deleting the trip would then
        # drop it. Refuse rather than silently re-scoping someone's follow.
        if trip_id and conn.execute(
                "SELECT 1 FROM account WHERE handle=? AND is_polled=1 "
                "AND trip_id IS NULL", (raw,)).fetchone():
            return _source_redirect(trip_id, err="already-home")
        conn.execute(
            "INSERT INTO account (handle, is_polled, seen_count, discovery_source, "
            "added_at, status, proposed_reason, trip_id) VALUES "
            "(?,1,0,'manual',datetime('now'),'approved','added by hand',?) "
            "ON CONFLICT(handle) DO UPDATE SET is_polled=1, status='approved', "
            "trip_id=excluded.trip_id", (raw, trip_id))
    return _source_redirect(trip_id, added=raw)


@app.post("/discover/website/add")
def add_website():
    trip_id = _form_trip_id()
    try:
        with db.session(app.config["DB"]) as conn:
            if trip_id and trips.get(conn, trip_id) is None:
                return redirect(url_for("trips_page", err="no-trip"))
            source_id = websites.add_source(
                conn, request.form.get("url") or "", request.form.get("name"),
                request.form.get("linked_handle"), trip_id=trip_id)
    except ValueError:
        return _source_redirect(trip_id, err="bad-website")
    return _source_redirect(trip_id, website_added=source_id)


@app.post("/discover/performer/add")
def add_performer():
    try:
        radius = float(request.form.get("radius_miles") or 250)
        with db.session(app.config["DB"]) as conn:
            source_id = websites.add_source(
                conn, request.form.get("url") or "", request.form.get("name"),
                source_type="performer", radius_miles=radius,
                notify=request.form.get("notify") == "on")
    except (TypeError, ValueError):
        return redirect(url_for("discover", err="bad-performer"))
    return redirect(url_for("discover", performer_added=source_id))


@app.post("/discover/website/<int:source_id>/disable")
def disable_website(source_id: int):
    trip_id = _form_trip_id()
    with db.session(app.config["DB"]) as conn:
        conn.execute("UPDATE web_source SET enabled=0 WHERE id=?", (source_id,))
    return _source_redirect(trip_id)


@app.post("/discover/website/<int:source_id>/remove")
def remove_website(source_id: int):
    trip_id = _form_trip_id()
    if JOB["state"] == "running":
        return _source_redirect(trip_id, err="busy")
    with db.session(app.config["DB"]) as conn:
        result = websites.remove_source(conn, source_id)
        if result is None:
            return ("no such website source", 404)
    return _source_redirect(trip_id, removed=result["source"])


@app.post("/discover/<handle>/<decision>")
def decide(handle: str, decision: str):
    if decision not in ("approve", "reject"):
        return ("unknown decision", 400)
    trip_id = _form_trip_id()
    with db.session(app.config["DB"]) as conn:
        discovery.decide(conn, handle, approve=(decision == "approve"))
        if decision == "reject":
            # Dropping a trip's account also drops its scope; otherwise it stays
            # invisible on both pages, present but unreachable.
            conn.execute("UPDATE account SET trip_id=NULL WHERE handle=?", (handle,))
    # Approving used to be silent, which read as nothing having happened -- the
    # account is not polled until the next scheduled run. Offer the fetch instead.
    if decision == "approve":
        return _source_redirect(trip_id, approved=handle)
    return _source_redirect(trip_id)


# --- hand-entered events ---------------------------------------------------
# The only rows in this database the user authors. See `manual` for why they are
# fenced off from dedupe, series expansion, and caption matching.

def _back_to(args, **extra) -> str:
    """Return to the view the form was submitted from, filters and all.

    The form carries the query string it was rendered under, so a save lands
    back on the same trip, month, and filters rather than dumping the user at
    an unfiltered calendar.
    """
    query = (args.get("back") or "").lstrip("?")
    items = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
             if k not in extra and k != "event_err"]
    items += [(k, v) for k, v in extra.items() if v is not None]
    return f"/?{urlencode(items)}" if items else "/"


@app.post("/events/manual/add")
def add_manual_event():
    with db.session(app.config["DB"]) as conn:
        trip = _active_trip(conn, request.form)
        try:
            manual.add(conn, request.form, trip)
        except ValueError as exc:
            return redirect(_back_to(request.form, event_err=str(exc)))
    return redirect(_back_to(request.form))


@app.post("/events/manual/<int:event_id>/edit")
def edit_manual_event(event_id: int):
    with db.session(app.config["DB"]) as conn:
        try:
            if not manual.update(conn, event_id, request.form):
                return ("no such event", 404)
        except ValueError as exc:
            return redirect(_back_to(request.form, event_err=str(exc)))
    return redirect(_back_to(request.form))


@app.post("/events/manual/<int:event_id>/delete")
def delete_manual_event(event_id: int):
    with db.session(app.config["DB"]) as conn:
        if not manual.delete(conn, event_id):
            return ("no such event", 404)
    return redirect(_back_to(request.form))


# --- trips ----------------------------------------------------------------
# A trip owns its own Instagram accounts and event pages; performer watches are
# national and every trip inherits them (websites.add_source enforces that).
# Browsing a trip is the ordinary calendar with ?trip=N, not a page of its own.

def _trip_row(conn, trip) -> dict:
    """A trip plus the counts its page reports."""
    row = dict(trip, label=trips.label(trip), status=trips.status(trip))
    opens, closes = trips.poll_window(trip)
    row["poll_opens"] = opens.isoformat()
    row["pollable"] = trips.is_pollable(trip)
    row["accounts"] = conn.execute(
        "SELECT handle, display_name, avatar_file FROM account "
        "WHERE is_polled=1 AND trip_id=? ORDER BY handle", (trip["id"],)).fetchall()
    row["websites"] = conn.execute(
        "SELECT * FROM web_source WHERE enabled=1 AND trip_id=? ORDER BY name",
        (trip["id"],)).fetchall()
    return row


def _claim_job_banner() -> dict:
    """Read the job slot, clearing a finished result so it is reported once.

    /discover and /trips share one slot and one banner; whichever page the user
    lands on after a fetch is the one that tells them how it went.
    """
    job = dict(JOB)
    if job["state"] in ("done", "error"):
        JOB.update(state="idle", handle=None, label="", message="", stats=None)
    return job


@app.route("/trips")
def trips_page():
    job = _claim_job_banner()
    with db.session(app.config["DB"]) as conn:
        rows = [_trip_row(conn, t) for t in trips.listing(conn)]
    return render_template("trips.html", trips=rows, trip=None, job=job,
                           err=request.args.get("err"),
                           saved=request.args.get("saved"),
                           removed=request.args.get("removed"),
                           lead_days=trips.POLL_LEAD_DAYS,
                           default_radius=trips.DEFAULT_RADIUS_MILES,
                           today=trips.today().isoformat())


@app.route("/trips/<int:trip_id>")
def trip_detail(trip_id: int):
    job = _claim_job_banner()
    with db.session(app.config["DB"]) as conn:
        trip = trips.get(conn, trip_id)
        if trip is None:
            return redirect(url_for("trips_page", err="no-trip"))
        row = _trip_row(conn, trip)
    return render_template("trips.html", trips=[row], trip=row, job=job,
                           err=request.args.get("err"),
                           saved=request.args.get("saved"),
                           added=request.args.get("added"),
                           website_added=request.args.get("website_added"),
                           removed=request.args.get("removed"),
                           lead_days=trips.POLL_LEAD_DAYS,
                           default_radius=trips.DEFAULT_RADIUS_MILES,
                           today=trips.today().isoformat())


@app.post("/trips/add")
def add_trip():
    form = request.form
    try:
        with db.session(app.config["DB"]) as conn:
            trip_id = trips.add(conn, form.get("name"), form.get("city") or "",
                                form.get("starts_on") or "", form.get("ends_on") or "",
                                form.get("radius_miles"))
    except ValueError as exc:
        return redirect(url_for("trips_page", err=str(exc)))
    return redirect(url_for("trip_detail", trip_id=trip_id, saved="1"))


@app.post("/trips/<int:trip_id>/edit")
def edit_trip(trip_id: int):
    form = request.form
    try:
        with db.session(app.config["DB"]) as conn:
            if not trips.update(conn, trip_id, form.get("name"), form.get("city") or "",
                                form.get("starts_on") or "", form.get("ends_on") or "",
                                form.get("radius_miles"), notes=form.get("notes")):
                return redirect(url_for("trips_page", err="no-trip"))
    except ValueError as exc:
        return redirect(url_for("trip_detail", trip_id=trip_id, err=str(exc)))
    return redirect(url_for("trip_detail", trip_id=trip_id, saved="1"))


@app.post("/trips/<int:trip_id>/notes")
def edit_trip_notes(trip_id: int):
    """Save trip notes from the calendar itself.

    Separate from the trip edit form because it is reached while looking at the
    plan, not while configuring the trip -- and it must not require the city and
    date fields that form validates.
    """
    with db.session(app.config["DB"]) as conn:
        if trips.get(conn, trip_id) is None:
            return redirect(url_for("trips_page", err="no-trip"))
        conn.execute("UPDATE trip SET notes=? WHERE id=?",
                     ((request.form.get("notes") or "").strip() or None, trip_id))
    return redirect(_back_to(request.form))


@app.post("/trips/<int:trip_id>/remove")
def remove_trip(trip_id: int):
    if JOB["state"] == "running":
        return redirect(url_for("trip_detail", trip_id=trip_id, err="busy"))
    with db.session(app.config["DB"]) as conn:
        result = trips.remove(conn, trip_id)
    if result is None:
        return redirect(url_for("trips_page", err="no-trip"))
    return redirect(url_for("trips_page", removed=result["name"]))


@app.post("/trips/<int:trip_id>/fetch")
def fetch_trip(trip_id: int):
    """Check this trip's own sources now, whatever its poll window says.

    The window governs the unattended rotation; an explicit click is the user
    saying they want it today.
    """
    with db.session(app.config["DB"]) as conn:
        trip = trips.get(conn, trip_id)
        if trip is None:
            return redirect(url_for("trips_page", err="no-trip"))
        handles = [r["handle"] for r in conn.execute(
            "SELECT handle FROM account WHERE is_polled=1 AND trip_id=?", (trip_id,))]
        website_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM web_source WHERE enabled=1 AND trip_id=?", (trip_id,))]
    if not handles and not website_ids:
        return redirect(url_for("trip_detail", trip_id=trip_id, err="nothing-polled"))
    err = start_fetch(handles, trip["name"], website_ids)
    return redirect(url_for("trip_detail", trip_id=trip_id, **({"err": err} if err else {})))


# --- ICS ------------------------------------------------------------------
# Additive, not an alternative to the UI (SPEC section 9): the same filters
# apply, so you can subscribe to a slice and still browse the whole thing.

def _ics_escape(text: str | None) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def _fold(line: str) -> str:
    """RFC 5545 caps lines at 75 octets; continuations start with a space."""
    out, cur = [], line
    while len(cur.encode()) > 75:
        cut = 74
        while len(cur[:cut].encode()) > 75:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def _ics_document(rows, calendar_name: str) -> str:
    """Build one valid calendar file for either the whole view or one event."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//social-calendar//EN",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{_ics_escape(calendar_name)}"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    tz = config.load()["timezone"]
    for r in rows:
        start = r["starts_at"]
        if r["start_time_known"] and "T" in start:
            d = dt.datetime.fromisoformat(start)
            dtstart = f"DTSTART;TZID={tz}:{d:%Y%m%dT%H%M%S}"
            dtend = f"DTEND;TZID={tz}:{d + dt.timedelta(hours=2):%Y%m%dT%H%M%S}"
        else:
            day = dt.date.fromisoformat(start[:10])
            dtstart = f"DTSTART;VALUE=DATE:{day:%Y%m%d}"
            dtend = f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}"

        desc = " | ".join(x for x in [
            # Your own note leads: on a phone the description is often all you
            # see, and "book the 6:40 train" outranks the price of the ticket.
            r["notes"] if "notes" in r.keys() else None,
            r["price_text"],
            "tickets: " + r["ticket_url"] if r["ticket_url"] else None,
            f"source: @{r['attributed_handle']}" if r["attributed_handle"] else None,
            r["permalink"],
            "NEEDS REVIEW: " + (r["review_reason"] or "") if r["needs_review"] else None,
        ] if x)

        title = r["title"] or "(untitled)"
        if r["needs_review"]:
            title = "⚠ " + title

        lines += [
            "BEGIN:VEVENT",
            f"UID:sc-{r['id']}@social-calendar",
            f"DTSTAMP:{stamp}",
            dtstart, dtend,
            _fold(f"SUMMARY:{_ics_escape(title)}"),
            _fold(f"LOCATION:{_ics_escape(r['venue_name'])}"),
            _fold(f"DESCRIPTION:{_ics_escape(desc)}"),
            f"URL:{r['permalink'] or ''}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.route("/calendar.ics")
def calendar_ics():
    with db.session(app.config["DB"]) as conn:
        trip = _active_trip(conn, request.args)
        rows = _rows(conn, request.args, trip)
        # Name the subscription for what it holds -- a phone shows the calendar
        # name, not the URL, and "Social Calendar" twice is unreadable.
        name = f"{trip['name']} · {trip['city']}" if trip else "Social Calendar"

    return Response(_ics_document(rows, name), mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=local-calendar.ics"})


@app.route("/event/<int:event_id>/calendar.ics")
def event_ics(event_id: int):
    """Download one listing as a calendar event without subscribing to the whole feed."""
    with db.session(app.config["DB"]) as conn:
        row = conn.execute(f"{BASE_SELECT} WHERE e.id=?", (event_id,)).fetchone()
    if row is None:
        abort(404)
    title = row["title"] or "Event"
    return Response(_ics_document([row], title), mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=event-{event_id}.ics"})


CSV_COLUMNS = ["date", "time", "title", "venue", "neighborhood", "address",
               "category", "price", "source_account", "confirmed", "needs_review",
               "review_reason", "notes", "link"]


@app.route("/events.csv")
def events_csv():
    """The same filtered view as the page, as a spreadsheet.

    Google Sheets, Excel and Numbers all open CSV directly. Filters ride along
    in the query string exactly as they do for the .ics feed, so the download
    matches whatever the page is showing. (Sheets' =IMPORTDATA() cannot reach
    this URL -- Google's servers would have to resolve it, and we bind to
    localhost/Tailscale. Import the downloaded file instead.)
    """
    with db.session(app.config["DB"]) as conn:
        rows = _rows(conn, request.args, _active_trip(conn, request.args))

    # utf-8-sig: Excel assumes the local codepage without a BOM and mangles
    # every accented venue name. Sheets ignores the BOM.
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        start = r["starts_at"] or ""
        w.writerow([
            start[:10],
            start[11:16] if r["start_time_known"] and "T" in start else "",
            r["title"] or "",
            r["venue_name"] or "",
            r["neighborhood"] or "",
            r["address"] or "",
            r["category"] or "",
            r["price_text"] or "",
            f"@{r['attributed_handle']}" if r["attributed_handle"] else "",
            "yes" if r["is_confirmed"] else "",
            "yes" if r["needs_review"] else "",
            r["review_reason"] or "",
            r["notes"] or "",
            r["permalink"] or "",
        ])

    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=local-calendar.csv"})


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--port", type=int, default=8730)
    # 0.0.0.0 so the phone can reach it over Tailscale/LAN. Single user, no auth.
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    app.config["DB"] = args.db
    print(f"http://localhost:{args.port}   ICS: http://localhost:{args.port}/calendar.ics")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
