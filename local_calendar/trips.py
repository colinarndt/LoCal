"""Upcoming trips: a city area plus the dates you are there.

A trip is a **lens**, not a second pipeline. Every performer tour date is
already fetched and geocoded whatever its distance -- `_qualify_performer`
resolves coordinates for every stop and `web_item.in_range` only decides
whether the home calendar shows it. So pointing a radius at Austin for four
days in August surfaces tour dates that are already in the database, with no
extra fetch and no extra model spend.

What a trip adds on top of that is scope: an account or website may carry a
`trip_id`, which means it belongs to that trip's area and must never reach the
home calendar. Those sources also go dormant outside the trip's poll window,
because an Austin venue page polled every night in February costs money and
answers a question nobody asked.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from . import config, geo

# How far ahead of a trip its scoped sources join the rotation. Long enough
# that announcements land before you go, short enough that a trip booked in
# January is not polling a city you visit in June.
POLL_LEAD_DAYS = 30

RADIUS_MIN, RADIUS_MAX = 1.0, 1000.0
DEFAULT_RADIUS_MILES = 30.0


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def today(cfg: dict | None = None) -> dt.date:
    """Local civil date. Trip bounds are dates a person typed, not instants."""
    return dt.datetime.now(config.tzinfo(cfg)).date()


def parse_date(value: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError("dates must look like 2026-08-20")


def validate(name: str | None, city: str, starts_on: str, ends_on: str,
             radius_miles: float | None) -> dict:
    """Normalize form input or raise ValueError with something worth showing."""
    city = (city or "").strip()
    if not city:
        raise ValueError("enter the city you are travelling to")
    start, end = parse_date(starts_on), parse_date(ends_on)
    if end < start:
        raise ValueError("the trip cannot end before it starts")
    radius = DEFAULT_RADIUS_MILES if radius_miles in (None, "") else float(radius_miles)
    if not RADIUS_MIN <= radius <= RADIUS_MAX:
        raise ValueError(f"radius must be between {RADIUS_MIN:g} and {RADIUS_MAX:g} miles")
    return {"name": (name or "").strip() or city, "city": city,
            "starts_on": start.isoformat(), "ends_on": end.isoformat(),
            "radius_miles": radius}


def add(conn: sqlite3.Connection, name: str | None, city: str, starts_on: str,
        ends_on: str, radius_miles: float | None = None,
        geocode=geo.geocode_city) -> int:
    """Create a trip, resolving its city to a centre point.

    A city that will not geocode is still stored. The trip's own sources work
    without coordinates -- only the "performers I follow, near where I'll be"
    half needs a centre -- so refusing the whole trip over a Nominatim miss
    would be the wrong trade. `/trips` says so plainly instead.
    """
    fields = validate(name, city, starts_on, ends_on, radius_miles)
    # Nominatim sleeps for its rate limit; do not hold the write lock across it.
    conn.commit()
    point = geocode(fields["city"])
    lat, lon = point if point else (None, None)
    return conn.execute(
        "INSERT INTO trip (name,city,lat,lon,radius_miles,starts_on,ends_on,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (fields["name"], fields["city"], lat, lon, fields["radius_miles"],
         fields["starts_on"], fields["ends_on"], _now())).lastrowid


def update(conn: sqlite3.Connection, trip_id: int, name: str | None, city: str,
           starts_on: str, ends_on: str, radius_miles: float | None = None,
           geocode=geo.geocode_city, notes: str | None = None) -> bool:
    """Edit a trip. Re-geocodes only when the city text actually changed.

    `notes` is free-form scratch space for the whole trip -- the ideas that have
    no date and so cannot live on the calendar. Passing None leaves it alone.
    """
    existing = get(conn, trip_id)
    if existing is None:
        return False
    fields = validate(name, city, starts_on, ends_on, radius_miles)
    if notes is not None:
        conn.execute("UPDATE trip SET notes=? WHERE id=?",
                     ((notes.strip() or None), trip_id))
    lat, lon = existing["lat"], existing["lon"]
    if fields["city"] != existing["city"] or lat is None or lon is None:
        conn.commit()
        point = geocode(fields["city"])
        lat, lon = point if point else (None, None)
    conn.execute(
        "UPDATE trip SET name=?,city=?,lat=?,lon=?,radius_miles=?,starts_on=?,ends_on=? "
        "WHERE id=?",
        (fields["name"], fields["city"], lat, lon, fields["radius_miles"],
         fields["starts_on"], fields["ends_on"], trip_id))
    return True


def remove(conn: sqlite3.Connection, trip_id: int) -> dict | None:
    """Delete a trip and everything scoped to it.

    Scoped sources exist only to serve this trip, so leaving them behind would
    strand rows that no view can reach. Website sources go through
    `websites.remove_source`, which also clears their imported events.
    """
    from . import websites

    trip = get(conn, trip_id)
    if trip is None:
        return None
    source_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM web_source WHERE trip_id=?", (trip_id,))]
    for source_id in source_ids:
        websites.remove_source(conn, source_id)
    handles = [r["handle"] for r in conn.execute(
        "SELECT handle FROM account WHERE trip_id=?", (trip_id,))]
    # An account keeps its posts and its discovery history; only the scope and
    # the rotation membership belonged to the trip. Deliberately 'candidate' and
    # not 'rejected': `discovery.stage` never re-surfaces a rejected handle, so
    # deleting a trip would silently blacklist those accounts forever.
    #
    # Clearing `proposed_reason` keeps them out of the /discover queue in the
    # meantime -- `discovery.pending` selects on it, and an Austin venue offered
    # for a Charlotte calendar under the words "added by hand" is noise. If one
    # is ever genuinely worth suggesting, a tagging pass will say why itself.
    conn.execute("UPDATE account SET trip_id=NULL, is_polled=0, status='candidate', "
                 "proposed_reason=NULL WHERE trip_id=?", (trip_id,))
    conn.execute("DELETE FROM trip WHERE id=?", (trip_id,))
    return {"name": trip["name"], "websites": len(source_ids), "accounts": len(handles)}


def get(conn: sqlite3.Connection, trip_id: int | None) -> sqlite3.Row | None:
    if trip_id is None:
        return None
    return conn.execute("SELECT * FROM trip WHERE id=?", (trip_id,)).fetchone()


def listing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Trips for the picker: soonest first, past trips last.

    A database without the table is a database without trips -- older installs
    reach this before `db.connect` has run its migrations, and so do the
    scheduler's minimal fixtures.
    """
    try:
        return conn.execute("SELECT * FROM trip ORDER BY starts_on, id").fetchall()
    except sqlite3.OperationalError:
        return []


