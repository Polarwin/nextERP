# Plan: last-500-orders voice recognition improvement loop

Status: **proposal — not yet executed**

## Goal

Focus voice-recognition tuning on the vocabulary that actually occurs in the
business: the customers and items appearing in the **last 500 ERP Sales
Orders**. Generate randomized spoken orders from the **shortest spoken name**
of each of those customers/items, run them through the production Whisper +
fast-parse pipeline, harvest the mishearings into `learned_aliases.json`,
then regenerate fresh random data and re-measure — iterated until the match
rate plateaus.

## Why this differs from the existing suites

- `customers` suite tests all 734 dictionary names — most never occur in
  real orders; effort is spread thin.
- `recent` suite replays only the last 20 orders verbatim — tiny sample.
- `random` suite samples from the whole dictionary — same dilution problem.

The new suite (`random500`) keeps the random suite's realistic sentence
variety but restricts the customer/item pool to what the last 500 orders
actually used.

## Step 1 — fetch the order vocabulary (read-only, ~3 API calls)

New helper script `fetch_last500_orders.py` (or a `--fetch` mode in the eval
script):

1. `GET /api/resource/Sales Order` — fields `name, customer`, `order_by
   creation desc`, `limit_page_length=500`.
2. `GET /api/resource/Sales Order Item` — fields `parent, item_code, qty`,
   filter `parent in [<those 500 names>]`. One paginated call instead of 500
   per-document GETs (the ERP is in China; per-doc latency makes the naive
   approach take 10+ minutes).
3. Write `/tmp/last500_orders.json` in the same shape as the existing
   `/tmp/last20_orders.json`.

Nothing writes to ERPNext — reads only.

## Step 2 — build the pool

From the 500 orders:

- **Customers**: distinct customer IDs. For each, take the shortest spoken
  name from `customer_spoken_names.txt` ∪ cache `_voice_names`. Customers
  with no spoken name are skipped and **reported as dictionary gaps** (a
  useful by-product: it tells us which active customers need a dictionary
  entry).
- **Items**: distinct item codes. Shortest of `_item_spoken_names()` +
  curated aliases from `voice_aliases.json`. Spoken names shared across
  vintages resolve to the **newest vintage** (standing rule).

Expected pool size (to be confirmed at runtime): roughly 100–300 customers,
40–100 items.

## Step 3 — new eval suite `random500`

Extend `voice_customer_eval.py`:

- `random500_test_cases(count, seed)`: like `random_test_cases`, but samples
  customer × item from the step-2 pool (shortest names only), random
  quantity, random one of the existing 6 sentence patterns （要 optional,
  qty/item either order, 给…来…, paused commas).
- Own results file `voice_customer_eval/random500_results.json` so it never
  clobbers other suites.
- `--harvest` works unchanged — it already reads the active results file and
  applies the collision-safety rules (never alias a phrase that is another
  target's spoken name; shared phrases resolve by newest order / newest
  vintage).

## Step 4 — the improvement loop

```
Round 0:  seed=1, 200 sentences → baseline match rate
          classify failures (misheard / parse-punt / qty)
          --harvest  → review the skip list
Round 1:  seed=2, 200 NEW sentences → did the harvest generalize?
          harvest again if failures remain
Round 2:  seed=3, ... until plateau
```

Design decisions:

- **Fresh random data each round** (new seed), never re-test on the exact
  sentences we harvested from — that would measure memorization, not
  improvement. Audio is cached by sentence hash, so any overlap between
  rounds is still cheap.
- Optionally re-run the previous round's seed after harvesting (audio is
  cached, ASR-only, fast) to confirm the harvested aliases fixed those
  specific cases without hijacking others.
- **Regression guard**: after each harvest, spot-check that overall
  customers/items suite numbers didn't drop (a bad alias can hijack a real
  spoken name; the harvest skip-list prevents known cases, but we verify).
- **Stop condition**: match rate moves < 2 points between rounds, or the
  remaining failures are all (a) TTS pronunciation artifacts or (b)
  fast-parse punts that the production LLM fallback handles anyway.

## Step 5 — metrics and reporting

Per round, record in the results file and print:

- overall match rate, split into customer-match and item-match
- failure classification counts (misheard-badly / in-candidates /
  parse-returned-none / qty-wrong)
- harvested alias count + skip list
- list of active customers/items with no spoken name (dictionary gaps)

## Cost estimate

- TTS: ~200 new sentences per round, a few minutes at concurrency 8;
  cached forever by hash.
- ASR: Whisper small int8 on CPU ≈ 2–4 s/file → 10–15 min per round.
- Total for 3 rounds: well under an hour of compute, all local.

## Risks / caveats

- TTS (XiaoxiaoNeural) is a proxy for the user's real voice. Aliases learned
  from TTS mishearings may be TTS-specific; real-audio evidence from
  `recordings/` + `voice_log.jsonl` still trumps (see VOICE_TESTING.md).
- 200 sentences per round samples a pool of thousands of possible
  customer×item combinations — rates between rounds have sampling noise of a
  few points; that's why the stop condition uses a 2-point threshold, not
  any single dip.
- The eval still excludes the LLM fallback, so numbers remain a lower bound
  on production accuracy.

## Open questions before executing

1. Pool size: last 500 orders as proposed, or fewer/more?
2. Sentences per round: 200 (proposed) or 100 for faster iteration?
3. Do the 500-order fetch now (read-only, ~3 API calls to the China ERP)?
