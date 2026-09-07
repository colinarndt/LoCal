"""Does spend actually reach the ledger through the real code paths?

test_spend.py checks the arithmetic in isolation. These drive `Extractor` and
`pipeline` with fake clients, because the failure that matters is not a wrong
multiplication -- it is a call site that quietly never records anything.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from local_calendar import db, pipeline, spend
from local_calendar.extract import Extractor, WEBSITE_MAX_OUTPUT_TOKENS
from local_calendar.sources import RawPost


class FakeDeepSeek:
    """Returns a valid gate/extract response and reports token usage.

    Mirrors the Responses API shape the real client returns: `input_tokens` is
    the total with the cached slice broken out beneath it, and a refusal is a
    content part rather than a status.
    """

    def __init__(self, status="completed", text='{"is_event_candidate": false}',
                 refusal=None, cached=0):
        self.status, self.text, self.refusal, self.cached = status, text, refusal, cached
        self.calls = 0
        self.requests = []
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        self.requests.append(kw)
        parts = ([types.SimpleNamespace(type="refusal", refusal=self.refusal)]
                 if self.refusal else [types.SimpleNamespace(type="output_text")])
        return types.SimpleNamespace(
            status=self.status,
            incomplete_details=None,
            output=[types.SimpleNamespace(content=parts)],
            output_text="" if self.refusal else self.text,
            usage=types.SimpleNamespace(
                input_tokens=1000, output_tokens=100,
                input_tokens_details=types.SimpleNamespace(
                    cached_tokens=self.cached, cache_write_tokens=0)),
        )


def _post():
    return {"post_id": "p1", "caption": "Live music friday", "local_images": [],
            "posted_at": "2026-07-01T00:00:00+00:00"}


# --- the extractor ----------------------------------------------------------

def test_each_stage_bills_the_model_it_actually_used():
    """Every model-backed stage uses and meters the configured DeepSeek model."""
    client = FakeDeepSeek()
    ex = Extractor(client=client, rung=1)
    ex.gate(_post())
    ex.extract(_post())
    gate_event, extract_event = ex.meter.drain()
    assert gate_event["detail"] == "deepseek-v4-flash-vision-exp"
    assert extract_event["detail"] == "deepseek-v4-flash-vision-exp"
    assert gate_event["provider"] == "deepseek"
    assert extract_event["provider"] == "deepseek"
    assert {request["model"] for request in client.requests} == {
        "deepseek-v4-flash-vision-exp"
    }
    assert ex.model_for("website") == "deepseek-v4-flash-vision-exp"


def test_a_cached_token_is_billed_once_at_the_cached_rate():
    """Responses usage includes cached input in the total; bill it only once."""
    ex = Extractor(client=FakeDeepSeek(cached=400), rung=1)
    ex.extract(_post())
    (event,) = ex.meter.drain()
    assert event["usd"] > 0
    assert event["input_tokens"] == 600 and event["cache_read_tokens"] == 400


def test_website_extraction_has_room_for_a_large_venue_listing():
    client = FakeDeepSeek(text='{"events": []}')
    Extractor(client=client).website("many dated events", "https://venue.example/events")

    assert client.requests[-1]["max_output_tokens"] == WEBSITE_MAX_OUTPUT_TOKENS == 32_768


def test_a_refusal_is_still_billed():
    """HTTP 200, tokens consumed, no useful answer -- the easiest spend to lose."""
    ex = Extractor(client=FakeDeepSeek(refusal="blocked"), rung=1)
    out = ex.gate(_post())
    assert out["_error"] == "refusal"
    assert len(ex.meter.drain()) == 1


def test_unparseable_output_is_still_billed():
    ex = Extractor(client=FakeDeepSeek(text="not json at all"), rung=1)
    out = ex.gate(_post())
    assert "_error" in out
    assert len(ex.meter.drain()) == 1


def test_a_failed_request_bills_nothing():
    """No response object means the model never ran."""
    class Boom:
        def __init__(self):
            self.responses = types.SimpleNamespace(create=self._raise)

        def _raise(self, **kw):
            raise RuntimeError("connection reset")

    ex = Extractor(client=Boom(), rung=1)
    assert "_error" in ex.gate(_post())
    assert ex.meter.drain() == []


# --- through the pipeline ---------------------------------------------------

class FakeApifySource:
    """Mimics ApifySource's metering contract without touching the network."""

    def __init__(self, usd=0.2654):
        self.meter = spend.Meter()
        self.usd = usd

    def source_name(self):
        return "apify"

    def fetch_recent(self, handles, limit=20, newer_than=None):
        posts = [RawPost(post_id="p1", polled_handle=handles[0],
                         attributed_handle=handles[0],
                         posted_at="2026-07-01T00:00:00+00:00", caption="show tonight",
                         permalink="", media_kind="post", image_urls=[], raw={})]
        self.meter.add_apify("apify/instagram-scraper",
                             types.SimpleNamespace(usage_total_usd=self.usd),
                             units=len(posts))
        return posts


