"""
Offline regression tests for safety and persistence behavior.

Run:
    python3 test_safety_regressions.py
"""
import os
import queue
import tempfile
from base64 import b64encode
from datetime import timezone
from pathlib import Path

import app
import import_cookies
import store


def test_parse_since_date_accepts_zulu_timestamp():
    parsed = app._parse_since_date("2026-06-11T12:00:00Z", "America/New_York")
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-06-11T12:00:00+00:00"


def test_register_scan_rejects_second_active_scan():
    original_scans = dict(app._scans)
    original_max_active = app.MAX_ACTIVE_SCANS
    try:
        app._scans.clear()
        app.MAX_ACTIVE_SCANS = 1

        q1 = queue.Queue()
        q2 = queue.Queue()
        assert app._register_scan("scan-1", q1) is True
        assert app._register_scan("scan-2", q2) is False

        app._complete_scan("scan-1", {"type": "error", "message": "done"})
        assert q1.get_nowait()["type"] == "error"
        assert app._register_scan("scan-2", q2) is True
    finally:
        app._scans.clear()
        app._scans.update(original_scans)
        app.MAX_ACTIVE_SCANS = original_max_active


def test_store_keeps_same_source_post_for_each_account():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            post = {
                "text": "Watching $NVDA here",
                "posted_at": "2026-06-11T12:00:00.000Z",
                "url": "https://x.com/source/status/12345",
                "likes": 1,
                "reposts": 2,
                "replies": 3,
                "views": 4,
                "is_repost": True,
            }
            occurrence = {
                "post_index": 1,
                "posted_at": post["posted_at"],
                "confidence": "cashtag",
                "sentiment": "bullish",
                "sentiment_score": 0.5,
                "signal_weight": 1.0,
                "is_trailing_tag": False,
            }
            run = {
                "combined_tickers": [{"ticker": "NVDA", "price": 100.0}],
                "results": {
                    "alpha": {
                        "error": None,
                        "follower_count": 10,
                        "posts": [post],
                        "tickers": [{"ticker": "NVDA", "occurrences": [occurrence]}],
                    },
                    "beta": {
                        "error": None,
                        "follower_count": 20,
                        "posts": [post],
                        "tickers": [{"ticker": "NVDA", "occurrences": [occurrence]}],
                    },
                },
            }

            store.record_run(run)

            with store._conn() as conn:
                rows = conn.execute(
                    "SELECT account, post_id, ticker FROM mentions ORDER BY account"
                ).fetchall()

            assert rows == [
                ("alpha", "alpha:12345", "NVDA"),
                ("beta", "beta:12345", "NVDA"),
            ]
    finally:
        store.DB_PATH = original_db_path


def test_import_cookies_writes_owner_only_session_file():
    original_session_file = import_cookies.SESSION_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            import_cookies.SESSION_FILE = Path(tmp) / "session.json"
            import_cookies._write_session_secure({"cookies": [], "origins": []})
            mode = os.stat(import_cookies.SESSION_FILE).st_mode & 0o777
            assert mode == 0o600
    finally:
        import_cookies.SESSION_FILE = original_session_file


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"passed {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")


def test_velocity_endpoint_accepts_share_class_tickers():
    original_db_path = store.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as tmp:
            store.DB_PATH = Path(tmp) / "scraper.db"
            client = app.app.test_client()
            assert client.get("/velocity/BRK.B").status_code == 200
            assert client.get("/velocity/NVDA").status_code == 200
            assert client.get("/velocity/AB%2FCD").status_code in (400, 404)
            assert client.get("/velocity/TOOLONGG").status_code == 400
    finally:
        store.DB_PATH = original_db_path


def test_basic_auth_protects_app_when_password_is_set():
    original_user = os.environ.get("APP_USERNAME")
    original_password = os.environ.get("APP_PASSWORD")
    try:
        os.environ["APP_USERNAME"] = "me"
        os.environ["APP_PASSWORD"] = "secret"
        client = app.app.test_client()

        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 401

        token = b64encode(b"me:secret").decode()
        assert client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
    finally:
        if original_user is None:
            os.environ.pop("APP_USERNAME", None)
        else:
            os.environ["APP_USERNAME"] = original_user
        if original_password is None:
            os.environ.pop("APP_PASSWORD", None)
        else:
            os.environ["APP_PASSWORD"] = original_password


if __name__ == "__main__":
    _run_all()
