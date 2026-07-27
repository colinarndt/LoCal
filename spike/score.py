#!/usr/bin/env python3
"""Phase 0, step 2: run one stage at one rung over the posts already on disk.

Reads from spike/posts/ and talks only to Anthropic -- it never re-hits the
scraper, so escalating a rung costs a rerun and nothing else.

    python3 spike/score.py --rung 1 --stage gate
    python3 spike/score.py --rung 1 --stage extract

--rung has no default on purpose. This script runs ONE rung and stops; it never
escalates on its own. If a rung misses the thresholds in SPEC.md section 3, that
is a decision for a human, not a retry loop.

Output: results/rung<N>_<stage>.csv with blank correct_*_yn columns for hand
scoring, plus results/raw/ holding each verbatim model response.

Untested against the live API -- no key was available when this was written.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import prompts

SPIKE_DIR = Path(__file__).parent
REPO_ROOT = SPIKE_DIR.parent
POSTS_DIR = SPIKE_DIR / "posts"
MEDIA_DIR = POSTS_DIR / "media"
RESULTS_DIR = SPIKE_DIR / "results"
RAW_DIR = RESULTS_DIR / "raw"

# The escalation ladder from SPEC.md section 3. Cheapest first.
RUNGS = {
    1: "claude-haiku-4-5",
    2: "claude-sonnet-5",
    3: "claude-opus-5",
}

# USD per million tokens (input, output), standard published rates.
# Sonnet 5 has an introductory $2/$10 rate through 2026-08-31; the standard
# rate is used here so estimates do not understate the steady state.
RATES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
}

IMAGE_TOKENS = 1600  # rough per-image cost, adequate for a pre-flight estimate


def load_posts() -> list[dict]:
    if not POSTS_DIR.exists():
        sys.exit(f"No posts on disk at {POSTS_DIR}. Run fetch.py first.")
    posts = [json.loads(p.read_text()) for p in sorted(POSTS_DIR.glob("*.json"))]
    if not posts:
        sys.exit(f"{POSTS_DIR} is empty. Run fetch.py first.")
    return posts


def gate_survivors(rung: int) -> set[str] | None:
    """Post ids the gate marked as candidates, from any previously scored rung."""
    for path in sorted(RESULTS_DIR.glob("rung*_gate.csv"), reverse=True):
        with path.open() as fh:
            keep = {
                row["post_id"]
                for row in csv.DictReader(fh)
                if row.get("is_event_candidate", "").lower() == "true"
            }
        print(f"Filtering to {len(keep)} gate survivors from {path.name}")
        return keep
    return None


def human_date(iso: str | None) -> str:
    if not iso:
        return "an unknown date"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"{dt:%Y-%m-%d} ({dt:%A})"
    except ValueError:
        return iso


def sniff_media_type(data: bytes) -> str:
    """Instagram serves webp behind .jpg URLs; the API rejects a wrong media_type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"  # fall back and let the API complain


def build_content(post: dict, stage: str) -> list[dict]:
    """The user turn: images first, then text -- images before text reads better."""
    if stage == "gate":
        caption = (post.get("caption") or "").strip() or "(no caption)"
        return [{"type": "text", "text": f"Caption:\n{caption}"}]

    content: list[dict] = []
    for name in post.get("local_images", []):
        path = MEDIA_DIR / name
        if not path.exists():
            continue
        data = path.read_bytes()
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": sniff_media_type(data),
                "data": base64.standard_b64encode(data).decode(),
            },
        })
    content.append({
        "type": "text",
        "text": prompts.extract_user_text(
            post.get("caption", ""), human_date(post.get("posted_at"))
        ),
    })
    return content


def estimate_cost(posts: list[dict], stage: str, model: str) -> float:
    sys_tokens = len(
        prompts.GATE_SYSTEM if stage == "gate" else prompts.EXTRACT_SYSTEM
    ) // 4
    out_tokens = 40 if stage == "gate" else 250
    in_rate, out_rate = RATES[model]

    total_in = total_out = 0
    for post in posts:
        n_img = 0 if stage == "gate" else len(post.get("local_images", []))
        total_in += sys_tokens + len(post.get("caption") or "") // 4 + n_img * IMAGE_TOKENS
        total_out += out_tokens

    return total_in / 1e6 * in_rate + total_out / 1e6 * out_rate


def call_model(client, model: str, stage: str, post: dict) -> dict:
    schema = prompts.GATE_SCHEMA if stage == "gate" else prompts.EXTRACT_SCHEMA
    system = prompts.GATE_SYSTEM if stage == "gate" else prompts.EXTRACT_SYSTEM

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": build_content(post, stage)}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )

    if resp.stop_reason == "refusal":
        return {"_error": "refusal", "_stop_details": str(resp.stop_details)}

    # With thinking enabled a thinking block can precede the text, so find the
    # first text block rather than indexing content[0].
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        return {"_error": f"no text block (stop_reason={resp.stop_reason})"}

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {"_error": f"unparseable JSON: {exc}", "_raw": text}


