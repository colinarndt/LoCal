"""Venue geocoding via OpenStreetMap Nominatim, plus distance math.

Only ~15 distinct venues exist, so this is a small cached lookup table rather
than a pipeline. Geocode once, store forever; nothing re-queries a venue that
already has coordinates.

Nominatim usage policy: max 1 request/second, identifying User-Agent required.
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request

from . import config

NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "local-calendar/0.3 (personal event aggregator)"

# Venue names as extracted do not always match OSM. The normalizer strips
# apostrophes ("Petra's" -> "Petras Bar"), and some "venues" are really a series
# or a room inside a larger campus. These are the author's Charlotte entries --
# harmless anywhere else, since a key that never appears never matches. Add your
# own as you hit them.
QUERY_OVERRIDES = {
    "petras": "Petra's Bar Charlotte",
    "petris": "Petra's Bar Charlotte",          # OCR misread of Petra's
    "crossroads cinema": "Camp North End Charlotte NC",  # film series at Camp
}


def default_city() -> str:
    return config.load()["city"]


def in_bounds(lat: float, lon: float, cfg: dict | None = None) -> bool:
    """Reject a geocode that landed in the wrong metro.

    Nominatim will happily answer with a same-named city elsewhere -- "Neptune
    Charlotte NC" resolved to Port Charlotte, FLORIDA. This used to be a
    hand-drawn bounding box, which does not survive moving cities; a radius from
    the configured centre is one number a person can actually set.
    """
    cfg = cfg or config.load()
    return haversine_miles(cfg["lat"], cfg["lon"], lat, lon) <= cfg["radius_miles"]


def geocode_city(city: str, pause: float = 1.1) -> tuple[float, float] | None:
    """Resolve a city name to a centre point. Used once, by `cli init`."""
    if not (city or "").strip():
        return None
    try:
        results = _get({"q": city, "format": "json", "limit": 1})
    except Exception:
        return None
    finally:
        time.sleep(pause)
    if not results:
        return None
    return float(results[0]["lat"]), float(results[0]["lon"])

# Nominatim reports the finest-grained match first; these are the address keys
# worth treating as "neighborhood", most specific first.
_HOOD_KEYS = ("neighbourhood", "suburb", "quarter", "city_district", "borough")


def _get(params: dict) -> list:
    url = f"{NOMINATIM}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def geocode_venue(name: str, city: str | None = None, pause: float = 1.1) -> dict | None:
    """Resolve a venue name to coordinates + neighborhood. None if not found.

    `pause` honours Nominatim's 1 req/sec policy -- callers loop over venues.
    `city` defaults to the configured one; pass "" to search without it.
    """
    if not name or not name.strip():
        return None
    if city is None:
        city = default_city()
    try:
        results = _get({
            "q": f"{name}, {city}",
            "format": "json",
            "limit": 1,
            "addressdetails": 1,
        })
    except Exception:
        return None
    finally:
        time.sleep(pause)

    if not results:
        return None
    r = results[0]
    lat, lon = float(r["lat"]), float(r["lon"])
    if not in_bounds(lat, lon):
        return None  # right name, wrong metro
    addr = r.get("address") or {}
    hood = next((addr[k] for k in _HOOD_KEYS if addr.get(k)), None)
    return {
        "lat": lat,
        "lon": lon,
        "neighborhood": hood,
        "address": r.get("display_name"),
        "osm_type": r.get("type"),
    }


def geocode_place(query: str, pause: float = 1.1) -> dict | None:
    """Resolve a touring-event venue without applying the local-metro guard."""
    if not (query or "").strip():
        return None
    try:
        results = _get({"q": query, "format": "json", "limit": 1,
                        "addressdetails": 1})
    except Exception:
        return None
    finally:
        time.sleep(pause)
    if not results:
        return None
    r = results[0]
    addr = r.get("address") or {}
    hood = next((addr[k] for k in _HOOD_KEYS if addr.get(k)), None)
    return {"lat": float(r["lat"]), "lon": float(r["lon"]),
            "neighborhood": hood, "address": r.get("display_name")}


def geocode_zip(zipcode: str, country: str | None = None, pause: float = 1.1,
                check_bounds: bool = True) -> tuple[float, float] | None:
    """Centroid of a postal code, for radius search."""
    zipcode = (zipcode or "").strip()
    if not zipcode.isdigit() or len(zipcode) != 5:
        return None
    if country is None:
        country = config.load()["country"]
    try:
        results = _get({"postalcode": zipcode, "country": country, "format": "json", "limit": 1})
    except Exception:
        return None
    finally:
        time.sleep(pause)
    if not results:
        return None
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    if check_bounds and not in_bounds(lat, lon):
        # A valid zip somewhere else entirely (99999 resolves to Ohio). Treating
        # it as "not found" surfaces a warning instead of an unexplained empty page.
        return None
    return lat, lon


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Accurate enough for 'is this venue within N miles'."""
    r = 3958.8  # earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bounding_box(lat: float, lon: float, miles: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lon, max_lon) enclosing a radius.

    A square around a circle, for use as a SQL prefilter only -- it over-selects
    by up to 27% at the corners, so the caller still has to measure the ones it
    returns. SQLite has no trig, and pulling every stored event into Python to
    reject Portland when you asked about Austin is the alternative.
    """
    dlat = miles / 69.0
    # Meridians converge; at 60 degrees a degree of longitude is half as wide.
    scale = math.cos(math.radians(max(-89.0, min(89.0, lat))))
    dlon = miles / (69.172 * scale) if scale > 1e-6 else 180.0
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def city_key(city: str | None) -> str:
    """'Austin, TX' -> 'austin'. Matches an event's `location_city` field."""
    return (city or "").split(",")[0].strip().casefold()