def test_ingest_records_the_scrape_cost():
    conn = db.connect(":memory:")
    source = FakeApifySource()
    pipeline.ingest(conn, source, ["venue"], limit=20, fetch_media=False)

    t = spend.totals(conn)
    assert round(t["by_provider"]["apify"], 4) == 0.2654
    assert source.meter.events == []      # drained, so a second pass cannot re-bill


def test_successful_batch_gives_every_account_one_shared_poll_mark(monkeypatch):
    conn = db.connect(":memory:")
    conn.executemany(
        "INSERT INTO account (handle, is_polled, added_at) VALUES (?,1,'before')",
        [("venue",), ("other",)],
    )
    times = iter(["touch-time", "shared-poll-time", "must-not-be-used"])
    monkeypatch.setattr(pipeline, "_now", lambda: next(times))

    pipeline.ingest(conn, FakeApifySource(), ["venue", "other"],
                    limit=20, fetch_media=False)

    marks = [r[0] for r in conn.execute(
        "SELECT last_polled_at FROM account ORDER BY handle").fetchall()]
    assert marks == ["shared-poll-time", "shared-poll-time"]


def test_ingest_survives_a_source_that_has_no_meter():
    """LocalSpikeSource reads off disk and spends nothing, so it has no meter."""
    class Free:
        def source_name(self):
            return "spike-local"

        def fetch_recent(self, handles, limit=20, newer_than=None):
            return [RawPost(post_id="p1", polled_handle=handles[0],
                            attributed_handle=handles[0],
                            posted_at="2026-07-01T00:00:00+00:00", caption="show",
                            permalink="", media_kind="post", image_urls=[], raw={})]

    conn = db.connect(":memory:")
    assert pipeline.ingest(conn, Free(), ["venue"], limit=20, fetch_media=False) == 1
    assert spend.totals(conn)["all_time"] == 0


def test_extraction_persists_an_explicit_end_time():
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO source_post (post_id,polled_handle,posted_at,fetched_at) "
        "VALUES ('p1','venue','2026-08-01','now')")

    pipeline._insert_event(conn, {"post_id": "p1", "posted_at": "2026-08-01"}, None, {
        "title": "Late Show", "starts_at": "2026-08-14T20:00:00",
        "ends_at": "2026-08-14T22:30:00", "start_time_known": True,
        "venue_name": "Example Room", "category": "music", "price_text": None,
        "confidence": 1, "date_reasoning": "The flyer states 8pm to 10:30pm.",
    }, {"events": 0, "flagged": 0})

    assert conn.execute("SELECT ends_at FROM event").fetchone()[0] == "2026-08-14T22:30:00"


def test_process_records_every_model_call():
    conn = db.connect(":memory:")
    source = FakeApifySource()
    pipeline.ingest(conn, source, ["venue"], limit=20, fetch_media=False)

    client = FakeDeepSeek()
    ex = Extractor(client=client, rung=1)
    pipeline.process(conn, ex)

    t = spend.totals(conn)
    assert client.calls == 1                       # gated out, so no extract call
    assert t["by_provider"]["deepseek"] > 0
    assert t["calls"] == 2                         # one apify run + one gate call
    assert ex.meter.events == []
