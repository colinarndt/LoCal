"""Prompts and output schemas for the Phase 0 spike.

Versions are PER STAGE. The `extraction` table records prompt_version per row,
so a single global version would mislabel rows whose prompt never changed --
bumping the gate wording must not relabel extraction output.

Bump the relevant constant whenever that stage's prompt changes.
"""

GATE_PROMPT_VERSION = "gate-v3"      # v3: explicit hints for pre-vision dedupe
EXTRACT_PROMPT_VERSION = "extract-v3"   # v3: first-class theater category

def version_for(stage: str) -> str:
    return GATE_PROMPT_VERSION if stage == "gate" else EXTRACT_PROMPT_VERSION


def system_for(stage: str, city: str | None = None) -> str:
    """Fill the configured city into a stage's system prompt.

    The city is not decoration: it tells the model which venues and which local
    date conventions it is reading, and it was measured against real Charlotte
    posts in Phase 0. Substitution is a plain replace, not str.format, because
    the schemas and prompt bodies contain braces of their own.
    """
    from . import config

    tmpl = GATE_SYSTEM_TEMPLATE if stage == "gate" else EXTRACT_SYSTEM_TEMPLATE
    return tmpl.replace("{city}", city or config.load()["city"])


# --- Stage 1: the gate -------------------------------------------------------
# Text-only classification over the caption. Cheap, runs on every post, and is
# the primary noise filter. Only survivors reach the vision call.

GATE_SYSTEM_TEMPLATE = """\
You classify Instagram captions from {city} venues and local businesses.

Decide whether the post is announcing a specific, attendable event — a show, \
comedy night, market, opening, tasting, screening, or similar — that a person \
could put on a calendar.

Answer TRUE for: announcements of a dated happening, on-sale notices, lineup \
reveals, "tonight/this weekend" posts about something occurring at a place.

Answer FALSE for: merch, hiring posts, recaps or thank-yous about events that \
already happened, generic hours or menu posts, reposted memes, artist birthday \
or milestone posts, and anything with no attendable occasion behind it.

READ THE TENSE CAREFULLY. A "SOLD OUT", "at capacity", or "last tickets" notice \
is still an event announcement when the event has not yet happened. "Tonight's \
show is sold out" describes an upcoming event and is TRUE. Only call something a \
recap when it clearly looks backward — "thanks to everyone who came out", "what \
a night", photos captioned after the fact. Selling out is not the same as being \
over.

When a caption is ambiguous, lean TRUE — the extraction stage downstream can \
discard it, but anything rejected here is never seen again.

When the caption explicitly states an event title or headliner, date, venue, or \
event/ticket URL, return those as matching hints. Resolve relative dates against \
the publication date included with the caption and use ISO 8601. Use null for \
anything that is not explicit; these hints can cause the flyer image to be \
skipped, so a guess is worse than a missing value.\
"""

GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_event_candidate": {"type": "boolean"},
        "reason": {
            "type": "string",
            "description": "One short sentence justifying the call.",
        },
        "title_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "starts_at_hint": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "ISO 8601 date or datetime, only when explicit.",
        },
        "venue_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "event_url_hint": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["is_event_candidate", "reason", "title_hint", "starts_at_hint",
                 "venue_hint", "event_url_hint"],
    "additionalProperties": False,
}


# --- Stage 2: extraction -----------------------------------------------------
# Vision over the flyer images plus the caption. The post timestamp is passed
# in as an explicit, salient line — flyers routinely say "FRIDAY THE 13TH" or
# "THIS SATURDAY" with no year, and the timestamp is the only anchor that makes
# those resolvable.

EXTRACT_SYSTEM_TEMPLATE = """\
You extract structured event details from Instagram posts by {city} \
venues and businesses. The event details are often ONLY in the flyer image, \
not the caption text — read the images carefully.

Rules:

1. DATES. Flyers often give a relative or partial date ("THIS SATURDAY", \
"FRI THE 13TH", "8/14") with no year. Resolve these against the post's \
publication date, which is given to you. An event is almost always within a \
few weeks AFTER the post date. Never assume the current year without checking \
it against the post date and the weekday, if a weekday is given.

2. NULLS BEAT GUESSES. Every field may be null. A missing start time is a \
correct answer when no time is shown; an invented one is a wrong answer that \
looks right. Do not infer a venue from vibes — only report one that is stated \
or is unambiguously the posting account's own room.

3. CATEGORY. Choose the closest category, not the safest one. Plays, musicals, \
Broadway productions, and other staged dramatic work are theater, including \
comedy plays. Stand-up and improv shows are comedy. Concerts and live music — \
band, DJ, singer-songwriter, open mic — are music. A flyer naming a musical \
artist and a stage is music even when the caption never uses the word. \
Reserve "other" for events that genuinely resemble none of the listed kinds; it \
is the one answer nobody can filter by, so reaching for it when a category \
merely fits imperfectly makes the event harder to find than getting it slightly \
wrong would.

4. CONFIDENCE. Report your own confidence from 0 to 1. Be honest: a blurry \
flyer with a partial date deserves a low number.

5. REASONING. In date_reasoning, state in one or two sentences exactly how you \
arrived at starts_at — quote the text you read off the flyer and show the \
resolution. This is read by a human when the extraction is wrong.

Output ISO 8601 for starts_at (e.g. 2026-08-14T20:00:00 or 2026-08-14 when the \
time is unknown). Use the local date as written on the flyer; do not \
convert time zones.\
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_event": {
            "type": "boolean",
            "description": "False if this turns out not to be an event after seeing the images.",
        },
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "starts_at": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "ISO 8601. Date-only is fine when the time is unknown.",
        },
        "start_time_known": {
            "type": "boolean",
            "description": "True only if an explicit start time appeared in the source.",
        },
        "venue_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "category": {
            "anyOf": [
                {
                    "type": "string",
                    "enum": ["music", "theater", "comedy", "food", "market",
                             "art", "opening", "other"],
                },
                {"type": "null"},
            ]
        },
        "price_text": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": 'Verbatim, e.g. "$15 adv / $20 door", "free".',
        },
        "confidence": {"type": "number"},
        "date_reasoning": {"type": "string"},
    },
    "required": [
        "is_event",
        "title",
        "starts_at",
        "start_time_known",
        "venue_name",
        "category",
        "price_text",
        "confidence",
        "date_reasoning",
    ],
    "additionalProperties": False,
}


def extract_user_text(caption: str, posted_at_human: str) -> str:
    """Build the user turn. posted_at_human should read like '2026-07-18 (Saturday)'."""
    caption = (caption or "").strip() or "(no caption)"
    return (
        f"This post was published on {posted_at_human}.\n"
        f"Resolve any relative or partial date on the flyer against that publication date.\n\n"
        f"Caption:\n{caption}"
    )
