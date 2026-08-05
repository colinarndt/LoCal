"""SPEC section 5.5 -- the weekday validator.

Free, deterministic, no model call. The extraction schema makes the model quote
the flyer and show its resolution in `date_reasoning`. Where that text asserts a
weekday for the EVENT, the resolved date must fall on it. If it doesn't, one of
the two was misread and the record cannot be trusted.

This catches the failure the whole design exists to prevent: a confidently wrong
date, indistinguishable from a right one in the UI.
"""

from __future__ import annotations

import datetime as dt
import re

WEEKDAYS = {
    d.lower(): i
    for i, d in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    )
}
_WEEKDAY_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I
)


def _date_of(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def check_weekday(
    starts_at: str | None, posted_at: str | None, date_reasoning: str | None
) -> tuple[bool, str | None]:
    """Return (needs_review, reason).

    A weekday matching the POST date is an aside about publication, not a claim
    about the event -- the model routinely writes "Published Friday 2026-07-24,
    so August 25 resolves to...". Excluding those is load-bearing: without it
    this check reported a 5% failure rate that was entirely artifact.

    Known limitation: when the event's asserted weekday happens to equal the
    post's weekday (~1 in 7), the aside and the claim are indistinguishable and
    the check fails OPEN. Preferred over the alternative, which reintroduces the
    artifact and mislabels correct extractions.
    """
    event = _date_of(starts_at)
    if event is None:
        return True, "no resolvable start date"

    claims = {w.lower() for w in _WEEKDAY_RE.findall(date_reasoning or "")}
    if not claims:
        return False, None  # nothing asserted; nothing to contradict

    posted = _date_of(posted_at)
    if posted is not None:
        claims = {c for c in claims if WEEKDAYS[c] != posted.weekday()}
    if not claims:
        return False, None  # every weekday mentioned was the publication date

    if any(WEEKDAYS[c] == event.weekday() for c in claims):
        return False, None

    named = ", ".join(sorted(claims))
    return True, (
        f"weekday mismatch: {event.isoformat()} is a {event.strftime('%A')}, "
        f"but the extraction asserts {named}"
    )


def sanity_window(
    starts_at: str | None, posted_at: str | None, max_ahead_days: int = 400
) -> tuple[bool, str | None]:
    """Flag dates resolved into the past or absurdly far ahead of the post."""
    event, posted = _date_of(starts_at), _date_of(posted_at)
    if event is None or posted is None:
        return False, None
    delta = (event - posted).days
    if delta < -1:  # -1 tolerates timezone slop on same-day posts
        return True, f"event date {event} precedes post date {posted}"
    if delta > max_ahead_days:
        return True, f"event date {event} is {delta} days after the post"
    return False, None


def validate(record: dict) -> tuple[bool, str | None]:
    """Run all checks. First failure wins."""
    for check in (
        lambda: check_weekday(
            record.get("starts_at"), record.get("posted_at"), record.get("date_reasoning")
        ),
        lambda: sanity_window(record.get("starts_at"), record.get("posted_at")),
    ):
        flagged, reason = check()
        if flagged:
            return True, reason
    return False, None
