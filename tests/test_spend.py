"""Cost arithmetic. Wrong numbers here are worse than no numbers at all."""

import datetime as dt
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar import db, spend


def _usage(**kw):
    """Stand-in for anthropic's usage object -- attribute access, not a dict."""
    return types.SimpleNamespace(**{
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, **kw})


def _conn() -> sqlite3.Connection:
    return db.connect(":memory:")


# --- pricing ----------------------------------------------------------------

def test_haiku_priced_at_published_rate():
    # 1M in at $1.00 + 1M out at $5.00.
    usd = spend.price_tokens("claude-haiku-4-5",
                             _usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert usd == 6.00


def test_realistic_gate_call_is_fractions_of_a_cent():
    usd = spend.price_tokens("claude-haiku-4-5", _usage(input_tokens=800, output_tokens=40))
    assert round(usd, 8) == round((800 * 1.00 + 40 * 5.00) / 1_000_000, 8)
    assert usd < 0.01


def test_cache_reads_are_a_tenth_and_writes_a_premium():
    read = spend.price_tokens("claude-haiku-4-5", _usage(cache_read_input_tokens=1_000_000))
    write = spend.price_tokens("claude-haiku-4-5", _usage(cache_creation_input_tokens=1_000_000))
    assert read == 0.10
    assert write == 1.25


def test_escalation_rungs_are_priced_not_free():
    tokens = _usage(input_tokens=1_000_000)
    assert spend.price_tokens("claude-sonnet-5", tokens) == 3.00
    assert spend.price_tokens("claude-opus-5", tokens) == 5.00


def test_unknown_model_prices_at_zero_rather_than_guessing():
    assert spend.price_tokens("claude-something-unreleased", _usage(input_tokens=999)) == 0.0


def test_missing_usage_object_does_not_raise():
    assert spend.price_tokens("claude-haiku-4-5", None) == 0.0
    assert spend.usage_fields(None)["input_tokens"] == 0


# --- the meter --------------------------------------------------------------

def test_drain_clears_so_a_second_drain_cannot_double_charge():
    m = spend.Meter()
    m.add_anthropic("claude-haiku-4-5", _usage(input_tokens=1_000_000))
    assert len(m.drain()) == 1
    assert m.drain() == []


def test_apify_prefers_actual_billed_dollars_over_the_estimate():
    m = spend.Meter()
    m.add_apify("apify/x", types.SimpleNamespace(usage_total_usd=0.2654),
                units=20, fallback_usd=99.0)
    (event,) = m.drain()
    assert event["usd"] == 0.2654
    assert event["estimated"] == 0


def test_apify_falls_back_to_the_estimate_and_flags_it():
    m = spend.Meter()
    m.add_apify("apify/x", types.SimpleNamespace(usage_total_usd=None),
                units=20, fallback_usd=0.04)
    (event,) = m.drain()
    assert event["usd"] == 0.04
    assert event["estimated"] == 1


# --- the ledger -------------------------------------------------------------

def test_totals_split_last_24h_from_all_time():
    conn = _conn()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO spend (occurred_at, provider, detail, usd) VALUES (?,?,?,?)",
        [(old, "anthropic", "claude-haiku-4-5", 1.00),
         (recent, "anthropic", "claude-haiku-4-5", 0.25),
         (recent, "apify", "apify/instagram-scraper", 0.10)])
    conn.commit()

    t = spend.totals(conn)
    assert round(t["all_time"], 2) == 1.35
    assert round(t["last_24h"], 2) == 0.35
    assert t["since"] == old
    assert round(t["by_provider"]["apify"], 2) == 0.10


def test_totals_on_a_fresh_install_are_zero_not_null():
    t = spend.totals(_conn())
    assert t["all_time"] == 0
    assert t["last_24h"] == 0
    assert t["since"] is None       # drives "since tracking started" in the UI
    assert t["calls"] == 0


def test_drain_into_writes_every_token_counter():
    conn = _conn()
    m = spend.Meter()
    m.add_anthropic("claude-haiku-4-5",
                    _usage(input_tokens=10, output_tokens=20,
                           cache_creation_input_tokens=30, cache_read_input_tokens=40))
    spend.drain_into(conn, m)

    row = conn.execute("SELECT * FROM spend").fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (10, 20)
    assert (row["cache_write_tokens"], row["cache_read_tokens"]) == (30, 40)
    assert row["provider"] == "anthropic"
    assert row["detail"] == "claude-haiku-4-5"


def test_drain_into_tolerates_a_source_with_no_meter():
    # LocalSpikeSource has no meter and spends nothing; ingest must not crash.
    assert spend.drain_into(_conn(), None) == 0.0
