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

---

# UI/UX improvements (2026-06-07)

## 10. `templates/index.html` — typography upgrade

Loaded **Fira Code** (headings/tickers) and **Fira Sans** (body) from Google
Fonts. Ticker symbols (`.ticker-accent`) now render in a monospaced face,
making prices and symbols scannable at a glance. Body text uses a clean
sans-serif matched to the data-dashboard style.

## 11. `templates/index.html` — accessibility pass

- **Focus rings:** all `<input>`, `<select>`, and `<textarea>` elements had
  `focus:outline-none` with no replacement; added `focus:ring-2
  focus:ring-sky-500/50` so keyboard users always see where they are.
- **Form semantics:** input card wrapped in `<form onsubmit>` — pressing Enter
  in the accounts textarea now submits the scan. `<label for="usernames">`
  linked to the textarea.
- **ARIA roles:** `role="tablist"` / `role="tab"` / `aria-selected` on the
  Posts / Ranked Tickers tabs; `scope="col"` on all `<th>` elements.
- **Live regions:** `role="alert"` + `aria-live="assertive"` on the error box
  (with a dismiss button); `role="alert"` on the session-expired banner;
  `aria-live="polite"` + `aria-label` on the scan progress log.
- **Semantic elements:** recent-run `<a href="#">` links replaced with
  `<button type="button">` elements.

## 12. `templates/index.html` — emoji → SVG icons

All structural emoji replaced with inline Lucide SVGs defined in a shared
`SVG` constant map. Affected locations:

| Was | Now |
|-----|-----|
| `📋` Daily Digest button & panel header | ClipboardList SVG |
| `🆕` New today section label | Star SVG |
| `🚀` Accelerating section label | TrendingUp SVG |
| `🎯` Top conviction section label | Target SVG |
| `♥ ↺ 💬 👁` post engagement stats | Heart / Repeat / MessageSquare / Eye SVGs |
| `⚠` suspicious price & digest error | AlertTriangle SVG |

SVGs are theme-aware (`stroke="currentColor"`), scale cleanly, and can be
styled with design tokens — unlike emoji which are font-dependent and
inconsistent across OS/browser.

## 13. `templates/index.html` — reduced-motion support

- `@media (prefers-reduced-motion: reduce)` CSS rule disables the
  `animate-spin` loader for users with vestibular/motion sensitivity.
- All `scrollIntoView({ behavior: 'smooth' })` calls now check
  `window.matchMedia('(prefers-reduced-motion: reduce)')` and fall back to
  `'auto'` when the OS setting is on.

## 14. `templates/index.html` — sortable ranked table

Ranked Tickers table columns (Ticker, Signal, Mood, Price, Change, Mentions,
Accounts) are now clickable to sort ascending or descending. Sort direction
is shown with a ▲/▼ indicator in the active column header. State is held in
`_sortState` and re-renders via the extracted `renderCombinedTable()` function
without re-fetching data.

## 15. `templates/index.html` — interaction improvements

- **Watchlist delete:** browser `confirm()` dialog replaced with an inline
  two-tap pattern — first click shows `✓?` on the × button for 2.5 s; a
  second click within that window commits the delete. No modal, no focus
  disruption.
- **Watchlist name input:** `event.preventDefault()` added to the Enter
  handler so the outer scan form is not inadvertently submitted.

---

# Scraper reliability & performance (2026-06-07)

## 16. `scraper.py` — retry on transient timeline load failures

**Problem:** a single `PWTimeout` immediately raised a user-facing error, even
when X had just returned a blank or loading page due to a momentary render
hiccup rather than a real account issue.

**Fix:** `_fetch_posts` now attempts one automatic page reload before raising,
but only when the body does not contain a clear permanent-failure signal
(`doesn't exist`, `account suspended`, `protected`). This eliminates most
false-positive "Could not load timeline" errors from transient X render
failures.

Also extracted `_timeline_load_error()` to produce richer, cause-specific
messages:

