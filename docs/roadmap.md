# Product roadmap

This file tracks planned work that has not shipped. Completed work belongs in
the README and release notes.

## Performer proximity alerts

Status: implemented on `codex/performer-event-alerts`, pending real-world checks

Let a user follow a performer, comedian, touring production, speaker, or other
act through its national tour page. Add a performance to the calendar when its
location falls within that watch's distance limit.

### First release

- Store one home ZIP code and its coordinates on the Mac.
- Let each performer watch set its own distance limit, with 250 miles as the
  default.
- Check tour pages every six hours while the Mac app runs and catch up after the
  Mac wakes.
- Keep out-of-range tour dates in local storage, but exclude them from the app's
  calendar and calendar feed.
- Show the performer, date and time, venue, city and state, distance, and direct
  event or ticket link in each qualifying listing.
- Record ticket availability only when the tour page states it. Treat a visible
  `Buy Tickets` link as tickets offered, and preserve labels such as sold out,
  waitlist, or canceled when a source supplies them.
- Import a ticket price when the tour page includes one. Do not crawl ticketing
  sites for prices in the first release.
- Send one macOS notification for each newly discovered qualifying performance.
  The notification should open the direct event or ticket page.
- Send one update notification when a saved performance changes venue, date, or
  availability. Keep a delivery record so unchanged listings do not alert twice.
- Report dates with missing or unresolvable locations on the source screen.
  Do not guess that they are within range.

### Source support

Use iCalendar and structured event metadata when a tour page publishes them.
Add adapters for common dynamic tour platforms as we collect examples.

Punchup is the first adapter. It reads the public show endpoint behind the tour
page and imports the event link, date and time, city and state, venue, ticket
link, and sold-out flag. The sample page does not expose ticket prices. Test the
adapter against:

- <https://punchup.live/timmynobrakes/tour>

### Acceptance checks

- A 250-mile watch imports nearby dates and excludes dates beyond the limit.
- Changing one performer's radius recalculates that watch without changing other
  watches.
- Multiple showtimes at one venue remain separate events.
- The same performance found through another source appears once and keeps both
  source links.
- A repeated source check produces no duplicate calendar entries or
  notifications.
- A sold-out or canceled label replaces the earlier availability state when the
  source changes it.

## Website ingestion architecture

Status: proposed after architecture review on 2026-08-12

The current pipeline has a useful common shape. It fetches a source, tries
iCalendar and schema.org Event data before paid extraction, converts every
result to `StructuredEvent`, and sends those records through the same update,
provenance, dedupe, geocoding, and storage paths. Its final model fallback uses
sanitized visible text, verifies titles and links against the page, and caches
the result by content hash.

The dispatch logic now mixes general formats with provider-specific handling in
`websites.fetch_events`. It checks iCalendar, JSON-LD, linked calendars,
Carbonhouse cards, Punchup, Bandsintown, Seated, Riverside, BOplex, and the model
fallback in a fixed sequence. The function returns as soon as one parser finds
events. A page with one incomplete JSON-LD event and a complete embedded feed
therefore imports only the JSON-LD result. Adding another provider also requires
editing the central fetch flow and reproducing decisions about recognition,
empty schedules, errors, and secondary requests.

### Evaluation baseline

The production database contained 21 enabled website sources at review time.
Seven of ten venue sources used general JSON-LD ingestion. One venue used
Carbonhouse cards, one used the BOplex API, and one used model extraction.
Performer coverage depended mainly on provider adapters for Bandsintown, Seated,
and Punchup.

The 808 stored website items broke down as follows:

- 457 came from general structured ingestion, primarily JSON-LD.
- 311 came from provider APIs.
- 17 came from Carbonhouse cards.
- 23 came from model extraction.

The general path covers most venue pages in the current sample. Provider
adapters carry most performer pages, so adapter maintenance will grow as source
support expands. The website and performer test set passed 51 checks, and the
full suite passed 202 checks. Most parser fixtures are small synthetic fragments
rather than saved responses from real sites.

### Risks to address

