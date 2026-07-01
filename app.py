import asyncio
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context

from pipeline import SCRAPE_LOCK, get_tickers_db, process_scrape_results
from scraper import InteractiveLoginRequired, SessionExpired, scrape_accounts, session_status, validate_username

try:
    import store  # optional SQLite persistence (time series + scorecard)
except Exception:  # pragma: no cover
    store = None

try:
    import scheduler  # optional background auto-scan + macOS notifications
except Exception:  # pragma: no cover
    scheduler = None

load_dotenv()

app = Flask(__name__)
OUTPUT_DIR = Path(os.getenv("XTS_OUTPUT_DIR", Path(__file__).parent / "output")).expanduser()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WATCHLISTS_FILE = Path(__file__).parent / "data" / "watchlists.json"

# In-progress scan registry  {scan_id: {"queue": Queue, "final": dict|None, "ts": float}}
_scans: dict = {}
_scans_lock = threading.Lock()
MAX_SCANS = 20
MAX_ACTIVE_SCANS = 1
SCAN_TTL = 300  # evict finished scans after 5 minutes
_services_started = False
_services_lock = threading.Lock()


def start_background_services() -> None:
    """Initialize startup work for both local runs and production WSGI servers."""
    global _services_started
    with _services_lock:
        if _services_started:
            return
        _services_started = True

    print("[→] Loading US ticker database...")
    get_tickers_db()
    if scheduler is not None and os.getenv("XTS_DISABLE_SCHEDULER", "").lower() not in {"1", "true", "yes"}:
        scheduler.start()




def _prune_scans_locked() -> None:
    now = time.time()
    for sid in list(_scans.keys()):
        if now - _scans[sid]["ts"] > SCAN_TTL:
            del _scans[sid]
    while len(_scans) > MAX_SCANS:
        oldest = next(iter(_scans))
        del _scans[oldest]


def _register_scan(scan_id: str, q: queue.Queue) -> bool:
    with _scans_lock:
        _prune_scans_locked()
        active = sum(1 for scan in _scans.values() if scan.get("final") is None)
        if active >= MAX_ACTIVE_SCANS:
            return False
        _scans[scan_id] = {"queue": q, "final": None, "ts": time.time()}
        _prune_scans_locked()
        return True


def _complete_scan(scan_id: str, msg: dict) -> None:
    with _scans_lock:
        scan = _scans.get(scan_id)
        if not scan:
            return
        scan["final"] = msg
        scan["ts"] = time.time()
        q = scan["queue"]
    q.put(msg)


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


def _client_timezone(tz_name: Optional[str]):
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def _local_today_str(tz_name: Optional[str]) -> str:
    return datetime.now(_client_timezone(tz_name)).strftime("%Y-%m-%d")


def _parse_since_date(since_raw: str, client_tz_name: Optional[str]):
    raw = (since_raw or "").strip()
    if not raw:
        return None

    # Reject anything that isn't a plain date/datetime string before parsing.
    if len(raw) > 32 or not re.match(r'^[\d\-T:+Z.]+$', raw):
        raise ValueError("Invalid date format")

    parse_raw = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(parse_raw)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)

    client_tz = _client_timezone(client_tz_name)
    try:
        return parsed.replace(tzinfo=client_tz).astimezone(timezone.utc)
    except Exception:
        # DST gap/ambiguity (e.g. spring-forward) — fall back to UTC
        return parsed.replace(tzinfo=timezone.utc)


