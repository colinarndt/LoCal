"""Queued local notifications for newly qualifying performer dates.

Alerts are queued in `alert_delivery` and delivered by `deliver_pending`.
Delivery goes to ntfy when `NTFY_URL` is set (a headless install, e.g. the
Optiplex), otherwise to Notification Center when this process is the Mac app.
An alert that no sender accepts stays queued and is retried on the next pass.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import urllib.error
import urllib.request

from . import tenancy

NTFY_URL_ENV = "NTFY_URL"          # full topic URL, e.g. http://127.0.0.1:2586/optiplex
NTFY_TOKEN_ENV = "NTFY_TOKEN"      # access token (tk_...), optional for open servers
NTFY_TIMEOUT_SECONDS = 10


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def enqueue(conn: sqlite3.Connection, source_id: int, external_id: str,
            content_hash: str, kind: str, title: str, body: str,
            ticket_url: str | None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO alert_delivery "
        "(source_id,external_id,content_hash,kind,title,body,ticket_url,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (source_id, external_id, content_hash, kind, title, body, ticket_url, _now()))
    return bool(cur.rowcount)


def _send_macos(row) -> bool:
    """Send through Notification Center when this process is the Mac app."""
    try:
        from Foundation import NSDictionary
        from AppKit import NSUserNotification, NSUserNotificationCenter
    except Exception:
        return False
    note = NSUserNotification.alloc().init()
    note.setTitle_(row["title"])
    note.setInformativeText_(row["body"])
    note.setHasActionButton_(bool(row["ticket_url"]))
    note.setActionButtonTitle_("Get tickets")
    note.setUserInfo_(NSDictionary.dictionaryWithObject_forKey_(
        row["ticket_url"] or "", "ticket_url"))
    NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(note)
    return True


def _header(value: str) -> str:
    """HTTP headers are latin-1; keep event titles readable rather than failing."""
    return value.encode("latin-1", "replace").decode("latin-1")


def _send_ntfy(row, url: str | None = None, token: str | None = None) -> bool:
    """Publish one alert to an ntfy topic. False (stay queued) on any failure."""
    url = url or os.getenv(NTFY_URL_ENV)
    token = token if token is not None else os.getenv(NTFY_TOKEN_ENV)
    if not url:
        return False
    headers = {
        "Title": _header(row["title"]),
        "Tags": "tickets",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if row["ticket_url"]:
        headers["Click"] = row["ticket_url"]
        headers["Actions"] = f"view, Get tickets, {row['ticket_url']}"
    request = urllib.request.Request(
        url, data=row["body"].encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=NTFY_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _send_default(row) -> bool:
    """ntfy when configured, otherwise the Mac's Notification Center.

    ntfy is used only for the local single-user install: a hosted server's
    environment belongs to the operator, and one topic must never carry
    another user's alerts.
    """
    if os.getenv(NTFY_URL_ENV) and tenancy.current().is_local:
        return _send_ntfy(row)
    return _send_macos(row)


def deliver_pending(conn: sqlite3.Connection, sender=_send_default) -> int:
    delivered = 0
    for row in conn.execute(
            "SELECT * FROM alert_delivery WHERE delivered_at IS NULL ORDER BY id").fetchall():
        if sender(row):
            conn.execute("UPDATE alert_delivery SET delivered_at=? WHERE id=?", (_now(), row["id"]))
            delivered += 1
    return delivered
