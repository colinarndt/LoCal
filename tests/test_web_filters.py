from werkzeug.datastructures import MultiDict

from social_calendar import db, web
from social_calendar.web import _filters


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
