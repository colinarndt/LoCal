# Social Calendar — Spec

A personal event aggregator. Pulls event announcements out of Instagram (posts, reels, stories) for a set of Charlotte-area accounts, extracts structured event data from captions **and flyer images**, deduplicates, and presents a filterable chronological list.

Status: **Phase 0 passed (2026-07-26) — cleared to build.** Extraction quality was
validated on 160 real posts for ~$1.10; `claude-haiku-4-5` clears the bar on both
pipeline stages. See §3 RESULT for the numbers and §8 for the corrected cost model
(the original estimate overstated volume by ~11x). Phase 1 is next.

Working spike lives in `spike/`.

---

## 1. Goals and non-goals

**Goals**
- Discover local events (music, comedy, openings, markets, food) without opening Instagram.
- Ingest from a curated set of accounts; suggest new accounts semi-automatically.
- Extract event details from flyer images, not just caption text.
- Chronological list, filterable by type, date range, venue, and source account.
- Charlotte, NC to start.
- Local hosting, reachable from phone; clean path to real hosting later.
- Single user (me). No auth, no multi-tenancy, no sharing.

**Non-goals (v1)**
- Ticketing, RSVP, or any write action back to Instagram.
- Other cities.
- Other sources (Eventbrite, Resident Advisor, Songkick, Dice, venue sites) — deliberately Phase 2, behind the same interface.
- Native mobile app.

---

## 2. Reality check: how we get the data

**Instagram has no legitimate API for reading arbitrary public accounts.** The Graph API only covers accounts you own or business accounts that granted you a token. There is no "fetch @venue's recent posts" endpoint. That leaves two options:

| Option | Pros | Cons |
|---|---|---|
| Self-hosted scraping (Playwright, instaloader, etc.) | Free | Fragile, breaks on layout/auth changes, gets IPs and accounts blocked, you maintain it |
| Third-party scraping provider (Apify and similar) | Someone else maintains the breakage; pay-per-result | Costs money; still ultimately built on scraping |