| Body signal | Error shown to user |
|---|---|
| doesn't exist / account suspended | Account not found or suspended |
| protected | Account has protected tweets |
| rate limit / try again later | X temporarily rate-limited the timeline |
| something went wrong / reload | X temporarily failed to render the timeline |
| log in / sign in | Session is no longer fully authenticated |

## 17. `market_data.py` — profile fetches opt-in via `include_profiles`

**Problem:** every `get_market_data()` call fetched yfinance `.info` profiles
(sector, industry, company name) for any ticker not in the 30-day cache. Since
sector enrichment is no longer shown in the dashboard, this was burning Yahoo
rate-limit quota on every scan for no user-visible benefit.

**Fix:** profile lookups are now gated behind `include_profiles=True` (default
`False`). Regular price-only scans skip `.info` entirely, keeping them fast and
reducing rate-limit exposure. The `need_profile` list, `_run_profiles()` call,
and profile cache write are all skipped unless the flag is set.

## 18. `sector_lookup.py` — pass `include_profiles=True`

Updated the one call site that genuinely needs full profiles (the dedicated
`lookup_sectors()` function) to pass `include_profiles=True`, preserving
existing behaviour for any caller that uses sector data explicitly.

---

# Auto-scan scheduler with macOS notifications (2026-06-07)

## 19. `scheduler.py` (new) — background auto-scan daemon

Eliminates the need to manually trigger hourly scans. A background daemon
thread runs a full watchlist scan automatically and sends a native macOS
notification when anything worth seeing surfaces.

**Schedule:**
- Every **60 minutes** during NYSE market hours (Mon–Fri 09:30–16:00 ET)
- Every **6 hours** outside market hours and on weekends

**Notification threshold:** only fires when a ticker is mentioned by **2 or
more distinct accounts** in the same scan window — single-account noise is
silently ignored.

**Notification format (macOS):**
```
X Monitor · 3 signals
$NVDA (4 accts), $AAPL (2 accts), $AMD (2 accts)
```

**Design notes:**
- Uses `osascript` for native macOS notifications — zero new dependencies
- Reads all saved watchlists at scan time (picks up any accounts you've added
  since startup without requiring a restart)
- Import-safe: `app.py` wraps the import in `try/except` so a failure here
  never prevents the Flask server from starting
- `get_status()` / `set_enabled()` for runtime control via the API

## 20. `app.py` — scheduler wiring + control endpoints

- Imports `scheduler` (guarded, optional)
- `scheduler.start()` called at app launch alongside the ticker DB preload
- `GET /auto-scan/status` — returns `enabled`, `seconds_until_next`,
  `last_tickers`, `last_error`, `market_hours` flag
- `POST /auto-scan/toggle` — enable or disable without restarting the app

## 21. `templates/index.html` — auto-scan status badge

A live countdown badge in the header shows the state of the scheduler at a
glance and lets you toggle it without opening any settings panel:

| State | Badge |
|---|---|
| Enabled, market hours | `● Auto · 47m` (green) |
| Enabled, off-hours | `● Auto · 3h 12m` (gray) |
| Disabled | `○ Auto · off` (dim) |

The badge polls `/auto-scan/status` every 30 seconds so the countdown stays
accurate. Clicking it toggles the scheduler on or off immediately.

## 22. `scheduler.py` + `app.py` + `templates/index.html` — market session badge

**Problem:** the original badge showed only a countdown (`Auto · 5h 59m`),
giving no indication of *why* the interval was long — was it off-hours, a
weekend, or disabled?

**Fix:** added `market_session()` to `scheduler.py` returning one of four
labels based on the current ET time, exposed via `/auto-scan/status`, and
updated the badge to display both the label and a colour:

| Session | Badge text | Colour |
|---|---|---|
| NYSE open (09:30–16:00 ET, weekday) | `● Market open · next 47m` | Green |
| Before open (weekday < 09:30 ET) | `● Pre-market · next 23m` | Amber |
| After close (weekday ≥ 16:00 ET) | `● After hours · next 5h` | Amber |
| Saturday or Sunday | `● Weekend · next 5h 59m` | Gray |
| Scheduler disabled | `○ Auto-scan off` | Dim |
