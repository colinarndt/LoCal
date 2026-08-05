import datetime as dt

from local_calendar import db, discovery, scheduler, trips, web, websites


AUSTIN = (30.2672, -97.7431)
PORTLAND = (45.5152, -122.6784)


def make_trip(conn, starts_on="2099-08-20", ends_on="2099-08-24", radius=30,
              city="Austin, TX", point=AUSTIN):
    return trips.add(conn, "ACL weekend", city, starts_on, ends_on, radius,
                     geocode=lambda _city: point)


def tour_stop(conn, source, external_id, starts_at, city, region, point,
              in_range=False, title="Touring act"):
    """A cached performer date. `in_range` is the HOME judgement, and False is
    the interesting case: those are exactly the dates a trip exists to find."""
    websites._upsert_event(
        conn, source,
        websites.StructuredEvent(
            external_id=external_id, title=title, starts_at=starts_at,
            start_time_known=False, venue_name=f"{city} Hall",
            permalink=f"https://artist.example/tour/{external_id}",
            city=city, region=region,
            lat=point[0] if point else None, lon=point[1] if point else None),
        "2099-07-01T12:00:00+00:00", in_range=in_range)


def performer_source(conn, conn_url="https://artist.example/tour"):
    source_id = websites.add_source(conn, conn_url, "Artist",
                                    source_type="performer", radius_miles=250)
    return conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()


