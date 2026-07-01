# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# First-time setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# One-time interactive X login (opens a browser window)
python3 -c "import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())"

# Run the app
./start.sh
# or: source venv/bin/activate && python3 app.py
# Opens at http://localhost:5000
```

There are no automated tests or linter configs in this project.

## Architecture

### Data flow

```
POST /scrape
  → scraper.py (Playwright, async)   — fetches raw posts from X per account
  → ticker_extractor.py              — two-pass extraction + signal scoring
  → price_lookup.py                  — yfinance fast_info prices (5-min cache)
  → app.py combine step              — merges per-account results, ranks tickers
  → store.py (optional SQLite)       — time-series persistence
  → build_digest()                   — velocity/acceleration/sentiment summary
  → SSE stream → browser
```

### Scraper (`scraper.py`)
Playwright-based with anti-detection hardening: strips `AutomationControlled`, patches `navigator.webdriver` and WebGL fingerprints, prefers real Chrome over bundled Chromium. Session is cached to `session.json`; raises `SessionExpired` / `InteractiveLoginRequired` when it's stale. The `/connect-x` endpoint triggers `_manual_login()` to re-authenticate visibly.

### Ticker extraction (`ticker_extractor.py`)
Two-pass design:
- **Pass 1**: collect all `$CASHTAG` symbols used by the account (the corroboration set).
- **Pass 2**: accept `$CASHTAGS` (high confidence) and plain ALL-CAPS tokens **only if the same symbol appeared as a cashtag** (corroboration). This is the fix for false positives like `MC`/`PM` — blocklisting alone can't handle real tickers used in non-ticker context.

### Scoring (`signals.py`)
Each occurrence gets `sentiment` (lexicon-based, negation-aware) and `conviction` (is the ticker the *subject* of the post or a trailing engagement tag?). `signal_weight` combines these: cashtag=1.0 base, plain=0.45, trailing tag ×0.2, has price/% thesis ×1.15.

### Ranking (`app.py` combine step)
Final ranking is **distinct accounts → signal_score → total mentions**. Raw mention count is intentionally not the primary sort key — it lets single-account cashtag spam flood the results. `low_confidence` flags single-account, no-cashtag tickers for de-ranking on the frontend.

### Store (`store.py`)
Optional SQLite layer at `data/scraper.db`. Imported inside `try/except` in `app.py` — any failure is non-fatal. Enables:
- `/velocity/<ticker>?days=N` — daily mention counts + first-mention timing
- `store.update_forward_returns()` — backfills 1d/5d/20d returns for the scorecard (run on a schedule)

Post de-duplication is by permalink URL; falls back to a content hash if no URL.

### Caching
All cache writes are atomic (temp file + `os.replace`). Cache locations:
- `data/us_tickers_cache.json` — SEC ticker list, 7-day TTL
- `data/price_cache.json` — yfinance prices, 5-min TTL
- `data/profile_cache.json` — sector/industry, 30-day last-known-good (failed fetches never overwrite a good entry)
- `data/scraper.db` — SQLite time series

### SSE streaming
`/scrape` starts a daemon thread and returns a `scan_id`. The browser connects to `/scan/stream/<scan_id>` to receive progress events. Reconnecting clients receive the final result immediately if the scan already finished (stored in `_scans[scan_id]["final"]`).
