"""
ticker_extractor.py — extract US stock tickers from X posts.

Design (why it changed):
  The old version ran a plain ALL-CAPS regex and accepted any 2-5 letter token
  that happened to be a real SEC symbol. That produced false positives like
  "MC" (from "$13B MC" = market cap) and "PM" (from "down $100 from PM" =
  pre-market), because MC/PM are themselves listed tickers and weren't in the
  blocklist. Blocklisting is whack-a-mole, so the precision fix is structural:

    * $CASHTAGS are high confidence and always accepted (after validation).
    * Plain ALL-CAPS tokens are LOW confidence and only accepted when the SAME
      symbol also appears as a cashtag somewhere in that account's posts
      (corroboration). A bare "PM"/"MC" with no $PM/$MC anywhere is dropped.
    * Share-class suffixes ($HPS.A) are preserved and flagged, since the SEC
      list is US-only and a suffix often signals a foreign/misresolved name.

Each occurrence is tagged with confidence, sentiment, conviction and a
signal_weight so the dashboard can rank by signal rather than raw counts.
"""
import re
from typing import Optional

import signals
from tickers_db import load_tickers

# Common English words / finance acronyms / chat slang that collide with real
# tickers. Corroboration handles most of this now, but the list is a cheap
# first filter and also guards cashtags (e.g. nobody means Moelis by "$MC").
BLOCKLIST = {
    # short English words
    "A", "I", "AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF",
    "IN", "IS", "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO",
    "UP", "US", "WE", "CAN", "DID", "FOR", "GET", "GOT", "HAD", "HAS", "HIM",
    "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOT", "NOW", "OLD", "ONE",
    "OUT", "OWN", "SAY", "SEE", "SHE", "THE", "TOO", "TWO", "USE", "WAY",
    "WHO", "WHY", "YES", "YET", "YOU", "ALL", "AND", "BUT", "THAT", "THIS",
    "WITH", "FROM", "THEY", "HAVE", "MORE", "WILL", "THAN", "BEEN", "SAID",
    "EACH", "WHICH", "THEIR", "TIME", "WHAT", "ABOUT", "MANY", "THEN", "THEM",
    "THESE", "SOME", "WHEN", "WOULD", "THERE", "COULD", "OTHER", "INTO",
    "LOOK", "MOST", "ALSO", "BACK", "COME", "GIVE", "JUST", "KNOW", "LIKE",
    "MAKE", "OVER", "SUCH", "TAKE", "WELL", "WERE", "YEAR",
    # finance / business acronyms and units
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FED", "GDP", "CPI",
    "PCE", "NFT", "API", "USD", "EUR", "GBP", "JPY", "ATH", "ATL", "ROI",
    "EPS", "MC", "PM", "AM", "AH", "PT", "DD", "TA", "SI", "FY", "TF", "MTM",
    "PE", "VC", "EV", "IV", "RR", "OG", "PR", "RN", "CC", "QE", "QT", "MA",
    "H1", "H2", "Q1", "Q2", "Q3", "Q4", "YOY", "QOQ", "WOW", "MOM", "BPS",
    # chat slang
    "IMO", "IMHO", "TBH", "FYI", "FWIW", "TLDR", "EOD", "EOY", "AI", "ML",
    "USA", "UK", "EU", "BREAKING", "HUGE", "LIVE", "VERY", "GREAT", "GOOD",
    "BEST", "HIGH", "LOW", "BIG", "TOP", "HOT", "MUST", "READ", "WATCH",
    "HERE", "THREAD", "UPDATE", "FOLLOW", "SHARE", "REPOST", "RT", "DM",
    "PLS", "LOL", "OMG", "WTF", "BTW", "AKA", "TBA", "TBD",
}

# $AAPL or $brk.b  — capture base symbol + optional share-class suffix.
_cashtag_re = re.compile(r'(?<![A-Za-z0-9])\$([A-Za-z]{1,5})(?:\.([A-Za-z]))?\b')
# Plain ALL-CAPS token in the ORIGINAL text, not preceded by $ or alphanumerics.
_plain_re = re.compile(r'(?<![A-Za-z0-9$.])([A-Z]{2,5})\b')


def _norm_post(post_item):
    if isinstance(post_item, dict):
        return post_item.get("text", ""), post_item.get("posted_at")
    return post_item, None


