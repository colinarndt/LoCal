"""Tests for the weekday validator.

The post-date-exclusion case is the important one: it is the bug that produced a
false 5% failure rate during Phase 0. The strings below are verbatim model output
from that run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar.validate import check_weekday, sanity_window, validate


def test_matching_weekday_passes():
    # 2026-07-09 really is a Thursday
    flagged, _ = check_weekday(
        "2026-07-09T20:00:00",
        "2026-07-07T12:00:00+00:00",
        "Flyer states 'THURSDAY 7/9@8PM'. Post published July 7, 2026 (Tuesday). "
        "July 9, 2026 is indeed a Thursday.",
    )
    assert flagged is False


def test_post_date_weekday_is_not_a_claim():
    """THE REGRESSION CASE.

    The only weekday named is Friday, and it describes the POST date. The event
    date 2026-08-25 is a Tuesday. A naive check flags this; it must not.
    """
    flagged, reason = check_weekday(
        "2026-08-25",
        "2026-07-24T12:00:00+00:00",  # a Friday
        "Caption states '@idkgreek comes through on August 25'. "
        "Published Friday 2026-07-24, so August 25 resolves to 2026-08-25.",
    )
    assert flagged is False, reason


def test_post_date_parenthetical_is_not_a_claim():
    # "post date of 2026-07-20 (Monday)" -- same artifact, different phrasing
    flagged, reason = check_weekday(
        "2026-10-09",
        "2026-07-20T09:00:00+00:00",  # a Monday
        "Caption states 'October 9'. Given the post date of 2026-07-20 (Monday), "
        "October 9 resolves to 2026-10-09.",
    )
    assert flagged is False, reason


def test_genuine_mismatch_is_flagged():
    # Flyer claims Friday but the date resolved to a Thursday -> untrustworthy.
    # Post date is a Wednesday, so the exclusion rule leaves "friday" standing.
    flagged, reason = check_weekday(
        "2026-08-20",  # a Thursday
        "2026-07-15T12:00:00+00:00",  # a Wednesday
        "The flyer reads 'FRIDAY, AUGUST 20TH'.",
    )
    assert flagged is True
    assert "Thursday" in reason and "friday" in reason.lower()


def test_fails_open_when_claim_collides_with_post_weekday():
    """Documented limitation, not a bug.

    The event is claimed to be a Friday and the date resolved to a Thursday --
    a genuine mismatch. But the post was ALSO made on a Friday, so exclusion
    cannot tell the claim from a publication aside and drops it. The check
    fails open (passes) rather than risking a false flag.

    Cost of the alternative: dropping the exclusion reintroduces the Phase 0
    artifact, which mislabelled 5 of 100 correct extractions. Failing open on
    a ~1-in-7 collision is the better trade.
    """
    flagged, _ = check_weekday(
        "2026-08-20",  # a Thursday
        "2026-07-17T12:00:00+00:00",  # also a Friday -> collision
        "The flyer reads 'FRIDAY, AUGUST 20TH'.",
    )
    assert flagged is False


def test_no_weekday_asserted_passes():
    flagged, _ = check_weekday("2026-08-14", "2026-07-01T00:00:00+00:00", "Flyer reads '8/14'.")
    assert flagged is False


def test_missing_date_is_flagged():
    flagged, reason = check_weekday(None, "2026-07-01T00:00:00+00:00", "unclear")
    assert flagged is True and "no resolvable" in reason


def test_same_day_post_passes():
    # "EVERY WEDNESDAY" posted on the Wednesday it refers to
    flagged, _ = check_weekday(
        "2026-07-15T21:00:00", "2026-07-15T14:00:00+00:00", "Flyer states 'EVERY WEDNESDAY NIGHT'."
    )
    assert flagged is False


def test_sanity_window_flags_past_event():
    flagged, reason = sanity_window("2026-05-01", "2026-07-20T00:00:00+00:00")
    assert flagged is True and "precedes" in reason


def test_sanity_window_allows_far_but_plausible():
    flagged, _ = sanity_window("2026-11-06", "2026-07-21T00:00:00+00:00")
    assert flagged is False


def test_validate_combines_checks():
    flagged, reason = validate(
        {
            "starts_at": "2026-05-01",
            "posted_at": "2026-07-20T00:00:00+00:00",
            "date_reasoning": "no weekday here",
        }
    )
    assert flagged is True and "precedes" in reason
