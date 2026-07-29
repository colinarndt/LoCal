"""Replay stored posts through the current model and diff against the old one.

    python eval_replay.py --stage gate --limit 40        # what it will cost
    python eval_replay.py --stage gate --limit 40 --yes  # actually run it

SPEC section 3 qualified `claude-haiku-4-5` against 160 hand-checked posts. That
grading lives in SPEC, not in the database -- the `correct_*_yn` columns on the
imported spike rows are empty placeholders -- so this cannot re-measure accuracy
against ground truth. What it measures is **agreement with the model already in
production**, on posts that model has already answered.

That is weaker than a correctness eval and stronger than nothing: Haiku's
accuracy on this corpus is known, so a replacement that tracks it closely
inherits the argument, and every row where the two disagree is precisely the row
worth reading by hand. Treat the disagreement list as the output, not the
percentage.

Costs money. Prints the estimate and refuses without --yes, for the same reason
`run-once` does.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from dotenv import load_dotenv

from social_calendar import db, extract, paths, spend

# Same order the CLI uses: .env.local wins over .env, and both lose to anything
# already exported in the shell.
load_dotenv(paths.ENV_LOCAL_PATH)
load_dotenv(paths.ENV_PATH)

BASELINE = "claude-haiku-4-5"

# Fields worth diffing, and how to compare them. Date is compared day-only: a
# differing timestamp on the same day is not the failure anyone cares about.
FIELDS = {
    "is_event": lambda v: bool(v),
    "starts_at": lambda v: (v or "")[:10],
    "venue_name": lambda v: (v or "").strip().lower(),
    "category": lambda v: (v or "").strip().lower(),
}


def _same(field: str, a, b) -> bool:
    """Whether two answers agree on one field.

    Venues get containment rather than equality: "The Fillmore" and "The
    Fillmore Charlotte" are one room, and scoring them apart measures how the
    two models punctuate rather than whether either found the venue. Everything
    else compares exactly.
    """
    norm = FIELDS[field]
    x, y = norm(a), norm(b)
    if x == y:
        return True
    if field == "venue_name" and x and y:
        return x in y or y in x
    return False


def baseline_rows(conn: sqlite3.Connection, stage: str, limit: int) -> list[dict]:
    """Posts the old model has already answered, newest first, with the media
    the extract stage needs."""
    rows = conn.execute(
        "SELECT e.post_id, e.raw_output, s.caption, s.posted_at, s.local_images "
        "FROM extraction e JOIN source_post s ON s.post_id = e.post_id "
        "WHERE e.stage = ? AND e.is_error = 0 AND e.model = ? "
        "GROUP BY e.post_id ORDER BY e.created_at DESC LIMIT ?",
        (stage, BASELINE, limit)).fetchall()
    out = []
    for r in rows:
        try:
            old = json.loads(r["raw_output"])
        except json.JSONDecodeError:
            continue
        out.append({
            "post_id": r["post_id"],
            "old": old,
            "post": {"post_id": r["post_id"], "caption": r["caption"] or "",
                     "posted_at": r["posted_at"],
                     "local_images": json.loads(r["local_images"] or "[]")},
        })
    return out


def compare(stage: str, old: dict, new: dict) -> list[str]:
    """Field names that disagree. Empty means the two models said the same thing."""
    if stage == "gate":
        a, b = bool(old.get("is_event_candidate")), bool(new.get("is_event_candidate"))
        return [] if a == b else ["is_event_candidate"]

    diffs = []
    for field in FIELDS:
        if not _same(field, old.get(field), new.get(field)):
            diffs.append(field)
        # Once the two disagree on whether this is an event at all, the
        # remaining fields are noise -- the old row's are unset by definition.
        if field == "is_event" and diffs:
            break
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("gate", "extract"), default="gate")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--rung", type=int, default=extract.DEFAULT_RUNG)
    ap.add_argument("--model", help="override the rung, e.g. gpt-5.4-nano")
    ap.add_argument("--yes", action="store_true", help="spend the money")
    ap.add_argument("--self", dest="self_check", action="store_true",
                    help="run the model against itself twice, to measure how much "
                         "of the disagreement below is just run-to-run variance")
    args = ap.parse_args()

    with db.read_session(paths.DB_PATH) as conn:
        rows = baseline_rows(conn, args.stage, args.limit)
    if not rows:
        print(f"No {args.stage} rows from {BASELINE} to replay.")
        return 1

    # Resolved before building a client: openai.OpenAI() raises without a key,
    # so constructing early would turn the cost estimate into a crash for
    # anyone who has not set one up yet.
    model = args.model or extract.RUNGS[args.rung]
    if model not in spend.OPENAI_PRICES:
        print(f"!! {model} is not in spend.OPENAI_PRICES -- it would log as $0.00")
        return 1

    # Rough, and deliberately over rather than under: gate is caption-only,
    # extract carries an image whose token cost is the thing we cannot predict.
    per_call = 0.002 if args.stage == "extract" else 0.0002
    print(f"{len(rows)} posts | {args.stage} | {model} vs {BASELINE}")
    print(f"estimated cost: ~${len(rows) * per_call:.2f}")
    if not args.yes:
        print("\nAdd --yes to run it.")
        return 0
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set.")
        return 1

    ex = extract.Extractor(rung=args.rung)
    ex.model = model

    agree, disagreements, errors = 0, [], []
    for i, row in enumerate(rows, 1):
        new = ex.run(row["post"], args.stage)
        # In self mode the comparison is this model against another run of
        # itself, so `baseline` is a second call rather than the stored row.
        baseline = ex.run(row["post"], args.stage) if args.self_check else row["old"]
        if "_error" in new or "_error" in baseline:
            errors.append((row["post_id"], new.get("_error") or baseline["_error"]))
        else:
            diffs = compare(args.stage, baseline, new)
            if diffs:
                disagreements.append((row["post_id"], diffs, baseline, new))
            else:
                agree += 1
        print(f"\r  {i}/{len(rows)}", end="", flush=True)

    scored = len(rows) - len(errors)
    label = "self-agreed" if args.self_check else "agreed"
    print(f"\n\n{label} on {agree}/{scored}"
          f"{f' ({100 * agree / scored:.0f}%)' if scored else ''}"
          f" | {len(errors)} errors")
    print(f"replay cost: ${sum(e['usd'] for e in ex.meter.events):.4f}")

    left = f"{ex.model} run A" if args.self_check else BASELINE
    right = f"{ex.model} run B" if args.self_check else ex.model
    for post_id, diffs, old, new in disagreements[:12]:
        print(f"\n  {post_id}  differs on {', '.join(diffs)}")
        for f in diffs:
            print(f"    {left:>22}: {old.get(f)!r}")
            print(f"    {right:>22}: {new.get(f)!r}")
    if len(disagreements) > 12:
        print(f"\n  ...and {len(disagreements) - 12} more")
    for post_id, err in errors[:5]:
        print(f"\n  {post_id}  ERROR {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
