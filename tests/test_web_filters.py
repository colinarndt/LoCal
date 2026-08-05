import datetime as dt
import re
import zoneinfo

from werkzeug.datastructures import MultiDict

from local_calendar import db, web, websites
from local_calendar.web import _filters


def test_ui_dates_and_times_are_short_and_converted_to_configured_zone(monkeypatch):
    eastern = zoneinfo.ZoneInfo("America/New_York")
    monkeypatch.setattr(web.config, "tzinfo", lambda: eastern)

    # This UTC instant is still July 31 in New York.
    value = "2026-08-01T01:30:00+00:00"
    assert web.short_date(value) == "Jul 31"
    assert web.clock(value) == "9:30pm"
    assert web.local_stamp(value) == "Jul 31, 9:30pm"


def test_naive_event_time_is_already_local(monkeypatch):
    monkeypatch.setattr(
        web.config, "tzinfo", lambda: dt.timezone(dt.timedelta(hours=-4)))

    assert web.clock("2026-08-01T20:00:00") == "8pm"


def test_preview_caption_html_is_formatted_but_sanitized():
    rendered = str(web.caption_html(
        '<p onclick="steal()">A <strong>strange</strong> night.</p>'
        '<script>alert(1)</script>'
        '<a href="javascript:steal()">bad link</a>'
        '<a href="https://venue.example/details" style="color:red">details</a>'))

    assert rendered == (
        '<p>A <strong>strange</strong> night.</p>'
        '<a>bad link</a>'
        '<a href="https://venue.example/details" target="_blank" rel="noopener">details</a>')


def test_preview_link_label_matches_its_destination():
    assert web.source_link_label("https://www.instagram.com/p/example/") == "instagram post"
    assert web.source_link_label("https://venue.example/events/show") == "event page"


def test_list_heading_uses_weekday_names_for_the_current_calendar_week():
    today = dt.date(2026, 8, 2)  # Sunday

    assert web.day_label("2026-08-03", today) == "Monday"
    assert web.day_label("2026-08-08", today) == "Saturday"
    assert web.day_label("2026-08-09", today) == "Aug 9"


def test_until_is_not_a_hidden_filter_after_date_field_removal():
    where, params = _filters(MultiDict([("when", "all"), ("until", "2026-08-01")]))

    assert "starts_at,1,10) <=" not in where
    assert "2026-08-01" not in params


def test_search_filter_covers_title_and_all_attached_captions():
    where, params = _filters(MultiDict([("when", "all"), ("q", "  Glass  ")]))

    assert "e.title LIKE" in where
    assert "event_source ses" in where
    assert params == ["%Glass%", "%Glass%", "%Glass%"]


def test_search_box_finds_an_event_by_attached_instagram_caption(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.executemany(
            "INSERT INTO source_post "
            "(post_id,polled_handle,posted_at,caption,fetched_at) VALUES (?,?,?,?,?)",
            [
                ("web:1", "", "2099-07-01", "", "now"),
                ("ig:1", "theatre", "2099-07-01",
                 "A Tennessee Williams classic returns this summer", "now"),
                ("web:2", "", "2099-07-01", "Weekend vendors", "now"),
            ],
        )
        play_id = conn.execute(
            "INSERT INTO event (post_id,title,starts_at,category,created_at) "
            "VALUES ('web:1','The Glass Menagerie','2099-08-01','theater','now')"
        ).lastrowid
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,category,created_at) "
            "VALUES ('web:2','Summer Market','2099-08-02','market','now')"
        )
        conn.execute(
            "INSERT INTO event_source "
            "(event_id,source_kind,source_item_id,match_method,created_at) "
            "VALUES (?,'instagram','ig:1','caption','now')",
            (play_id,),
        )

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    by_caption = client.get("/?q=Tennessee")
    by_title = client.get("/?q=glass")

    assert b"The Glass Menagerie" in by_caption.data
    assert b"Summer Market" not in by_caption.data
    assert b"The Glass Menagerie" in by_title.data
    assert b'name="q"' in by_caption.data
    assert b'value="Tennessee"' in by_caption.data
    # Search leads the bar; the view and trip pickers sit right of it.
    assert by_caption.data.index(b'class="search"') < by_caption.data.index(b'class="views"')
    assert b'>Search</button>' not in by_caption.data
    assert b"setTimeout(refreshResults, 250)" in by_caption.data
    assert b"activeRequest.abort()" in by_caption.data


