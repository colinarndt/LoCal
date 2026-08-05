"""What this install has actually spent, and on what.

Neither provider will tell you. Anthropic exposes no balance or per-key cost
endpoint (the org-level Admin API needs a different class of key, reports across
the whole organization, and lags) -- so the only way to answer "what has this app
cost me" is to add it up as it happens.

That turns out to be exact rather than estimated. Every Anthropic response
carries real token counts, and token counts times the published rate is the same
arithmetic Anthropic bills. Apify is better still: a finished run reports
`usage_total_usd`, the actual dollars charged. The one number that can drift is
the rate table below, and only when Anthropic changes prices.

Costs are recorded on their own connection, outside whatever transaction the
caller is in. That is deliberate: if extraction fails and the pipeline rolls
back, the money was still spent, and a ledger that forgets it is worse than no
ledger. See `Meter` for how call sites with no database handle report in.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

# USD per million tokens, (input, output), from the published Anthropic pricing.
# Nothing calls Anthropic since the OpenAI port, but the table stays: `stats`
# and the menu bar re-read historical ledger rows, and a model that priced
# correctly in March should not start reading as free in August.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

# Cache writes cost a premium over base input, reads a small fraction of it.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

# USD per million tokens, (input, cached_input, output), from the published
# OpenAI pricing. Cached input is a distinct published rate rather than a
# multiplier of input, which is why this table carries three numbers where the
# Anthropic one carries two.
#
# The rung 1..3 ladder plus `gpt-5.4-nano`, which is not on the ladder but is
# the cheaper gate `extract.GATE_MODEL` points at.
OPENAI_PRICES = {
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
}

_MILLION = 1_000_000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def price_tokens(model: str, usage) -> float:
    """Dollars for one Anthropic call. Unknown model prices at zero rather than
    guessing -- a wrong number here is worse than a visibly missing one."""
    rates = PRICES.get(model)
    if rates is None or usage is None:
        return 0.0
    per_in, per_out = rates
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    plain_in = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (
        plain_in * per_in
        + write * per_in * CACHE_WRITE_MULTIPLIER
        + read * per_in * CACHE_READ_MULTIPLIER
        + out * per_out
    ) / _MILLION


def usage_fields(usage) -> dict:
    """The four token counters, flattened. All four are stored even when zero,
    so a later question about cache effectiveness is answerable from history."""
    g = lambda name: (getattr(usage, name, 0) or 0) if usage is not None else 0
    return {
        "input_tokens": g("input_tokens"),
        "output_tokens": g("output_tokens"),
        "cache_write_tokens": g("cache_creation_input_tokens"),
        "cache_read_tokens": g("cache_read_input_tokens"),
    }


def _openai_counts(usage) -> tuple[int, int, int, int]:
    """(uncached_in, cached_in, cache_write, out) for one Responses call.

    OpenAI reports `input_tokens` as the *total*, with the cached portion
    broken out underneath it -- the opposite of Anthropic, where the top-level
    count already excludes what was cached. Subtracting here is what stops a
    cached token being billed twice.
    """
    if usage is None:
        return 0, 0, 0, 0
    total_in = getattr(usage, "input_tokens", 0) or 0
    details = getattr(usage, "input_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    write = (getattr(details, "cache_write_tokens", 0) or 0) if details else 0
    out = getattr(usage, "output_tokens", 0) or 0
    return max(total_in - cached, 0), cached, write, out


def price_openai_tokens(model: str, usage) -> float:
    """Dollars for one OpenAI call. Unknown model prices at zero rather than
    guessing -- a wrong number here is worse than a visibly missing one."""
    rates = OPENAI_PRICES.get(model)
    if rates is None or usage is None:
        return 0.0
    per_in, per_cached, per_out = rates
    plain_in, cached, write, out = _openai_counts(usage)
    # Cache writes bill at the ordinary input rate; only reads are discounted.
    return (plain_in * per_in + write * per_in
            + cached * per_cached + out * per_out) / _MILLION


def openai_usage_fields(usage) -> dict:
    """The same four counters the Anthropic path stores, so one ledger schema
    covers both providers and old rows stay comparable to new ones."""
    plain_in, cached, write, out = _openai_counts(usage)
    return {
        "input_tokens": plain_in,
        "output_tokens": out,
        "cache_write_tokens": write,
        "cache_read_tokens": cached,
    }


class Meter:
    """A holding pen for cost events raised where no database handle exists.

    `Extractor` and `ApifySource` are deliberately ignorant of storage, so they
    accumulate here and the pipeline layer -- which has a connection -- drains
    them. Draining clears, which is what stops a retried stage from
    double-charging.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def add(self, provider: str, detail: str, usd: float, *, units: float | None = None,
            estimated: bool = False, **tokens) -> None:
        self.events.append({
            "occurred_at": _now(), "provider": provider, "detail": detail,
            "usd": usd, "units": units, "estimated": int(estimated), **tokens,
        })

    def add_anthropic(self, model: str, usage) -> None:
        self.add("anthropic", model, price_tokens(model, usage), **usage_fields(usage))

    def add_openai(self, model: str, usage) -> None:
        self.add("openai", model, price_openai_tokens(model, usage),
                 **openai_usage_fields(usage))

    def add_apify(self, actor_id: str, run, *, units: float | None = None,
                  fallback_usd: float = 0.0) -> None:
        """Actual billed dollars where the run reports them, an estimate where it
        does not -- flagged either way, so the UI never presents a guess as fact."""
        actual = getattr(run, "usage_total_usd", None)
        if actual is None:
            self.add("apify", actor_id, fallback_usd, units=units, estimated=True)
        else:
            self.add("apify", actor_id, float(actual), units=units)

    def drain(self) -> list[dict]:
        events, self.events = self.events, []
        return events


