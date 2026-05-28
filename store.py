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
from datetime import datetime, timezone
from pathlib import Path

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
"""


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.executescript(_SCHEMA)
    return c


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
    Rank accounts by average forward return on their mentions (once ret_* are
    backfilled by update_forward_returns). Trailing tags are excluded so spam
    doesn't dilute the score.
    """
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT account, COUNT(*) calls, "
            "AVG(ret_5d) avg5, AVG(ret_20d) avg20 FROM mentions "
            "WHERE is_trailing_tag=0 AND ret_5d IS NOT NULL "
            "GROUP BY account HAVING calls >= ? ORDER BY avg5 DESC",
            (min_calls,),
        ).fetchall()
    return [{"account": r[0], "calls": r[1], "avg_ret_5d": r[2], "avg_ret_20d": r[3]}
            for r in rows]


def update_forward_returns() -> int:
    """
    Backfill forward returns for mentions old enough to measure. Run on a
    schedule (cron / APScheduler). Uses yfinance history. Returns rows updated.
    """
    import yfinance as yf  # local import keeps store.py usable without yfinance

    with _lock, _conn() as c:
        pending = c.execute(
            "SELECT id, ticker, posted_at, price_at_mention FROM mentions "
            "WHERE ret_5d IS NULL AND price_at_mention IS NOT NULL AND posted_at IS NOT NULL"
        ).fetchall()

    updated = 0
    for mid, ticker, posted_at, px in pending:
        try:
            start = posted_at[:10]
            hist = yf.Ticker(ticker).history(start=start, period="2mo")
            if hist.empty or not px:
                continue
            closes = hist["Close"].tolist()

            def ret(n):
                return round((closes[n] - px) / px * 100, 2) if len(closes) > n else None

            with _lock, _conn() as c:
                c.execute(
                    "UPDATE mentions SET ret_1d=?, ret_5d=?, ret_20d=? WHERE id=?",
                    (ret(1), ret(5), ret(20), mid),
                )
            updated += 1
        except Exception:
            continue
    return updated
