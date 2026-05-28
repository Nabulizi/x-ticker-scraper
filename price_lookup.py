import json
import os
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PRICE_CACHE = Path(__file__).parent / "data" / "price_cache.json"
CACHE_TTL = 5 * 60  # 5 minutes — prices are near real-time

_cache_lock = threading.Lock()


def _fetch_one(ticker: str) -> tuple:
    """Fetch price data for a single ticker. Returns (ticker, data_dict)."""
    try:
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            info = yf.Ticker(ticker).info

        price      = info.get("currentPrice") or info.get("regularMarketPrice") or None
        prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose") or None
        change_pct = info.get("regularMarketChangePercent")

        if change_pct is None and price and prev_close and prev_close != 0:
            change_pct = ((price - prev_close) / prev_close) * 100

        return ticker, {
            "price":        round(price, 2)                        if price is not None      else None,
            "prev_close":   round(prev_close, 2)                   if prev_close is not None else None,
            "change_abs":   round(price - prev_close, 2)           if (price and prev_close) else None,
            "change_pct":   round(change_pct, 2)                   if change_pct is not None else None,
            "currency":     info.get("currency", "USD"),
            "market_state": info.get("marketState", "UNKNOWN"),
            "_ts": time.time(),
        }
    except Exception:
        return ticker, {
            "price": None, "prev_close": None, "change_abs": None,
            "change_pct": None, "currency": "USD", "market_state": "UNKNOWN",
            "_ts": time.time(),
        }


def _write_cache_atomic(path: Path, data: dict) -> None:
    """Write JSON atomically via a temp file so a crash never corrupts the cache."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def lookup_prices(tickers: list) -> dict:
    """
    Return {ticker: {price, prev_close, change_abs, change_pct, currency, market_state}}
    Uses parallel fetching and a 5-minute local cache protected by a threading.Lock.
    """
    if not tickers:
        return {}

    PRICE_CACHE.parent.mkdir(exist_ok=True)

    with _cache_lock:
        cache = {}
        if PRICE_CACHE.exists():
            try:
                with open(PRICE_CACHE) as f:
                    raw = json.load(f)
                now = time.time()
                cache = {k: v for k, v in raw.items() if now - v.get("_ts", 0) < CACHE_TTL}
            except Exception:
                cache = {}

        to_fetch = [t for t in tickers if t not in cache]

    if to_fetch:
        print(f"[→] Fetching prices for {len(to_fetch)} ticker(s) (parallel)...")
        workers = min(len(to_fetch), 15)
        new_data: dict = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, t): t for t in to_fetch}
            for future in as_completed(futures):
                ticker, data = future.result()
                new_data[ticker] = data

        with _cache_lock:
            if PRICE_CACHE.exists():
                try:
                    with open(PRICE_CACHE) as f:
                        cache = json.load(f)
                except Exception:
                    pass
            cache.update(new_data)
            _write_cache_atomic(PRICE_CACHE, cache)
        print("[✓] Price fetch complete")

    result = {}
    for ticker in tickers:
        d = cache.get(ticker, {})
        result[ticker] = {
            "price":        d.get("price"),
            "prev_close":   d.get("prev_close"),
            "change_abs":   d.get("change_abs"),
            "change_pct":   d.get("change_pct"),
            "currency":     d.get("currency", "USD"),
            "market_state": d.get("market_state", "UNKNOWN"),
        }
    return result