def extract_tickers(posts: list, valid_tickers: Optional[set] = None) -> list:
    """
    Extract tickers from one account's posts.

    Returns a list ranked by signal_score (not raw count):
      [{ ticker, share_class, mentions, cashtag_mentions, signal_score,
         net_sentiment, occurrences: [ {post_index, snippet, posted_at,
         confidence, sentiment, sentiment_score, signal_weight,
         is_subject, is_trailing_tag} ] }]
    """
    if valid_tickers is None:
        valid_tickers = load_tickers()

    norm_posts = [_norm_post(p) for p in posts]

    # --- pass 1: collect the set of cashtag symbols used by this account ------
    # Plain ALL-CAPS hits are only trusted if corroborated by a cashtag here.
    cashtag_symbols: set = set()
    for text, _ in norm_posts:
        for m in _cashtag_re.finditer(text):
            cashtag_symbols.add(m.group(1).upper())

    found: dict = {}

    def _record(symbol, share_cls, text, start, posted_at, idx, is_cashtag, n_tickers):
        conv = signals.conviction(text, start, n_tickers)
        senti_label, senti_score = signals.sentiment(text)
        weight = signals.signal_weight(is_cashtag, conv, share_class=bool(share_cls))
        key = symbol + (f".{share_cls}" if share_cls else "")
        entry = found.setdefault(key, {
            "ticker": symbol,
            "share_class": share_cls,
            "occurrences": [],
        })
        entry["occurrences"].append({
            "post_index": idx,
            "snippet": _snippet(text, symbol),
            "posted_at": posted_at,
            "confidence": "cashtag" if is_cashtag else "plain",
            "sentiment": senti_label,
            "sentiment_score": senti_score,
            "signal_weight": weight,
            "is_subject": conv["is_subject"],
            "is_trailing_tag": conv["is_trailing_tag"],
        })

    # --- pass 2: extract per post -------------------------------------------
    for idx, (text, posted_at) in enumerate(norm_posts, start=1):
        if not text:
            continue

        # cashtags (high confidence)
        cash_hits = {}   # symbol -> (start, share_class)
        for m in _cashtag_re.finditer(text):
            sym = m.group(1).upper()
            cls = m.group(2).upper() if m.group(2) else None
            if sym in valid_tickers and sym not in BLOCKLIST:
                cash_hits.setdefault(sym, (m.start(), cls))

        # plain ALL-CAPS (low confidence) — only if corroborated by a cashtag
        plain_hits = {}
        for m in _plain_re.finditer(text):
            sym = m.group(1)
            if (sym in valid_tickers and sym not in BLOCKLIST
                    and sym in cashtag_symbols and sym not in cash_hits):
                plain_hits.setdefault(sym, m.start())

        n_tickers = len(cash_hits) + len(plain_hits)
        for sym, (start, cls) in cash_hits.items():
            _record(sym, cls, text, start, posted_at, idx, True, n_tickers)
        for sym, start in plain_hits.items():
            _record(sym, None, text, start, posted_at, idx, False, n_tickers)

    # --- aggregate -----------------------------------------------------------
    out = []
    for key, entry in found.items():
        occ = entry["occurrences"]
        sig = round(sum(o["signal_weight"] for o in occ), 3)
        cash = sum(1 for o in occ if o["confidence"] == "cashtag")
        net = round(sum(o["sentiment_score"] for o in occ), 2)
        out.append({
            "ticker": entry["ticker"],
            "share_class": entry["share_class"],
            "mentions": len(occ),
            "cashtag_mentions": cash,
            "signal_score": sig,
            "net_sentiment": net,
            "occurrences": occ,
        })

    out.sort(key=lambda x: (-x["signal_score"], -x["mentions"]))
    return out


def _snippet(post: str, ticker: str) -> str:
    """Return ~100-char context window around the first ticker mention."""
    pattern = re.compile(rf'(?:\${re.escape(ticker)}|\b{re.escape(ticker)}\b)', re.IGNORECASE)
    m = pattern.search(post)
    if not m:
        return (post[:97] + "...") if len(post) > 100 else post
    start = max(0, m.start() - 40)
    end = min(len(post), m.end() + 60)
    snippet = post[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(post):
        snippet = snippet + "…"
    return snippet
