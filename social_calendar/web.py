"""Phase 2: local web UI + ICS feed. SPEC section 9.

Server-rendered HTML, no SPA. Responsive from the start because the point is to
pull it up on a phone (over Tailscale/LAN -- no auth, single user).

    python -m social_calendar.web
    open http://localhost:8730
"""

from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import (Flask, Response, abort, redirect, render_template, request,
                   send_from_directory, url_for)

from . import config, db, discovery, geo, runner

# Only so /settings can report whether a key is present. Values are never
# rendered, logged, or accepted over HTTP -- see the note above that route.
load_dotenv(config.ROOT / ".env.local")
load_dotenv(config.ROOT / ".env")

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
app.config["DB"] = str(db.DB_PATH)
# Single-user local app: pick up template edits without a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

MEDIA_DIRS = [Path(__file__).parent.parent / "data" / "media",
              Path(__file__).parent.parent / "spike" / "posts" / "media"]
AVATAR_DIR = Path(__file__).parent.parent / "data" / "avatars"


@app.route("/avatar/<path:name>")
def avatar(name: str):
    safe = Path(name).name
    if (AVATAR_DIR / safe).exists():
        return send_from_directory(AVATAR_DIR, safe, max_age=604800)
    abort(404)


@app.route("/media/<path:name>")
def media(name: str):
    """Serve a stored flyer. Basename-only: the name comes from the DB, but
    treating it as a path would still be a traversal risk."""
    safe = Path(name).name
    for d in MEDIA_DIRS:
        if (d / safe).exists():
            return send_from_directory(d, safe, max_age=86400)
    abort(404)


def day_label(iso: str, today: dt.date) -> str:
    """Friendlier than a bare ISO date: weekday inside a week, then a written
    date, with the year only when it is not the current one."""
    try:
        d = dt.date.fromisoformat(iso)
    except ValueError:
        return iso
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if 0 < delta < 7:
        return d.strftime("%A")
    base = d.strftime("%A, %B %-d")
    return base if d.year == today.year else f"{base}, {d.year}"


@app.template_filter("clock")
def clock(starts_at: str | None) -> str:
    """19:30 -> 7:30pm, 20:00 -> 8pm. Drops ':00' because nobody says 'eight oh clock'."""
    if not starts_at or "T" not in str(starts_at):
        return ""
    try:
        t = dt.datetime.fromisoformat(starts_at)
    except ValueError:
        return ""
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}:{t.minute:02d}{suffix}" if t.minute else f"{hour}{suffix}"


CATEGORIES = ["music", "comedy", "food", "market", "art", "opening", "other"]


# --- month grid -----------------------------------------------------------

def parse_month(value: str | None) -> dt.date | None:
    """'2026-07' -> date(2026, 7, 1). None for anything unparseable."""
    try:
        return dt.date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError):
        return None


def month_weeks(first: dt.date) -> list[list[dt.date]]:
    """Whole Sunday-start weeks covering `first`'s month, spill days included."""
    return calendar.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)