def test_venue_filter_omits_out_of_range_performer_stops(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        performer_id = websites.add_source(
            conn, "https://artist.example/tour", "Artist", source_type="performer",
            radius_miles=250)
        performer = conn.execute(
            "SELECT * FROM web_source WHERE id=?", (performer_id,)).fetchone()
        websites._upsert_event(
            conn, performer,
            websites.StructuredEvent(
                external_id="remote-stop", title="Remote show", starts_at="2099-08-01",
                start_time_known=False, venue_name="Remote Hall",
                permalink="https://artist.example/tour/remote"),
            "2099-07-01T12:00:00+00:00", in_range=False)
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('local:1','venue','2099-07-01','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,venue_key,created_at) "
            "VALUES ('local:1','Local show','2099-08-02','Local Hall','local hall','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get("/?when=all")

    assert b'<option value="remote hall"' not in page.data
    assert b'<option value="local hall"' in page.data


def test_venue_filter_counts_only_upcoming_events_by_default(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) VALUES "
            "('past:1','venue','2000-07-01','now'),"
            "('future:1','venue','2099-07-01','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,venue_name,venue_key,created_at) VALUES "
            "('past:1','Past show','2000-08-01','Boileryard at Camp North End',"
            "'boileryard camp north end','now'),"
            "('future:1','Future show','2099-08-01','Boileryard at Camp North End',"
            "'boileryard camp north end','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    page = web.app.test_client().get("/")

    assert re.search(
        br">\s*Boileryard at Camp North End \(1\)</option>", page.data)


def test_review_link_counts_only_events_visible_under_current_date_filter(
        tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) VALUES "
            "('past:review','venue','2000-07-01','now'),"
            "('future:review','venue','2099-07-01','now')")
        conn.execute(
            "INSERT INTO event "
            "(post_id,title,starts_at,needs_review,created_at) VALUES "
            "('past:review','Past review','2000-08-01',1,'now'),"
            "('future:review','Future review','2099-08-01',1,'now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    upcoming = client.get("/")
    review_page = client.get("/?review=1")
    all_dates = client.get("/?when=all")

    assert b"1 to review" in upcoming.data
    assert b"2 to review" not in upcoming.data
    assert upcoming.data.index(b'id="event-total"') < upcoming.data.index(
        b'id="review-total"') < upcoming.data.index(b">sources</a>")
    assert b'name="review"' not in upcoming.data
    assert b">Filter</button>" not in upcoming.data
    assert b".filters select" in upcoming.data
    assert b'.filters input[type="range"]' in upcoming.data
    assert b'.filters input[type="text"]' in upcoming.data
    assert b"Future review" in review_page.data
    assert b"Past review" not in review_page.data
    assert b"2 to review" in all_dates.data


def test_confirm_returns_to_the_event_card(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id, polled_handle, posted_at, fetched_at) VALUES ('p1','venue','2099-07-29','now')"
        )
        conn.execute(
            "INSERT INTO event (post_id, title, starts_at, created_at) "
            "VALUES ('p1','Show','2099-08-01','now')"
        )

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    page = client.get("/")
    response = client.post(
        "/event/1/confirm",
        headers={"Referer": "http://localhost/?category=music"},
    )

    assert b'id="event-1"' in page.data
    assert b'<div class="day" id="d-2099-08-01">Aug 1</div>' in page.data
    assert b'class="confirm-form"' in page.data
    assert b"event.preventDefault()" in page.data
    assert b'/static/icon.svg' in page.data
    assert client.get("/static/icon.svg").status_code == 200
    assert response.headers["Location"] == "http://localhost/?category=music#event-1"

    ajax_response = client.post(
        "/event/1/confirm",
        headers={"X-Requested-With": "fetch"},
    )
    assert ajax_response.get_json() == {"confirmed": False}


def test_inferred_series_is_reviewed_before_grouping_and_can_be_hidden(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        source_id = websites.add_source(
            conn, "https://whitewater.org/calendar", "WWC River Jam")
        source = conn.execute("SELECT * FROM web_source WHERE id=?", (source_id,)).fetchone()
        for day in ("2099-08-02", "2099-08-03"):
            month, date = day[5:7], day[8:10]
            url = f"https://whitewater.org/event/hours-of-operation-{month}-{date}-2099/"
            event = websites.StructuredEvent(
                external_id=url, title="Hours of Operation", starts_at=day,
                start_time_known=False, venue_name=None, permalink=url)
            websites._upsert_event(conn, source, event, "2099-08-01T12:00:00+00:00")
        websites.refresh_series_candidates(conn, source_id)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    regular_page = client.get("/")
    review_page = client.get("/?review=1")

    assert b"This looks like a recurring series" in regular_page.data
    assert b"Possible recurring series: Hours of Operation" in review_page.data
    assert b"treat as series" in review_page.data
    with db.read_session(path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM event WHERE occurrence_of IS NOT NULL").fetchone()[0] == 0
        candidate_id = conn.execute("SELECT id FROM web_series_candidate").fetchone()[0]

    response = client.post(
        f"/series-candidate/{candidate_id}/confirm-hide",
        headers={"Referer": "http://localhost/?review=1"})
    assert response.status_code == 302
    with db.read_session(path) as conn:
        candidate = conn.execute("SELECT * FROM web_series_candidate").fetchone()
        assert candidate["status"] == "confirmed"
        assert conn.execute(
            "SELECT is_hidden FROM series WHERE id=?", (candidate["series_id"],)).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event WHERE occurrence_of=?",
            (candidate["series_id"],)).fetchone()[0] == 2
    hidden_page = client.get("/")
    assert b"Hours of Operation" not in hidden_page.data


def test_hide_on_a_confirmed_recurring_event_hides_the_series(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('p1','venue','2099-08-01','now'),('p2','venue','2099-08-01','now')")
        series_id = conn.execute(
            "INSERT INTO series (title,kind,rule,created_at) "
            "VALUES ('Weekly Show','recurring','every monday','now')").lastrowid
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,occurrence_of,created_at) VALUES "
            "('p1','Weekly Show','2099-08-03',?,'now'),"
            "('p2','Weekly Show','2099-08-10',?,'now')", (series_id, series_id))

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    page = client.get("/")
    assert b"hide series" in page.data

    response = client.post("/event/1/hide", headers={"Referer": "http://localhost/"})
    assert response.status_code == 302
    with db.read_session(path) as conn:
        assert conn.execute("SELECT is_hidden FROM series WHERE id=?", (series_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT SUM(is_hidden) FROM event").fetchone()[0] == 0
    assert b"Weekly Show" not in client.get("/").data


def test_event_calendar_download_contains_only_the_selected_event(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,permalink,fetched_at) "
            "VALUES ('p1','venue','2026-07-29','https://events.example/show','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,start_time_known,venue_name,ticket_url,created_at) "
            "VALUES ('p1','Show','2026-08-01T20:00:00',1,'Example Room',"
            "'https://tickets.example/show','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    response = web.app.test_client().get("/event/1/calendar.ics")

    assert response.status_code == 200
    assert response.mimetype == "text/calendar"
    assert response.headers["Content-Disposition"] == "attachment; filename=event-1.ics"
    assert b"SUMMARY:Show" in response.data
    assert b"LOCATION:Example Room" in response.data
    assert b"tickets: https://tickets.example/show" in response.data
    assert b"UID:sc-1@social-calendar" in response.data


def test_csv_link_uses_a_browser_download_instead_of_replacing_the_app_view(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('p1','venue','2099-07-29','now')")
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('p1','Show','2099-08-01','now')")

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    page = client.get("/")
    csv = client.get("/events.csv")

    assert b'download="local-calendar.csv"' in page.data
    assert b'href="/events.csv?' in page.data
    assert csv.headers["Content-Disposition"] == "attachment; filename=local-calendar.csv"


def test_existing_schema_all_day_span_is_repaired_when_database_opens(tmp_path):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,raw_provider_json,source_name,fetched_at) "
            "VALUES ('web:1','', '2026-08-01', ?, 'website', 'now')",
            ('{"startDate": "2026-08-20T00:00:00-04:00", '
             '"endDate": "2026-08-20T23:59:59-04:00"}',))
        conn.execute(
            "INSERT INTO event (post_id,title,starts_at,ends_at,start_time_known,created_at) "
            "VALUES ('web:1','All-day event','2026-08-20T00:00:00','2026-08-20T23:59:59',1,'now')")

    # Opening an existing database runs its idempotent data repair.
    conn = db.connect(path)
    event = conn.execute("SELECT starts_at,ends_at,start_time_known FROM event").fetchone()
    conn.close()

    assert tuple(event) == ("2026-08-20", "2026-08-20", 0)