def test_home_calendar_hides_trip_sources_and_the_trip_shows_them(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        source_id = websites.add_source(conn, "https://austinvenue.example/events",
                                        "Austin Venue", trip_id=trip_id)
        source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
        websites._upsert_event(
            conn, source,
            websites.StructuredEvent(
                external_id="austin-1", title="Austin Showcase", starts_at="2099-08-21",
                start_time_known=False, venue_name="Austin Venue",
                permalink="https://austinvenue.example/events/1",
                city="Austin", region="TX", lat=AUSTIN[0], lon=AUSTIN[1]),
            "2099-07-01T12:00:00+00:00")
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('local:1','venue','2099-07-01','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,created_at) "
            "VALUES ('local:1','Home Show','2099-08-21','Local Hall','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    home = client.get("/?when=all")
    trip = client.get(f"/?trip={trip_id}")

    assert b"Home Show" in home.data
    assert b"Austin Showcase" not in home.data
    assert b"Austin Showcase" in trip.data
    assert b"Home Show" not in trip.data


def test_trip_surfaces_followed_performers_near_it_and_nowhere_else(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        source = performer_source(conn)
        tour_stop(conn, source, "austin", "2099-08-22", "Austin", "TX", AUSTIN,
                  title="Touring act in Austin")
        tour_stop(conn, source, "portland", "2099-08-22", "Portland", "OR", PORTLAND,
                  title="Touring act in Portland")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    home = client.get("/?when=all")
    trip = client.get(f"/?trip={trip_id}")

    # Neither stop qualifies at home -- that is why they were cached, not shown.
    assert b"Touring act in Austin" not in home.data
    assert b"Touring act in Portland" not in home.data
    assert b"Touring act in Austin" in trip.data
    assert b"Touring act in Portland" not in trip.data


def trip_account_post(conn, trip_id, handle="austin_acct"):
    conn.execute(
        "INSERT INTO account (handle,is_polled,seen_count,added_at,status,trip_id) "
        "VALUES (?,1,0,'now','approved',?)", (handle, trip_id))
    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
        "VALUES ('ig:1',?,'2099-07-01','now')", (handle,))


def test_an_instagram_account_added_for_a_trip_stays_out_of_the_home_calendar(
        tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip_account_post(conn, trip_id)
        event_id = conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,created_at) "
            "VALUES ('ig:1','Austin IG Show','2099-08-21','Some Bar','now')").lastrowid
        conn.execute(
            "INSERT INTO event_source "
            "(event_id,source_kind,source_item_id,match_method,created_at) "
            "VALUES (?,'instagram','ig:1','extracted','now')", (event_id,))

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    assert b"Austin IG Show" not in client.get("/?when=all").data
    assert b"Austin IG Show" in client.get(f"/?trip={trip_id}").data


def test_a_generated_occurrence_inherits_its_accounts_trip_scope(tmp_path, monkeypatch):
    """`pipeline.expand_series` writes no event_source row for the dates it
    generates, so a scope test that only walks event_source loses them in both
    directions at once: leaked home, missing from the trip."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip_account_post(conn, trip_id)
        series_id = conn.execute(
            "INSERT INTO series (title,kind,created_at) "
            "VALUES ('Austin Weekly','recurring','now')").lastrowid
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,occurrence_of,created_at) "
            "VALUES ('ig:1','Austin Weekly','2099-08-22','Some Bar',?,'now')", (series_id,))

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    assert b"Austin Weekly" not in client.get("/?when=all").data
    assert b"Austin Weekly" in client.get(f"/?trip={trip_id}").data


def test_an_account_you_already_follow_at_home_is_not_stolen_by_a_trip(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        conn.execute(
            "INSERT INTO account (handle,is_polled,seen_count,added_at,status) "
            "VALUES ('home_venue',1,0,'now','approved')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    response = web.app.test_client().post(
        "/discover/add", data={"handle": "home_venue", "trip_id": str(trip_id)})

    assert "err=already-home" in response.headers["Location"]
    with db.session(path) as conn:
        row = conn.execute(
            "SELECT is_polled,trip_id FROM account WHERE handle='home_venue'").fetchone()
    assert (row["is_polled"], row["trip_id"]) == (1, None)


def test_deleting_a_trip_does_not_blacklist_its_accounts_from_discovery(tmp_path):
    """`discovery.stage` never re-surfaces a rejected handle, so 'rejected' here
    would bar those accounts from the home calendar permanently."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip_account_post(conn, trip_id)
        conn.execute("UPDATE account SET proposed_reason='added by hand' "
                     "WHERE handle='austin_acct'")
        trips.remove(conn, trip_id)
        row = conn.execute(
            "SELECT is_polled,status,trip_id FROM account WHERE handle='austin_acct'").fetchone()
        # ...and it does not turn up in the home suggestion queue either: an
        # Austin venue proposed for a Charlotte calendar is noise.
        suggested = [c["handle"] for c in discovery.pending(conn)]

    assert (row["is_polled"], row["status"], row["trip_id"]) == (0, "candidate", None)
    assert suggested == []


def test_trip_dates_bound_the_view_in_both_directions(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        source = performer_source(conn)
        tour_stop(conn, source, "during", "2099-08-21", "Austin", "TX", AUSTIN,
                  title="During the trip")
        tour_stop(conn, source, "after", "2099-09-30", "Austin", "TX", AUSTIN,
                  title="After the trip")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    trip = web.app.test_client().get(f"/?trip={trip_id}")

    assert b"During the trip" in trip.data
    assert b"After the trip" not in trip.data


def test_a_finished_trip_still_shows_its_events(tmp_path, monkeypatch):
    """The 'upcoming' floor would empty a trip you have already taken."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn, starts_on="2020-05-01", ends_on="2020-05-03")
        source = performer_source(conn)
        tour_stop(conn, source, "past", "2020-05-02", "Austin", "TX", AUSTIN,
                  title="A show back then")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get(f"/?trip={trip_id}")

    assert b"A show back then" in page.data


def test_a_stop_without_coordinates_still_matches_the_trip_city(tmp_path, monkeypatch):
    """Failed geocodes are cached, so a coordinate-only test loses these forever."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        source = performer_source(conn)
        tour_stop(conn, source, "nocoords", "2099-08-21", "Austin", "TX", None,
                  title="Unlocatable venue show")
        tour_stop(conn, source, "elsewhere", "2099-08-21", "Boise", "ID", None,
                  title="Unlocatable elsewhere")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get(f"/?trip={trip_id}")

    assert b"Unlocatable venue show" in page.data
    assert b"Unlocatable elsewhere" not in page.data


def test_the_trip_radius_is_the_trips_own_not_the_performers(tmp_path, monkeypatch):
    """A 250-mile home watch must not drag Houston into an Austin weekend."""
    path = tmp_path / "calendar.db"
    houston = (29.7604, -95.3698)   # ~145 miles from Austin
    with db.session(path) as conn:
        trip_id = make_trip(conn, radius=30)
        source = performer_source(conn)          # radius_miles=250
        tour_stop(conn, source, "houston", "2099-08-21", "Houston", "TX", houston,
                  title="Houston date")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    assert b"Houston date" not in web.app.test_client().get(f"/?trip={trip_id}").data

    with db.session(path) as conn:
        trips.update(conn, trip_id, "ACL weekend", "Austin, TX", "2099-08-20",
                     "2099-08-24", 200, geocode=lambda _city: AUSTIN)

    assert b"Houston date" in web.app.test_client().get(f"/?trip={trip_id}").data


def test_trip_sources_are_polled_only_near_the_trip(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        soon = trips.add(conn, "Soon", "Austin, TX", "2099-08-20", "2099-08-24",
                         30, geocode=lambda _city: AUSTIN)
        later = trips.add(conn, "Later", "Portland, OR", "2099-11-01", "2099-11-05",
                          30, geocode=lambda _city: PORTLAND)
        home_id = websites.add_source(conn, "https://home.example/events", "Home")
        soon_id = websites.add_source(conn, "https://austin.example/events", "Austin",
                                      trip_id=soon)
        later_id = websites.add_source(conn, "https://portland.example/events", "Portland",
                                       trip_id=later)
        for handle, trip_id in (("home_acct", None), ("austin_acct", soon),
                                ("portland_acct", later)):
            conn.execute(
                "INSERT INTO account (handle,is_polled,seen_count,added_at,status,trip_id) "
                "VALUES (?,1,0,'now','approved',?)", (handle, trip_id))

        # Inside the lead time for the August trip, nowhere near the November one.
        day = dt.date(2099, 8, 1)
        pollable = trips.pollable_source_ids(conn, day=day)
        clause = trips.scope_clause(conn, day=day)
        handles = [r["handle"] for r in conn.execute(
            f"SELECT handle FROM account WHERE is_polled=1 AND {clause} ORDER BY handle")]

    assert home_id in pollable and soon_id in pollable
    assert later_id not in pollable
    assert handles == ["austin_acct", "home_acct"]


def test_the_rotation_ignores_dormant_trip_sources(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    monkeypatch.setattr(trips, "today", lambda cfg=None: dt.date(2099, 1, 1))
    with db.session(path) as conn:
        trip_id = trips.add(conn, "Later", "Austin, TX", "2099-08-20", "2099-08-24",
                            30, geocode=lambda _city: AUSTIN)
        home_id = websites.add_source(conn, "https://home.example/events", "Home")
        websites.add_source(conn, "https://austin.example/events", "Austin",
                            trip_id=trip_id)
        conn.execute(
            "INSERT INTO account (handle,is_polled,seen_count,added_at,status,trip_id) "
            "VALUES ('austin_acct',1,0,'now','approved',?)", (trip_id,))
        conn.execute(
            "INSERT INTO account (handle,is_polled,seen_count,added_at,status) "
            "VALUES ('home_acct',1,0,'now','approved')")

        assert scheduler.due_venue_source_ids(conn, 24) == [home_id]
        assert discovery.approved_handles(conn) == ["home_acct"]


def test_a_performer_watch_cannot_be_narrowed_to_one_trip(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        try:
            websites.add_source(conn, "https://artist.example/tour", "Artist",
                                source_type="performer", radius_miles=250,
                                trip_id=trip_id)
        except ValueError as exc:
            assert "everywhere" in str(exc)
        else:                                  # pragma: no cover - failure path
            raise AssertionError("a performer watch was scoped to a trip")


def test_deleting_a_trip_takes_its_sources_with_it(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        websites.add_source(conn, "https://austin.example/events", "Austin",
                            trip_id=trip_id)
        conn.execute(
            "INSERT INTO account (handle,is_polled,seen_count,added_at,status,trip_id) "
            "VALUES ('austin_acct',1,0,'now','approved',?)", (trip_id,))
        home_id = websites.add_source(conn, "https://home.example/events", "Home")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    web.app.test_client().post(f"/trips/{trip_id}/remove")

    with db.session(path) as conn:
        assert trips.get(conn, trip_id) is None
        assert [r["id"] for r in conn.execute("SELECT id FROM web_source")] == [home_id]
        account = conn.execute(
            "SELECT is_polled,trip_id FROM account WHERE handle='austin_acct'").fetchone()
        assert (account["is_polled"], account["trip_id"]) == (0, None)


def test_the_trip_picker_lists_trips_and_offers_to_add_one(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get("/").data.decode()

    assert ">home</option>" in page
    assert 'value="/trips"' in page
    assert f'trip={trip_id}' in page
    # Two pickers, both server-rendered: iOS Safari draws a blank row for an
    # option whose text JavaScript rewrote after load, so the wide and narrow
    # labels are separate elements rather than one element and a script.
    assert 'id="trippick"' in page and "ACL weekend · Aug 20-24" in page
    assert 'id="trippick-short"' in page and ">Austin</option>" in page
    assert "syncTripLabels" not in page
    # Search sits left of the two dropdowns now.
    assert page.index('class="search"') < page.index('class="views"')


def test_a_deleted_trip_in_a_bookmark_falls_back_to_the_home_calendar(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('local:1','venue','2099-07-01','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,created_at) "
            "VALUES ('local:1','Home Show','2099-08-21','Local Hall','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get("/?trip=404&when=all")

    assert page.status_code == 200
    assert b"Home Show" in page.data


def test_a_trip_ics_is_the_same_filtered_view(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        source = performer_source(conn)
        tour_stop(conn, source, "austin", "2099-08-22", "Austin", "TX", AUSTIN,
                  title="Touring act in Austin")
        tour_stop(conn, source, "portland", "2099-08-22", "Portland", "OR", PORTLAND,
                  title="Touring act in Portland")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    feed = web.app.test_client().get(f"/calendar.ics?trip={trip_id}").data.decode()

    assert "Touring act in Austin" in feed
    assert "Touring act in Portland" not in feed
    assert "X-WR-CALNAME:ACL weekend" in feed
