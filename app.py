import asyncio
import json
import os
import queue
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

from price_lookup import lookup_prices
from scraper import InteractiveLoginRequired, SessionExpired, scrape_accounts, session_status, validate_username
from sector_lookup import lookup_sectors
from ticker_extractor import extract_tickers
from tickers_db import load_tickers

try:
    import store  # optional SQLite persistence (time series + scorecard)
except Exception:  # pragma: no cover
    store = None

load_dotenv()

app = Flask(__name__)
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
WATCHLISTS_FILE = Path(__file__).parent / "data" / "watchlists.json"

# Thread-safe lazy-loaded ticker DB (double-checked locking)
_tickers_db = None
_tickers_db_lock = threading.Lock()

# In-progress scan registry  {scan_id: {"queue": Queue, "final": dict|None}}
_scans: dict = {}
_scans_lock = threading.Lock()
MAX_SCANS = 20


def get_tickers_db() -> set:
    global _tickers_db
    if _tickers_db is not None:
        return _tickers_db
    with _tickers_db_lock:
        if _tickers_db is None:
            _tickers_db = load_tickers()
    return _tickers_db


def _register_scan(scan_id: str, q: queue.Queue) -> None:
    with _scans_lock:
        _scans[scan_id] = {"queue": q, "final": None}
        if len(_scans) > MAX_SCANS:
            oldest = next(iter(_scans))
            del _scans[oldest]


