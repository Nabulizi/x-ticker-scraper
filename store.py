"""
store.py — optional SQLite persistence so the scraper builds a time series
instead of a pile of disconnected JSON snapshots.

What it unlocks:
  * Incremental de-dupe across runs (a post is recorded once, by id).
  * Mention VELOCITY and FIRST-mention timing — the actual tradeable signal,
    invisible in a single-day snapshot.
  * An account SCORECARD: store price at first mention, later backfill forward
    returns (1d/5d/20d) to rank which accounts actually lead vs. add noise.

Fully optional and import-safe: app.py calls record_run() inside try/except,
so any failure here never breaks a scan.
"""
import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "data" / "scraper.db"
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id     TEXT PRIMARY KEY,
    account     TEXT NOT NULL,
    posted_at   TEXT,
    text        TEXT,
    url         TEXT,
    likes       INTEGER,
    reposts     INTEGER,
    replies     INTEGER,
    views       INTEGER,
    is_repost   INTEGER DEFAULT 0,
    first_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL,
    account         TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    posted_at       TEXT,
    confidence      TEXT,
    sentiment       TEXT,
    sentiment_score REAL,
    signal_weight   REAL,
    is_trailing_tag INTEGER DEFAULT 0,
    price_at_mention REAL,
    ret_1d  REAL, ret_5d REAL, ret_20d REAL,
    UNIQUE(post_id, ticker)
);
CREATE TABLE IF NOT EXISTS accounts (
    account      TEXT PRIMARY KEY,
    followers    INTEGER,
    last_scraped TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON mentions(ticker, posted_at);
CREATE INDEX IF NOT EXISTS idx_mentions_account ON mentions(account, posted_at);
CREATE INDEX IF NOT EXISTS idx_mentions_posted_at ON mentions(posted_at);
"""


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    # WAL mode allows concurrent reads during writes and reduces lock contention
    # when multiple scans finish simultaneously.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; faster than FULL
    c.executescript(_SCHEMA)
    return c


def _tz_or_utc(tz_name: Optional[str]):
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _parse_posted_at(ts: Optional[str]):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _post_id(account: str, post: dict) -> str:
    """Prefer the X status id from the permalink; else a stable content hash."""
    url = post.get("url") or ""
    if "/status/" in url:
        return url.rsplit("/status/", 1)[1].split("?")[0].strip("/")
    h = hashlib.sha1(f"{account}:{post.get('text','')}".encode()).hexdigest()[:16]
    return f"h_{h}"


def record_run(run: dict) -> None:
    """Persist a completed scan. Idempotent on (post_id) and (post_id, ticker)."""
    now = datetime.now(timezone.utc).isoformat()
    # price lookup for price_at_mention, taken from the enriched combined list
    price_by_ticker = {c["ticker"]: c.get("price") for c in run.get("combined_tickers", [])}

    with _lock, _conn() as c:
        for account, data in run.get("results", {}).items():
            if data.get("error"):
                continue
            if data.get("follower_count") is not None:
                c.execute(
                    "INSERT INTO accounts(account, followers, last_scraped) VALUES(?,?,?) "
                    "ON CONFLICT(account) DO UPDATE SET followers=excluded.followers, "
                    "last_scraped=excluded.last_scraped",
                    (account, data.get("follower_count"), now),
                )

            posts = data.get("posts", [])
            for post in posts:
                pid = _post_id(account, post)
                c.execute(
                    "INSERT OR IGNORE INTO posts(post_id, account, posted_at, text, url, "
                    "likes, reposts, replies, views, is_repost, first_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, account, post.get("posted_at"), post.get("text"),
                     post.get("url"), post.get("likes"), post.get("reposts"),
                     post.get("replies"), post.get("views"),
                     1 if post.get("is_repost") else 0, now),
                )

            # mentions: map each ticker occurrence back to its post by index
            for t in data.get("tickers", []):
                sym = t["ticker"]
                for occ in t.get("occurrences", []):
                    idx = occ.get("post_index", 0) - 1
                    if not (0 <= idx < len(posts)):
                        continue
                    pid = _post_id(account, posts[idx])
                    c.execute(
                        "INSERT OR IGNORE INTO mentions(post_id, account, ticker, posted_at, "
                        "confidence, sentiment, sentiment_score, signal_weight, "
                        "is_trailing_tag, price_at_mention) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (pid, account, sym, occ.get("posted_at"),
                         occ.get("confidence"), occ.get("sentiment"),
                         occ.get("sentiment_score"), occ.get("signal_weight"),
                         1 if occ.get("is_trailing_tag") else 0,
                         price_by_ticker.get(sym)),
                    )


def ticker_first_seen(tickers: list, tz_name: Optional[str] = None) -> dict:
    """
    {ticker: 'YYYY-MM-DD'} earliest mention date across ALL history.
    Used by the digest to flag names appearing for the very first time today.
    """
    syms = [t.upper() for t in tickers if t]
    if not syms:
        return {}
    placeholders = ",".join("?" * len(syms))
    with _lock, _conn() as c:
        rows = c.execute(
            f"SELECT ticker, MIN(posted_at) FROM mentions "
            f"WHERE ticker IN ({placeholders}) AND posted_at IS NOT NULL "
            f"GROUP BY ticker",
            syms,
        ).fetchall()
    tz = _tz_or_utc(tz_name)
    out = {}
    for ticker, first_at in rows:
        parsed = _parse_posted_at(first_at)
        if parsed is None:
            continue
        out[ticker] = parsed.astimezone(tz).strftime("%Y-%m-%d")
    return out


def ticker_daily_counts(
    tickers: list,
    days: int = 5,
    tz_name: Optional[str] = None,
    today: Optional[str] = None,
) -> dict:
    """
    {ticker: {'YYYY-MM-DD': count}} per-day mention counts over the last `days`
    days. Used by the digest to detect mention acceleration (today vs. prior avg).
    """
    syms = [t.upper() for t in tickers if t]
    if not syms:
        return {}
    placeholders = ",".join("?" * len(syms))

    if not tz_name:
        with _lock, _conn() as c:
            rows = c.execute(
                f"SELECT ticker, substr(posted_at,1,10) d, COUNT(*) n FROM mentions "
                f"WHERE ticker IN ({placeholders}) AND posted_at >= date('now', ?) "
                f"GROUP BY ticker, d",
                syms + [f"-{int(days)} days"],
            ).fetchall()
        out: dict = {}
        for ticker, d, n in rows:
            out.setdefault(ticker, {})[d] = n
        return out

    tz = _tz_or_utc(tz_name)
    if today:
        today_local = datetime.fromisoformat(today).date()
    else:
        today_local = datetime.now(tz).date()
    start_local = today_local - timedelta(days=int(days))
    start_utc = datetime.combine(start_local, datetime.min.time(), tz).astimezone(timezone.utc).isoformat()

    with _lock, _conn() as c:
        rows = c.execute(
            f"SELECT ticker, posted_at FROM mentions "
            f"WHERE ticker IN ({placeholders}) AND posted_at >= ? AND posted_at IS NOT NULL",
            syms + [start_utc],
        ).fetchall()

    out: dict = {}
    for ticker, posted_at in rows:
        parsed = _parse_posted_at(posted_at)
        if parsed is None:
            continue
        local_day = parsed.astimezone(tz).date()
        if local_day < start_local or local_day > today_local:
            continue
        day_key = local_day.isoformat()
        bucket = out.setdefault(ticker, {})
        bucket[day_key] = bucket.get(day_key, 0) + 1

    return out


def mention_velocity(ticker: str, days: int = 7) -> list:
    """Daily mention counts for a ticker over the last `days` days."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT substr(posted_at,1,10) d, COUNT(*) n FROM mentions "
            "WHERE ticker=? AND posted_at >= date('now', ?) GROUP BY d ORDER BY d",
            (ticker.upper(), f"-{int(days)} days"),
        ).fetchall()
    return [{"date": r[0], "mentions": r[1]} for r in rows]


