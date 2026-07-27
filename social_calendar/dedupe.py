"""Normalization, dedup, and series expansion. SPEC sections 4 and 5.4.

Storage is occurrence-based, so every dedup key is a single date -- no interval
matching. The venue normalizer is the load-bearing piece: dedup keys on it, and
the Phase 0 corpus produced `Amos' Southend`/`Amos Southend`,
`The Milestone Club`/`The Milestone`, and a bare `Camp` for Camp North End.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from difflib import SequenceMatcher

# Explicit aliases beat clever fuzzy matching for a single-city app: the set of
# venues is small, known, and the failure mode of over-eager matching (merging
# two real venues) is worse than the failure mode of missing one.
VENUE_ALIASES = {
    "camp": "camp north end",
    "camp north end": "camp north end",
    "milestone": "the milestone",
    "milestone club": "the milestone",
    "world famous milestone": "the milestone",
    "amos southend": "amos southend",
    "amos': southend": "amos southend",
    "petras": "petras",
    "petras bar": "petras",
    "petris": "petras",        # OCR misread; geocodes to the same address
    "petrals": "petras",
    "evening muse": "the evening muse",
    "comedy zone": "the comedy zone",
    "comedy zone charlotte": "the comedy zone",
    "the comedy zone charlotte": "the comedy zone",
    "neighborhood theatre": "neighborhood theatre",
    "neighborhood theater": "neighborhood theatre",
    "optimist hall": "optimist hall",
    "fillmore": "the fillmore",
    "fillmore charlotte": "the fillmore",
    "the fillmore charlotte": "the fillmore",
    "underground": "the underground",
    "underground charlotte": "the underground",
    "the underground charlotte": "the underground",
    "visulite": "visulite theatre",
    "visulite theater": "visulite theatre",
    "visulite theatre": "visulite theatre",
}

_STOPWORDS = {"the", "a", "an", "at", "in"}
_APOSTROPHE = re.compile(r"['’ʼ`]")
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def _base(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Apostrophes are ELIDED, not spaced: "Petra's" -> "petras", matching how
    # the same venue gets written as "PETRAS BAR" elsewhere. Spacing them would
    # produce "petra s" and defeat the alias table.
    text = _APOSTROPHE.sub("", text.lower())
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_venue(name: str | None) -> str:
    """Map a venue string onto a stable key. Empty string when unknown."""
    if not name:
        return ""
    b = _base(name)
    if not b:
        return ""
    if b in VENUE_ALIASES:
        return VENUE_ALIASES[b]

    tokens = [t for t in b.split() if t not in _STOPWORDS]
    stripped = " ".join(tokens)
    if stripped in VENUE_ALIASES:
        return VENUE_ALIASES[stripped]

    # Trailing generic nouns vary between postings ("The Milestone Club" vs
    # "The Milestone"), so try once without them before giving up.
    generic = {"club", "bar", "lounge", "venue"}
    while tokens and tokens[-1] in generic:
        tokens = tokens[:-1]
        cand = " ".join(tokens)
        if cand in VENUE_ALIASES:
            return VENUE_ALIASES[cand]

    key = " ".join(tokens) or stripped
    return _snap_to_known(key)


def _snap_to_known(key: str, threshold: float = 0.87) -> str:
    """Snap near-misses onto a known venue.

    Vision extraction misreads venue names off flyers -- Phase 0 produced
    "Petral's" for Petra's, which would otherwise get its own key and its own
    duplicate events. Only snaps to venues already in the alias table, so an
    unknown venue is never silently absorbed into a known one.
    """
    if not key or key in set(VENUE_ALIASES.values()):
        return key
    best, score = None, 0.0
    for canonical in set(VENUE_ALIASES.values()):
        r = SequenceMatcher(None, key, canonical).ratio()
        if r > score:
            best, score = canonical, r
    return best if score >= threshold else key


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    b = _base(title)
    # Promoter boilerplate carries no identity and blocks otherwise-clean
    # matches. But a title can consist ENTIRELY of these words ("Show"), and
    # stripping to empty would make it match nothing -- keep the base then.
    stripped = _WS.sub(
        " ",
        re.sub(r"\b(presents|live|tour|show|concert|w|with|feat|featuring)\b", " ", b),
    ).strip()
    return stripped or b


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0

    # Spacing is unreliable in the source: the same act appears as
    # "DustBowlChampion" and "Dustbowl Champion". Compare despaced too, so
    # tokenization differences do not sink an otherwise obvious match.
    da, db = na.replace(" ", ""), nb.replace(" ", "")
    if da == db:
        return 1.0
    # A shared distinctive prefix counts even when one posting lists the whole
    # bill and the other names only the headliner.
    if da.startswith(db) or db.startswith(da):
        return 0.95

    # Bills diverge after the headliner -- "DustBowlChampion with Night Ritualz"
    # vs 'Dustbowl Champion "Be The Cowboy" Tour'. Neither contains the other,
    # but they share a long head. Safe because this function is only ever called
    # within a single (date, venue) bucket, where a 12+ character shared opening
    # means the same act.
    shared = _common_prefix_len(da, db)
    if shared >= 12:
        return 0.9

    return max(
        SequenceMatcher(None, na, nb).ratio(),
        SequenceMatcher(None, da, db).ratio(),
    )


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def dedupe_key(starts_at: str | None, venue_name: str | None) -> str:
    day = (starts_at or "")[:10]
    return f"{day}|{normalize_venue(venue_name)}"


def group_events(events: list[dict], threshold: float = 0.6) -> list[dict]:
    """Assign `dedupe_group` and `is_canonical`.

    Bucket on the exact key (day + normalized venue), then split each bucket by
    fuzzy title. Events lacking a date are never grouped -- merging on a missing
    key would silently collapse unrelated records.
    """
    buckets: dict[str, list[dict]] = {}
    for ev in events:
        if not (ev.get("starts_at") or "")[:10]:
            ev["dedupe_group"] = f"solo:{ev.get('post_id')}"
            ev["is_canonical"] = 1
            continue
        buckets.setdefault(dedupe_key(ev.get("starts_at"), ev.get("venue_name")), []).append(ev)

    for key, bucket in buckets.items():
        clusters: list[list[dict]] = []
        for ev in bucket:
            for cluster in clusters:
                if title_similarity(ev.get("title"), cluster[0].get("title")) >= threshold:
                    cluster.append(ev)
                    break
            else:
                clusters.append([ev])

        for i, cluster in enumerate(clusters):
            gid = f"{key}#{i}"
            # Prefer the richest record as canonical: most fields populated,
            # tie-broken by the earliest post (the original announcement).
            cluster.sort(
                key=lambda e: (
                    -sum(1 for f in ("title", "venue_name", "price_text") if e.get(f)),
                    -int(bool(e.get("start_time_known"))),
                    e.get("posted_at") or "",
                )
            )
            for j, ev in enumerate(cluster):
                ev["dedupe_group"] = gid
                ev["is_canonical"] = 1 if j == 0 else 0
    return events


# --- series expansion ------------------------------------------------------

_WD = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def detect_recurrence(text: str | None) -> dict | None:
    """Recognize the recurring shapes the Phase 0 corpus actually contained."""
    if not text:
        return None
    t = _base(text)
    m = re.search(r"\bevery\s+(" + "|".join(_WD) + r")\b", t)
    if m:
        return {"kind": "recurring", "rule": f"every {m.group(1)}", "weekday": _WD.index(m.group(1))}
    m = re.search(r"\bfirst and third\s+(" + "|".join(_WD) + r")\b", t)
    if m:
        return {"kind": "recurring", "rule": f"first and third {m.group(1)}",
                "weekday": _WD.index(m.group(1)), "ordinals": [1, 3]}
    return None


def detect_run(text: str | None, year: int) -> list[dt.date] | None:
    """Parse a multi-night run such as 'JULY 24-26' into one date per night."""
    if not text:
        return None
    # NOT _base(): it replaces the hyphen with a space, which is the very
    # character that marks the range ("JULY 24-26" -> "july 24 26").
    t = _WS.sub(" ", text.lower().replace("–", "-").replace("—", "-"))
    m = re.search(r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\s*-\s*(\d{1,2})\b", t)
    if not m:
        return None
    month, a, b = _MONTHS[m.group(1)], int(m.group(2)), int(m.group(3))
    if not (1 <= a <= b <= 31) or b - a > 14:
        return None
    try:
        return [dt.date(year, month, d) for d in range(a, b + 1)]
    except ValueError:
        return None


def expand_recurring(rule: dict, after: dt.date, weeks: int = 8) -> list[dt.date]:
    """Occurrences strictly AFTER `after`, through the horizon.

    Strictly-after matters: Phase 0 caught the model resolving 'Open Mic' to the
    previous occurrence rather than the next one. Generation must not repeat it.
    """
    end = after + dt.timedelta(weeks=weeks)
    out, day = [], after + dt.timedelta(days=1)
    while day <= end:
        if day.weekday() == rule["weekday"]:
            if "ordinals" in rule:
                if ((day.day - 1) // 7) + 1 in rule["ordinals"]:
                    out.append(day)
            else:
                out.append(day)
        day += dt.timedelta(days=1)
    return out
