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
