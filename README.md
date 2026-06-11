# X Ticker Scraper

A web app that scrapes posts from X (Twitter) accounts, extracts US stock ticker mentions, and enriches them with real-time price data and sector information.

## What It Does

- Scrapes posts from one or more X accounts using Playwright (headless browser)
- Extracts US stock tickers using both `$CASHTAG` patterns and plain uppercase symbols, validated against the SEC's official ticker list
- Enriches results with live price data (via yfinance)
- Tracks which accounts mentioned each ticker
- Streams progress to the browser via Server-Sent Events (SSE)
- Saves every scan as a JSON file in `output/` and shows the 10 most recent runs on the dashboard
- Supports per-user watchlists

## Project Structure

```
.
├── app.py               # Flask web server — API routes and SSE streaming
├── scraper.py           # Playwright-based X scraper with session caching
├── ticker_extractor.py  # Ticker detection with blocklist filtering
├── tickers_db.py        # SEC ticker list loader with 7-day local cache
├── price_lookup.py      # yfinance price fetcher with 5-min cache
├── sector_lookup.py     # yfinance sector/industry fetcher with 30-day cache
├── requirements.txt
├── start.sh             # Convenience launch script
├── templates/
│   └── index.html       # Frontend dashboard
├── data/                # Local caches (auto-created)
│   ├── us_tickers_cache.json
│   ├── price_cache.json
│   ├── sector_cache.json
│   └── watchlists.json
└── output/              # Scan results (auto-created)
```

## Prerequisites

- Python 3.10+
- An X (Twitter) account with credentials

## Setup

### 1. Create and activate a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure credentials

Create a `.env` file in the project root:

```env
X_USERNAME=your_x_username
X_PASSWORD=your_x_password
```

### 4. Cache your X session (first time only)

X may require interactive verification (unusual-activity check or 2FA) on the first login. Run this once from a terminal to complete login and save the session:

```bash
python3 -c "import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())"
```

A browser window will open. After completing any verification, the session is saved to `session.json` and subsequent runs are fully headless.

## Running the App

```bash
./start.sh
```

Or manually:

```bash
source venv/bin/activate
python3 app.py
```

Then open **http://localhost:8080** in your browser.

## Testing

Offline regression tests cover parser behavior, scan lifecycle safety, secure
session-file writes, and persistence semantics. They do not scrape X or call
Yahoo Finance.

```bash
source venv/bin/activate
python3 test_scraper_parse.py
python3 test_safety_regressions.py
```

GitHub Actions runs the same offline checks on push and pull request.

## Usage

1. Enter one or more X usernames (comma or newline separated) in the dashboard
2. Set the number of posts to fetch per account (default 10, max 200)
3. Optionally set a `since_date` to only analyze posts after a given date
4. Click **Scan** — progress streams in real time
5. Results show tickers ranked by mention count, with price, % change, and source posts

## Caching

| Data | Cache Location | TTL |
|---|---|---|
| US ticker list (SEC) | `data/us_tickers_cache.json` | 7 days |
| Stock prices | `data/price_cache.json` | 5 minutes |
All cache writes are atomic (temp file + rename) to prevent corruption.

## Output Files

Each scan is saved to `output/` as a timestamped JSON file, e.g.:

```
output/20260528_160933_Mr_Derivatives.json
```

The file contains the full scan: accounts analyzed, settings, per-account posts and tickers, combined ticker counts, and price data.
