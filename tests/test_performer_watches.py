from social_calendar import db, websites


PUNCHUP = """<!doctype html><html><body><main>
<a href="/e/180cca11-e33f-4cb5-8126-8a4c4d93f6e6">August 6, 2026</a>
<a href="/e/180cca11-e33f-4cb5-8126-8a4c4d93f6e6">Lexington, KY</a>
<a href="/e/180cca11-e33f-4cb5-8126-8a4c4d93f6e6">Thursday – 7:00 PM Comedy Off Broadway</a>
<a href="https://tickets.example/timmy">Buy Tickets</a>
</main></body></html>"""


class Response:
    def __init__(self, body, url):
        self.body = body.encode()
        self.url = url
        self.headers = {"Content-Type": "text/html", "ETag": '"one"'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return self.body

    def geturl(self):
        return self.url


def test_performer_watch_caches_tour_but_alerts_once_for_nearby_date(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    monkeypatch.setattr(websites.config, "load", lambda: {
        "lat": 35.2271, "lon": -80.8431, "home_lat": 35.2271, "home_lon": -80.8431,
        "timezone": "America/New_York",
    })
    monkeypatch.setattr(websites.geo, "geocode_place", lambda _query: {
        "lat": 35.2300, "lon": -80.8400, "address": "Charlotte, NC",
    })

    with db.session(path) as conn:
        source_id = websites.add_source(
            conn, "https://punchup.live/timmynobrakes/tour", "Timmy No Brakes",
            source_type="performer", radius_miles=250, notify=True)
        opener = lambda request, timeout=30: Response(PUNCHUP, request.full_url)
        first = websites.poll_source(conn, source_id, opener=opener)
        second = websites.poll_source(conn, source_id, opener=opener)

        item = conn.execute("SELECT in_range,distance_miles FROM web_item").fetchone()
        event = conn.execute(
            "SELECT venue_name,location_city,location_region,ticket_url,ticket_status FROM event").fetchone()
        alerts = conn.execute("SELECT kind,ticket_url FROM alert_delivery").fetchall()

    assert first["new"] == 1
    assert first["alerts"] == 1
    assert second["alerts"] == 0
    assert item["in_range"] == 1
    assert item["distance_miles"] < 1
    assert dict(event) == {
        "venue_name": "Comedy Off Broadway", "location_city": "Lexington",
        "location_region": "KY", "ticket_url": "https://tickets.example/timmy",
        "ticket_status": "tickets",
    }
    assert [(row["kind"], row["ticket_url"]) for row in alerts] == [
        ("new", "https://tickets.example/timmy")]


def test_performer_watch_keeps_out_of_range_date_out_of_calendar(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    monkeypatch.setattr(websites.config, "load", lambda: {
        "lat": 35.2271, "lon": -80.8431, "home_lat": 35.2271, "home_lon": -80.8431,
        "timezone": "America/New_York",
    })
    monkeypatch.setattr(websites.geo, "geocode_place", lambda _query: {
        "lat": 40.7128, "lon": -74.0060, "address": "New York, NY",
    })

    with db.session(path) as conn:
        source_id = websites.add_source(
            conn, "https://punchup.live/timmynobrakes/tour", "Timmy No Brakes",
            source_type="performer", radius_miles=250, notify=True)
        result = websites.poll_source(
            conn, source_id,
            opener=lambda request, timeout=30: Response(PUNCHUP, request.full_url))
        item = conn.execute("SELECT in_range FROM web_item").fetchone()
        alerts = conn.execute("SELECT COUNT(*) FROM alert_delivery").fetchone()[0]

    assert result["new"] == 1
    assert result["alerts"] == 0
    assert item["in_range"] == 0
    assert alerts == 0


def test_performer_distance_uses_the_configured_city_not_a_legacy_home_zip(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    monkeypatch.setattr(websites.config, "load", lambda: {
        "lat": 35.2271, "lon": -80.8431,
        # These retired keys intentionally point elsewhere: they must have no effect.
        "home_lat": 40.7128, "home_lon": -74.0060,
    })
    event = websites.StructuredEvent(
        external_id="nearby", title="Nearby", starts_at="2026-08-20",
        start_time_known=False, venue_name="Charlotte", permalink="https://events.example/nearby",
        lat=35.2300, lon=-80.8400)

    with db.session(path) as conn:
        source = websites.add_source(
            conn, "https://events.example/performer", "Performer",
            source_type="performer", radius_miles=10)
        source_row = conn.execute("SELECT * FROM web_source WHERE id=?", (source,)).fetchone()
        in_range, distance = websites._qualify_performer(conn, source_row, event)

    assert in_range is True
    assert distance < 1


def test_readding_performer_watch_recalculates_its_cached_radius(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    monkeypatch.setattr(websites.config, "load", lambda: {
        "lat": 35.2271, "lon": -80.8431, "home_lat": 35.2271, "home_lon": -80.8431,
        "timezone": "America/New_York",
    })
    monkeypatch.setattr(websites.geo, "geocode_place", lambda _query: {
        "lat": 36.25, "lon": -80.8431, "address": "Elsewhere, NC",
    })

    with db.session(path) as conn:
        source_id = websites.add_source(
            conn, "https://punchup.live/timmynobrakes/tour", "Timmy No Brakes",
            source_type="performer", radius_miles=250)
        websites.poll_source(
            conn, source_id,
            opener=lambda request, timeout=30: Response(PUNCHUP, request.full_url))
        websites.add_source(
            conn, "https://punchup.live/timmynobrakes/tour", "Timmy No Brakes",
            source_type="performer", radius_miles=25)
        item = conn.execute("SELECT in_range FROM web_item").fetchone()

    assert item["in_range"] == 0
