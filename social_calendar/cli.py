"""CLI. Cron schedules `run-once`; there is no daemon.

    python -m social_calendar.cli run-once
    python -m social_calendar.cli import-spike     # replay Phase 0, spends nothing
    python -m social_calendar.cli stats
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import avatars, db, discovery, extract, geo, migrate, paths, pipeline, runner, spend
from .config import API_KEYS, ENV_PATH, write_env
from .sources import ApifySource, LocalSpikeSource, read_accounts

# `spike/` is the Phase 0 corpus: author-only, gitignored, and read-only. It is
# a source-tree asset rather than user data, so unlike everything in `paths` it
# stays anchored to the checkout -- `import-spike` is a dev command and does not
# exist for someone running the packaged app.
ROOT = Path(__file__).parent.parent
ACCOUNTS = ROOT / "spike" / "accounts.txt"


def _env() -> None:
    load_dotenv(paths.ENV_LOCAL_PATH)
    load_dotenv(paths.ENV_PATH)


def _extractor(rung: int):
    from .extract import RUNGS, Extractor

    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY not set.\n  Run `python -m social_calendar.cli init`, or set it in .env.")
    if rung != 1:
        print(f"  !! rung {rung} ({RUNGS[rung]}) is an ESCALATION above the Phase 0 "
              f"production config. Only run with explicit approval.")
    return Extractor(rung=rung)


def _ask(prompt: str, default: str = "") -> str:
    got = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return got or default


def cmd_init(args) -> None:
    """Interactive first-run setup: where you are, and what you can spend."""
    _env()
    from . import config

    print("\nsocial-calendar setup\n" + "-" * 21)
    cfg = config.load()

    # --- city -------------------------------------------------------------
    city = _ask("\nYour city (e.g. 'Portland, OR' or 'Lisbon, Portugal')", cfg["city"])
    print("  looking it up...", end="", flush=True)
    center = geo.geocode_city(city)
    if center is None:
        print(" not found.")
        print("  Nominatim could not resolve that. Try a bigger nearby city, or add "
              "the state/country.")
        sys.exit(1)
    lat, lon = center
    print(f" {lat:.4f}, {lon:.4f}")

    # A radius, not a bounding box: a box has to be hand-drawn per metro and
    # silently drops suburban venues when it is wrong. One number is answerable.
    radius = _ask("How far out do you care about, in miles", str(int(cfg["radius_miles"])))
    try:
        radius = float(radius)
    except ValueError:
        radius = config.DEFAULTS["radius_miles"]

    tz = _ask("Timezone (IANA name)", config.system_timezone())
    while not config.is_valid_timezone(tz):
        print(f"  '{tz}' is not an IANA zone name -- ICS feeds need e.g. "
              f"'America/Denver', not 'MDT'.")
        tz = _ask("Timezone", config.system_timezone())

    country = _ask("Country, for postal-code lookups", cfg["country"])

    saved = config.save({"city": city, "lat": lat, "lon": lon,
                         "radius_miles": radius, "timezone": tz, "country": country})
    print(f"\nwrote {config.CONFIG_PATH.name}: {saved['city']}, "
          f"{saved['radius_miles']:g} mi, {saved['timezone']}")

    # --- keys -------------------------------------------------------------
    print("\nAPI keys. Both are needed for `run-once`; leave blank to skip and add "
          "them to .env yourself later.")
    values = {}
    for key, label, url in API_KEYS:
        if os.getenv(key):
            print(f"  {key} already set — leaving it alone.")
            continue
        print(f"  {label} — {url}")
        values[key] = _ask(f"  {key}", "")
    if any(values.values()):
        write_env(values)
        print(f"wrote {ENV_PATH.name} (mode 600)")
    elif values:
        print("  skipped — add them to .env when you have them.")

    # --- accounts ---------------------------------------------------------
    print("\nNow: which Instagram accounts to watch. Nothing is polled until you "
          "approve it.")
    desc = _ask("Describe what you like (blank to skip)", "")
    if desc and (values.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")):
        os.environ.setdefault("OPENAI_API_KEY", values.get("OPENAI_API_KEY", ""))
        ex = _extractor(1)
        cand = discovery.propose(ex, desc, [], city=city)
        with db.session(args.db) as conn:
            discovery.stage(conn, cand)
            spend.drain_into(conn, ex.meter)
        print(f"  staged {len(cand)} candidate account(s) for review.")
        for c in cand:
            print(f"    @{c['handle']} — {c.get('why','')}")

    print("\nDone. Next:")
    print("  python -m social_calendar.web            # then open /discover to approve accounts")
    print("  python -m social_calendar.cli run-once   # scrape + extract (COSTS MONEY)")


def cmd_run_once(args) -> None:
    _env()
    # The poll rotation lives in the DB so approvals in /discover take effect.
    # A seed file is only used for an empty database, and only when asked for --
    # a fresh clone must not silently start polling the author's Charlotte venues.
    with db.session(args.db) as conn:
        handles = discovery.approved_handles(conn)
        website_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM web_source WHERE enabled=1 ORDER BY id")]
        if not handles and not website_ids and not args.accounts:
            sys.exit(
                "No Instagram accounts or websites are followed yet.\n"
                "  python -m social_calendar.cli init          # setup, incl. suggestions\n"
                "  python -m social_calendar.web               # add sources at /discover\n"
                f"  ...or seed from a file: --accounts {ACCOUNTS.name}")
        if not handles and args.accounts:
            handles = read_accounts(Path(args.accounts))
            print(f"seeding rotation from {args.accounts}")
            for h in handles:
                conn.execute(
                    "INSERT INTO account (handle, is_polled, seen_count, discovery_source, "
                    "added_at, status) VALUES (?,1,0,'manual',datetime('now'),'approved') "
                    "ON CONFLICT(handle) DO UPDATE SET is_polled=1, status='approved'", (h,))
    source = extractor = None
    if handles:
        token = os.getenv("APIFY_TOKEN")
        if not token:
            sys.exit("APIFY_TOKEN not set.\n  Run `python -m social_calendar.cli init`, or set it in .env.")
        source = ApifySource(token)
        extractor = _extractor(args.rung)
    elif website_ids and os.getenv("OPENAI_API_KEY"):
        # Structured sites still work without a key. Supplying one merely
        # enables the nano fallback for pages with no supported markup.
        extractor = _extractor(args.rung)

    # Measured on real data: posts older than 30 days produced 3% of upcoming
    # events while being 18% of fetches, so the window is worth narrowing.
    with db.session(args.db) as conn:
        groups = runner.fetch_windows(conn, handles, args.history_days)
    for window, batch in groups:
        print(f"{len(batch)} account(s) -> posts newer than {window}")

    est = source.estimate_cost(handles, args.limit) if source else 0
    print(f"{len(website_ids)} website(s) + {len(handles)} Instagram account(s)")
    if handles:
        print(f"{len(handles)} accounts x {args.limit} posts -> ~${est:.2f} scraping")
    if handles and not args.yes:
        # Scheduled runs have no stdin, and a bare input() there dies with an
        # EOFError traceback. Spending money still requires saying so -- but say
        # it in the crontab, not into a pipe that cannot answer.
        if not sys.stdin.isatty():
            sys.exit("run-once needs --yes when nothing can answer a prompt "
                     "(cron, launchd, systemd). It spends money; that is why it asks.")
        if input("Proceed? [y/N] ").strip().lower() != "y":
            sys.exit("Aborted.")

    with db.session(args.db) as conn:
        runner.poll(conn, source, extractor, handles, args.limit,
                    groups=groups, max_posts=args.max_posts, log=print,
                    website_source_ids=website_ids)


def cmd_import_spike(args) -> None:
    """Replay the Phase 0 corpus into the DB. No network, no spend.

    Author-only: `spike/posts/` is scraped Instagram content, so it is
    gitignored and absent from a clone.
    """
    _env()
    posts = ROOT / "spike" / "posts"
    if not posts.exists():
        sys.exit(
            f"No Phase 0 corpus at {posts}.\n"
            "This command replays the author's local scrape, which is not "
            "redistributable and is not in the repo.\n"
            "Start with: python -m social_calendar.cli init")
    handles = read_accounts(Path(args.accounts))
    src = LocalSpikeSource(posts)
    with db.session(args.db) as conn:
        n = pipeline.ingest(conn, src, handles, fetch_media=False)
        print(f"ingested {n} posts from the Phase 0 corpus")
        if args.with_results:
            print(f"replayed {_replay(conn)} stored extractions (no model calls)")
        # series first: generated occurrences must be visible to dedupe in the
        # same pass, or duplicates survive until the next run
        print("series:", pipeline.expand_series(conn))
        print("dedupe:", pipeline.rebuild_dedupe(conn))


def _replay(conn) -> int:
    """Load the Phase 0 CSV as if extraction had just run. Keeps the acceptance
    test free."""
    import csv
    from . import dedupe as dd
    from . import validate as v

    path = ROOT / "spike" / "results" / "rung1_extract.csv"
    if not path.exists():
        return 0
    n = 0
    for r in csv.DictReader(path.open()):
        if not conn.execute("SELECT 1 FROM source_post WHERE post_id=?", (r["post_id"],)).fetchone():
            continue
        if conn.execute("SELECT 1 FROM extraction WHERE post_id=? AND stage='extract'",
                        (r["post_id"],)).fetchone():
            continue

        # Record the extraction for EVERY post, including non-events -- otherwise
        # process() sees no extract row and pays to re-run them.
        if r["is_event"] != "True":
            conn.execute(
                "INSERT INTO extraction (post_id, stage, prompt_version, model, raw_output, "
                "is_error, created_at) VALUES (?,?,?,?,?,0,datetime('now'))",
                (r["post_id"], "extract", "extract-v1", extract.RUNGS[extract.DEFAULT_RUNG], json.dumps(r)))
            continue

        eid = conn.execute(
            "INSERT INTO extraction (post_id, stage, prompt_version, model, raw_output, "
            "is_error, created_at) VALUES (?,?,?,?,?,0,datetime('now'))",
            (r["post_id"], "extract", "extract-v1", extract.RUNGS[extract.DEFAULT_RUNG], json.dumps(r))).lastrowid
        flagged, reason = v.validate({"starts_at": r["starts_at"], "posted_at": r["posted_at"],
                                      "date_reasoning": r["date_reasoning"]})
        conn.execute(
            "INSERT INTO event (post_id, extraction_id, title, starts_at, start_time_known, "
            "venue_name, venue_key, category, price_text, confidence, date_reasoning, "
            "needs_review, review_reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (r["post_id"], eid, r["title"], r["starts_at"],
             int(r["start_time_known"] == "True"), r["venue_name"],
             dd.normalize_venue(r["venue_name"]), r["category"], r["price_text"],
             float(r["confidence"] or 0), r["date_reasoning"], int(flagged), reason))
        n += 1
    return n


def cmd_discover(args) -> None:
    """Surface candidate accounts. Never adds anything without approval."""
    _env()
    with db.session(args.db) as conn:
        found = discovery.rank_tagged(conn, min_events=args.min_events)
        print(f"{len(found)} tagged candidates (>= {args.min_events} event)")

        if args.describe:
            ex = _extractor(args.rung)
            try:
                proposed = discovery.propose(ex, args.describe,
                                             discovery.approved_handles(conn))
            finally:
                spend.drain_into(conn, ex.meter)
            print(f"{len(proposed)} proposed from interests")
            found += proposed

        discovery.stage(conn, found)
        queue = discovery.pending(conn)
        print(f"\n{len(queue)} awaiting review:")
        for c in queue[:args.top]:
            print(f"  @{c['handle']:26s} {c['events']:2d} events  [{c['discovery_source']}]")
            print(f"     {c['proposed_reason']}")
        print("\nApprove in the web UI (/discover) or: "
              "cli approve-account <handle> / reject-account <handle>")


def cmd_decide(args) -> None:
    with db.session(args.db) as conn:
        discovery.decide(conn, args.handle, approve=(args.cmd == "approve-account"))
        print(f"@{args.handle}: {'APPROVED -> now polled' if args.cmd == 'approve-account' else 'rejected'}")
        print(f"poll rotation is now {len(discovery.approved_handles(conn))} accounts")


def cmd_migrate(args) -> None:
    """Copy an in-tree install into the app data directory. Idempotent."""
    print(f"copying into {paths.HOME}")
    report = migrate.run()
    if not report:
        print("  nothing to do -- already migrated, or no in-tree data found.")
        return
    for line in report:
        print(f"  {line}")
    print(f"\nOriginals were left where they were. Once the app looks right, "
          f"{migrate.SOURCE_ROOT / 'data'} can be deleted.")


def cmd_stats(args) -> None:
    with db.session(args.db) as conn:
        def q(sql: str) -> int:
            return conn.execute(sql).fetchone()[0]

        print(f"posts:         {q('SELECT COUNT(*) FROM source_post')}")
        collab = q("SELECT COUNT(*) FROM source_post WHERE polled_handle=''")
        print(f"  collab-only: {collab}")
        print(f"events:        {q('SELECT COUNT(*) FROM event')}")
        print(f"  canonical:   {q('SELECT COUNT(*) FROM event WHERE is_canonical=1')}")
        print(f"  needs review:{q('SELECT COUNT(*) FROM event WHERE needs_review=1')}")
        print(f"series:        {q('SELECT COUNT(*) FROM series')}")
        print(f"accounts:      {q('SELECT COUNT(*) FROM account')} "
              f"({q('SELECT COUNT(*) FROM account WHERE is_polled=1')} polled)")

        t = spend.totals(conn)
        if t["since"]:
            by = "  ".join(f"{k} ${v:.2f}" for k, v in sorted(t["by_provider"].items()))
            print(f"\nspend:         ${t['all_time']:.2f} since {t['since'][:10]} "
                  f"({t['calls']} paid calls)")
            print(f"  last 24h:    ${t['last_24h']:.2f}")
            print(f"  by provider: {by}")
        else:
            # Not "$0.00 all time": the ledger postdates this install, so a zero
            # here means nothing has been recorded yet, not nothing was spent.
            print("\nspend:         nothing recorded yet (tracking starts at the next run)")

        print("\nnext 10 events:")
        for r in conn.execute(
            "SELECT starts_at, title, venue_name, needs_review FROM event "
            "WHERE is_canonical=1 AND starts_at >= date('now') "
            "ORDER BY starts_at LIMIT 10"):
            flag = "  [REVIEW]" if r["needs_review"] else ""
            print(f"  {r['starts_at'][:16]:17s} {(r['title'] or '')[:40]:42s} "
                  f"{(r['venue_name'] or '')[:20]}{flag}")


def _avatars_cmd(args) -> None:
    _env()
    token = os.getenv("APIFY_TOKEN")
    if not token:
        sys.exit("APIFY_TOKEN not set.\n  Run `python -m social_calendar.cli init`, or set it in .env.")
    from apify_client import ApifyClient

    with db.session(args.db) as conn:
        discovery.stage(conn, discovery.rank_tagged(conn))
        print("avatars:", avatars.backfill(conn, ApifyClient(token)))


def _geocode_cmd(args) -> None:
    with db.session(args.db) as conn:
        print("dedupe:", pipeline.rebuild_dedupe(conn))   # apply any new aliases first
        print("geocode:", geo.geocode_all(conn, force=args.force))
        runner.relabel(conn)


def main() -> None:
    ap = argparse.ArgumentParser(prog="social-calendar")
    ap.add_argument("--db", default=str(db.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("init", help="first-run setup: city, radius, timezone, API keys")
    n.set_defaults(func=cmd_init)

    r = sub.add_parser("run-once", help="scrape, extract, dedupe (costs money)")
    r.add_argument("--accounts", help="seed an empty rotation from this file")
    r.add_argument("--limit", type=int, default=20)
    r.add_argument("--history-days", type=int,
                   help="override the fetch window (default: 30d for new accounts, "
                        "since-last-poll otherwise)")
    r.add_argument("--max-posts", type=int)
    r.add_argument("--rung", type=int, choices=[1, 2, 3], default=1)
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=cmd_run_once)

    i = sub.add_parser("import-spike", help="replay the Phase 0 corpus; spends nothing")
    i.add_argument("--accounts", default=str(ACCOUNTS))
    i.add_argument("--with-results", action="store_true", default=True)
    i.set_defaults(func=cmd_import_spike)

    d = sub.add_parser("discover", help="surface candidate accounts for review")
    d.add_argument("--min-events", type=int, default=1)
    d.add_argument("--top", type=int, default=15)
    d.add_argument("--describe", help="interests, to also get LLM proposals (costs a call)")
    d.add_argument("--rung", type=int, choices=[1, 2, 3], default=1)
    d.set_defaults(func=cmd_discover)

    for name in ("approve-account", "reject-account"):
        a = sub.add_parser(name, help=f"{name.split('-')[0]} a candidate")
        a.add_argument("handle")
        a.set_defaults(func=cmd_decide)

    v = sub.add_parser("avatars", help="fetch missing profile pictures")
    v.set_defaults(func=lambda a: _avatars_cmd(a))

    g = sub.add_parser("geocode", help="fill in venue coordinates + neighborhoods")
    g.add_argument("--force", action="store_true", help="re-query venues already done")
    g.set_defaults(func=lambda a: _geocode_cmd(a))

    s = sub.add_parser("stats", help="what is in the database")
    s.set_defaults(func=cmd_stats)

    m = sub.add_parser("migrate", help="copy an in-tree install into the app data directory")
    m.set_defaults(func=cmd_migrate)

    args = ap.parse_args()
    # Cheap check, and the one moment the user is looking at a terminal. Skipped
    # for `migrate` itself, which would otherwise advertise what it is about to do.
    if args.func is not cmd_migrate and migrate.pending():
        print(f"  !! found an in-tree install at {migrate.SOURCE_ROOT / 'data'} that has not "
              f"moved to {paths.HOME}.\n"
              f"     Run `social-calendar migrate` first, or this run starts from empty.\n",
              file=sys.stderr)
    args.func(args)


if __name__ == "__main__":
    main()
