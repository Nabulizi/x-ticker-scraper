import json
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "data" / "us_tickers_cache.json"
SEC_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_TTL = 7 * 24 * 3600  # 7 days

_cache_lock = threading.Lock()


def _write_cache_atomic(path: Path, data) -> None:
    """Write JSON atomically via a temp file so a crash never corrupts the cache."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_tickers() -> set:
    """
    Return a set of valid US-listed ticker symbols, using a local 7-day cache.
    Cache reads/writes are protected by a threading.Lock.
    """
    CACHE_FILE.parent.mkdir(exist_ok=True)

    with _cache_lock:
        if CACHE_FILE.exists() and (time.time() - CACHE_FILE.stat().st_mtime) < CACHE_TTL:
            try:
                with open(CACHE_FILE) as f:
                    return set(json.load(f))
            except Exception:
                pass  # corrupted cache — fall through to re-download

        print("[→] Downloading US ticker list from SEC EDGAR...")
        try:
            req = urllib.request.Request(
                SEC_URL,
                # SEC EDGAR requires a descriptive User-Agent with a real contact address
                headers={"User-Agent": "x-ticker-scraper/1.0 (personal research tool; abulizi.nueraili@gmail.com)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            tickers = {v["ticker"].upper() for v in data.values() if v.get("ticker")}
            _write_cache_atomic(CACHE_FILE, sorted(tickers))
            print(f"[✓] Loaded {len(tickers):,} US tickers from SEC EDGAR")
            return tickers

        except Exception as exc:
            print(f"[!] Could not fetch SEC tickers: {exc}")
            if CACHE_FILE.exists():
                print("[→] Using stale cache as fallback")
                try:
                    with open(CACHE_FILE) as f:
                        return set(json.load(f))
                except Exception:
                    pass
            return set()
