# Phase 0 spike

Answers one question: **is extraction good enough to build on?** See `SPEC.md`
§3 for the pass thresholds. If rung 1 clears them, stop — there is no reason to
pay for a bigger model.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in ANTHROPIC_API_KEY and APIFY_TOKEN
```

## Before spending anything

Open each handle in `accounts.txt` in a browser and confirm it exists and still
posts events. They are model recall, not a verified list — every dead handle is
a wasted paid request.

## Run order

```bash
# 1. Pull posts once. Costs Apify money. Downloads images locally so that
#    escalating a rung later never re-hits the scraper.
python3 fetch.py --accounts accounts.txt --limit 12

# 2. Gate at rung 1. Costs Anthropic money.
python3 score.py --rung 1 --stage gate

# 3. Extraction at rung 1, over the gate survivors.
python3 score.py --rung 1 --stage extract
```

Both scripts print a cost estimate and wait for confirmation before any billed
call. `--yes` skips the prompt.

## Then hand-score

Open `results/rung1_extract.csv`. Each extracted field is followed by a blank
`correct_*_yn` column — fill in `y` or `n` against the real post (the
`post_url` column is one click away). Compare the totals to the §3 thresholds.

When something is wrong, read the model's `date_reasoning` first, and the
verbatim response in `results/raw/` second. That usually distinguishes "the
model can't do this" from "the prompt is bad" — the second is free to fix and
rerun, since re-scoring never re-fetches.

## Escalating

`--rung` has no default and nothing auto-escalates. Rungs are
`1=haiku-4-5`, `2=sonnet-5`, `3=opus-5`. Only move up if the rung below was
scored and missed the thresholds — and confirm before spending on it.

The two stages ladder independently. The gate settling at rung 1 while
extraction climbs to rung 2 is the expected outcome, not a compromise.
