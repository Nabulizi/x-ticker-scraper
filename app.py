import asyncio
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

from price_lookup import lookup_prices
from scraper import InteractiveLoginRequired, scrape_accounts, validate_username
from sector_lookup import lookup_sectors
from ticker_extractor import extract_tickers
from tickers_db import load_tickers

load_dotenv()

app = Flask(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Thread-safe lazy-loaded ticker DB (double-checked locking)
_tickers_db = None
_tickers_db_lock = threading.Lock()


def get_tickers_db() -> set:
    global _tickers_db
    if _tickers_db is not None:
        return _tickers_db
    with _tickers_db_lock:
        if _tickers_db is None:
            _tickers_db = load_tickers()
    return _tickers_db


@app.route("/")
def index():
    runs = sorted(OUTPUT_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:10]
    recent = [
        {
            "filename": f.name,
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for f in runs
    ]
    return render_template("index.html", recent_runs=recent)


@app.route("/scrape", methods=["POST"])
def scrape():
    body = request.get_json(force=True)

    # Validate usernames — alphanumeric + underscore only, 1–50 chars
    raw_input = body.get("usernames", "")
    valid_usernames = []
    rejected = []
    for raw in raw_input.replace(",", "\n").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            valid_usernames.append(validate_username(raw))
        except ValueError as exc:
            rejected.append(str(exc))

    if not valid_usernames:
        msg = "No valid usernames provided."
        if rejected:
            msg += " Issues: " + "; ".join(rejected)
        return jsonify({"error": msg}), 400

    # Post count — default 10, max 200
    try:
        count = max(1, min(int(body.get("count", 10)), 200))
    except (TypeError, ValueError):
        count = 10

    # Optional since_date — ISO date string "YYYY-MM-DD"
    since_date = None
    since_raw = body.get("since_date", "").strip()
    if since_raw:
        try:
            since_date = datetime.fromisoformat(since_raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"error": f"Invalid since_date '{since_raw}'. Use YYYY-MM-DD."}), 400

    # NOTE: asyncio.run() blocks this WSGI thread for the duration of the scrape.
    # This is intentional for Flask's default synchronous server.  Do not switch
    # to async Flask or an ASGI server without refactoring scrape_accounts.
    try:
        scraped = asyncio.run(scrape_accounts(valid_usernames, count=count, since_date=since_date))
    except InteractiveLoginRequired as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    valid_tickers = get_tickers_db()

    run: dict = {
        "run_at": datetime.now(timezone.utc).isoformat(),  # always UTC
        "accounts_analyzed": valid_usernames,
        "scan_settings": {
            "max_posts": count,
            "since_date": since_raw or None,
        },
        "results": {},
        "combined_tickers": [],
        "by_sector": {},
    }

    combined: dict = {}

    for username, data in scraped.items():
        if data["error"]:
            run["results"][username] = {
                "posts_analyzed": 0,
                "posts": [],
                "tickers": [],
                "error": data["error"],
            }
            continue

        tickers = extract_tickers(data["posts"], valid_tickers)
        run["results"][username] = {
            "posts_analyzed": len(data["posts"]),
            "stopped_by": data.get("stopped_by"),
            "posts": data["posts"],
            "tickers": tickers,
            "error": None,
        }

        for t in tickers:
            entry = combined.setdefault(
                t["ticker"],
                {"ticker": t["ticker"], "total_mentions": 0, "sources": {}},
            )
            entry["total_mentions"] += t["mentions"]
            entry["sources"][username] = t["occurrences"]

    # Sector + price enrichment — fetched once against the deduplicated set
    all_ticker_symbols = list(combined.keys())
    sector_map = lookup_sectors(all_ticker_symbols) if all_ticker_symbols else {}
    price_map  = lookup_prices(all_ticker_symbols)  if all_ticker_symbols else {}

    def _enrich(t: dict) -> None:
        sym = t["ticker"]
        s = sector_map.get(sym, {})
        p = price_map.get(sym, {})
        t["sector"]       = s.get("sector",   "Unknown")
        t["industry"]     = s.get("industry", "Unknown")
        t["company"]      = s.get("company",  sym)
        t["price"]        = p.get("price")
        t["change_pct"]   = p.get("change_pct")
        t["change_abs"]   = p.get("change_abs")
        t["currency"]     = p.get("currency",     "USD")
        t["market_state"] = p.get("market_state", "UNKNOWN")

    for data in run["results"].values():
        if not data["error"]:
            for t in data["tickers"]:
                _enrich(t)

    combined_list = sorted(combined.values(), key=lambda x: -x["total_mentions"])
    for entry in combined_list:
        _enrich(entry)

    run["combined_tickers"] = combined_list

    by_sector: dict = {}
    for entry in combined_list:
        sector = entry.get("sector", "Unknown")
        by_sector.setdefault(sector, []).append({
            "ticker":         entry["ticker"],
            "company":        entry["company"],
            "industry":       entry["industry"],
            "total_mentions": entry["total_mentions"],
            "price":          entry.get("price"),
            "change_pct":     entry.get("change_pct"),
            "market_state":   entry.get("market_state"),
        })
    run["by_sector"] = dict(sorted(by_sector.items()))

    # Persist to disk (atomic write via write_json_atomic in tickers_db)
    slug = "_".join(valid_usernames[:3])
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{slug}.json"
    _write_json_atomic(OUTPUT_DIR / filename, run)

    return jsonify({"result": run, "saved_as": filename})


@app.route("/results/<path:filename>")
def view_result(filename):
    # send_from_directory resolves the path inside OUTPUT_DIR and raises 404
    # for any traversal attempt (e.g. ../../.env), preventing path traversal.
    return send_from_directory(OUTPUT_DIR, filename, mimetype="application/json")


def _write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON to a temp file then atomically rename — safe against crashes and races."""
    import os, tempfile
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
        raise


if __name__ == "__main__":
    print("[→] Loading US ticker database...")
    get_tickers_db()
    print("[✓] Ready — open http://localhost:5000")
    app.run(debug=False, port=5000)
