import datetime as dt
import zoneinfo

from werkzeug.datastructures import MultiDict

from social_calendar import db, web
from social_calendar.web import _filters


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
    assert by_caption.data.index(b'class="views"') < by_caption.data.index(b'class="search"')
    assert b'>Search</button>' not in by_caption.data
    assert b"setTimeout(refreshResults, 250)" in by_caption.data
    assert b"activeRequest.abort()" in by_caption.data


def test_confirm_returns_to_the_event_card(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post "
            "(post_id, polled_handle, posted_at, fetched_at) VALUES ('p1','venue','2026-07-29','now')"
        )
        conn.execute(
            "INSERT INTO event (post_id, title, starts_at, created_at) "
            "VALUES ('p1','Show','2026-08-01','now')"
        )

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()

    page = client.get("/")
    response = client.post(
        "/event/1/confirm",
        headers={"Referer": "http://localhost/?category=music"},
    )

    assert b'id="event-1"' in page.data
    assert b'<div class="day" id="d-2026-08-01">Aug 1</div>' in page.data
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

    assert b'download="social-calendar.csv"' in page.data
    assert b'href="/events.csv?' in page.data
    assert csv.headers["Content-Disposition"] == "attachment; filename=social-calendar.csv"


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
