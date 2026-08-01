import json

from social_calendar import db, pipeline, websites


JSONLD = """<!doctype html><html><head>
<script type="application/ld+json">%s</script>
</head></html>"""


class Response:
    def __init__(self, body: str, url="https://venue.example/events",
                 content_type="text/html; charset=utf-8"):
        self.body = body.encode()
        self.url = url
        self.headers = {"Content-Type": content_type, "ETag": '"one"'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit=-1):
        return self.body

    def geturl(self):
        return self.url


def opener_for(body, content_type="text/html; charset=utf-8"):
    return lambda request, timeout=30: Response(body, request.full_url, content_type)


def event_payload(start="2026-08-14T20:00:00-04:00"):
    return {
        "@context": "https://schema.org",
        "@type": "MusicEvent",
        "@id": "show-123",
        "name": "Tennis",
        "startDate": start,
        "endDate": "2026-08-14T22:00:00-04:00",
        "location": {"@type": "Place", "name": "Neighborhood Theatre"},
        "url": "/events/tennis",
        "offers": {"price": 25, "priceCurrency": "USD"},
    }


def test_jsonld_music_event_is_parsed_without_a_model_call():
    page = JSONLD % json.dumps(event_payload())
    events = websites.parse_jsonld(page, "https://venue.example/calendar")

    assert len(events) == 1
    event = events[0]
    assert event.title == "Tennis"
    assert event.starts_at == "2026-08-14T20:00:00"
    assert event.venue_name == "Neighborhood Theatre"
    assert event.permalink == "https://venue.example/events/tennis"
    assert event.price_text == "$25"
    assert event.category == "music"


def test_ics_feed_is_parsed():
    feed = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:abc@example.com
DTSTART:20260814T233000Z
DTEND:20260815T013000Z
SUMMARY:Late Show
LOCATION:The Milestone
URL:https://venue.example/late-show
END:VEVENT
END:VCALENDAR
"""
    events = websites.parse_ics(feed, "https://venue.example/calendar.ics")

    assert len(events) == 1
    assert events[0].external_id == "abc@example.com"
    # August is daylight time in the configured America/New_York zone.
    assert events[0].starts_at == "2026-08-14T19:30:00"
    assert events[0].start_time_known is True


def test_poll_inserts_and_then_updates_one_structured_event():
    conn = db.connect(":memory:")
    source_id = websites.add_source(conn, "https://venue.example/events", "Venue")
    first = JSONLD % json.dumps(event_payload())

    result = websites.poll_source(conn, source_id, opener_for(first))
    assert result["new"] == 1
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1
    assert conn.execute("SELECT stage FROM extraction").fetchone()[0] == "structured"
    assert conn.execute("SELECT source_kind FROM event_source").fetchone()[0] == "website"

    changed = JSONLD % json.dumps(event_payload("2026-08-14T21:00:00-04:00"))
    result = websites.poll_source(conn, source_id, opener_for(changed))
    assert result["new"] == 0 and result["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1
    assert conn.execute("SELECT starts_at FROM event").fetchone()[0] == "2026-08-14T21:00:00"


class MatchingExtractor:
    model = "fake"
    meter = None

    def model_for(self, stage):
        return "fake-gate"

    def gate(self, post):
        return {
            "is_event_candidate": True,
            "reason": "explicit event",
            "title_hint": "Tennis",
            "starts_at_hint": "2026-08-14",
            "venue_hint": "Neighborhood Theatre",
            "event_url_hint": None,
        }

    def extract(self, post):
        raise AssertionError("a confident caption match must not read the flyer")


def test_clear_instagram_caption_attaches_source_and_skips_vision():
    conn = db.connect(":memory:")
    source_id = websites.add_source(conn, "https://venue.example/events", "Venue",
                                    "neighborhoodtheatre")
    page = JSONLD % json.dumps(event_payload())
    websites.poll_source(conn, source_id, opener_for(page))
    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,attributed_handle,posted_at,caption,"
        "permalink,media_kind,local_images,raw_provider_json,source_name,fetched_at) "
        "VALUES ('ig-1','neighborhoodtheatre','neighborhoodtheatre',"
        "'2026-08-01T12:00:00+00:00','Tennis August 14 at Neighborhood Theatre',"
        "'https://instagram.com/p/ig-1','post','[]','{}','apify','now')")

    stats = pipeline.process(conn, MatchingExtractor())

    assert stats["vision_skipped"] == 1
    assert stats["events"] == 0
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 2
    prematch = conn.execute(
        "SELECT raw_output FROM extraction WHERE post_id='ig-1' AND stage='prematch'").fetchone()
    assert prematch is not None


def test_dedupe_moves_all_provenance_to_the_website_canonical_event():
    conn = db.connect(":memory:")
    source_id = websites.add_source(conn, "https://venue.example/events", "Venue")
    websites.poll_source(conn, source_id, opener_for(JSONLD % json.dumps(event_payload())))
    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,attributed_handle,posted_at,caption,"
        "permalink,media_kind,local_images,raw_provider_json,source_name,fetched_at) "
        "VALUES ('ig-2','venue','venue','2026-07-20','Tennis','https://instagram.com/p/ig-2',"
        "'post','[]','{}','apify','now')")
    ig_event = conn.execute(
        "INSERT INTO event (post_id,title,starts_at,start_time_known,venue_name,venue_key,created_at) "
        "VALUES ('ig-2','Tennis','2026-08-14T20:00:00',1,'Neighborhood Theatre',"
        "'neighborhood theatre','now')").lastrowid
    conn.execute(
        "INSERT INTO event_source (event_id,source_kind,source_item_id,permalink,match_method,created_at) "
        "VALUES (?,'instagram','ig-2','https://instagram.com/p/ig-2','extracted','now')",
        (ig_event,))

    pipeline.rebuild_dedupe(conn)

    canonical = conn.execute(
        "SELECT e.id,p.source_name FROM event e JOIN source_post p ON p.post_id=e.post_id "
        "WHERE e.is_canonical=1").fetchone()
    assert canonical["source_name"] == "website"
    assert conn.execute(
        "SELECT COUNT(*) FROM event_source WHERE event_id=?", (canonical["id"],)).fetchone()[0] == 2


def test_source_page_can_add_and_disable_a_website(tmp_path):
    from social_calendar import web

    path = tmp_path / "calendar.db"
    web.app.config.update(TESTING=True, DB=str(path))
    client = web.app.test_client()

    response = client.post("/discover/website/add", data={
        "url": "https://venue.example/events",
        "name": "Example Venue",
        "linked_handle": "@example",
    })
    assert response.status_code == 302
    with db.session(path) as conn:
        source = conn.execute("SELECT * FROM web_source").fetchone()
        assert source["name"] == "Example Venue"
        assert source["linked_handle"] == "example"

    page = client.get("/discover")
    assert page.status_code == 200
    assert b"Example Venue" in page.data
    client.post(f"/discover/website/{source['id']}/disable")
    with db.session(path) as conn:
        assert conn.execute("SELECT enabled FROM web_source").fetchone()[0] == 0
