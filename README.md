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
pip install -e .

social-calendar init
```

Everything the app writes — database, flyers, avatars, settings, keys — lives in
`~/Library/Application Support/instagram-calendar` (`$XDG_DATA_HOME` on Linux,
`%APPDATA%` on Windows), not in the checkout. Set `SOCIAL_CALENDAR_HOME` to move
it. Upgrading from a version that kept `data/` beside the code:

```bash
social-calendar migrate      # copies; originals are left alone
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

On a Mac, as an app: open the `.dmg` and drag **Instagram Calendar** to
Applications. No Python, no checkout — the runtime is inside the bundle. Build
the image yourself with `./make_dmg.sh` (see [Building the .app](#building-the-app));
there is no published release to download yet.

The app is signed, but not *notarized* — that needs a paid Apple Developer
account. So macOS blocks the first launch. Once, to get past it: open it, dismiss
the dialog, then **System Settings → Privacy & Security**, where a button offers
to open it anyway. On macOS 15 and later that is the route; right-click → Open no
longer clears it. Every launch after that is an ordinary double-click. If you
would rather do it in one line:

```bash
xattr -dr com.apple.quarantine "/Applications/Instagram Calendar.app"
```

Running from a checkout instead:

```bash
pip install -e ".[app]"
python -m social_calendar.app
```

A calendar icon appears in the menu bar, and the calendar
opens in its own window — the server runs inside the app, so no browser is
involved. Closing the window leaves it running in the menu bar. It also **runs
the daily poll itself**, which is the thing that replaces cron; see
[Keeping it up to date](#keeping-it-up-to-date).

On first launch it asks for your API keys in a native window, and the menu has:

- **API Keys…** — set or correct them later. Leave a field blank to keep the key
  that is already there.
- **Show in Dock** — off by default, which is why Cmd-Tab can't reach it. Turning
  it on adds a Dock tile and an app-switcher entry immediately. The same
  checkbox is on `/settings`; the running app picks that up within 30 seconds.

Or headless, which is still how it runs on Linux or a server:

```bash
social-calendar-web          # http://localhost:8730
```

Open `/discover` and approve some accounts — nothing is polled until you do. Add
handles by hand there too; pasting a profile URL works.

Then:

```bash
social-calendar run-once   # scrape + extract. COSTS MONEY.
social-calendar geocode    # fill in venue coordinates
social-calendar stats      # what's in the database, and what it has cost
```

## What it has actually cost

Every paid call is recorded as it happens — model calls priced from the token
counts on the response, Apify runs from the actual dollars the finished run
reports. `social-calendar stats` prints the totals; the menu bar shows them when
you open it; `/spend.json` returns the same numbers.

Two things worth knowing about those figures:

- **Neither provider will tell you your balance.** There is no Anthropic endpoint
  for account spend or remaining credit that a normal API key can reach, so this
  is counted locally rather than fetched. Token counts are exact, so the only
  thing that can drift is the published price table in `spend.py`.
- **Totals start when tracking started, not at install.** The ledger postdates
  the rest of the app, and the extraction history it would have to be rebuilt
  from never recorded token counts. The UI says "since <date>" rather than
  quietly presenting a smaller number as an all-time total.

## Keeping it up to date

`run-once` is the whole refresh. Either the Mac app runs it for you, or you
schedule it.

It's **incremental and self-healing**: every account records `last_polled_at`, and
a run fetches only what's newer than that plus a two-day overlap. A missed run
isn't a gap to repair; the next one widens its own window. Geocoding new venues,
dedupe, and avatars all happen in the same pass and only touch new rows.

Venues post about once a day, so **daily is the right cadence** — more often
spends money to find nothing.

**The Mac app schedules itself.** It doesn't fire on a clock — it asks how long
it has been since anything was actually polled, and runs when that passes 24
hours. A laptop asleep at 3am gets its run shortly after you open it, which is
the case cron gets wrong: cron skips the run and never catches up. If you use the
app, don't also install a cron entry; they'd race for the same accounts.

Otherwise, schedule it yourself:

```cron
0 7 * * * /path/to/.venv/bin/social-calendar run-once --yes >> /tmp/run.log 2>&1
```

**`--yes` is required.** Scheduled runs have no terminal to answer the
confirmation prompt, and `run-once` refuses rather than hanging. It spends money,
so saying so moves into the crontab.

No `cd` is needed — `pip install -e .` puts `social-calendar` on the path and the
data directory no longer depends on the working directory. On a **laptop** prefer
`launchd` over cron, for the same lid-shut reason the app's own scheduler exists.

### Adding an account without waiting

Approving an account in `/discover` puts it in the rotation, but nothing appears
until the next scheduled run. The **fetch now** button (↻) polls that one account
immediately so you can see it work — it runs in the background, reports progress
on the page, and shows the cost before you confirm. One at a time.

### Views

- **List** — chronological, grouped by day. The default.
- **Calendar** — month grid at `?view=calendar`. On a phone the cells show event
  counts and the day list underneath does the reading.
- **`/calendar.ics`** — the same filters apply, so you can subscribe to just
  `?category=music&hood=NoDa` and still browse everything in the UI.

The server binds `0.0.0.0` so you can reach it from your phone over Tailscale or
your LAN.

## Building the .app

```bash
pip install pyinstaller
./make_dmg.sh                 # -> dist/Instagram Calendar.dmg (~26MB)
```

That draws the icon, builds the bundle, and wraps it in the drag-to-Applications
image. To build the `.app` alone, or to re-wrap one you already built:

```bash
python make_icon.py                              # -> AppIcon.icns
pyinstaller --noconfirm InstagramCalendar.spec    # -> dist/Instagram Calendar.app
./make_dmg.sh --no-build                         # wrap the existing .app
```

`make_icon.py` *draws* the icon — a calendar page with a camera lens punched
through it — with CoreGraphics and compiles the `.icns`, rather than checking in
a binary nobody can edit. pyobjc is already a dependency, every size is rendered
from the same geometry at its native pixel count instead of being downsampled
from one master, and changing the artwork means editing constants at the top of
the file.

The app is about 44MB, and self-contained: it carries its own Python and every
dependency, so it runs with the repo and the virtualenv deleted. It stays that
size because the ~200MB of cached flyers lives in Application Support rather than
inside the bundle — if a build comes out around 250MB, that separation has broken.

The build is **ad-hoc signed**, not notarized. The signature is intact in the
image — `codesign --verify --deep --strict` passes on the mounted app, which is
what separates "Gatekeeper wants approval" from the "app is damaged" dead end —
but `spctl` rejects it on policy, so a recipient has to approve it by hand once.
See [Running it](#running-it). Removing that step needs a $99/yr Apple Developer
account, a Developer ID signature, and notarization; nothing in the code has to
change for it.

Worth re-running after any change to how the image is staged, since copying a
signed bundle is the step that breaks signatures:

```bash
codesign --verify --deep --strict --verbose=2 "/Volumes/Instagram Calendar/Instagram Calendar.app"
```

`hdiutil` builds the image because it ships with macOS. `create-dmg` is the
usual pick and its extra value — background art, positioned icons — is a
`.DS_Store` baked by driving Finder over AppleScript, which needs a logged-in
session and fails under ssh or CI.

(`py2app` is the more obvious choice for a Mac bundle and does not work here: its
latest release reads `[project].dependencies` out of `pyproject.toml` as
`install_requires` and then rejects it.)

## Security

There is **no authentication**, by design — it's a single-user app on your own
network. Two consequences worth being explicit about:

- **Don't expose it to the public internet.** Tailscale or LAN only.
- **API keys are not editable from the web UI**, and `/settings` reports only
  whether a key is present, never its value. A key field on a no-auth
  `0.0.0.0` service would be a key field for everyone on the network. Set keys
  with `social-calendar init` or by editing
  `~/Library/Application Support/instagram-calendar/.env` (mode 600).

The Mac app asks for them in a **native window** instead — **API Keys…** in its
menu, and automatically on first launch when either key is missing. That is not a
loophole in the rule above: the objection to a key field on `/settings` is that
Flask is listening on `0.0.0.0` with no login, and a Cocoa window is not reachable
over the network at all. It writes the same mode-600 file `init` does.

## What's not in this repo

The SQLite database, cached flyers, and profile pictures — now in Application
Support rather than `data/`, and `spike/posts/` either way. That's scraped
third-party content: fine to cache locally for personal use, not something to
redistribute. Both are gitignored, and `social-calendar import-spike` (which
replays the author's Phase 0 corpus) won't work from a clone.

## Design notes

[SPEC.md](SPEC.md) is the real document — schema, the dedupe strategy, the
escalation ladder, measured extraction accuracy, and the reasoning behind the
things that look arbitrary. Read §3 before changing prompts.

## License

None yet. Ask.
