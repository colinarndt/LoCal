"""User-authored display details for events found from external sources.

The source title remains intact in ``event.title`` so polling and dedupe can
continue to reason about what was published. A correction lives in a separate
override and is selected only at display/export time. Notes live alongside it.
"""

from __future__ import annotations

import sqlite3


TITLE_MAX = 200
NOTES_MAX = 4000


def _single_line(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())
    return text[:limit] if text else None


def _notes(value: str | None) -> str | None:
    text = (value or "").strip()
    return text[:NOTES_MAX] if text else None


def update_event(conn: sqlite3.Connection, event_id: int, form) -> bool:
    """Save the title shown and notes for a found event.

    Hand-entered events have a richer editor and remain fenced off here. An
    empty title, or the source title verbatim, removes the override.
    """
    existing = conn.execute(
        "SELECT title FROM event WHERE id=? AND is_manual=0",
        (event_id,),
    ).fetchone()
    if existing is None:
        return False

    shown_title = _single_line(form.get("title"), TITLE_MAX)
    source_title = existing["title"]
    override = None if not shown_title or shown_title == source_title else shown_title
    conn.execute(
        "UPDATE event SET title_override=?,notes=? WHERE id=?",
        (override, _notes(form.get("notes")), event_id),
    )
    return True
