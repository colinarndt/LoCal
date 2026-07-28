# instagram-calendar

Many local events are announced on Instagram and nowhere else. This pulls those
announcements out - captions *and* flyer images - turns them into structured
events, deduplicates them, and gives you a filterable calendar plus an ICS feed
you can subscribe to on your phone.

It runs entirely on your own machine against a SQLite file. There is no hosted
service and no account.

## What it actually does

1. Polls a set of Instagram accounts you approve (via [Apify](https://apify.com)).
2. **Gate:** a cheap text-only pass over each caption decides "is this announcing
   a dated, attendable event?" This is the main noise filter.
3. **Extract:** survivors go to a vision call that reads the flyer image, because
   the date, time, and lineup are usually *only* in the image.
4. Deduplicates across accounts (venue + promoter + collab repost all announce the
   same show), geocodes venues, expands recurring events.
5. Serves a web UI — list or month grid, filters, confirm/hide/flag — and
   `/calendar.ics`.

## Cost

**`run-once` spends real money.** Two paid APIs: Apify per scraped post, Anthropic
per model call.

Measured against 8 real venue accounts: venues post ~1.1×/day. **50 accounts would be ~$10/month** on the default all-Haiku
configuration, ~$32/month if you do image extraction with Opus. Scraping is
~$0.07/account/month, so adding accounts is nearly free — the practical ceiling is
how many you can curate. Full breakdown in [SPEC.md §8](SPEC.md).

Start with a handful of accounts and `--limit` while you're calibrating.

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/colinarndt/instagram-calendar
cd instagram-calendar
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m social_calendar.cli init
```

`init` asks for:

- **your city** — geocoded once, and named in the extraction prompts so the model
  knows whose venues and which local date conventions it's reading
- **a radius in miles** — a venue that geocodes outside it is treated as a
  wrong-metro match and dropped (Nominatim will cheerfully resolve a Charlotte
  query to Port Charlotte, Florida)
- **your timezone** — IANA name, written into the ICS feed as `TZID`
- **your two API keys** — [Anthropic](https://platform.claude.com/settings/keys)
  and [Apify](https://console.apify.com/settings/integrations)
- **what you're into** — optional; asks the model for starter accounts and drops
  them in the review queue

Non-secret settings land in `config.json` and are editable at `/settings`. Keys
land in `.env` (mode 600) and are **only** settable from the CLI — see
[Security](#security).

## Running it

```bash
python -m social_calendar.web          # http://localhost:8730
```

Open `/discover` and approve some accounts — nothing is polled until you do. Add
handles by hand there too; pasting a profile URL works.

Then:

```bash
python -m social_calendar.cli run-once   # scrape + extract. COSTS MONEY.
python -m social_calendar.cli geocode    # fill in venue coordinates
python -m social_calendar.cli stats
```

`run-once` is what you'd put on a cron. There's no daemon.

### Views

- **List** — chronological, grouped by day. The default.
- **Calendar** — month grid at `?view=calendar`. On a phone the cells show event
  counts and the day list underneath does the reading.
- **`/calendar.ics`** — the same filters apply, so you can subscribe to just
  `?category=music&hood=NoDa` and still browse everything in the UI.

The server binds `0.0.0.0` so you can reach it from your phone over Tailscale or
your LAN.

## Security

There is **no authentication**, by design — it's a single-user app on your own
network. Two consequences worth being explicit about:

- **Don't expose it to the public internet.** Tailscale or LAN only.
- **API keys are not editable from the web UI**, and `/settings` reports only
  whether a key is present, never its value. A key field on a no-auth
  `0.0.0.0` service would be a key field for everyone on the network. Set keys
  with `cli init` or by editing `.env`.

## What's not in this repo

`data/` and `spike/posts/` — the SQLite database, cached flyers, and profile
pictures. That's scraped third-party content: fine to cache locally for personal
use, not something to redistribute. Both are gitignored, and `cli import-spike`
(which replays the author's Phase 0 corpus) won't work from a clone.

## Design notes

[SPEC.md](SPEC.md) is the real document — schema, the dedupe strategy, the
escalation ladder, measured extraction accuracy, and the reasoning behind the
things that look arbitrary. Read §3 before changing prompts.

## License

None yet. Ask.