def first_mentions(ticker: str) -> list:
    """Who mentioned a ticker first, in order — useful for lead/lag analysis."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT account, MIN(posted_at) first_at, MIN(price_at_mention) px "
            "FROM mentions WHERE ticker=? GROUP BY account ORDER BY first_at",
            (ticker.upper(),),
        ).fetchall()
    return [{"account": r[0], "first_at": r[1], "price_at_mention": r[2]} for r in rows]


def account_scorecard(min_calls: int = 3) -> list:
    """
    Rank accounts by forward return on their mentions (once ret_* are backfilled
    by update_forward_returns). Trailing-tag spam is excluded so it doesn't
    dilute the score. win_rate_5d = share of calls that were green 5 trading
    days out — an intuitive 'how often are they right' number.
    """
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT account, COUNT(*) calls, "
            "AVG(ret_1d) avg1, AVG(ret_5d) avg5, AVG(ret_20d) avg20, "
            "AVG(CASE WHEN ret_5d > 0 THEN 1.0 ELSE 0.0 END) win5 "
            "FROM mentions WHERE is_trailing_tag=0 AND ret_5d IS NOT NULL "
            "GROUP BY account HAVING calls >= ? ORDER BY avg5 DESC",
            (min_calls,),
        ).fetchall()

    def r2(v):
        return round(v, 2) if v is not None else None

    return [{
        "account": r[0],
        "calls": r[1],
        "avg_ret_1d": r2(r[2]),
        "avg_ret_5d": r2(r[3]),
        "avg_ret_20d": r2(r[4]),
        "win_rate_5d": round(r[5] * 100, 0) if r[5] is not None else None,
    } for r in rows]


def update_forward_returns(progress=None) -> dict:
    """
    Backfill forward returns for mentions old enough to measure.

    Baseline is the CLOSE on the first trading day on/after the post date
    (NOT price_at_mention, which is captured at scan time and can be days after
    the post). ret_Nd = % change from that baseline N trading days later. History
    is fetched in a single yf.download() batch call to minimise HTTP requests.

    Returns a summary dict:
      {updated, tickers_total, tickers_ok, tickers_failed, rate_limited}
    so a manual trigger can tell "Yahoo throttled us, try later" apart from
    "no mentions are old enough yet".
    """
    import yfinance as yf  # local import keeps store.py usable without yfinance
    try:
        from market_data import _yf_session  # reuse SSL/proxy-tolerant session
        session = _yf_session()
    except Exception:
        session = None

    with _lock, _conn() as c:
        pending = c.execute(
            "SELECT id, ticker, posted_at FROM mentions "
            "WHERE ret_5d IS NULL AND posted_at IS NOT NULL"
        ).fetchall()

    summary = {"updated": 0, "tickers_total": 0, "tickers_ok": 0,
               "tickers_failed": 0, "rate_limited": False}
    if not pending:
        return summary

    by_ticker: dict = {}
    for mid, ticker, posted_at in pending:
        by_ticker.setdefault(ticker, []).append((mid, posted_at[:10]))
    summary["tickers_total"] = len(by_ticker)

    today = datetime.now(timezone.utc).date()

    # Compute the overall date range for a single batch download
    all_dates = [d for items in by_ticker.values() for _, d in items if d]
    if not all_dates:
        return summary
    global_start = min(all_dates)
    try:
        global_end = min(
            datetime.fromisoformat(max(all_dates)).date() + timedelta(days=45), today
        )
    except ValueError:
        return summary

    # Single batch download for all tickers
    import warnings
    all_tickers = list(by_ticker.keys())
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            kwargs = {"session": session} if session else {}
            raw = yf.download(
                all_tickers,
                start=global_start,
                end=global_end.isoformat(),
                auto_adjust=True,
                progress=False,
                **kwargs,
            )
    except Exception as exc:
        if "rate" in str(exc).lower() or "too many" in str(exc).lower():
            summary["rate_limited"] = True
        summary["tickers_failed"] = len(all_tickers)
        return summary

    if raw is None or raw.empty:
        return summary

    # yf.download returns MultiIndex columns when multiple tickers are requested
    close_df = raw["Close"] if "Close" in raw.columns else raw

    for ticker, items in by_ticker.items():
        dates = [d for _, d in items if d]
        if not dates:
            continue

        try:
            if ticker not in close_df.columns:
                summary["tickers_failed"] += 1
                continue
            series = close_df[ticker].dropna()
            if series.empty:
                summary["tickers_failed"] += 1
                continue

            closes = series.tolist()
            idx_dates = [d.date().isoformat() for d in series.index]
            summary["tickers_ok"] += 1

            rows = []
            for mid, pd_date in items:
                base_i = next((i for i, d in enumerate(idx_dates) if d >= pd_date), None)
                if base_i is None:
                    continue
                base = closes[base_i]
                if not base:
                    continue

                def ret(n, _closes=closes, _base_i=base_i, _base=base):
                    j = _base_i + n
                    return round((_closes[j] - _base) / _base * 100, 2) if j < len(_closes) else None

                rows.append((ret(1), ret(5), ret(20), mid))

            if rows:
                with _lock, _conn() as c:
                    c.executemany(
                        "UPDATE mentions SET ret_1d=?, ret_5d=?, ret_20d=? WHERE id=?", rows
                    )
                summary["updated"] += sum(1 for r in rows if r[1] is not None)

        except Exception:
            summary["tickers_failed"] += 1
            continue

        if progress:
            try:
                progress(ticker, summary["updated"])
            except Exception:
                pass

    return summary