def build_digest(run: dict) -> dict:
    """
    Distill a finished run into a daily 'signal' summary. Cheap to compute and
    attached to every scan, so the dashboard can show a glanceable digest:
      - top_conviction: highest cross-account / signal-score names (spam excluded)
      - new_today:      tickers appearing for the FIRST time ever today (DB-backed)
      - accelerating:   today's mentions well above the prior few-day average
      - bullish/bearish: directional splits by aggregate sentiment
    DB-dependent sections degrade gracefully to empty if store is unavailable.
    """
    combined = run.get("combined_tickers", [])
    scan_settings = run.get("scan_settings") or {}
    client_tz_name = scan_settings.get("client_timezone")
    today = _local_today_str(client_tz_name)

    def slim(t: dict) -> dict:
        return {
            "ticker": t.get("ticker"),
            "company": t.get("company"),
            "sector": t.get("sector"),
            "price": t.get("price"),
            "change_pct": t.get("change_pct"),
            "total_mentions": t.get("total_mentions"),
            "accounts": t.get("accounts"),
            "signal_score": t.get("signal_score"),
            "conviction_score": t.get("conviction_score"),
            "net_sentiment": t.get("net_sentiment"),
            "sentiment_label": t.get("sentiment_label"),
            "sources": list((t.get("sources") or {}).keys()),
        }

    digest = {
        "date": today,
        "accounts": run.get("accounts_analyzed", []),
        "total_tickers": len(combined),
        "top_conviction": sorted(
            [slim(t) for t in combined if not t.get("low_confidence")],
            key=lambda x: (-(x.get("conviction_score") or 0), -(x.get("signal_score") or 0))
        )[:8],
        "bullish": [slim(t) for t in combined if t.get("sentiment_label") == "bullish"][:8],
        "bearish": [slim(t) for t in combined if t.get("sentiment_label") == "bearish"][:8],
        "new_today": [],
        "accelerating": [],
    }

    if store is not None and combined:
        syms = [t["ticker"] for t in combined]
        try:
            first_seen = store.ticker_first_seen(syms, tz_name=client_tz_name)
            daily = store.ticker_daily_counts(
                syms,
                days=5,
                tz_name=client_tz_name,
                today=today,
            )
        except Exception:
            first_seen, daily = {}, {}

        seen_new: set = set()
        for t in combined:
            sym = t["ticker"]
            # Stamp the full entry so the ranked table can badge first-ever mentions.
            t["is_new_today"] = first_seen.get(sym) == today
            if t["is_new_today"] and sym not in seen_new:
                seen_new.add(sym)
                digest["new_today"].append(slim(t))

            counts = daily.get(sym, {})
            today_n = counts.get(today, 0)
            prior = [v for d, v in counts.items() if d != today]
            # Skip acceleration for brand-new tickers (no prior history).
            # They are already captured in new_today; flagging them as
            # "accelerating" with no baseline is semantically wrong.
            if not prior:
                continue
            prior_avg = sum(prior) / len(prior)
            # Accelerating: at least 3 mentions today, 2× prior average, AND
            # at least +2 above baseline to filter low-volume noise (1→2 jumps).
            if today_n >= 3 and today_n > prior_avg * 2.0 and (today_n - prior_avg) >= 2:
                item = slim(t)
                item["today_mentions"] = today_n
                item["prior_avg"] = round(prior_avg, 1)
                item["accel_factor"] = round(today_n / max(prior_avg, 0.1), 1)
                digest["accelerating"].append(item)

        digest["accelerating"].sort(key=lambda x: -x["today_mentions"])

    return digest


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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/session-status")
def get_session_status():
    data = session_status()
    data["headless"] = os.getenv("XTS_CONNECT_HEADLESS", "").lower() in {"1", "true", "yes"}
    return jsonify(data)


@app.route("/session-health")
def session_health():
    """
    Proactive session check: verify the session file exists AND its cookies
    haven't expired. Returns { healthy, connected, reason }.
    """
    from scraper import SESSION_FILE
    if not SESSION_FILE.exists():
        return jsonify({"healthy": False, "connected": False, "reason": "No session file found."})
    try:
        with open(SESSION_FILE) as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        if not cookies:
            return jsonify({"healthy": False, "connected": True, "reason": "Session file has no cookies."})
        # Check if key auth cookies exist and haven't expired
        now = time.time()
        auth_names = {"auth_token", "ct0"}
        found = {}
        for c in cookies:
            name = c.get("name", "")
            if name in auth_names:
                expires = c.get("expires", -1)
                found[name] = expires
        if not found:
            return jsonify({"healthy": False, "connected": True,
                            "reason": "Session missing auth cookies (auth_token / ct0)."})
        for name, expires in found.items():
            if expires > 0 and expires < now:
                return jsonify({"healthy": False, "connected": True,
                                "reason": f"Cookie '{name}' expired."})
        # Check file age — sessions older than 7 days are risky
        age_days = (now - SESSION_FILE.stat().st_mtime) / 86400
        warning = None
        if age_days > 7:
            warning = f"Session is {int(age_days)} days old — consider refreshing."
        return jsonify({"healthy": True, "connected": True, "warning": warning,
                        "age_days": round(age_days, 1),
                        "auth_cookies": list(found.keys())})
    except Exception as exc:
        return jsonify({"healthy": False, "connected": True, "reason": str(exc)})


