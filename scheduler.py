"""
scheduler.py — background auto-scan scheduler with macOS notifications.

Scans all saved watchlist accounts automatically:
  - Every 60 min during NYSE market hours  (Mon–Fri 09:30–16:00 ET)
  - Every 6 hours outside market hours
  - Sends a native macOS notification when any ticker is mentioned by 2+
    distinct accounts

Import-safe: app.py wraps the import in try/except so any failure here
never prevents the Flask server from starting.
"""
import asyncio
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MARKET_OPEN      = (9, 30)    # NYSE open
MARKET_CLOSE     = (16, 0)    # NYSE close
INTERVAL_MARKET  = 60 * 60    # 1 hour during market hours
INTERVAL_OFF     = 6 * 60 * 60  # 6 hours outside market hours
MIN_ACCOUNTS     = 2          # minimum distinct accounts to trigger notification

WATCHLISTS_FILE = Path(__file__).parent / "data" / "watchlists.json"

_state: dict = {
    "enabled": True,
    "last_scan_at": None,   # unix timestamp
    "next_scan_at": None,   # unix timestamp
    "last_tickers": [],     # [{ticker, accounts, total_mentions}]
    "last_error": None,
}
_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_market_hours(dt=None) -> bool:
    """Return True if NYSE is currently open (Mon–Fri 09:30–16:00 ET)."""
    now = dt or datetime.now(ET)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def _next_interval() -> int:
    return INTERVAL_MARKET if is_market_hours() else INTERVAL_OFF


def _load_watchlist_accounts() -> list:
    """Return deduplicated list of all accounts across every saved watchlist."""
    try:
        with open(WATCHLISTS_FILE) as f:
            wl = json.load(f)
        return list({a for accs in wl.values() for a in accs})
    except Exception:
        return []


def _notify(tickers: list) -> None:
    """Send a native macOS notification listing the qualified tickers."""
    if not tickers:
        return
    parts = [f"${t['ticker']} ({t['accounts']} accts)" for t in tickers[:4]]
    body = ", ".join(parts)
    if len(tickers) > 4:
        body += f" +{len(tickers) - 4} more"
    title = f"X Monitor · {len(tickers)} signal{'s' if len(tickers) != 1 else ''}"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Ping"'],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass  # notifications are best-effort; never crash the scheduler


# ── Public API ────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Return a snapshot of the current scheduler state (thread-safe)."""
    with _lock:
        return dict(_state)


def set_enabled(value: bool) -> None:
    with _lock:
        _state["enabled"] = value


# ── Scan logic ────────────────────────────────────────────────────────────────

def _run_scan() -> list:
    """
    Scrape today's posts for all watchlist accounts, extract tickers, and
    return those mentioned by MIN_ACCOUNTS or more distinct accounts.
    """
    from scraper import scrape_accounts
    from ticker_extractor import extract_tickers
    from tickers_db import load_tickers

    accounts = _load_watchlist_accounts()
    if not accounts:
        return []

    since = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    scraped = asyncio.run(
        scrape_accounts(accounts, count=40, since_date=since, progress=None)
    )

    valid_tickers = load_tickers()
    combined: dict = {}
    for username, data in scraped.items():
        if data.get("error"):
            continue
        for t in extract_tickers(data["posts"], valid_tickers):
            entry = combined.setdefault(
                t["ticker"],
                {"ticker": t["ticker"], "account_set": set(), "total_mentions": 0},
            )
            entry["account_set"].add(username)
            entry["total_mentions"] += t["mentions"]

    qualified = [
        {
            "ticker": sym,
            "accounts": len(e["account_set"]),
            "total_mentions": e["total_mentions"],
        }
        for sym, e in combined.items()
        if len(e["account_set"]) >= MIN_ACCOUNTS
    ]
    qualified.sort(key=lambda x: (-x["accounts"], -x["total_mentions"]))
    return qualified


# ── Background loop ───────────────────────────────────────────────────────────

def _loop() -> None:
    while True:
        interval = _next_interval()
        with _lock:
            _state["next_scan_at"] = time.time() + interval

        time.sleep(interval)

        with _lock:
            enabled = _state["enabled"]

        if not enabled:
            continue

        with _lock:
            _state["last_scan_at"] = time.time()
            _state["last_error"] = None

        try:
            tickers = _run_scan()
            with _lock:
                _state["last_tickers"] = tickers
            _notify(tickers)
        except Exception as exc:
            with _lock:
                _state["last_error"] = str(exc)


def start() -> None:
    """Start the background scheduler daemon thread. Safe to call multiple times."""
    t = threading.Thread(target=_loop, daemon=True, name="auto-scan-scheduler")
    t.start()
    print("[✓] Auto-scan scheduler started")
