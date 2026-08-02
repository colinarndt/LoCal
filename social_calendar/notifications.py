"""Queued local notifications for newly qualifying performer dates."""

from __future__ import annotations

import datetime as dt
import sqlite3


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


def deliver_pending(conn: sqlite3.Connection, sender=_send_macos) -> int:
    delivered = 0
    for row in conn.execute(
            "SELECT * FROM alert_delivery WHERE delivered_at IS NULL ORDER BY id").fetchall():
        if sender(row):
            conn.execute("UPDATE alert_delivery SET delivered_at=? WHERE id=?", (_now(), row["id"]))
            delivered += 1
    return delivered
