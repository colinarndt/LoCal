import datetime as dt

from local_calendar import db, manual, pipeline, trips, web, websites


AUSTIN = (30.2672, -97.7431)


def nowhere(conn, fields, trip=None):
    """Stand in for Nominatim: tests never reach the network."""
    return None, None


def somewhere(point):
    return lambda conn, fields, trip=None: point


def make_trip(conn):
    return trips.add(conn, "ACL weekend", "Austin, TX", "2099-08-20", "2099-08-24", 30,
                     geocode=lambda _city: AUSTIN)


def test_a_title_and_a_date_are_enough(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        manual.add(conn, {"title": "Dinner with Sam", "date": "2099-08-21"},
                   locate=nowhere)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get("/?when=all")

    assert b"Dinner with Sam" in page.data
    assert b"yours" in page.data


def test_a_date_only_event_is_not_given_an_invented_time(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        timed = manual.add(conn, {"title": "Show", "date": "2099-08-21", "time": "19:30"},
                           locate=nowhere)
        untimed = manual.add(conn, {"title": "Somewhere Thursday", "date": "2099-08-21"},
                             locate=nowhere)
        rows = {r["id"]: r for r in conn.execute(
            "SELECT id,starts_at,start_time_known FROM event")}

    assert rows[timed]["starts_at"] == "2099-08-21T19:30:00"
    assert rows[timed]["start_time_known"] == 1
    assert rows[untimed]["starts_at"] == "2099-08-21"
    assert rows[untimed]["start_time_known"] == 0


def test_a_manual_event_is_scoped_to_the_trip_it_was_created_in(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip = trips.get(conn, trip_id)
        manual.add(conn, {"title": "Barton Springs", "date": "2099-08-21"}, trip,
                   locate=nowhere)
        manual.add(conn, {"title": "Local dinner", "date": "2099-08-21"}, locate=nowhere)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    home = client.get("/?when=all")
    away = client.get(f"/?trip={trip_id}")

    assert b"Local dinner" in home.data and b"Barton Springs" not in home.data
    assert b"Barton Springs" in away.data and b"Local dinner" not in away.data


def test_a_trip_event_cannot_be_saved_outside_the_trips_dates(tmp_path, monkeypatch):
    """The form's date defaults to today, which is outside the trip you are
    looking at. Saved silently, such a row is excluded from the home calendar
    for being trip-scoped and from the trip for being out of window: it exists,
    nothing can display it, and no delete button can reach it."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    response = client.post("/events/manual/add", data={
        "title": "Typed under the trip", "date": "2026-08-05",
        "trip": str(trip_id), "back": f"trip={trip_id}"})

    assert "event_err" in response.headers["Location"]
    with db.session(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0
    # And the form it comes back to offers a date that would have worked.
    page = " ".join(client.get(f"/?trip={trip_id}").data.decode().split())
    assert 'name="date" required min="2099-08-20" max="2099-08-24" value="2099-08-20"' in page


def test_an_edit_cannot_walk_an_event_out_of_its_trip(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip = trips.get(conn, trip_id)
        event_id = manual.add(conn, {"title": "Barton Springs", "date": "2099-08-21"},
                              trip, locate=nowhere)
        try:
            manual.update(conn, event_id, {"title": "Barton Springs",
                                           "date": "2099-12-25"}, locate=nowhere)
        except ValueError as exc:
            assert "ACL weekend" in str(exc)
        else:                                  # pragma: no cover - failure path
            raise AssertionError("an event was edited out of its own trip")
        assert conn.execute(
            "SELECT starts_at FROM event WHERE id=?", (event_id,)).fetchone()[0] == "2099-08-21"


def test_a_trip_event_without_coordinates_still_shows_on_the_trip(tmp_path, monkeypatch):
    """'Sam's place' will never geocode, and refusing to show it would defeat
    the point of letting you type an itinerary in the first place."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trip = trips.get(conn, trip_id)
        manual.add(conn, {"title": "Sam's place", "date": "2099-08-22",
                          "venue": "Sam's place"}, trip, locate=nowhere)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    assert b"Sam&#39;s place" in web.app.test_client().get(f"/?trip={trip_id}").data


def test_dedupe_never_merges_a_manual_event_into_a_scraped_one(tmp_path):
    """Same title, same night, same venue -- and they must stay two rows. One of
    them is the user's own record and rewriting it is not ours to do."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,caption,fetched_at) "
            "VALUES ('ig:1','venue','2099-07-01','Riot Fest','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,venue_key,created_at) "
            "VALUES ('ig:1','Riot Fest','2099-08-21','Douglass Park','douglass park','now')")
        mine = manual.add(conn, {"title": "Riot Fest", "date": "2099-08-21",
                                 "venue": "Douglass Park",
                                 "notes": "meet Sam at the gate"}, locate=nowhere)
        pipeline.rebuild_dedupe(conn)
        row = conn.execute(
            "SELECT is_canonical,notes,title FROM event WHERE id=?", (mine,)).fetchone()
        titles = [r["title"] for r in conn.execute("SELECT title FROM event ORDER BY id")]

    assert row["is_canonical"] == 1
    assert row["notes"] == "meet Sam at the gate"
    assert titles == ["Riot Fest", "Riot Fest"]


def test_a_note_that_sounds_recurring_does_not_generate_a_series(tmp_path):
    """The note is mirrored into the caption so search can find it, which puts
    the user's words in front of the recurrence detector."""
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        manual.add(conn, {"title": "Trivia with Sam", "date": "2099-08-21",
                          "notes": "they run this every Wednesday"}, locate=nowhere)
        pipeline.expand_series(conn)
        count = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
        series = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]

    assert (count, series) == (1, 0)


def test_removing_a_website_source_cannot_delete_your_own_events(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        source_id = websites.add_source(conn, "https://venue.example/events", "Venue")
        source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
        websites._upsert_event(
            conn, source,
            websites.StructuredEvent(
                external_id="one", title="Imported", starts_at="2099-08-21",
                start_time_known=False, venue_name="Venue",
                permalink="https://venue.example/events/1"),
            "2099-07-01T12:00:00+00:00")
        mine = manual.add(conn, {"title": "Mine", "date": "2099-08-21"}, locate=nowhere)
        websites.remove_source(conn, source_id)
        survived = conn.execute("SELECT title FROM event WHERE id=?", (mine,)).fetchone()

    assert survived["title"] == "Mine"


def test_editing_keeps_the_event_and_deleting_removes_its_post_row(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        event_id = manual.add(conn, {"title": "Dinner", "date": "2099-08-21"},
                              locate=nowhere)
        manual.update(conn, event_id, {"title": "Dinner with Sam", "date": "2099-08-22",
                                       "time": "19:00", "notes": "book the 6:40 train"},
                      locate=nowhere)
        edited = conn.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()

    assert (edited["title"], edited["starts_at"]) == ("Dinner with Sam", "2099-08-22T19:00:00")
    assert edited["notes"] == "book the 6:40 train"

    with db.session(path) as conn:
        assert manual.delete(conn, event_id) is True
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_post").fetchone()[0] == 0


def test_the_delete_route_refuses_an_event_you_did_not_type(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('ig:1','venue','2099-07-01','now')")
        scraped = conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('ig:1','Not yours','2099-08-21','now')").lastrowid

    monkeypatch.setitem(web.app.config, "DB", str(path))
    response = web.app.test_client().post(f"/events/manual/{scraped}/delete")

    assert response.status_code == 404
    with db.session(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1


def test_a_javascript_link_is_dropped_rather_than_rendered(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        event_id = manual.add(conn, {"title": "Show", "date": "2099-08-21",
                                     "ticket_url": "javascript:steal()"}, locate=nowhere)
        plain = manual.add(conn, {"title": "Show 2", "date": "2099-08-21",
                                  "ticket_url": "venue.example/tickets"}, locate=nowhere)
        rows = {r["id"]: r["ticket_url"] for r in conn.execute(
            "SELECT id,ticket_url FROM event")}

    assert rows[event_id] is None
    assert rows[plain] == "https://venue.example/tickets"


def test_the_add_form_reports_what_was_wrong_and_stays_on_the_same_view(
        tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        make_trip(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    response = web.app.test_client().post(
        "/events/manual/add", data={"title": "", "date": "2099-08-21",
                                    "back": "trip=1&category=music"})

    assert "trip=1" in response.headers["Location"]
    assert "category=music" in response.headers["Location"]
    assert "event_err" in response.headers["Location"]


def test_a_manual_event_carries_its_note_into_the_calendar_feed(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        manual.add(conn, {"title": "Dinner", "date": "2099-08-21",
                          "notes": "book the 6:40 train"}, locate=nowhere)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    feed = web.app.test_client().get("/calendar.ics?when=all").data.decode()

    assert "book the 6:40 train" in feed


def test_trip_notes_can_be_saved_from_the_calendar_without_leaving_it(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    response = client.post(f"/trips/{trip_id}/notes", data={
        "notes": "  Blue Line from the airport  ", "back": f"trip={trip_id}&view=calendar"})

    assert response.headers["Location"] == f"/?trip={trip_id}&view=calendar"
    with db.session(path) as conn:
        assert trips.get(conn, trip_id)["notes"] == "Blue Line from the airport"

    # Trip planning actions share one line, in the requested order. Add event is
    # a hidden disclosure; notes open into a large modal instead of occupying the
    # calendar page while closed.
    page = client.get(f"/?trip={trip_id}").data.decode()
    actions_start = page.index('<p class="tripnote"')
    actions = page[actions_start:page.index('</p>', actions_start)]
    assert actions.index('>add event</a>') < actions.index('>trip notes')
    assert actions.index('>trip notes') < actions.index('>edit trip</a>')
    assert '>trip notes</a>' in actions
    assert '<details class="addev triptool"\n         id="trip-add-event"' in page
    assert '.addev.triptool:not([open]) { display:none; }' in page
    assert 'onclick="document.getElementById(\'trip-notes\').showModal()' in actions
    assert '<dialog class="notes-modal" id="trip-notes"' in page
    assert '<textarea name="notes" rows="14"' in page
    assert 'min-height:min(48vh,420px)' in page
    assert page.index("subscribe to this calendar") > page.index('id="trip-add-event"')


def test_trip_notes_survive_an_edit_that_does_not_mention_them(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        trip_id = make_trip(conn)
        trips.update(conn, trip_id, "ACL weekend", "Austin, TX", "2099-08-20",
                     "2099-08-24", 30, geocode=lambda _city: AUSTIN,
                     notes="stay at the Driskill\nrent bikes")
        trips.update(conn, trip_id, "ACL weekend", "Austin, TX", "2099-08-20",
                     "2099-08-25", 30, geocode=lambda _city: AUSTIN)
        trip = trips.get(conn, trip_id)

    assert trip["notes"] == "stay at the Driskill\nrent bikes"
    assert trip["ends_on"] == "2099-08-25"
