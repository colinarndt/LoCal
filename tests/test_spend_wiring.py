"""Does spend actually reach the ledger through the real code paths?

test_spend.py checks the arithmetic in isolation. These drive `Extractor` and
`pipeline` with fake clients, because the failure that matters is not a wrong
multiplication -- it is a call site that quietly never records anything.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar import db, pipeline, spend
from social_calendar.extract import Extractor
from social_calendar.sources import RawPost


class FakeAnthropic:
    """Returns a valid gate/extract response and reports token usage."""

    def __init__(self, stop_reason="end_turn", text='{"is_event_candidate": false}'):
        self.stop_reason, self.text = stop_reason, text
        self.calls = 0
        self.messages = types.SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        return types.SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details="blocked" if self.stop_reason == "refusal" else None,
            content=[types.SimpleNamespace(type="text", text=self.text)],
            usage=types.SimpleNamespace(
                input_tokens=1000, output_tokens=100,
                cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )


def _post():
    return {"post_id": "p1", "caption": "Live music friday", "local_images": [],
            "posted_at": "2026-07-01T00:00:00+00:00"}


# --- the extractor ----------------------------------------------------------

def test_a_successful_call_meters_its_usage():
    ex = Extractor(client=FakeAnthropic(), rung=1)
    ex.gate(_post())
    (event,) = ex.meter.drain()
    # 1000 in @ $1/MTok + 100 out @ $5/MTok
    assert round(event["usd"], 8) == round((1000 * 1.00 + 100 * 5.00) / 1_000_000, 8)
    assert event["detail"] == "claude-haiku-4-5"


def test_a_refusal_is_still_billed():
    """HTTP 200, tokens consumed, no useful answer -- the easiest spend to lose."""
    ex = Extractor(client=FakeAnthropic(stop_reason="refusal"), rung=1)
    out = ex.gate(_post())
    assert out["_error"] == "refusal"
    assert len(ex.meter.drain()) == 1


def test_unparseable_output_is_still_billed():
    ex = Extractor(client=FakeAnthropic(text="not json at all"), rung=1)
    out = ex.gate(_post())
    assert "_error" in out
    assert len(ex.meter.drain()) == 1


def test_a_failed_request_bills_nothing():
    """No response object means the model never ran."""
    class Boom:
        def __init__(self):
            self.messages = types.SimpleNamespace(create=self._raise)

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


def test_process_records_every_model_call():
    conn = db.connect(":memory:")
    source = FakeApifySource()
    pipeline.ingest(conn, source, ["venue"], limit=20, fetch_media=False)

    client = FakeAnthropic()
    ex = Extractor(client=client, rung=1)
    pipeline.process(conn, ex)

    t = spend.totals(conn)
    assert client.calls == 1                       # gated out, so no extract call
    assert t["by_provider"]["anthropic"] > 0
    assert t["calls"] == 2                         # one apify run + one gate call
    assert ex.meter.events == []