def _load_watchlists() -> dict:
    if WATCHLISTS_FILE.exists():
        try:
            with open(WATCHLISTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_watchlists(data: dict) -> None:
    WATCHLISTS_FILE.parent.mkdir(exist_ok=True)
    _write_json_atomic(WATCHLISTS_FILE, data)


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


@app.route("/session-status")
def get_session_status():
    return jsonify(session_status())


@app.route("/connect-x", methods=["POST"])
def connect_x():
    """
    Opens a visible Playwright browser so the user can log in to X once.
    Saves session.json on success.  Runs synchronously (blocks until done or error).
    """
    from scraper import _manual_login  # local import to avoid circular ref at module load
    try:
        asyncio.run(_manual_login())
        return jsonify({"ok": True, "message": "Connected successfully. You can now run scans."})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


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

    scan_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _register_scan(scan_id, q)

    def run_scan():
        def emit(msg: dict) -> None:
            q.put(msg)

        try:
            scraped = asyncio.run(
                scrape_accounts(valid_usernames, count=count, since_date=since_date, progress=emit)
            )
        except (InteractiveLoginRequired, SessionExpired) as exc:
            q.put({"type": "session_expired", "message": str(exc)})
            return
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
            return

        valid_tickers = get_tickers_db()

        run: dict = {
            "run_at": datetime.now(timezone.utc).isoformat(),
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
                "last_post_date": data.get("last_post_date"),
                "follower_count": data.get("follower_count"),
                "posts": data["posts"],
                "tickers": tickers,
                "error": None,
            }

            for t in tickers:
                entry = combined.setdefault(
                    t["ticker"],
                    {"ticker": t["ticker"], "total_mentions": 0,
                     "cashtag_mentions": 0, "signal_score": 0.0,
                     "net_sentiment": 0.0, "sources": {}},
                )
                entry["total_mentions"] += t["mentions"]
                entry["cashtag_mentions"] += t.get("cashtag_mentions", 0)
                entry["signal_score"] += t.get("signal_score", 0.0)
                entry["net_sentiment"] += t.get("net_sentiment", 0.0)
                entry["sources"][username] = t["occurrences"]

        all_ticker_symbols = list(combined.keys())

        if all_ticker_symbols:
            emit({"type": "progress",
                  "message": f"Looking up sector data for {len(all_ticker_symbols)} ticker(s)..."})
            sector_map = lookup_sectors(all_ticker_symbols)
            emit({"type": "progress",
                  "message": f"Fetching prices for {len(all_ticker_symbols)} ticker(s)..."})
            price_map = lookup_prices(all_ticker_symbols)
        else:
            sector_map = {}
            price_map = {}

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
            t["price_suspicious"] = p.get("suspicious", False)

        for data in run["results"].values():
            if not data["error"]:
                for t in data["tickers"]:
                    _enrich(t)

        # Finalize derived signal fields and rank by CONVICTION, not raw counts:
        # distinct accounts first (kills single-account cashtag spam like $TSLA),
        # then aggregate signal_score, then total mentions as a tiebreaker.
        for entry in combined.values():
            entry["accounts"] = len(entry["sources"])
            entry["signal_score"] = round(entry["signal_score"], 3)
            entry["net_sentiment"] = round(entry["net_sentiment"], 2)
            entry["sentiment_label"] = (
                "bullish" if entry["net_sentiment"] > 0.15
                else "bearish" if entry["net_sentiment"] < -0.15
                else "mixed/neutral"
            )
            # Low-confidence: one account, no cashtag, weak signal — surface but de-rank.
            entry["low_confidence"] = (
                entry["accounts"] == 1
                and entry["cashtag_mentions"] == 0
            )

        combined_list = sorted(
            combined.values(),
            key=lambda x: (-x["accounts"], -x["signal_score"], -x["total_mentions"]),
        )
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

        if store is not None:
            try:
                store.record_run(run)
            except Exception as exc:  # persistence must never break a scan
                print(f"[!] store.record_run failed (non-fatal): {exc}")

        slug = "_".join(valid_usernames[:3])
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{slug}.json"
        _write_json_atomic(OUTPUT_DIR / filename, run)

        done_msg = {"type": "done", "result": run, "saved_as": filename}
        with _scans_lock:
            if scan_id in _scans:
                _scans[scan_id]["final"] = done_msg
        q.put(done_msg)

    threading.Thread(target=run_scan, daemon=True).start()
    return jsonify({"scan_id": scan_id})


@app.route("/scan/stream/<scan_id>")
def scan_stream(scan_id):
    with _scans_lock:
        scan = _scans.get(scan_id)

    if not scan:
        def not_found():
            yield 'data: {"type":"error","message":"Scan not found or expired"}\n\n'
        return Response(not_found(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Reconnecting client — scan already finished
    if scan.get("final"):
        final = scan["final"]
        def immediate():
            yield f"data: {json.dumps(final)}\n\n"
        return Response(immediate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def generate():
        q = scan["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/results/<path:filename>")
def view_result(filename):
    # send_from_directory resolves the path inside OUTPUT_DIR and raises 404
    # for any traversal attempt (e.g. ../../.env), preventing path traversal.
    return send_from_directory(OUTPUT_DIR, filename, mimetype="application/json")


@app.route("/watchlists", methods=["GET"])
def get_watchlists():
    return jsonify(_load_watchlists())


@app.route("/watchlists", methods=["POST"])
def save_watchlist():
    body = request.get_json(force=True)
    name = str(body.get("name", "")).strip()
    accounts = body.get("accounts", [])

    if not name or len(name) > 50:
        return jsonify({"error": "Watchlist name must be 1–50 characters"}), 400
    if not re.match(r'^[A-Za-z0-9 _\-]{1,50}$', name):
        return jsonify({"error": "Watchlist name: letters, digits, spaces, _ and - only"}), 400
    if not accounts or not isinstance(accounts, list):
        return jsonify({"error": "accounts must be a non-empty list"}), 400

    valid_accounts = []
    for a in accounts[:20]:
        try:
            valid_accounts.append(validate_username(str(a)))
        except ValueError:
            pass

    if not valid_accounts:
        return jsonify({"error": "No valid usernames in the list"}), 400

    wl = _load_watchlists()
    wl[name] = valid_accounts
    _save_watchlists(wl)
    return jsonify({"ok": True, "name": name, "accounts": valid_accounts})


@app.route("/watchlists/<path:name>", methods=["DELETE"])
def delete_watchlist(name):
    wl = _load_watchlists()
    if name not in wl:
        return jsonify({"error": "Watchlist not found"}), 404
    del wl[name]
    _save_watchlists(wl)
    return jsonify({"ok": True})


@app.route("/velocity/<ticker>")
def velocity(ticker):
    if store is None:
        return jsonify({"error": "persistence not available"}), 503
    try:
        days = max(1, min(int(request.args.get("days", 7)), 90))
    except (TypeError, ValueError):
        days = 7
    return jsonify({
        "ticker": ticker.upper(),
        "velocity": store.mention_velocity(ticker, days),
        "first_mentions": store.first_mentions(ticker),
    })


@app.route("/scorecard")
def scorecard():
    if store is None:
        return jsonify({"error": "persistence not available"}), 503
    try:
        min_calls = max(1, min(int(request.args.get("min_calls", 3)), 100))
    except (TypeError, ValueError):
        min_calls = 3
    return jsonify(store.account_scorecard(min_calls))


def _write_json_atomic(path: Path, data) -> None:
    """Write JSON to a temp file then atomically rename — safe against crashes and races."""
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