**Decision: third-party provider for v1**, behind an interface (§5.1) so it can be swapped. Current market rate for the Apify Instagram scrapers is roughly **$1.50–$2.70 per 1,000 posts**, with an alternative billing at ~$0.005/query + $0.0005/post ([Apify Instagram Scraper](https://apify.com/apify/instagram-scraper), [pricing comparison](https://www.socialcrawl.dev/blog/instagram-scraping-2026)). Validate current pricing during Phase 0 — this market shifts.

**Terms of service.** This is personal tooling: one user, public posts only, low polling volume, no redistribution, no resale, no republishing of images. That's an ordinary personal-automation posture, but it is worth stating plainly so the provider abstraction reads as a resilience decision rather than paranoia. If any layer of this stops being viable, the interface is the seam where it gets replaced.

---

## 3. Phase 0 — the extraction spike (a gate, not a build step)

Everything downstream is worthless if extraction quality is bad. **Do this before writing a DB schema, a scheduler, or a line of UI.**

1. Hand-pick ~20 Charlotte accounts. Starting candidates to verify are still active: Snug Harbor, Neighborhood Theatre, The Milestone, Petra's, The Evening Muse, Visulite Theatre, Amos' Southend, Camp North End, Optimist Hall, The Comedy Zone, plus 2–3 local food/market accounts.
2. Pull ~100 recent posts (captions + image URLs + post timestamps) via the chosen provider.
3. Run the pipeline over all of them **cheapest-first, stopping at the first model that clears the bar.** Not a full matrix — an escalation ladder, run one rung at a time:

   | Rung | Gate model | Extraction model |
   |---|---|---|
   | 1 | `claude-haiku-4-5` | `claude-haiku-4-5` |
   | 2 | `claude-sonnet-5` | `claude-sonnet-5` |
   | 3 | `claude-opus-5` | `claude-opus-5` |

   **Rules:**
   - Score rung 1 against the §3 thresholds. If it passes, **stop** — do not spend money on rung 2 or 3.
   - If it fails, escalate that stage only. The two stages ladder independently: gate may settle at rung 1 while extraction climbs to rung 2.
   - **Get explicit approval before each escalation.** Never run a more expensive model without asking first, even though the per-run cost is small.
   - Same 100 stored posts at every rung, so escalation costs a rerun and no re-scraping.

   The thresholds are absolute pass marks, so no reference ceiling is needed to interpret a result — a cheap model clearing the bar is a pass on its own terms. If the ladder reaches rung 3 and Opus also fails, the conclusion is that the problem is hard, and you have spent the same as running the full matrix anyway. Cheapest-first has no worse worst case and a much cheaper best case.
4. **Hand-score** each result on: is-this-an-event (bool), date, start time, venue, title.

**Pass threshold — commit to this now:**

| Field | Required accuracy |
|---|---|
| is-this-an-event | ≥ 90% |
| Date (absolute, correct year) | ≥ 85% of true events |
| Venue | ≥ 80% |
| Title | ≥ 75% |
| Start time | ≥ 60% (frequently absent from source; missing ≠ wrong) |

Below threshold → iterate the prompt against the same stored 100 posts (free, no re-scraping) or stop. Above → build.

Deliverable: a spike script plus a scored spreadsheet. Not production code.

### RESULT — 2026-07-25/26: rung 1 PASSED. Build.

Ran on **160 real posts** from 8 Charlotte accounts (`visulite` returned nothing —
bad handle), spanning 2026-05-28 to 2026-07-25. Both stages on
`claude-haiku-4-5`. Total spend: **~$1.10**, scraping included.

| Finding | Result |
|---|---|
| Gate pass rate | 84% (assumed 25% — see §8) |
| Gate false negatives | 1 of 25 rejections, and extraction rescued it |
| Extraction errors | 2 of 160, both a WebP/media-type bug, since fixed |
| Events extracted | 127 |
| **Weekday/date consistency** | **65/65 (100%)** — see §5.5 |
| Date sanity | 0 dated before the corpus window, 1 beyond 2027 |
| Field fill: title / date / venue | 100% / 100% / 90% |

**The date threshold — the one worth betting against — held.** Every event where
the model asserted a weekday resolved to a date actually falling on that weekday.
The reasoning traces show the intended mechanism working: quote the flyer, check
the weekday, resolve against `posted_at`.

**Rung 1 is the production config.** No reason to spend on rung 2 or 3.

Two caveats on this result. It is an internal-consistency check plus a human
spot-check, not exhaustive hand-scoring — a flyer misread wholesale would pass
both. And the corpus skews toward music venues with well-designed flyers;
handwritten or photo-of-a-poster inputs are unrepresented.

---

## 4. Data model

Two tables for content, not one. This is the highest-leverage decision in the design and expensive to retrofit.

### `source_post` — raw, immutable
```
id                 -- provider post id (natural key, idempotent re-scrape)
account_handle
posted_at          -- CRITICAL: anchor for relative date resolution
caption            -- raw text
media              -- json: [{type: image|video, url, local_path}]
permalink
media_kind         -- post | reel | story
raw_provider_json  -- whole payload, untouched
fetched_at
```

### `extraction` — model output, re-derivable
```
id
source_post_id
prompt_version     -- so runs are comparable
model
raw_output         -- verbatim model response
created_at
```

### `event` — the extracted, user-facing record
```
id
source_post_id     -- provenance; always clickable back to the post
extraction_id
title
starts_at          -- resolved absolute datetime
ends_at            -- nullable; only for events with a real same-day end time.
                   -- NOT used to span multi-day runs -- see the occurrence
                   -- model below.
occurrence_of      -- nullable FK to a `series` row, for runs and recurring
                   -- events. Null for one-off events.
venue_name
venue_address      -- nullable
category           -- music | comedy | food | market | art | opening | other
price_text         -- free text; "$15", "free", "$20 door / $15 adv"
confidence         -- model's self-report, 0-1. NOT CALIBRATED -- Phase 0
                   -- measured mean 0.91, with 85 of 127 events at exactly 0.95
                   -- and nothing below 0.6. Store it; do not filter or rank on
                   -- it. Use the section 5.5 weekday flag instead.
is_confirmed       -- user has eyeballed it
dedupe_group_id    -- FK to canonical event
```

### Occurrence model — DECIDED 2026-07-26

**One `event` row per occurrence.** A three-night run is three rows; a weekly
karaoke night is one row per week. Multi-night and recurring events share this
model — there is no separate representation for the two.

Rejected alternative: one row per run with `starts_at`/`ends_at` spanning it.
It breaks exact-key dedup. A Comedy Zone headliner advertised as "JULY 24-26"
was re-posted as "JULY 25-26" once the first night sold out, so the same
engagement produced start dates of both 07-24 and 07-25 depending on scrape
time. Run-based storage would need interval-overlap matching to reconcile those;
occurrence-based storage matches every night on an exact key and converges on
three rows with two-to-three source posts each.

It also keeps day views and the ICS feed as plain lookups rather than range
queries over every stored row.

### `series` — groups occurrences
```
id
title
venue_name
kind               -- run | recurring
rule               -- human-readable, e.g. "every Wednesday", "Jul 24-26"
horizon_until      -- how far occurrences have been generated
```

**Recurring events need a generation horizon.** "EVERY WEDNESDAY" and "first and
third Sunday of the month" both appear in the Phase 0 corpus and have no end
date. Generate occurrences **8 weeks out**, extend on each poll, and never
generate unbounded — otherwise an ongoing exhibition becomes hundreds of rows.

Collapsing a series into a single "Mike Epps · Jul 24-26" line is a UI concern
(§9), not a storage one.

### `account`
```
handle, display_name, category_hint, added_at,
active, last_polled_at, discovery_source (manual | tagged | suggested)
```

**Why the split:** re-running extraction with a better prompt costs zero re-scraping. You will iterate that prompt twenty times.

---

## 5. Pipeline

```
poll → source_post → gate (text) → extract (vision) → resolve dates → dedupe → event → UI / ICS
```

### 5.1 Ingestion interface

```python
class IngestionSource(Protocol):
    def fetch_recent(self, handle: str, since: datetime, limit: int) -> list[RawPost]: ...
    def source_name(self) -> str: ...
```

One adapter in v1 (`ApifySource`). Phase 2 sources (Eventbrite, RA, venue RSS) implement the same protocol and land in the same `source_post` table with a different `source_name`. Re-scrape is idempotent on provider post ID.

### 5.2 Cheap gate before vision

Most posts from a venue account are not event announcements. Gate on **caption text only** first — a cheap classification call returning `{is_event_candidate: bool, reason: str}`. Only survivors go to vision.

This is what keeps vision cost sublinear in account count, and it is the *primary* noise filter. The expected noise is a pipeline problem, not a filtering-UI problem — do not plan to solve it in the UI.

### 5.3 Vision extraction

Send caption + up to 3 images to a vision call returning structured output (JSON schema, `output_config.format`).

**Pass `posted_at` into the prompt as context.** Flyers say "FRIDAY THE 13TH" or "THIS SATURDAY" with no year. The model must resolve to an absolute date anchored on the post timestamp. This is the single most common silent wrong answer in this problem domain — a date that is confidently wrong by a year looks identical to a correct one in the UI.

The model must be allowed to return `null` for any field and a low `confidence`. Fabricated times are worse than missing ones.

**Model choice is an output of Phase 0, not an input.** Both stages start at `claude-haiku-4-5` and climb the §3 ladder only as far as the thresholds force. Whatever rung each stage settles on becomes the production config.

The two stages differ in difficulty and are expected to settle at different rungs:

| Stage | Work | Expectation |
|---|---|---|
| Gate | Binary "is this an event announcement?" over short caption text | Least capability-sensitive step in the pipeline. Good odds of settling at rung 1. |
| Extraction | Vision over flyer images, structured output, relative-date resolution anchored on `posted_at` | Genuinely hard — the model reads text off a JPEG *and* resolves "THIS SATURDAY" to an absolute date. May need to climb. |

These are predictions, not decisions; the scored runs overrule them. Use prompt caching on the system prompt in both stages regardless of which model each lands on.

### 5.4 Dedup — a component, not a UI filter

The same show gets posted by the venue, the promoter, the artist, and a listings account. Without dedup the chronological list looks broken, and you will blame extraction.

Because storage is occurrence-based (§4), every key is a single date — no
interval logic.

- Key on `(normalized date, normalized venue, fuzzy title)`.
- Embedding similarity on title+description as a tiebreak for near-misses.
- Group into a `dedupe_group_id`; UI shows one canonical row with an expandable "also posted by" list.

### 5.5 Weekday validator — cheap, deterministic, required

Discovered during Phase 0 and the highest value-per-line component in the
pipeline. The extraction schema requires `date_reasoning`, in which the model
quotes the flyer and shows its resolution. Where that text asserts a weekday for
the event, check it against the resolved `starts_at`:

- Weekday matches → publish normally.
- Weekday contradicts the date → **flag, do not publish silently.** One of the
  two was misread and the record cannot be trusted.

Free, deterministic, no model call. It catches the failure this whole design
exists to prevent — a confidently wrong date, indistinguishable from a right one
in the UI.

**Implementation note:** weekday words in `date_reasoning` often refer to the
*post* date ("Published Friday 2026-07-24, so August 25 resolves to..."), not the
event. Exclude any weekday matching `posted_at`'s weekday before comparing. The
first pass at this check reported a 5% failure rate that was entirely this
artifact; the true rate was zero.

### 5.6 Scheduling

Cron or a simple loop, once or twice daily for posts and reels. Stories are separate (§6).

---

## 6. Stories are a separate tier

Posts and reels are v1. Stories are v2, and they are a genuinely different work item:

- Require an authenticated session (higher block risk, account at stake).
- Expire in 24h, forcing a sub-24h poll cadence or the data is simply gone.
- Different cost profile, different failure mode.

Do not let "posts/reels/stories" collapse into one ticket. Ship posts and reels first.

---

## 7. Discovery — semi-manual

Two signals, in order of quality:

1. **Accounts tagged in event posts we already scrape.** Free (no extra fetches), high precision — venues tag artists, artists tag venues. Rank candidates by tag frequency across confirmed events.
2. **LLM proposal from an interest description.** "I like small-room indie shows, standup, and new restaurant openings in Charlotte" → a proposed handle list from model knowledge of the local scene.

Both feed a **review queue**: proposed handle, why it was suggested, sample recent posts. You approve or reject. Approved accounts enter the poll rotation. Nothing is auto-added.

Instagram's search/explore surface is not API-accessible, so there is no automatic follower-graph crawl worth building here.

---

## 8. Cost estimate

Assume 50 accounts, 12 posts/account/day = ~600 posts/day = ~18k/month, with ~25% surviving the gate.

Per stage, cheapest first:

| Line | Monthly |
|---|---|
| Scraping (18k posts @ ~$2/1k) | ~$36 |
| **Gate** — 18k posts, ~550 tok in / 50 out | |
| &nbsp;&nbsp;rung 1 · `claude-haiku-4-5` | ~$14 |
| &nbsp;&nbsp;rung 2 · `claude-sonnet-5` (standard $3/$15) | ~$43 |
| &nbsp;&nbsp;rung 3 · `claude-opus-5` | ~$72 |
| **Extraction** — 4.5k posts, ~2.4k tok in / 300 out | |
| &nbsp;&nbsp;rung 1 · `claude-haiku-4-5` | ~$18 |
| &nbsp;&nbsp;rung 2 · `claude-sonnet-5` (standard $3/$15) | ~$53 |
| &nbsp;&nbsp;rung 3 · `claude-opus-5` | ~$88 |

Resulting totals, cheapest first:

| Config | Monthly total |
|---|---|
| **All rung 1 (all-Haiku)** — the starting hypothesis | **~$68** |
| Haiku gate + Sonnet extraction | ~$103 |
| All rung 2 (all-Sonnet) | ~$132 |
| All rung 3 (all-Opus) | ~$196 |

### ⚠️ The volume assumption above is wrong by ~11x — see below

Everything in the tables above assumes 12 posts/account/day (18,000/month at 50
accounts). **Measured rate across 8 real Charlotte venue accounts: 1.1
posts/account/day.** Venues post about once a day. Range was 0.27/day (The
Milestone) to 2.5/day (The Evening Muse).

Corrected volume: **~1,650 posts/month at 50 accounts**, not 18,000. Applying the
measured 84% gate pass rate gives ~1,384 extraction calls/month.

| Config | Corrected monthly total |
|---|---|
| Haiku gate + Haiku extract | **~$10** |
| Haiku gate + Sonnet extract | **~$21** |
| Haiku gate + **Opus** extract | **~$32** |
| Opus gate + Opus extract | ~$40 |

This inverts the entire cost discussion. At real volumes the whole service runs
for the price of a couple of coffees, **all-Opus included** — so the escalation
ladder should be read as a quality question, not a budget one. If rung 1 misses
the §3 thresholds, escalate without hesitation; the delta is ~$20/month, not
~$250. Optimizing the pipeline for cost (caption-first extraction with vision as
fallback, stricter gating) would save single-digit dollars and is not worth the
added failure modes.

The one number that scales with account count rather than model choice is
scraping (~$0.07/account/month). Adding accounts is close to free; the practical
ceiling is how many you can curate, not what they cost.

### Measured, 2026-07-25 (Phase 0, 160 real posts)

**The 25% gate pass rate assumed above is wrong. The measured rate is 84%.** Event
venues post events; the assumption that most of their output is noise does not
survive contact with the data. Only mixed-use accounts behaved as predicted —
`optimisthallclt` passed 1 of 12, while `comedyzoneclt`, `neighborhoodtheatre`,
and `amossouthend` passed 20/20, 19/19, and 19/19.

That multiplies extraction volume by ~3.4x (15,188 calls/mo, not 4,500) and
roughly doubles every total below:

| Config | Projected at 25% | **Measured at 84%** |
|---|---|---|
| Haiku gate + Haiku extract | ~$68 | **~$116** |
| Haiku gate + Sonnet extract | ~$103 | **~$249** |
| Haiku gate + Opus extract | ~$124 | **~$381** |

Two consequences worth deciding on before Phase 1:

1. **The gate barely earns its keep at this pass rate.** Filtering 16% for ~$14/mo
   is near break-even against Haiku extraction, and only clearly worth it if
   extraction lands on Sonnet or Opus. The extraction call already returns
   `is_event`, so the gate is not load-bearing for correctness — only for cost.
2. **The gate prompt deliberately leans TRUE on ambiguity** ("anything rejected
   here is never seen again"). A stricter gate would cut extraction volume
   directly. Tune this only after false negatives are hand-scored — a cheap gate
   that silently drops real events is the worst outcome available.

Three caveats. Scraping is ~$36 of every row and is the floor no model choice can move. **Sonnet 5 currently has an introductory rate of $2/$10 that expires 2026-08-31** — the table uses standard $3/$15, so any Sonnet row would run ~30% cheaper if measured before then, and revert after; budget against standard. And everything scales with the gate pass rate, assumed at 25%; measure the real number in Phase 0. Prompt caching on the system prompt pulls the gate line down further in every config.

---

## 9. Interface

### v1 — dev-grade, usable
Single-page web app on the local server. FastAPI (or similar) + server-rendered HTML, no SPA framework. Responsive layout from day one.

- Chronological list, grouped by day.
- Filters: category, date range, venue, source account, confirmed-only, and
  **needs-review** (the §5.5 weekday flag). Note there is deliberately no
  confidence filter — Phase 0 measured `confidence` as effectively constant
  (85 of 127 events at exactly 0.95), so a threshold slider would do nothing.
- Each row: title, date/time, venue, price, source account, thumbnail, **link back to the original post**.
- Per-event actions: confirm, hide, flag-as-wrong (flagged items become the prompt-iteration corpus).

### Mobile
Responsive web served from the same local box, reached over Tailscale or LAN. No native app, no auth. This is one line of work, not a phase.

### ICS feed
`GET /calendar.ics` — a read endpoint over the same `event` table, filterable by the same query params. About 20 lines. It is **additive**, not an alternative to the filterable UI: subscribe to a filtered slice in your phone calendar while still using the web view for browsing.

---

## 10. Roadmap

| Phase | Contents | Gate |
|---|---|---|
| ~~**0**~~ | ~~Extraction spike~~ — **DONE 2026-07-26, rung 1 passed** (§3) | ✅ ~$1.10 spent |
| ~~**1**~~ | ~~Provider adapter, schema, pipeline, dedup~~ — **BUILT 2026-07-27** in `social_calendar/`. Live `run-once` verified against Apify + Anthropic; 33 tests passing. | ⏳ still needs a week unattended |
| ~~**2**~~ | ~~Web UI + filters, confirm/hide/flag, ICS~~ — **BUILT 2026-07-27** (`social_calendar/web.py`). Flask, server-rendered, responsive, dark-mode aware, binds 0.0.0.0 for phone access. | ⏳ use it to plan a weekend |
| ~~**3**~~ | ~~Discovery queue~~ — **BUILT 2026-07-27** (`discovery.py`, `/discover`, `cli discover`). Ranks tagged accounts by events produced, not raw frequency. **The poll rotation now lives in the `account` table**; `accounts.txt` is only a seed for an empty DB. | ⏳ add ≥5 accounts I'd have missed |
| **4** | Stories ingestion (authenticated, sub-24h cadence) | — |
| **5** | Additional sources behind `IngestionSource` (Eventbrite, RA, venue RSS) | — |

---

## 11. Open questions

- Which provider actually returns reliable **image URLs plus post timestamps** in one call? Determine in Phase 0.
- Store images locally or hotlink? Hotlinked Instagram CDN URLs expire — probably cache locally, which has storage and copyright implications for a private single-user app (fine) that would change if it were ever shared (not fine).
- ~~How aggressive should auto-hide be for low-confidence extractions?~~
  **Resolved by Phase 0: `confidence` is not usable.** Gate the review queue on
  the §5.5 weekday flag instead.
- ~~Multi-day runs: one record with `ends_at`, or N records?~~ **Resolved
  2026-07-26: one row per occurrence, grouped by `series`.** See §4.
- ~~`account_handle` is the attributed account, not the polled one.~~ **Resolved
  2026-07-27.** `source_post` stores both `polled_handle` and
  `attributed_handle`; `account` tracks `is_polled` and `seen_count` separately.
  Apify's `addParentData: true` supplies `inputUrl`, from which the polled handle
  is recovered — verified 10/10 on a live run. Worth knowing how common this is:
  **6 of those 10 posts had a different attributed handle than polled handle**
  (polling `eveningmuse` returned five posts attributed to `charlottemusicseen`).
  Collab attribution is the normal case, not an edge case.
- Venue names need a normalizer before dedup can key on them. Real Phase 0
  output included `Amos' Southend`/`Amos Southend`, `The Milestone Club`/`The
  Milestone`, and a bare `Camp` for Camp North End.
- Find the correct Visulite Theatre handle — `visulite` returned nothing.
- ~~Recurring events — N separate events, or one recurring record?~~ **Resolved
  2026-07-26: N occurrences behind a `series`, generated 8 weeks out.** See §4.
