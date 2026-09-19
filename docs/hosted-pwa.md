# Hosted PWA architecture

Status: in progress on `codex/hosted-pwa`

The hosted edition is one service for multiple private users. It is not a
shared calendar and does not ask each user to deploy an instance. Cloudflare
Access authenticates approved email addresses; LoCal remains responsible for
mapping that identity to private data and provider usage.

## Topology

- Cloudflare Access provides the login challenge and email allowlist.
- Cloudflare Tunnel is the only public path to the origin.
- One small VM runs Flask, SQLite, source refresh workers, and backups.
- The browser installs the same responsive PWA on iPhone, iPad, and Mac.
- A service worker caches only the app icons and offline notice. Event HTML,
  JSON, exports, and media remain network-only so an offline cache cannot leak
  one user's calendar into another session.

The origin validates every Cloudflare Access JWT itself. A tunnel is a route,
not an authorization boundary. Validation covers the signature, issuer,
application audience, expiry, subject, and email claim.

## Authentication modes

Local installs remain the default:

```text
LOCAL_CALENDAR_AUTH_MODE=local
```

Hosted mode requires all three settings:

```text
LOCAL_CALENDAR_AUTH_MODE=cloudflare
CLOUDFLARE_ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=the-application-audience-tag
```

Private routes fail closed when hosted configuration or a valid assertion is
missing. `/healthz` and static install assets remain available for platform
health checks and PWA startup. Cloudflare Access still protects the public
hostname at the edge.

## Tenant storage decision

The existing schema has provider identifiers and source URLs as global primary
or unique keys. Adding a `user_id` only to the visible tables would not isolate
accounts, trips, raw posts, extraction, editorial state, cached website items,
or media. Retrofitting compound tenant keys through every relationship would be
a high-risk migration for the current local database.

The first hosted version will instead route each identity to a separate data
directory containing its SQLite database, configuration, media, and secret
references. A small central registry maps the stable Access subject to an email
and tenant directory. This preserves the current schema and makes accidental
cross-user SQL reads impossible by construction.

Future shared trips should be explicit shared objects in the central registry,
with a membership table and references to selected events. Private calendars
stay separate; sharing never means granting access to another user's database.

## Provider keys and reimbursement

Each user receives a distinct DeepSeek key. The server chooses it after
authentication and never sends it to the browser. The existing spend ledger
already records actual response token counts and the applicable rate at call
time; placing that ledger inside the tenant database naturally produces a
per-user reimbursement report.

DeepSeek's account balance and concurrency remain account-wide even when keys
are distinct, so LoCal's ledger—not the provider dashboard—is the per-user
source of truth.

## Delivery sequence

1. Installable PWA shell and origin JWT boundary.
2. Tenant registry and request-local database/media/config routing.
3. Per-user DeepSeek secrets and refresh jobs, with usage reports.
4. Real WebP thumbnails so normal browsing remains well below VM egress limits.
5. Headless scheduler, encrypted backups, deployment service, and Cloudflare
   Tunnel configuration.
6. Migrate the current local calendar into the owner's tenant and invite the
   second user into a new empty tenant.

The service must not be opened to a second user until step 2 is complete.
