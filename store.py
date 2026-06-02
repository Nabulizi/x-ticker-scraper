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


def ticker_first_seen(tickers: list) -> dict:
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
            f"SELECT ticker, substr(MIN(posted_at),1,10) FROM mentions "
            f"WHERE ticker IN ({placeholders}) AND posted_at IS NOT NULL "
            f"GROUP BY ticker",
            syms,
        ).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


def ticker_daily_counts(tickers: list, days: int = 5) -> dict:
    """
    {ticker: {'YYYY-MM-DD': count}} per-day mention counts over the last `days`
    days. Used by the digest to detect mention acceleration (today vs. prior avg).
    """
    syms = [t.upper() for t in tickers if t]
    if not syms:
        return {}
    placeholders = ",".join("?" * len(syms))
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
    is fetched once per ticker (grouped) to minimise yfinance calls.

    Returns a summary dict:
      {updated, tickers_total, tickers_ok, tickers_failed, rate_limited}
    so a manual trigger can tell "Yahoo throttled us, try later" apart from
    "no mentions are old enough yet".
    """
    from datetime import timedelta

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

    def _history(tk_symbol, start, end):
        """Fetch with backoff; re-raise so rate-limits are distinguishable."""
        last = None
        for attempt in range(3):
            try:
                tk = yf.Ticker(tk_symbol, session=session) if session else yf.Ticker(tk_symbol)
                return tk.history(start=start, end=end, auto_adjust=True)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "rate" in str(exc).lower() or "too many" in str(exc).lower():
                    summary["rate_limited"] = True
                time.sleep(1.0 * (attempt + 1))
        raise last if last else RuntimeError("history failed")

    for ticker, items in by_ticker.items():
        dates = [d for _, d in items if d]
        if not dates:
            continue
        start = min(dates)
        # End ~45 calendar days past the latest post (≈30 trading days for ret_20d),
        # capped at today. yfinance ignores period when start/end are set.
        try:
            end = min(datetime.fromisoformat(max(dates)).date() + timedelta(days=45), today)
        except ValueError:
            continue

        try:
            hist = _history(ticker, start, end.isoformat())
        except Exception:
            summary["tickers_failed"] += 1
            continue
        if hist is None or hist.empty:
            # No data in range (e.g. post date still in the future vs. real market data)
            continue

        summary["tickers_ok"] += 1
        closes = hist["Close"].tolist()
        idx_dates = [d.date().isoformat() for d in hist.index]

        rows = []
        for mid, pd_date in items:
            base_i = next((i for i, d in enumerate(idx_dates) if d >= pd_date), None)
            if base_i is None:
                continue
            base = closes[base_i]
            if not base:
                continue

            def ret(n):
                j = base_i + n
                return round((closes[j] - base) / base * 100, 2) if j < len(closes) else None

            rows.append((ret(1), ret(5), ret(20), mid))

        if rows:
            with _lock, _conn() as c:
                c.executemany(
                    "UPDATE mentions SET ret_1d=?, ret_5d=?, ret_20d=? WHERE id=?", rows
                )
            summary["updated"] += sum(1 for r in rows if r[1] is not None)

        if progress:
            try:
                progress(ticker, summary["updated"])
            except Exception:
                pass
        time.sleep(0.3)  # be gentle with yfinance between tickers

    return summary
