"""
signals.py — lightweight, dependency-free scoring for a single ticker mention.

Two things we care about per mention:

  1. Sentiment   — is the author bullish, bearish, or neutral on the name?
  2. Conviction  — is the ticker the *subject* of the post (a real call), or just
                   a trailing cashtag tacked onto unrelated content (engagement
                   farming, e.g. "$TSLA" appended to a post about an Iran deal)?

These are deliberately simple lexicon/heuristic scorers. They are not meant to be
state-of-the-art NLP — they exist to stop raw mention counts from being dominated
by noise, and to surface disagreement between accounts on the same ticker.
"""
from __future__ import annotations

import re

# --- sentiment lexicon -------------------------------------------------------
# Multi-word phrases are checked first (substring), then single tokens.
_BULL_PHRASES = (
    "blew away", "blew past", "like the name", "love the name", "long term hold",
    "back up the truck", "to the moon", "all time high", "breaking out",
    "good feeling", "bread & butter", "bread and butter", "adding more",
    "building more", "buying more", "compelling compounder",
)
_BEAR_PHRASES = (
    "piece of shit", "dead money", "falling knife", "bull trap", "bag holder",
    "bagholder", "crashing back", "stopped out", "cut the position",
    "timing error", "no clue", "overvalued",
)
_BULL_WORDS = {
    "bull", "bullish", "long", "buy", "buying", "bought", "breakout", "ripping",
    "rip", "pop", "popped", "mooning", "moon", "rally", "rallied", "calls",
    "strong", "love", "accumulate", "accumulating", "add", "adding", "higher",
    "beat", "beats", "undervalued", "spiffy", "percolating", "dynamite",
    "upside", "squeeze", "leader", "leading", "winner", "monster", "parabolic",
}
_BEAR_WORDS = {
    "bear", "bearish", "short", "shorting", "sell", "selling", "sold", "dump",
    "dumping", "crash", "crashed", "puts", "weak", "weakness", "rug", "avoid",
    "down", "drop", "dropping", "fade", "fading", "overhyped", "dumbfounded",
    "missed", "lower", "miss", "trap", "blacklisted",
}
_NEGATIONS = {"not", "no", "never", "isn't", "aint", "ain't", "wasn't", "without"}

_word_re = re.compile(r"[a-z']+")
_money_re = re.compile(r"\$\d")          # "$443", "$13B"
_pct_re = re.compile(r"\d+(\.\d+)?\s*%")  # "10%", "23.1 %"


def sentiment(text: str) -> tuple[str, float]:
    """Return (label, score) where score is in roughly [-1, 1]."""
    low = text.lower()
    score = 0
    for p in _BULL_PHRASES:
        if p in low:
            score += 2
    for p in _BEAR_PHRASES:
        if p in low:
            score -= 2

    tokens = _word_re.findall(low)
    for i, tok in enumerate(tokens):
        hit = 1 if tok in _BULL_WORDS else (-1 if tok in _BEAR_WORDS else 0)
        if hit:
            prev = tokens[i - 1] if i else ""
            if prev in _NEGATIONS:        # "not strong" -> flip
                hit = -hit
            score += hit

    if score == 0:
        return "neutral", 0.0
    # squash to [-1, 1]
    norm = max(-1.0, min(1.0, score / 4.0))
    return ("bullish" if score > 0 else "bearish"), round(norm, 2)


def conviction(text: str, mention_start: int, num_tickers_in_post: int) -> dict:
    """
    Judge whether the mention is the *subject* of the post or just a trailing
    cashtag. Crucially this is judged LOCALLY (in a window around the mention),
    not at the post level — otherwise a macro rant ending in "$TSLA", or a post
    about ticker A that mentions a price, would wrongly credit the tag on B.

    Returns {is_subject, is_trailing_tag, has_thesis}.
    """
    n = max(1, len(text))
    rel_pos = mention_start / n

    # words appearing BEFORE the mention (is there a body it's tacked onto?)
    words_before = len(text[:mention_start].split())
    # local window after the mention — a real subject usually keeps talking
    after = text[mention_start: mention_start + 60]
    words_after = len(after.split())

    # thesis = price/percent near the mention (within ~40 chars either side)
    local = text[max(0, mention_start - 40): mention_start + 40]
    has_thesis = bool(_money_re.search(local) or _pct_re.search(local))

    # Trailing tag: sits in the back of the post, has a real body before it, and
    # nothing of substance after it — and isn't carrying local thesis content.
    is_trailing_tag = (
        rel_pos >= 0.7
        and words_before >= 3
        and words_after <= 2
        and not has_thesis
    )

    # Subject: near the front, or it has local thesis content, or it leads into
    # more text. Multi-ticker posts can have several subjects (e.g. "$NVDA or $AMD").
    is_subject = (not is_trailing_tag) and (
        rel_pos <= 0.5 or has_thesis or words_after >= 4
    )

    return {
        "is_subject": is_subject,
        "is_trailing_tag": is_trailing_tag,
        "has_thesis": has_thesis,
    }


def signal_weight(is_cashtag: bool, conv: dict, share_class: bool = False) -> float:
    """
    Combine confidence + conviction into a single 0..1 weight used for ranking.
    A clean cashtag thesis ~= 1.0; a plain corroborated mention is worth less;
    a trailing tag is heavily discounted.
    """
    w = 1.0 if is_cashtag else 0.45
    if conv["is_trailing_tag"]:
        w *= 0.2
    elif conv["is_subject"]:
        w *= 1.0
    else:
        w *= 0.6
    if conv["has_thesis"]:
        w *= 1.15
    if share_class:           # $HPS.A style — may be a foreign/misresolved name
        w *= 0.6
    return round(min(w, 1.0), 3)