@app.route("/paste-cookies", methods=["POST"])
def paste_cookies():
    """
    Accept raw Cookie-Editor JSON (array of cookie objects) pasted from the
    browser, convert to Playwright session format, and save as session.json.
    This lets users fix expired sessions without touching the terminal.
    """
    from import_cookies import convert
    from scraper import SESSION_FILE, _secure_session_file
    body = request.get_json(force=True, silent=True)
    if not body:
        return jsonify({"ok": False, "message": "No JSON body received."}), 400
    # Accept either the raw array or { "cookies": [...] }
    raw = body if isinstance(body, list) else body.get("cookies", body)
    if not isinstance(raw, list) or len(raw) == 0:
        return jsonify({"ok": False, "message": "Expected a JSON array of cookies from Cookie-Editor."}), 400
    # Basic validation: each entry should have at least name + value
    for i, c in enumerate(raw[:200]):
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            return jsonify({"ok": False, "message": f"Cookie at index {i} is missing 'name' or 'value'."}), 400
    try:
        session = convert(raw)
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=SESSION_FILE.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(session, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(SESSION_FILE))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _secure_session_file()
        return jsonify({"ok": True, "message": f"Session saved with {len(session['cookies'])} cookies. You can now run scans."})
    except Exception as exc:
        return jsonify({"ok": False, "message": f"Failed to convert cookies: {exc}"}), 500