def record(conn: sqlite3.Connection, events: list[dict]) -> float:
    """Write drained events. Returns the total written, for logging."""
    total = 0.0
    for e in events:
        conn.execute(
            "INSERT INTO spend (occurred_at, provider, detail, usd, units, estimated, "
            "input_tokens, output_tokens, cache_write_tokens, cache_read_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (e["occurred_at"], e["provider"], e["detail"], e["usd"], e.get("units"),
             e.get("estimated", 0), e.get("input_tokens", 0), e.get("output_tokens", 0),
             e.get("cache_write_tokens", 0), e.get("cache_read_tokens", 0)))
        total += e["usd"]
    return total


def drain_into(conn: sqlite3.Connection, *meters) -> float:
    """Drain any number of meters into the ledger. The usual call shape."""
    total = 0.0
    for m in meters:
        if m is not None:
            total += record(conn, m.drain())
    return total


def totals(conn: sqlite3.Connection) -> dict:
    """Headline numbers for the menu bar.

    `since` is the first recorded event, not the install date. Spend predating
    this ledger is unrecoverable -- the `extraction` table logged a row per call
    but never the token counts -- so the UI says "since tracking started" rather
    than presenting a smaller number as an all-time total.
    """
    day_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) AS all_time, MIN(occurred_at) AS since, "
        "COUNT(*) AS calls, "
        # Carried separately so the UI can mark a total that is partly inferred.
        # Summing the two together would defeat the point of storing the flag.
        "COALESCE(SUM(CASE WHEN estimated THEN usd ELSE 0 END), 0) AS estimated_usd "
        "FROM spend").fetchone()
    last_24h = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) FROM spend WHERE occurred_at >= ?",
        (day_ago,)).fetchone()[0]
    by_provider = {
        r["provider"]: r["usd"] for r in conn.execute(
            "SELECT provider, COALESCE(SUM(usd), 0) AS usd FROM spend GROUP BY provider")
    }
    return {
        "last_24h": last_24h,
        "all_time": row["all_time"],
        "since": row["since"],
        "calls": row["calls"],
        "estimated_usd": row["estimated_usd"],
        "by_provider": by_provider,
    }