- First-parser-wins behavior can discard events or richer fields from other
  representations on the same page.
- The normal fetch path does not render JavaScript. Client-rendered calendars
  need a known provider API, server-rendered fallback content, or a model-visible
  copy of the events.
- Adapters do not share a contract for "recognized with events," "recognized
  but empty," and "not recognized." A broken parser can look like a valid empty
  schedule, or an empty schedule can fall through as unsupported.
- The custom iCalendar reader omits `TZID`, recurrence rules, exclusions,
  durations, and other common calendar behavior.
- Offset-bearing times are converted to the configured home timezone and stored
  without timezone data. A national performer date can display in the wrong
  local time.
- A successful poll upserts returned events but does not reconcile future
  events that disappeared from an authoritative feed. Canceled listings and
  records from an older parser can remain in the calendar.
- Synthetic parser tests protect known field mappings but do not measure event
  recall against complete real pages.

### Proposed design

Keep `StructuredEvent` and the shared persistence pipeline. Replace the central
parser sequence with an extractor registry. Each extractor should own detection,
parsing, and any provider request it needs, then return a result with:

- whether it recognized the representation;
- whether it considers its result authoritative;
- events and an explicit valid-empty state;
- coverage or confidence metadata;
- diagnostics and any discovered child feeds.

The orchestrator should discover every representation on the fetched page, run
all cheap general extractors, run adapters for detected providers, and merge
complementary events before persistence. Stable provider IDs should win when
available. The merge should use normalized date, time, venue, title, and
permalink when a representation has no provider ID. The model fallback should
run only when deterministic extraction leaves no events or shows evidence of
incomplete coverage.

Suggested processing order:

1. Fetch the source once and discover JSON-LD, calendar links, embedded provider
   configuration, semantic HTML, and application-state JSON.
2. Run all applicable local parsers and follow discovered public feeds.
3. Merge their records and retain field-level provenance so a richer feed can
   supplement incomplete JSON-LD.
4. Use an optional rendered-page extractor for client-only calendars that expose
   no public feed.
5. Use the guarded text model after deterministic and rendered extraction.
6. Reconcile the merged snapshot with active future events from the prior
   successful poll.

### Implementation sequence

1. Define the extractor result contract and registry. Move existing provider
   adapters into separate modules without changing behavior.
2. Change orchestration from first-success selection to multi-extractor merge.
   Add fixtures for pages that expose overlapping partial representations.
3. Replace the hand-written iCalendar parser with a maintained standards-aware
   library and preserve each event's source timezone.
4. Add generic application-state discovery and an optional browser-rendered
   extraction tier before writing more site-specific DOM parsers.
5. Add active, canceled, or last-confirmed state and reconcile missing future
   events only after a complete authoritative response.
6. Build a versioned replay corpus from sanitized real HTML, calendar feeds, and
   provider responses. Track source success, event recall, duplicate rate, field
   accuracy, extraction cost, and parser errors.

### Acceptance checks

- A page with partial JSON-LD and a complete linked or embedded feed imports the
  union without duplicate events.
- Adding an adapter requires a new module and registry entry, not edits to the
  central fetch algorithm.
- Every extractor distinguishes unsupported, valid empty, partial, complete,
  and failed results.
- Performer times display in the venue's local timezone and survive export with
  that meaning intact.
- A failed or partial fetch cannot remove saved events. A complete authoritative
  response can mark a missing future event canceled or inactive.
- Replay evaluation reports event-level recall as well as parser success for
  each supported source.

## Mobile notifications

Status: future

Send performer alerts to a phone when the Mac is asleep or the app is closed.
The first performer-alert release will use macOS Notification Center, so mobile
delivery needs a service that can run checks and deliver messages without the
Mac.

Before implementation, choose the delivery channel and operating model:

- Push notification, email, or SMS
- Optional hosted account or a user-owned service
- Authentication and private storage for home location and followed performers
- Provider costs, rate limits, retries, and notification history
- A ticket deep link that opens from the phone

Keep mobile delivery opt-in. The local calendar and Mac notifications should
continue to work without an account or hosted service.