GATE_COLUMNS = [
    "post_id", "account", "posted_at", "post_url", "caption_preview",
    "is_event_candidate", "reason",
    "correct_yn",  # <- hand-scored
]

EXTRACT_COLUMNS = [
    "post_id", "account", "posted_at", "post_url",
    "is_event", "correct_is_event_yn",
    "title", "correct_title_yn",
    "starts_at", "correct_date_yn",
    "start_time_known", "correct_time_yn",
    "venue_name", "correct_venue_yn",
    "category", "price_text", "confidence", "date_reasoning", "raw_file",
]


def row_for(post: dict, result: dict, stage: str, raw_name: str) -> dict:
    base = {
        "post_id": post["post_id"],
        "account": post.get("account_handle", ""),
        "posted_at": post.get("posted_at", ""),
        "post_url": post.get("permalink", ""),
    }
    if stage == "gate":
        caption = (post.get("caption") or "").replace("\n", " ")
        return base | {
            "caption_preview": caption[:120],
            "is_event_candidate": result.get("is_event_candidate", ""),
            "reason": result.get("_error") or result.get("reason", ""),
            "correct_yn": "",
        }
    return base | {
        "is_event": result.get("is_event", ""),
        "correct_is_event_yn": "",
        "title": result.get("title") or "",
        "correct_title_yn": "",
        "starts_at": result.get("starts_at") or "",
        "correct_date_yn": "",
        "start_time_known": result.get("start_time_known", ""),
        "correct_time_yn": "",
        "venue_name": result.get("venue_name") or "",
        "correct_venue_yn": "",
        "category": result.get("category") or "",
        "price_text": result.get("price_text") or "",
        "confidence": result.get("confidence", ""),
        "date_reasoning": result.get("_error") or result.get("date_reasoning", ""),
        "raw_file": raw_name,
    }


def main() -> None:
    # .env.local wins over .env; absolute paths so cwd doesn't matter.
    load_dotenv(REPO_ROOT / ".env.local")
    load_dotenv(REPO_ROOT / ".env")

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rung", type=int, choices=[1, 2, 3], required=True,
                    help="1=haiku-4.5, 2=sonnet-5, 3=opus-5. No default by design.")
    ap.add_argument("--stage", choices=["gate", "extract"], required=True)
    ap.add_argument("--all", action="store_true",
                    help="extract: score every post, not just gate survivors")
    ap.add_argument("--limit", type=int, help="score only the first N posts")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    args = ap.parse_args()

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")

    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic not installed. Run: pip install -r requirements.txt")

    model = RUNGS[args.rung]
    posts = load_posts()

    if args.stage == "extract" and not args.all:
        keep = gate_survivors(args.rung)
        if keep is None:
            sys.exit(
                "No gate results found. Run --stage gate first, or pass --all to "
                "score extraction over every post."
            )
        posts = [p for p in posts if p["post_id"] in keep]

    if args.limit:
        posts = posts[: args.limit]
    if not posts:
        sys.exit("Nothing to score.")

    cost = estimate_cost(posts, args.stage, model)

    print(f"Rung:   {args.rung}  ({model})")
    print(f"Stage:  {args.stage}")
    print(f"Posts:  {len(posts)}")
    print(f"Est. cost: ~${cost:.2f}")
    if args.rung == 3:
        print("  note: Opus 5 thinks by default, so real output tokens -- and cost --")
        print("        will run above this estimate.")
    if args.rung > 1:
        print(f"\n  !! Rung {args.rung} is an ESCALATION. Only run it if rung "
              f"{args.rung - 1} was scored and missed the SPEC.md section 3 thresholds.")

    if not args.yes and input("\nProceed? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")

    client = anthropic.Anthropic(api_key=key)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    columns = GATE_COLUMNS if args.stage == "gate" else EXTRACT_COLUMNS
    out_path = RESULTS_DIR / f"rung{args.rung}_{args.stage}.csv"
    rows, errors = [], 0

    for i, post in enumerate(posts, 1):
        print(f"  [{i}/{len(posts)}] {post['post_id']}", end=" ", flush=True)
        try:
            result = call_model(client, model, args.stage, post)
        except Exception as exc:  # keep going; one bad post shouldn't end the run
            result = {"_error": f"{type(exc).__name__}: {exc}"}

        raw_name = f"rung{args.rung}_{args.stage}_{post['post_id']}.json"
        (RAW_DIR / raw_name).write_text(json.dumps(result, indent=2))

        if "_error" in result:
            errors += 1
            print(f"ERROR: {result['_error']}")
        else:
            print("ok")

        rows.append(row_for(post, result, args.stage, raw_name))

    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows ({errors} errors) to {out_path}")
    print(f"Raw responses in {RAW_DIR}")
    print(f"Prompt version: {prompts.PROMPT_VERSION}")
    print(
        "\nNext: hand-score the correct_*_yn columns, then compare against the\n"
        "SPEC.md section 3 thresholds. This script will not escalate on its own --\n"
        "if the rung fails, that is a conversation, not a rerun."
    )


if __name__ == "__main__":
    main()