def label_for(venue_key: str, osm_address: str | None, fallback: str | None = None) -> str:
    """Human label for a venue.

    Nominatim's first address component is usually the proper name ("Petra\'s"),
    but not always: a query override or a fuzzy match can return the campus or
    the neighbourhood instead ("Camp Greene" for Camp North End). Adopt the OSM
    name only when it actually resembles the key; otherwise title-case the key,
    which is already normalized and never wrong.
    """
    from .dedupe import title_similarity

    osm = (osm_address or "").split(",")[0].strip()
    # 0.75, not 0.6: "Camp Greene" (the neighbourhood OSM returned for Camp
    # North End) scores 0.64 against its key, while genuine matches score 0.95+.
    if osm and title_similarity(osm, venue_key) >= 0.75:
        return osm
    return " ".join(w.capitalize() for w in venue_key.split()) or (fallback or venue_key)


def geocode_all(conn, city: str | None = None, force: bool = False) -> dict:
    """Fill the `venue` table for every venue_key seen in events.

    Idempotent: a venue with coordinates and a neighborhood is skipped unless
    `force`; older partial records are retried so they can join the filter.
    """
    if city is None:
        city = default_city()
    rows = conn.execute(
        "SELECT e.venue_key, e.venue_name, e.location_city, e.location_region, "
        "EXISTS (SELECT 1 FROM event_source es JOIN web_item wi "
        "ON wi.post_id=es.source_item_id JOIN web_source ws ON ws.id=wi.source_id "
        "WHERE es.event_id=e.id AND ws.source_type='performer' AND wi.in_range=1) "
        "AS performer_in_range, "
        # A venue belonging to a trip is in another metro by definition, so the
        # local-bounds guard below would reject its coordinates as a namesake.
        "(SELECT t.city FROM trip t WHERE t.id = COALESCE("
        # A hand-entered event carries its trip directly and has no source to
        # walk, so it has to be checked before the two source paths.
        " e.trip_id,"
        " (SELECT ws2.trip_id FROM event_source es2 JOIN web_item wi2 "
        "  ON wi2.post_id=es2.source_item_id JOIN web_source ws2 ON ws2.id=wi2.source_id "
        "  WHERE es2.event_id=e.id AND ws2.trip_id IS NOT NULL LIMIT 1),"
        " (SELECT a.trip_id FROM event_source es3 "
        "  JOIN source_post sp ON sp.post_id=es3.source_item_id "
        "  JOIN account a ON a.handle=sp.polled_handle "
        "  WHERE es3.event_id=e.id AND a.trip_id IS NOT NULL LIMIT 1))) AS trip_city, "
        "COUNT(*) n FROM event e "
        "WHERE e.venue_key != '' AND e.venue_key IS NOT NULL "
        "GROUP BY e.venue_key ORDER BY n DESC").fetchall()

    stats = {"seen": len(rows), "geocoded": 0, "skipped": 0, "failed": 0}
    for r in rows:
        existing = conn.execute(
            "SELECT lat,neighborhood FROM venue WHERE venue_key=?", (r["venue_key"],)).fetchone()
        if (existing and existing["lat"] is not None
                and existing["neighborhood"] is not None and not force):
            stats["skipped"] += 1
            continue

        # Commit before the lookup: geocode_venue sleeps 1.1s for Nominatim's
        # rate limit, and holding the write lock across that many sleeps takes
        # the web UI down for the length of the whole geocoding pass.
        conn.commit()
        location = ", ".join(part for part in (r["location_city"], r["location_region"])
                             if part)
        query = QUERY_OVERRIDES.get(r["venue_key"])
        if r["trip_city"]:
            # Trip venues sit outside the home radius on purpose. Anchor them on
            # the trip's own city and skip the metro guard entirely.
            hit = geocode_place(", ".join(
                part for part in (query or r["venue_name"], location or r["trip_city"])
                if part))
        elif r["performer_in_range"] and location:
            # A performer page can list dates outside the configured city but
            # inside that watch's radius. Its own city/region is authoritative;
            # the exact place lookup supplies the neighborhood for the filter.
            hit = geocode_place(", ".join(part for part in (query or r["venue_name"], location)
                                             if part))
        else:
            # Structured event pages commonly include the city where the show
            # actually happens. Prefer it to the configured city, which makes
            # a venue's neighborhood resilient to nearby-city namesakes.
            hit = (geocode_venue(query, city="") if query
                   else geocode_venue(r["venue_name"], location or city))
        if not hit:
            conn.execute(
                "INSERT INTO venue (venue_key, display_name, geocoded_at) VALUES (?,?,datetime('now')) "
                "ON CONFLICT(venue_key) DO UPDATE SET geocoded_at=datetime('now')",
                (r["venue_key"], r["venue_name"]))
            stats["failed"] += 1
            continue

        conn.execute(
            "INSERT INTO venue (venue_key, display_name, lat, lon, neighborhood, address, "
            "geocoded_at) VALUES (?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(venue_key) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, "
            "neighborhood=excluded.neighborhood, address=excluded.address, "
            "geocoded_at=excluded.geocoded_at",
            (r["venue_key"], r["venue_name"], hit["lat"], hit["lon"],
             hit["neighborhood"], hit["address"]))
        stats["geocoded"] += 1
    return stats
