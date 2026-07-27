"""Dedup tests. Fixtures are verbatim strings from the Phase 0 corpus."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from social_calendar.dedupe import (
    detect_recurrence, detect_run, expand_recurring, group_events,
    normalize_venue, title_similarity,
)


# --- venue normalization: the real Phase 0 variants -------------------------

def test_amos_apostrophe_variants_collapse():
    assert normalize_venue("Amos' Southend") == normalize_venue("Amos Southend")


def test_milestone_club_variants_collapse():
    keys = {normalize_venue(v) for v in
            ["The Milestone Club", "The Milestone", "Milestone", "The World Famous MILESTONE club"]}
    assert len(keys) == 1, keys


def test_bare_camp_maps_to_camp_north_end():
    assert normalize_venue("Camp") == normalize_venue("Camp North End")


def test_petras_variants_collapse():
    assert normalize_venue("Petra's") == normalize_venue("PETRAS BAR") == normalize_venue("Petras")


def test_theatre_spelling_collapses():
    assert normalize_venue("Neighborhood Theatre") == normalize_venue("Neighborhood Theater")


def test_distinct_venues_stay_distinct():
    keys = {normalize_venue(v) for v in
            ["The Comedy Zone", "The Evening Muse", "Optimist Hall", "Camp North End"]}
    assert len(keys) == 4


def test_empty_venue_is_empty_key():
    assert normalize_venue(None) == "" and normalize_venue("") == ""


# --- title similarity -------------------------------------------------------

def test_partial_bill_matches_full_bill():
    a = "DustBowlChampion with Night Ritualz and Computer Kill"
    b = 'Dustbowl Champion "Be The Cowboy" Tour 2026'
    assert title_similarity(a, b) >= 0.6


def test_unrelated_titles_do_not_match():
    assert title_similarity("Pauly Shore", "Shop Local Sundays") < 0.6


# --- grouping: the two real duplicate cases ---------------------------------

def test_dustbowl_three_posts_collapse_to_one_event():
    evs = [
        {"post_id": "a", "title": "Dustbowl Champion", "starts_at": "2026-07-21T19:30:00",
         "venue_name": "The Evening Muse", "posted_at": "2026-07-18"},
        {"post_id": "b", "title": 'Dustbowl Champion "Be The Cowboy" Tour 2026',
         "starts_at": "2026-07-21T19:30:00", "venue_name": "The Evening Muse",
         "posted_at": "2026-07-20", "price_text": "$15"},
        {"post_id": "c", "title": "DustBowlChampion with Night Ritualz and Computer Kill",
         "starts_at": "2026-07-21", "venue_name": "Evening Muse", "posted_at": "2026-07-24"},
    ]
    group_events(evs)
    assert len({e["dedupe_group"] for e in evs}) == 1
    assert sum(e["is_canonical"] for e in evs) == 1


def test_mike_epps_same_night_collapses_across_posts():
    # Two posts, same opening night -> one event with two sources
    evs = [
        {"post_id": "a", "title": "Mike Epps", "starts_at": "2026-07-24",
         "venue_name": "The Comedy Zone", "posted_at": "2026-07-16"},
        {"post_id": "b", "title": "Mike Epps", "starts_at": "2026-07-24",
         "venue_name": "The Comedy Zone", "posted_at": "2026-07-21"},
    ]
    group_events(evs)
    assert len({e["dedupe_group"] for e in evs}) == 1


def test_different_nights_stay_separate():
    # Occurrence-based storage: each night is its own event
    evs = [
        {"post_id": "a", "title": "Mike Epps", "starts_at": "2026-07-24",
         "venue_name": "The Comedy Zone", "posted_at": "2026-07-16"},
        {"post_id": "b", "title": "Mike Epps", "starts_at": "2026-07-25",
         "venue_name": "The Comedy Zone", "posted_at": "2026-07-25"},
    ]
    group_events(evs)
    assert len({e["dedupe_group"] for e in evs}) == 2


def test_undated_events_never_merge():
    evs = [{"post_id": "a", "title": "X", "starts_at": None, "venue_name": "Y"},
           {"post_id": "b", "title": "X", "starts_at": None, "venue_name": "Y"}]
    group_events(evs)
    assert evs[0]["dedupe_group"] != evs[1]["dedupe_group"]


def test_canonical_prefers_richer_record():
    evs = [
        {"post_id": "sparse", "title": "Show", "starts_at": "2026-08-01",
         "venue_name": "Petra's", "posted_at": "2026-07-01"},
        {"post_id": "rich", "title": "Show", "starts_at": "2026-08-01",
         "venue_name": "Petra's", "posted_at": "2026-07-02",
         "price_text": "$10", "start_time_known": True},
    ]
    group_events(evs)
    assert next(e for e in evs if e["is_canonical"])["post_id"] == "rich"


# --- series -----------------------------------------------------------------

def test_detect_weekly_recurrence():
    r = detect_recurrence("Flyer states 'EVERY WEDNESDAY NIGHT'")
    assert r and r["weekday"] == 2


def test_detect_ordinal_recurrence():
    r = detect_recurrence("Every First and Third Sunday of the month at Petri's.")
    assert r and r["weekday"] == 6 and r["ordinals"] == [1, 3]


def test_detect_run_expands_each_night():
    dates = detect_run("Flyer reads 'JULY 24-26'", 2026)
    assert dates == [dt.date(2026, 7, 24), dt.date(2026, 7, 25), dt.date(2026, 7, 26)]


def test_run_rejects_implausible_range():
    assert detect_run("JULY 3-30", 2026) is None


def test_recurring_expansion_is_strictly_forward():
    """The Phase 0 bug: an Open Mic posted 07-24 resolved BACKWARD to 07-20."""
    out = expand_recurring({"weekday": 0}, dt.date(2026, 7, 24), weeks=4)
    assert all(d > dt.date(2026, 7, 24) for d in out)
    assert out[0] == dt.date(2026, 7, 27)  # the next Monday, not the previous one


def test_ordinal_expansion_picks_right_weeks():
    out = expand_recurring({"weekday": 6, "ordinals": [1, 3]}, dt.date(2026, 7, 19), weeks=8)
    assert dt.date(2026, 8, 2) in out       # first Sunday of August
    assert dt.date(2026, 8, 9) not in out   # second Sunday -- excluded


def test_ocr_typo_snaps_to_known_venue():
    """Vision misreads venue names off flyers: Phase 0 produced 'Petral's'."""
    assert normalize_venue("Petral's") == normalize_venue("Petra's")


def test_unknown_venue_is_not_absorbed():
    """A venue we do not know must keep its own key rather than being snapped
    onto a superficially similar known one."""
    assert normalize_venue("Some Brand New Room") not in {
        normalize_venue(v) for v in ["Petra's", "The Milestone", "Camp North End"]}
