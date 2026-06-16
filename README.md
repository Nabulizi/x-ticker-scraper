# X Ticker Scraper

A web app that scrapes posts from X (Twitter) accounts, extracts US stock ticker mentions, and enriches them with real-time price data and signal scoring.

## What It Does

- Scrapes posts from one or more X accounts using Playwright (headless browser)
- Extracts US stock tickers using both `$CASHTAG` patterns and plain uppercase symbols, validated against the SEC's official ticker list
- Enriches results with live price data (via yfinance)
- Tracks which accounts mentioned each ticker and ranks by conviction, not raw counts
- Streams progress to the browser via Server-Sent Events (SSE)
- Saves every scan as a JSON file in `output/` and shows the 10 most recent runs on the dashboard
- Persists every scan (manual, auto, and CLI) to a SQLite time series powering velocity sparklines, "new today" flags, and an account scorecard with forward returns
- Auto-scans saved watchlists hourly during market hours, with macOS and optional Telegram notifications
- Supports per-user watchlists and a terminal CLI (`scan.py`)

## Project Structure

```
.
├── app.py               # Flask web server — API routes and SSE streaming
├── pipeline.py          # Shared scan pipeline (combine/enrich/persist) + scrape lock
├── scraper.py           # Playwright-based X scraper with session caching
├── scheduler.py         # Background auto-scan loop + notifications
├── scan.py              # Terminal CLI — scan watchlists/accounts without the browser
├── ticker_extractor.py  # Ticker detection with blocklist filtering
├── signals.py           # Per-mention sentiment + conviction scoring
├── store.py             # SQLite time series (velocity, first-seen, scorecard)
├── tickers_db.py        # SEC ticker list loader with 7-day local cache
├── market_data.py       # Batched yfinance price fetcher with 5-min cache
├── price_lookup.py      # Thin adapter over market_data
├── import_cookies.py    # Build session.json from exported browser cookies
├── install_launchd.sh   # Install as a macOS login agent (auto-start)
├── launchd/             # launchd plist template
├── requirements.txt
├── start.sh             # Convenience launch script
├── templates/
│   └── index.html       # Frontend dashboard
├── data/                # Local caches + SQLite DB (auto-created)
│   ├── us_tickers_cache.json
│   ├── price_cache.json
│   ├── scraper.db
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

## Deploying to Render

This repo includes a `render.yaml` Blueprint and Dockerfile for Render. The
Docker image uses the Playwright Python runtime, runs the app with Gunicorn, and
mounts a 1 GB persistent disk at `/app/data` so your SQLite history, caches,
watchlists, scan output, and X session survive restarts.

In Render, create the service from the Blueprint and set the prompted secrets:

```env
X_USERNAME=your_x_username_or_email
X_PASSWORD=your_x_password
X_EMAIL=optional_email_for_extra_X_verification
```

By default, anyone with the Render URL can open and use the dashboard. If you
later want to make the dashboard private, set `APP_PASSWORD` in Render; the
username defaults to `admin` unless you also set `APP_USERNAME`.

The deployed service uses:

```env
XTS_SESSION_FILE=/app/data/session.json
XTS_OUTPUT_DIR=/app/data/output
XTS_CONNECT_HEADLESS=1
```

With `XTS_CONNECT_HEADLESS=1`, the dashboard's reconnect button uses
credential-based headless login instead of trying to open a visible browser in
the Render container.

Keep one Gunicorn worker for this app. The scraper uses one X session plus a
single background scheduler, and multiple worker processes would each try to run
their own scheduler.

### Auto-start on login (macOS)

```bash
./install_launchd.sh          # install + start as a login agent
./install_launchd.sh remove   # uninstall
```

This keeps the app (and the hourly auto-scan scheduler) running whenever you
are logged in, restarting it if it crashes. Logs go to `data/launchd.log`.

## CLI

Run a scan from the terminal without opening the dashboard:

```bash
python3 scan.py mywatchlist                    # scan a saved watchlist
python3 scan.py some_account other_account     # scan accounts directly
python3 scan.py --count 30 --since 2026-06-10  # all watchlists, custom depth
python3 scan.py --list-watchlists
```

CLI scans go through the same pipeline as the dashboard, including persistence
to the time-series DB.

## Notifications

Auto-scans notify when a ticker is mentioned by 2+ distinct watchlist accounts:

- **macOS notification** — always, best-effort.
- **Telegram** (optional, reaches your phone) — create a bot with
  [@BotFather](https://t.me/BotFather), get your chat id from
  [@userinfobot](https://t.me/userinfobot), then add to `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=123456789
```

Session-expiry alerts use the same channels, so you can reconnect before the
next market open.

## Testing

Offline regression tests cover parser behavior, scan lifecycle safety, the
shared pipeline, scheduler timing, secure session-file writes, and persistence
semantics. They do not scrape X or call Yahoo Finance.

```bash
source venv/bin/activate
python3 -m pytest -q test_*.py    # or run each test_*.py file directly
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