def shift_month(first: dt.date, delta: int) -> dt.date:
    m = first.month - 1 + delta
    return dt.date(first.year + m // 12, m % 12 + 1, 1)


def query_with(args, **over) -> str:
    """Rebuild the query string with `over` keys replaced, not appended -- the
    month/view links are followed repeatedly, so appending would pile up."""
    items = [(k, v) for k, v in args.items(multi=True) if k not in over]
    items += [(k, v) for k, v in over.items() if v is not None]
    return urlencode(items)


app.jinja_env.globals["query_with"] = query_with


def _filters(args) -> tuple[str, list]:
    """Build the WHERE clause shared by the HTML view and the ICS feed."""
    where = ["e.is_canonical = 1", "e.is_hidden = 0", "e.starts_at IS NOT NULL"]
    params: list = []

    if args.getlist("category"):
        cats = args.getlist("category")
        where.append(f"e.category IN ({','.join('?' * len(cats))})")
        params += cats
    if hood := args.get("hood"):
        where.append("v.neighborhood = ?")
        params.append(hood)
    if venue := args.get("venue"):
        where.append("e.venue_key = ?")
        params.append(venue)
    if account := args.get("account"):
        where.append("(p.polled_handle = ? OR p.attributed_handle = ?)")
        params += [account, account]
    if args.get("confirmed") == "1":
        where.append("e.is_confirmed = 1")
    if args.get("review") == "1":
        where.append("e.needs_review = 1")

    # A month bounds the window on both sides, so the "upcoming" floor would
    # only blank out the days of the current month that have already passed.
    # Bounds cover the whole grid, spill days included, so those cells are real.
    month = parse_month(args.get("month"))
    if month:
        weeks = month_weeks(month)
        where.append("substr(e.starts_at,1,10) BETWEEN ? AND ?")
        params += [weeks[0][0].isoformat(), weeks[-1][-1].isoformat()]
    elif args.get("when", "upcoming") == "upcoming":
        where.append("substr(e.starts_at,1,10) >= ?")
        params.append(dt.date.today().isoformat())
    if until := args.get("until"):
        where.append("substr(e.starts_at,1,10) <= ?")
        params.append(until)

    return " AND ".join(where), params


BASE_SELECT = """
SELECT e.id, e.title, e.starts_at, e.start_time_known, e.venue_name, e.venue_key,
       e.category, e.price_text, e.needs_review, e.review_reason, e.is_confirmed,
       e.occurrence_of, e.dedupe_group, p.permalink, p.polled_handle,
       p.attributed_handle, p.local_images, p.caption, p.posted_at,
       v.neighborhood, v.lat, v.lon, v.address,
       (SELECT COUNT(*) FROM event s WHERE s.dedupe_group = e.dedupe_group) AS source_count
FROM event e LEFT JOIN source_post p ON p.post_id = e.post_id
              LEFT JOIN venue v ON v.venue_key = e.venue_key
"""


_ZIP_CACHE: dict[str, tuple[float, float] | None] = {}


def _zip_center(zipcode: str):
    """Cached -- Nominatim is rate limited and a zip does not move."""
    if zipcode not in _ZIP_CACHE:
        _ZIP_CACHE[zipcode] = geo.geocode_zip(zipcode)
    return _ZIP_CACHE[zipcode]


def _rows(conn, args):
    where, params = _filters(args)
    rows = conn.execute(
        f"{BASE_SELECT} WHERE {where} ORDER BY e.starts_at LIMIT 500", params).fetchall()

    zipcode, radius = (args.get("zip") or "").strip(), args.get("radius")
    if not (zipcode and radius):
        return [dict(r) for r in rows]

    center = _zip_center(zipcode)
    if center is None:
        return [dict(r) for r in rows]   # unresolvable zip: do not silently empty the page

    lat0, lon0 = center
    miles = float(radius)
    out = []
    for r in rows:
        d = dict(r)
        if d["lat"] is None:
            continue  # no coordinates -- cannot honour a radius, so exclude
        d["distance"] = geo.haversine_miles(lat0, lon0, d["lat"], d["lon"])
        if d["distance"] <= miles:
            out.append(d)
    return out


@app.route("/")
def index():
    today = dt.date.today()

    # The grid needs a month even when the URL omits one, and _filters() reads
    # it off args -- so resolve it up front and hand the filters a copy.
    view = "calendar" if request.args.get("view") == "calendar" else "list"
    args = request.args
    month = None
    if view == "calendar":
        month = parse_month(args.get("month")) or today.replace(day=1)
        args = request.args.copy()
        args["month"] = f"{month:%Y-%m}"

    with db.session(app.config["DB"]) as conn:
        rows = _rows(conn, args)

        # Group by calendar day, with a human label per group
        grouped: dict[str, list] = {}
        for r in rows:
            d = dict(r)
            imgs = json.loads(d.get("local_images") or "[]")
            d["thumb"] = imgs[0] if imgs else None
            grouped.setdefault(d["starts_at"][:10], []).append(d)
        days = [{"date": k, "label": day_label(k, today), "events": v}
                for k, v in grouped.items()]

        weeks = [[{"date": d.isoformat(), "day": d.day,
                   "in_month": d.month == month.month, "is_today": d == today,
                   "events": grouped.get(d.isoformat(), [])}
                  for d in wk]
                 for wk in month_weeks(month)] if month else []

        hoods = conn.execute(
            "SELECT v.neighborhood, COUNT(*) n FROM event e JOIN venue v ON v.venue_key=e.venue_key "
            "WHERE e.is_canonical=1 AND v.neighborhood IS NOT NULL "
            "GROUP BY 1 ORDER BY v.neighborhood").fetchall()
        # One row per venue_key -- the raw venue_name has many spellings per
        # venue ("PETRAS BAR", "Petra's", "Petras Bar") and listing them all
        # made the dropdown look broken even though the filter worked.
        venues = conn.execute(
            "SELECT e.venue_key, COALESCE(v.display_name, MIN(e.venue_name)) AS label, "
            "COUNT(*) AS n FROM event e LEFT JOIN venue v ON v.venue_key = e.venue_key "
            "WHERE e.venue_key != '' AND e.is_canonical = 1 "
            "GROUP BY e.venue_key ORDER BY label").fetchall()
        accounts = conn.execute(
            "SELECT handle FROM account WHERE is_polled = 1 ORDER BY handle").fetchall()
        review_count = conn.execute(
            "SELECT COUNT(*) FROM event WHERE needs_review = 1 AND is_canonical = 1 "
            "AND is_hidden = 0").fetchone()[0]

        pending_count = len(discovery.pending(conn))

    return render_template(
        "index.html", days=days, total=len(rows), venues=venues, accounts=accounts,
        pending_count=pending_count, hoods=hoods,
        zip_failed=bool((request.args.get("zip") or "").strip()
                        and request.args.get("radius")
                        and _zip_center(request.args["zip"].strip()) is None),
        categories=CATEGORIES, review_count=review_count, args=args,
        today=today.isoformat(),
        cfg=config.load(), configured=config.exists(),
        view=view, weeks=weeks, month=month,
        month_label=f"{month:%B %Y}" if month else "",
        prev_month=f"{shift_month(month, -1):%Y-%m}" if month else "",
        next_month=f"{shift_month(month, 1):%Y-%m}" if month else "",
        weekday_names=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    )


@app.post("/event/<int:event_id>/<action>")
def act(event_id: int, action: str):
    field = {"confirm": "is_confirmed", "hide": "is_hidden", "flag": "needs_review"}.get(action)
    if not field:
        return ("unknown action", 400)
    with db.session(app.config["DB"]) as conn:
        # Toggle, so the same button undoes it
        cur = conn.execute(f"SELECT {field} FROM event WHERE id=?", (event_id,)).fetchone()
        if cur is None:
            return ("no such event", 404)
        new = 0 if cur[0] else 1
        reason = "manually flagged" if (field == "needs_review" and new) else None
        if field == "needs_review":
            conn.execute("UPDATE event SET needs_review=?, review_reason=? WHERE id=?",
                         (new, reason, event_id))
        else:
            conn.execute(f"UPDATE event SET {field}=? WHERE id=?", (new, event_id))
    return redirect(request.referrer or url_for("index"))


# --- settings -------------------------------------------------------------
# Non-secret configuration only. API keys are set by `cli init` and never
# accepted here: this app binds 0.0.0.0 with no auth so a phone on the LAN can
# reach it, which makes any key field on it a key field for the whole network.
# Showing whether a key is *present* is fine; showing or accepting one is not.

@app.route("/settings", methods=["GET", "POST"])
def settings():
    import os

    cfg = config.load()
    error = saved = None

    if request.method == "POST":
        form = request.form
        new = {"radius_miles": cfg["radius_miles"], "timezone": cfg["timezone"],
               "country": (form.get("country") or cfg["country"]).strip()}

        try:
            new["radius_miles"] = max(1.0, min(500.0, float(form.get("radius_miles"))))
        except (TypeError, ValueError):
            pass

        tz = (form.get("timezone") or "").strip()
        if tz and not config.is_valid_timezone(tz):
            error = f"'{tz}' is not an IANA timezone name (try 'America/Denver', not 'MDT')."
        elif tz:
            new["timezone"] = tz

        city = (form.get("city") or "").strip()
        if not error and city and city != cfg["city"]:
            center = geo.geocode_city(city)
            if center is None:
                error = f"Could not find '{city}'. Add a state or country and try again."
            else:
                new["city"], (new["lat"], new["lon"]) = city, center

        if not error:
            cfg = config.save(new)
            _ZIP_CACHE.clear()   # radius/centre changed -- stale hits would lie
            saved = True

    return render_template(
        "settings.html", cfg=cfg, error=error, saved=saved,
        configured=config.exists(),
        keys=[(name, label, bool(os.getenv(name)), url)
              for name, label, url in config.API_KEYS],
    )


# --- fetch-now jobs -------------------------------------------------------
# Polling takes minutes -- an Apify scrape plus a vision call per surviving post,
# for one account or for the whole rotation -- so it cannot happen inside the
# request. A worker thread does the work, the page polls /jobs, and single-flight
# keeps two expensive runs (and two SQLite writers) from overlapping.
#
# Single-flight is per process: cron's `run-once` is a separate process and is not
# covered by it. WAL plus busy_timeout keep that safe, just not free.
#
# This is the one place the web UI spends money. It is cost-capped by `limit`
# per account, and the estimate is shown before you click.

JOB: dict = {"state": "idle", "handle": None, "label": "", "message": "", "stats": None}
_JOB_LOCK = threading.Lock()
FETCH_LIMIT = 20
POST_COST = 0.002   # ~$2.00 per 1,000 posts, same figure ApifySource quotes


def _fetch_worker(handles: list[str], db_path: str, limit: int) -> None:
    from .extract import Extractor
    from .sources import ApifySource

    def progress(msg):
        JOB["message"] = str(msg)

    try:
        source = ApifySource(os.environ["APIFY_TOKEN"])
        with db.session(db_path) as conn:
            # Same window cron uses: posts newer than the oldest last_polled_at
            # across the batch, so a refresh only pays for what it has not seen.
            newer_than, _ = runner.fetch_window(conn, handles)
            stats = runner.poll(conn, source, Extractor(), handles, limit,
                                newer_than=newer_than, log=progress)
        JOB.update(state="done", stats=stats,
                   message=f"{stats['ingested']} new posts, "
                           f"{stats['processed'].get('events', 0)} events")
    except Exception as exc:
        # Surfaced in the UI rather than only in the server log -- a fetch that
        # silently did nothing is worse than one that says why.
        JOB.update(state="error", message=f"{type(exc).__name__}: {exc}")
    finally:
        if JOB["state"] == "running":
            JOB.update(state="error", message="worker exited without a result")


def _start_fetch(handles: list[str], label: str):
    """Claim the single job slot and hand the batch to a worker thread."""
    missing = [k for k, _, _ in config.API_KEYS if not os.getenv(k)]
    if missing:
        return redirect(url_for("discover", err="no-keys"))

    with _JOB_LOCK:
        if JOB["state"] == "running":
            return redirect(url_for("discover", err="busy"))
        JOB.update(state="running", handle=handles[0] if len(handles) == 1 else None,
                   label=label, message="starting...", stats=None)

    threading.Thread(target=_fetch_worker, daemon=True,
                     args=(handles, app.config["DB"], FETCH_LIMIT)).start()
    return redirect(url_for("discover"))


@app.post("/discover/<handle>/fetch")
def fetch_now(handle: str):
    if not HANDLE_RE.match(handle):
        return redirect(url_for("discover", err="bad-handle"))
    return _start_fetch([handle], f"@{handle}")


@app.post("/discover/fetch-all")
def fetch_all():
    """Refresh the whole rotation -- the same work cron's `run-once` does.

    Deliberately not routed as /discover/all/fetch: HANDLE_RE accepts "all",
    so that URL would be ambiguous with a real account named @all.
    """
    with db.session(app.config["DB"]) as conn:
        handles = discovery.approved_handles(conn)
    if not handles:
        # fetch_recent([]) would still bill a run, so never let it start.
        return redirect(url_for("discover", err="nothing-polled"))
    return _start_fetch(handles, f"{len(handles)} accounts")


@app.route("/jobs")
def jobs():
    """Polled by /discover so the button can report progress without a reload."""
    return JOB


@app.route("/discover")
def discover():
    """Review queue. Nothing enters the poll rotation without a click here."""
    # A finished job is reported once and then cleared, so its banner does not
    # follow you around on every later visit to this page.
    job = dict(JOB)
    if job["state"] in ("done", "error"):
        JOB.update(state="idle", handle=None, label="", message="", stats=None)

    with db.session(app.config["DB"]) as conn:
        discovery.stage(conn, discovery.rank_tagged(conn))
        polled = conn.execute(
            "SELECT handle, display_name, avatar_file FROM account WHERE is_polled=1 "
            "ORDER BY handle").fetchall()
        return render_template("discover.html", queue=discovery.pending(conn),
                               polled=polled, added=request.args.get("added"),
                               approved=request.args.get("approved"),
                               err=request.args.get("err"), job=job,
                               fetch_limit=FETCH_LIMIT,
                               fetch_cost=FETCH_LIMIT * POST_COST,
                               fetch_all_cost=len(polled) * FETCH_LIMIT * POST_COST)


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


@app.post("/discover/add")
def add_account():
    """Add a handle by hand. Goes straight into the rotation -- an explicit add
    is its own approval."""
    raw = (request.form.get("handle") or "").strip().lstrip("@")
    # Accept a pasted profile URL as well as a bare handle
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", raw)
    if m:
        raw = m.group(1)
    if not HANDLE_RE.match(raw):
        return redirect(url_for("discover", err="bad-handle"))

    with db.session(app.config["DB"]) as conn:
        conn.execute(
            "INSERT INTO account (handle, is_polled, seen_count, discovery_source, "
            "added_at, status, proposed_reason) VALUES (?,1,0,'manual',datetime('now'),"
            "'approved','added by hand') ON CONFLICT(handle) DO UPDATE SET "
            "is_polled=1, status='approved'", (raw,))
    return redirect(url_for("discover", added=raw))


@app.post("/discover/<handle>/<decision>")
def decide(handle: str, decision: str):
    if decision not in ("approve", "reject"):
        return ("unknown decision", 400)
    with db.session(app.config["DB"]) as conn:
        discovery.decide(conn, handle, approve=(decision == "approve"))
    # Approving used to be silent, which read as nothing having happened -- the
    # account is not polled until the next scheduled run. Offer the fetch instead.
    if decision == "approve":
        return redirect(url_for("discover", approved=handle))
    return redirect(url_for("discover"))


# --- ICS ------------------------------------------------------------------
# Additive, not an alternative to the UI (SPEC section 9): the same filters
# apply, so you can subscribe to a slice and still browse the whole thing.

def _ics_escape(text: str | None) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def _fold(line: str) -> str:
    """RFC 5545 caps lines at 75 octets; continuations start with a space."""
    out, cur = [], line
    while len(cur.encode()) > 75:
        cut = 74
        while len(cur[:cut].encode()) > 75:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


@app.route("/calendar.ics")
def calendar_ics():
    with db.session(app.config["DB"]) as conn:
        rows = _rows(conn, request.args)

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//social-calendar//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Social Calendar"]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    tz = config.load()["timezone"]
    for r in rows:
        start = r["starts_at"]
        if r["start_time_known"] and "T" in start:
            d = dt.datetime.fromisoformat(start)
            dtstart = f"DTSTART;TZID={tz}:{d:%Y%m%dT%H%M%S}"
            dtend = f"DTEND;TZID={tz}:{d + dt.timedelta(hours=2):%Y%m%dT%H%M%S}"
        else:
            day = dt.date.fromisoformat(start[:10])
            dtstart = f"DTSTART;VALUE=DATE:{day:%Y%m%d}"
            dtend = f"DTEND;VALUE=DATE:{day + dt.timedelta(days=1):%Y%m%d}"

        desc = " | ".join(x for x in [
            r["price_text"],
            f"source: @{r['attributed_handle']}" if r["attributed_handle"] else None,
            r["permalink"],
            "NEEDS REVIEW: " + (r["review_reason"] or "") if r["needs_review"] else None,
        ] if x)

        title = r["title"] or "(untitled)"
        if r["needs_review"]:
            title = "⚠ " + title

        lines += [
            "BEGIN:VEVENT",
            f"UID:sc-{r['id']}@social-calendar",
            f"DTSTAMP:{stamp}",
            dtstart, dtend,
            _fold(f"SUMMARY:{_ics_escape(title)}"),
            _fold(f"LOCATION:{_ics_escape(r['venue_name'])}"),
            _fold(f"DESCRIPTION:{_ics_escape(desc)}"),
            f"URL:{r['permalink'] or ''}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")

    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=social-calendar.ics"})


CSV_COLUMNS = ["date", "time", "title", "venue", "neighborhood", "address",
               "category", "price", "source_account", "confirmed", "needs_review",
               "review_reason", "link"]


@app.route("/events.csv")
def events_csv():
    """The same filtered view as the page, as a spreadsheet.

    Google Sheets, Excel and Numbers all open CSV directly. Filters ride along
    in the query string exactly as they do for the .ics feed, so the download
    matches whatever the page is showing. (Sheets' =IMPORTDATA() cannot reach
    this URL -- Google's servers would have to resolve it, and we bind to
    localhost/Tailscale. Import the downloaded file instead.)
    """
    with db.session(app.config["DB"]) as conn:
        rows = _rows(conn, request.args)

    # utf-8-sig: Excel assumes the local codepage without a BOM and mangles
    # every accented venue name. Sheets ignores the BOM.
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        start = r["starts_at"] or ""
        w.writerow([
            start[:10],
            start[11:16] if r["start_time_known"] and "T" in start else "",
            r["title"] or "",
            r["venue_name"] or "",
            r["neighborhood"] or "",
            r["address"] or "",
            r["category"] or "",
            r["price_text"] or "",
            f"@{r['attributed_handle']}" if r["attributed_handle"] else "",
            "yes" if r["is_confirmed"] else "",
            "yes" if r["needs_review"] else "",
            r["review_reason"] or "",
            r["permalink"] or "",
        ])

    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=social-calendar.csv"})


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--port", type=int, default=8730)
    # 0.0.0.0 so the phone can reach it over Tailscale/LAN. Single user, no auth.
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    app.config["DB"] = args.db
    print(f"http://localhost:{args.port}   ICS: http://localhost:{args.port}/calendar.ics")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