@app.route("/import-session", methods=["POST"])
def import_session():
    """
    Accept a session.json file upload and write it to SESSION_FILE.
    This is the recommended path for headless deployments (e.g. Render) where
    the browser-based /connect-x login is blocked by X's bot detection.

    Usage: generate session.json locally with _manual_login(), then upload it
    via the web UI's "Import Session File" button.
    """
    from scraper import SESSION_FILE, _secure_session_file  # local import
    f = request.files.get("session")
    if not f:
        return jsonify({"ok": False, "message": "No file uploaded."}), 400
    try:
        data = f.read()
        json.loads(data)  # validate well-formed JSON before writing
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=SESSION_FILE.parent)
        try:
            os.write(fd, data)
            os.close(fd)
            os.replace(tmp, str(SESSION_FILE))
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        _secure_session_file()
        return jsonify({"ok": True, "message": "Session imported successfully. You can now run scans."})
    except (json.JSONDecodeError, ValueError):
        return jsonify({"ok": False, "message": "Invalid file — must be a valid session.json exported by Playwright."}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route("/connect-x", methods=["POST"])
def connect_x():
    """
    Logs in to X and saves session.json.
    Locally this opens a visible Playwright browser. On hosted containers, set
    XTS_CONNECT_HEADLESS=1 to use credential-based headless login instead.
    Saves session.json on success.  Runs synchronously (blocks until done or error).
    """
    from scraper import _manual_login, _save_login_session  # local import avoids circular ref
    is_headless_server = os.getenv("XTS_CONNECT_HEADLESS", "").lower() in {"1", "true", "yes"}
    try:
        if is_headless_server:
            timeout = max(15, min(int(os.getenv("XTS_CONNECT_TIMEOUT", "120")), 180))

            async def headless_connect():
                await asyncio.wait_for(
                    _save_login_session(headless=True, slow_mo=60),
                    timeout=timeout,
                )

            asyncio.run(headless_connect())
        else:
            asyncio.run(_manual_login())
        return jsonify({"ok": True, "message": "Connected successfully. You can now run scans."})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        if is_headless_server:
            return jsonify({
                "ok": False,
                "message": (
                    "Browser-based login failed on this server — X blocks automated logins from cloud IPs. "
                    "Use the \"Import Session File\" button instead: generate session.json locally by running "
                    "python3 -c \"import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())\" "
                    "then upload the file."
                ),
            }), 400
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.route("/scrape", methods=["POST"])
def scrape():
    body = request.get_json(force=True) or {}

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

    # Optional since_date — ISO date string "YYYY-MM-DD" or datetime "YYYY-MM-DDTHH:MM:SS"
    since_date = None
    client_timezone = str(body.get("client_timezone", "")).strip()[:64]
    since_raw = str(body.get("since_date", "") or "").strip()
    if since_raw:
        try:
            since_date = _parse_since_date(since_raw, client_timezone)
        except ValueError:
            return jsonify({"error": "Invalid since_date. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."}), 400

    # The scrape lock is shared with the auto-scan scheduler: it is acquired
    # here in the request thread and released by the worker thread when the
    # scan finishes (threading.Lock permits cross-thread release).
    if not SCRAPE_LOCK.acquire(blocking=False):
        return jsonify({
            "error": "A scan is already running (possibly an automatic background scan). "
                     "Wait for it to finish before starting another."
        }), 429

    scan_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue(maxsize=200)
    if not _register_scan(scan_id, q):
        SCRAPE_LOCK.release()
        return jsonify({
            "error": "A scan is already running. Wait for it to finish before starting another."
        }), 429

    def run_scan():
        def emit(msg: dict) -> None:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # client fell behind — drop progress message

        try:
            scraped = asyncio.run(
                scrape_accounts(valid_usernames, count=count, since_date=since_date, progress=emit)
            )
        except (InteractiveLoginRequired, SessionExpired) as exc:
            _complete_scan(scan_id, {"type": "session_expired", "message": str(exc)})
            return
        except Exception as exc:
            _complete_scan(scan_id, {"type": "error", "message": str(exc)})
            return

        # Combine, enrich with prices, finalize signals, persist to the store —
        # shared with the auto-scan scheduler (pipeline.py).
        run = process_scrape_results(
            scraped,
            valid_usernames,
            count=count,
            since_raw=since_raw,
            client_timezone=client_timezone,
            emit=emit,
        )

        # Digest runs AFTER record_run so today's mentions are already in the DB.
        try:
            run["digest"] = build_digest(run)
        except Exception as exc:  # digest must never break a scan
            print(f"[!] build_digest failed (non-fatal): {exc}")
            run["digest"] = None
            run["digest_error"] = str(exc)

        slug = "_".join(valid_usernames[:3])
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{slug}.json"
        _write_json_atomic(OUTPUT_DIR / filename, run)

        done_msg = {"type": "done", "result": run, "saved_as": filename}
        _complete_scan(scan_id, done_msg)

    def run_scan_locked():
        try:
            run_scan()
        finally:
            SCRAPE_LOCK.release()

    threading.Thread(target=run_scan_locked, daemon=True).start()
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
                if msg.get("type") in ("done", "error", "session_expired"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'  # keepalive for Render's proxy

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
    if not re.match(r'^(?=.*[A-Za-z0-9])[A-Za-z0-9 _\-]{1,50}$', name):
        return jsonify({"error": "Watchlist name must contain at least one letter or digit, using only letters, digits, spaces, _ and -"}), 400
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


@app.route("/auto-scan/status")
def auto_scan_status():
    if scheduler is None:
        return jsonify({"available": False})
    s = scheduler.get_status()
    now = time.time()
    return jsonify({
        "available": True,
        "enabled": s["enabled"],
        "last_scan_at": s["last_scan_at"],
        "next_scan_at": s["next_scan_at"],
        "seconds_until_next": max(0, int((s["next_scan_at"] or now) - now)),
        "last_tickers": s["last_tickers"],
        "last_error": s["last_error"],
        "market_hours": scheduler.is_market_hours(),
        "market_session": scheduler.market_session(),
    })


@app.route("/auto-scan/toggle", methods=["POST"])
def auto_scan_toggle():
    if scheduler is None:
        return jsonify({"error": "scheduler not available"}), 503
    body = request.get_json(force=True)
    scheduler.set_enabled(bool(body.get("enabled", True)))
    return jsonify({"enabled": scheduler.get_status()["enabled"]})


@app.route("/velocity/<ticker>")
def velocity(ticker):
    if store is None:
        return jsonify({"error": "persistence not available"}), 503
    # Allow share-class symbols like BRK.B — the extractor preserves them.
    if not re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', ticker.upper()):
        return jsonify({"error": "Invalid ticker"}), 400
    try:
        days = max(1, min(int(request.args.get("days", 7)), 90))
    except (TypeError, ValueError):
        days = 7
    return jsonify({
        "ticker": ticker.upper(),
        "velocity": store.mention_velocity(ticker, days),
        "first_mentions": store.first_mentions(ticker),
    })


@app.route("/velocity/batch")
def velocity_batch():
    if store is None:
        return jsonify({"error": "persistence not available"}), 503
    raw = request.args.get("tickers", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    tickers = [t for t in tickers if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', t)][:50]
    if not tickers:
        return jsonify({})
    try:
        days = max(1, min(int(request.args.get("days", 7)), 30))
    except (TypeError, ValueError):
        days = 7
    return jsonify({t: store.mention_velocity(t, days) for t in tickers})


@app.route("/scorecard")
def scorecard():
    if store is None:
        return jsonify({"error": "persistence not available"}), 503
    return jsonify(store.account_scorecard(min_calls=_min_calls_arg(default=3)))


def _min_calls_arg(default=2):
    try:
        return max(1, min(int(request.args.get("min_calls", default)), 100))
    except (TypeError, ValueError):
        return default


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
    port = int(os.environ.get("PORT", "8080"))
    start_background_services()
    print(f"[✓] Ready — open http://localhost:{port}")
    app.run(debug=False, port=port)
