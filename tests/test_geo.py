from social_calendar import db, geo, websites


def test_geocode_all_uses_event_locations_for_website_and_performer_venues(monkeypatch):
    conn = db.connect(":memory:")
    venue_source_id = websites.add_source(
        conn, "https://venue.example/events", "Venue calendar")
    performer_source_id = websites.add_source(
        conn, "https://artist.example/tour", "Artist tour", source_type="performer",
        radius_miles=250)
    venue_source = conn.execute(
        "SELECT * FROM web_source WHERE id=?", (venue_source_id,)).fetchone()
    performer_source = conn.execute(
        "SELECT * FROM web_source WHERE id=?", (performer_source_id,)).fetchone()
    websites._upsert_event(
        conn, venue_source,
        websites.StructuredEvent(
            external_id="venue-show", title="Venue show", starts_at="2099-08-01",
            start_time_known=False, venue_name="Town Hall", city="Gastonia", region="NC",
            permalink="https://venue.example/events/show"),
        "2099-07-01T12:00:00+00:00")
    websites._upsert_event(
        conn, performer_source,
        websites.StructuredEvent(
            external_id="tour-stop", title="Tour stop", starts_at="2099-08-02",
            start_time_known=False, venue_name="Tour Hall", city="Lexington", region="KY",
            permalink="https://artist.example/tour/stop"),
        "2099-07-01T12:00:00+00:00", in_range=True)
    # A previous lookup may have found coordinates but not a neighborhood.
    # It must be retried so existing installs can gain the new filter data.
    conn.execute(
        "INSERT INTO venue (venue_key,display_name,lat,lon,geocoded_at) "
        "VALUES ('town hall','Town Hall',35.1,-81.1,'now')")

    venue_queries, performer_queries = [], []
    monkeypatch.setattr(geo, "geocode_venue", lambda name, city: (
        venue_queries.append((name, city)) or {
            "lat": 35.1, "lon": -81.1, "neighborhood": "Downtown",
            "address": "Town Hall, Gastonia, NC"}))
    monkeypatch.setattr(geo, "geocode_place", lambda query: (
        performer_queries.append(query) or {
            "lat": 38.0, "lon": -84.5, "neighborhood": "East End",
            "address": "Tour Hall, Lexington, KY"}))

    stats = geo.geocode_all(conn)

    assert stats["geocoded"] == 2
    assert venue_queries == [("Town Hall", "Gastonia, NC")]
    assert performer_queries == ["Tour Hall, Lexington, KY"]
    neighborhoods = conn.execute(
        "SELECT venue_key,neighborhood FROM venue ORDER BY venue_key").fetchall()
    assert [tuple(row) for row in neighborhoods] == [
        ("tour hall", "East End"),
        ("town hall", "Downtown"),
    ]
