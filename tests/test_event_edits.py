import csv
import io

from local_calendar import db, web, websites


def _website_event(conn, title="Bad Imported Title"):
    source_id = websites.add_source(
        conn, "https://venue.example/events", "Example Venue"
    )
    source = conn.execute(
        "SELECT * FROM web_source WHERE id=?", (source_id,)
    ).fetchone()
    event = websites.StructuredEvent(
        external_id="show-1",
        title=title,
        starts_at="2099-08-21T19:30:00",
        start_time_known=True,
        venue_name="Example Room",
        permalink="https://venue.example/events/show-1",
    )
    websites._upsert_event(conn, source, event, "2099-08-01T12:00:00+00:00")
    event_id = conn.execute("SELECT event_id FROM web_item").fetchone()[0]
    return source, event, event_id


def test_webpage_event_title_and_notes_can_be_edited_and_exported(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        _, _, event_id = _website_event(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    response = client.post(
        f"/events/{event_id}/edit",
        data={
            "title": "The Correct Show Title",
            "notes": "Meet Alex by the west entrance",
            "back": "when=all",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"] == f"/?when=all#event-{event_id}"
    with db.session(path) as conn:
        stored = conn.execute(
            "SELECT title,title_override,notes FROM event WHERE id=?", (event_id,)
        ).fetchone()
    assert stored["title"] == "Bad Imported Title"
    assert stored["title_override"] == "The Correct Show Title"
    assert stored["notes"] == "Meet Alex by the west entrance"

    page = client.get("/?when=all").data.decode()
    assert "The Correct Show Title" in page
    assert "Meet Alex by the west entrance" in page
    assert "From the source: Bad Imported Title" in page
    assert "<summary>edit</summary>" in page
    assert 'class="flag-form" hidden method="post" action="/event/' in page
    card = page[page.index(f'id="event-{event_id}"') :]
    assert (
        card.index(f'action="/event/{event_id}/confirm"')
        < card.index(f'action="/event/{event_id}/hide"')
        < card.index('class="addev inline event-edit"')
        < card.index(f'action="/event/{event_id}/flag"')
    )

    feed = client.get("/calendar.ics?when=all").data.decode()
    assert "SUMMARY:The Correct Show Title" in feed
    assert "Meet Alex by the west entrance" in feed

    sheet = client.get("/events.csv?when=all").data.decode("utf-8-sig")
    exported = list(csv.DictReader(io.StringIO(sheet)))
    assert exported[0]["title"] == "The Correct Show Title"
    assert exported[0]["notes"] == "Meet Alex by the west entrance"


def test_webpage_edits_survive_a_source_refresh_and_notes_are_searchable(
    tmp_path, monkeypatch
):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        source, event, event_id = _website_event(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    client.post(
        f"/events/{event_id}/edit",
        data={"title": "My Short Title", "notes": "Jordan has the tickets"},
    )

    with db.session(path) as conn:
        refreshed = websites.StructuredEvent(
            **{**event.__dict__, "title": "A Better Source Title"}
        )
        websites._upsert_event(
            conn, source, refreshed, "2099-08-02T12:00:00+00:00"
        )

    page = client.get("/?when=all").data.decode()
    assert "My Short Title" in page
    assert "A Better Source Title" in page
    assert "Jordan has the tickets" in client.get("/?when=all&q=Jordan").data.decode()


def test_clearing_an_override_restores_the_webpage_title(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        _, _, event_id = _website_event(conn)

    monkeypatch.setitem(web.app.config, "DB", str(path))
    client = web.app.test_client()
    client.post(
        f"/events/{event_id}/edit",
        data={"title": "Temporary Title", "notes": "Keep this"},
    )
    client.post(
        f"/events/{event_id}/edit",
        data={"title": "", "notes": "Keep this"},
    )

    with db.session(path) as conn:
        stored = conn.execute(
            "SELECT title_override,notes FROM event WHERE id=?", (event_id,)
        ).fetchone()
    assert stored["title_override"] is None
    assert stored["notes"] == "Keep this"
    assert "Bad Imported Title" in client.get("/?when=all").data.decode()


def test_title_and_notes_can_also_be_edited_for_a_social_event(tmp_path, monkeypatch):
    path = tmp_path / "calendar.db"
    with db.session(path) as conn:
        conn.execute(
            "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
            "VALUES ('ig:1','venue','2099-07-01','now')"
        )
        event_id = conn.execute(
            "INSERT INTO event (post_id,title,starts_at,created_at) "
            "VALUES ('ig:1','Social event','2099-08-21','now')"
        ).lastrowid

    monkeypatch.setitem(web.app.config, "DB", str(path))
    response = web.app.test_client().post(
        f"/events/{event_id}/edit",
        data={"title": "A Better Social Title", "notes": "Bring a chair"},
    )

    assert response.status_code == 302
    with db.session(path) as conn:
        stored = conn.execute(
            "SELECT title,title_override,notes FROM event WHERE id=?", (event_id,)
        ).fetchone()
    assert tuple(stored) == ("Social event", "A Better Social Title", "Bring a chair")
    page = web.app.test_client().get("/?when=all").data.decode()
    assert "A Better Social Title" in page
    assert "Bring a chair" in page
    assert "<summary>edit</summary>" in page