def poll_window(trip: sqlite3.Row | dict) -> tuple[dt.date, dt.date]:
    """The dates a trip's own sources are worth checking."""
    start = dt.date.fromisoformat(trip["starts_on"])
    return start - dt.timedelta(days=POLL_LEAD_DAYS), dt.date.fromisoformat(trip["ends_on"])


def is_pollable(trip: sqlite3.Row | dict, day: dt.date | None = None) -> bool:
    opens, closes = poll_window(trip)
    return opens <= (day or today()) <= closes


def pollable_trip_ids(conn: sqlite3.Connection, day: dt.date | None = None) -> list[int]:
    day = day or today()
    return [t["id"] for t in listing(conn) if is_pollable(t, day)]


def scope_clause(conn: sqlite3.Connection, column: str = "trip_id",
                 day: dt.date | None = None) -> str:
    """SQL fragment limiting a source table to what should be polled today.

    Home sources always; a trip's sources only inside its window. Written as a
    literal id list rather than a join because callers splice it into queries
    that already carry their own parameters.
    """
    all_trips = listing(conn)
    if not all_trips:
        # No trips means nothing to exclude -- and possibly no `trip_id` column
        # either, on a database that predates the migration.
        return "1"
    ids = [t["id"] for t in all_trips if is_pollable(t, day or today())]
    if not ids:
        return f"{column} IS NULL"
    return f"({column} IS NULL OR {column} IN ({','.join(str(int(i)) for i in ids)}))"


def pollable_source_ids(conn: sqlite3.Connection, day: dt.date | None = None) -> list[int]:
    """Every enabled website source worth checking on a full run today."""
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM web_source WHERE enabled=1 AND {scope_clause(conn, day=day)} "
        "ORDER BY id")]


def label(trip: sqlite3.Row | dict) -> str:
    """'Austin · Aug 20-24' for the picker, without repeating the year."""
    start = dt.date.fromisoformat(trip["starts_on"])
    end = dt.date.fromisoformat(trip["ends_on"])
    if start == end:
        span = start.strftime("%b %-d")
    elif (start.year, start.month) == (end.year, end.month):
        span = f"{start.strftime('%b %-d')}-{end.day}"
    else:
        span = f"{start.strftime('%b %-d')} - {end.strftime('%b %-d')}"
    return f"{trip['name']} · {span}"


def short_label(trip: sqlite3.Row | dict) -> str:
    """'Austin' -- the picker's phone-width label, where a date range will not fit."""
    return (trip["city"] or "").split(",")[0].strip() or trip["name"]


def status(trip: sqlite3.Row | dict, day: dt.date | None = None) -> str:
    """past | current | upcoming -- drives how the picker reads, nothing else."""
    day = day or today()
    if dt.date.fromisoformat(trip["ends_on"]) < day:
        return "past"
    if dt.date.fromisoformat(trip["starts_on"]) <= day:
        return "current"
    return "upcoming"
