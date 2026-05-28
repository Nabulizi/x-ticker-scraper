"""
price_lookup.py — thin adapter kept for backwards compatibility.

Price fetching now lives in market_data.py (single reliable fetch via
fast_info, shared with sector lookup). This wrapper preserves the original
lookup_prices() signature so callers don't change.
"""
from market_data import get_market_data


def lookup_prices(tickers: list) -> dict:
    md = get_market_data(tickers)
    return {
        t: {
            "price": d["price"],
            "prev_close": d["prev_close"],
            "change_abs": d["change_abs"],
            "change_pct": d["change_pct"],
            "currency": d["currency"],
            "market_state": d["market_state"],
            "suspicious": d["suspicious"],
        }
        for t, d in md.items()
    }
