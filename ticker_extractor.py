import re
from typing import Optional
from tickers_db import load_tickers

# Common English words / financial acronyms that match ticker patterns but aren't stocks
BLOCKLIST = {
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
    # Financial/business acronyms
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FED", "GDP", "CPI",
    "NFT", "API", "USD", "EUR", "GBP", "JPY", "ATH", "ATL", "ROI", "EPS",
    "IMO", "IMHO", "TBH", "FYI", "FWIW", "TLDR", "EOD", "EOY", "YOY", "QOQ",
    "AI", "ML", "US", "USA", "UK", "EU", "EV", "VC", "PE", "RN", "PR",
    "BREAKING", "HUGE", "LIVE", "VERY", "GREAT", "GOOD", "BEST", "HIGH",
    "LOW", "BIG", "TOP", "HOT", "MUST", "READ", "WATCH", "JUST", "HERE",
    "THREAD", "UPDATE", "FOLLOW", "SHARE", "LIKE", "REPOST",
    "RT", "DM", "PLS", "LOL", "OMG", "WTF", "BTW", "AKA", "TBA", "TBD",
}

_cashtag_re = re.compile(r'\$([A-Z]{1,5})\b')
_plain_re = re.compile(r'\b([A-Z]{2,5})\b')


def extract_tickers(posts: list, valid_tickers: Optional[set] = None) -> list:
    """
    Extract US stock tickers from a list of post texts.

    Returns a list sorted by mention count:
      [{ ticker, mentions, occurrences: [{post_index, snippet}] }]
    """
    if valid_tickers is None:
        valid_tickers = load_tickers()

    found: dict = {}

    for idx, post_item in enumerate(posts, start=1):
        # Accept both plain strings and {"text": ..., "posted_at": ...} dicts
        if isinstance(post_item, dict):
            post = post_item["text"]
            posted_at = post_item.get("posted_at")
        else:
            post = post_item
            posted_at = None

        in_this_post: set = set()

        # Cashtag $AAPL / $nvda  — case-insensitive, high confidence
        for m in _cashtag_re.finditer(post.upper()):
            ticker = m.group(1)
            if ticker in valid_tickers and ticker not in BLOCKLIST:
                in_this_post.add(ticker)

        # Plain uppercase: only match words already ALL-CAPS in the original post.
        # Running on the original (not upper-cased) text avoids treating every word
        # as a potential ticker just because we uppercased the whole string.
        for m in _plain_re.finditer(post):
            ticker = m.group(1)  # already uppercase because regex requires [A-Z]
            if ticker in valid_tickers and ticker not in BLOCKLIST:
                in_this_post.add(ticker)

        for ticker in in_this_post:
            snippet = _snippet(post, ticker)
            occ = {"post_index": idx, "snippet": snippet}
            if posted_at:
                occ["posted_at"] = posted_at
            found.setdefault(ticker, []).append(occ)

    return [
        {"ticker": t, "mentions": len(occ), "occurrences": occ}
        for t, occ in sorted(found.items(), key=lambda x: -len(x[1]))
    ]


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
