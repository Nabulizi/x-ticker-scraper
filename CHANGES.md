# x-ticker-scraper — data quality & signal upgrade

Summary of changes made to fix the extraction/enrichment bugs surfaced in the
sample scan and to turn raw mention counts into a usable signal.

## 1. Ticker extraction precision — `ticker_extractor.py` + new `signals.py`

**Bug:** the plain ALL-CAPS regex accepted any 2–5 letter token that happened to
be a real SEC symbol, so `"$13B MC"` → **MC** (Moelis) and `"down $100 from PM"`
→ **PM** (Philip Morris) became fake tickers. Blocklisting can't fix this —
they're real symbols.

**Fix (structural):**
- `$CASHTAGS` are high-confidence and accepted after validation.
- Plain ALL-CAPS tokens are accepted **only if the same symbol also appears as a
  cashtag in that account's posts** (corroboration). A bare `PM`/`MC` with no
  `$PM`/`$MC` anywhere is dropped.
- Share-class suffixes (`$HPS.A`) are preserved and down-weighted (the SEC list
  is US-only, so a suffix often signals a foreign/misresolved name).
- Every occurrence is tagged with `confidence` (cashtag/plain), `sentiment`,
  `sentiment_score`, `is_subject`/`is_trailing_tag`, and a `signal_weight`.
  Trailing cashtags (`$TSLA` tacked onto unrelated posts) are heavily discounted.

Verified on the real posts: MC and PM no longer appear; HPS keeps its `.A` class
and a reduced weight; sentiment is captured per mention.

## 2. Ranking — `app.py` combine step

**Bug:** results were ranked by raw mention count, so `$TSLA` (9 mentions, all
from one account appending it to unrelated posts) ranked #1.

**Fix:** combined tickers now rank by **distinct accounts → aggregate
signal_score → total mentions**. New per-ticker fields: `accounts`,
`signal_score`, `cashtag_mentions`, `net_sentiment`, `sentiment_label`,
`low_confidence`. On the sample data TSLA drops to #3 and the cross-account
names (NFLX, PLTR) rise to the top, with NFLX flagged bearish, PLTR bullish.

## 3. Enrichment reliability — new `market_data.py`, `price_lookup.py` /
`sector_lookup.py` now thin adapters

**Bug:** both modules called `yfinance.Ticker(t).info` (the flakiest, most
rate-limited endpoint) — twice per ticker, 15 workers each. Under throttling,
major names returned sector **"Unknown"** and bad prices (e.g. MU $925), and the
30-day sector cache **persisted those failures for a month**.

**Fix:**
- Price now comes from `fast_info` (lightweight, reliable); profile from `.info`
  only on cache miss.
- Profiles kept in a 30-day **last-known-good** memory that a failed fetch can
  **never overwrite** → no more poisoned "Unknown".
- Failures cached only ~10 min, so Unknowns self-heal next run.
- Concurrency dropped to 5 with retry/backoff.
- Sanity flag `suspicious` on absurd moves / non-positive prices, surfaced as
  `price_suspicious` on each enriched ticker.

`.info` is now fetched roughly **once per ticker per 30 days** instead of twice
per scan.

## 4. Richer scrape metadata — `scraper.py`

Each post now also captures (best-effort, fully guarded): `url` (status
permalink), `likes`, `reposts`, `replies`, `views`, and `is_repost`. Follower
count is captured once per account (`follower_count`). These enable
influence-weighting and a programmatic book-talking flag.

## 5. Time series + scorecard — new `store.py` + endpoints

Optional SQLite layer (`data/scraper.db`), called from `app.py` inside
try/except so it can never break a scan. Idempotent on post id (from permalink,
else content hash). Enables:
- `/velocity/<ticker>?days=N` — daily mention counts + first-mention-by-account.
- `/scorecard?min_calls=N` — accounts ranked by avg forward return (once
  `store.update_forward_returns()` is run on a schedule).

## Migration / ops notes
- New cache file `data/profile_cache.json` (old `sector_cache.json` is no longer
  used; safe to delete). `data/price_cache.json` format gained `ok`/`suspicious`
  fields — delete it once to avoid stale entries.
- New DB `data/scraper.db` is created automatically.
- No new dependencies: `sqlite3` is stdlib; `fast_info` is covered by the
  existing `yfinance` pin. Run `store.update_forward_returns()` via cron/
  APScheduler to populate the scorecard.
- Front-end (`templates/index.html`) untouched; it will keep working, and can
  optionally read the new fields (`accounts`, `signal_score`, `sentiment_label`,
  `low_confidence`, `price_suspicious`) and endpoints.

## Known limitation
The trailing-tag discount is a heuristic; a spam cashtag sharing a post with an
unrelated price (e.g. `"$MU down $100 ... $TSLA"`) can still get partial credit.
The distinct-account ranking is the primary defense against single-account spam.

---

# Bug fixes & hardening (2026-06-07)

## 6. `store.py` — missing `import time` (crash fix)

**Bug:** `update_forward_returns()` called `time.sleep()` but `time` was never
imported. Any attempt to backfill forward returns raised `NameError: name 'time'
is not defined` at runtime.

**Fix:** added `import time` to the module-level imports. Also removed a
redundant `from datetime import timedelta` that was re-imported locally inside
`update_forward_returns` — `timedelta` was already imported at the top of the
file.

## 7. `app.py` — input validation on `/velocity/<ticker>`

**Issue:** the ticker value from the URL was passed directly to the store
without any format check. An arbitrarily long or malformed string would hit the
DB with no guard.

**Fix:** added a `re.match(r'^[A-Z]{1,5}$', ticker.upper())` check that returns
400 before touching the DB. Consistent with the 1–5 character constraint on all
valid US equity symbols.

## 8. `signals.py` — deeper negation look-back

**Issue:** negation detection only checked the immediately preceding token, so
`"not going to rally"` still scored bullish (negation separated by one word).

**Fix:** now checks the two preceding tokens (`prev` and `prev2`). Catches
common patterns like `"not going to rally"`, `"never been bullish"`, and
`"no reason to buy"`.

## 9. `README.md` — removed stale sector references

Sector enrichment (`.info` lookups + `by_sector` grouping) was dropped in a
prior pass (see §3 above) but the README still described it. Removed:
- "Enriches results with … sector/industry info"
- "Groups tickers by sector"
- `sector_cache.json` row from the Caching table
- "sector" from the Usage step 5 and output file description
