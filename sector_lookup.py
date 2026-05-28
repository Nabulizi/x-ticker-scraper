import json
import os
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SECTOR_CACHE = Path(__file__).parent / "data" / "sector_cache.json"
CACHE_TTL = 30 * 24 * 3600  # 30 days

_cache_lock = threading.Lock()


def _fetch_sector_one(ticker: str) -> tuple:
    """Fetch sector/industry/company for one ticker. Returns (ticker, data_dict)."""
    try:
        import yfinance as yf
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            info = yf.Ticker(ticker).info
        return ticker, {
            "sector":   info.get("sector")    or "Unknown",
            "industry": info.get("industry")  or "Unknown",
            "company":  info.get("shortName") or info.get("longName") or ticker,
            "_ts": time.time(),
        }
    except Exception:
        return ticker, {
            "sector": "Unknown", "industry": "Unknown",
            "company": ticker, "_ts": time.time(),
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


def lookup_sectors(tickers: list) -> dict:
    """
    Return {ticker: {sector, industry, company}} for a list of tickers.
    Uses parallel yfinance fetching and a local 30-day cache.
    Cache reads/writes are protected by a threading.Lock.
    """
    SECTOR_CACHE.parent.mkdir(exist_ok=True)

    with _cache_lock:
        cache = {}
        if SECTOR_CACHE.exists():
            try:
                with open(SECTOR_CACHE) as f:
                    raw = json.load(f)
                now = time.time()
                cache = {k: v for k, v in raw.items() if now - v.get("_ts", 0) < CACHE_TTL}
            except Exception:
                cache = {}

        to_fetch = [t for t in tickers if t not in cache]

    if to_fetch:
        print(f"[→] Looking up sector info for {len(to_fetch)} ticker(s) (parallel)...")
        workers = min(len(to_fetch), 15)
        new_data: dict = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_sector_one, t): t for t in to_fetch}
            for future in as_completed(futures):
                ticker, data = future.result()
                new_data[ticker] = data

        with _cache_lock:
            # Re-read cache in case another thread wrote while we were fetching
            if SECTOR_CACHE.exists():
                try:
                    with open(SECTOR_CACHE) as f:
                        cache = json.load(f)
                except Exception:
                    pass
            cache.update(new_data)
            _write_cache_atomic(SECTOR_CACHE, cache)
        print("[✓] Sector lookup complete")

    result = {}
    for ticker in tickers:
        d = cache.get(ticker, {})
        result[ticker] = {
            "sector":   d.get("sector",   "Unknown"),
            "industry": d.get("industry", "Unknown"),
            "company":  d.get("company",  ticker),
        }
    return result
