"""
Convert exported browser cookies (Cookie-Editor JSON format) into a
Playwright session.json so the scraper skips login entirely.

Usage:
    1. Install the Cookie-Editor extension (https://cookie-editor.com/)
    2. Go to x.com while logged in
    3. Click Cookie-Editor → Export → "Export as JSON" → copy the JSON
    4. Paste it into a file called cookies.json in this folder
    5. Run:  python3 import_cookies.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

COOKIES_FILE = Path(__file__).parent / "cookies.json"
SESSION_FILE = Path(__file__).parent / "session.json"

SAMSITE_MAP = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def convert(raw: list) -> dict:
    cookies = []
    for c in raw:
        same_site_raw = str(c.get("sameSite", "Lax")).lower()
        same_site = SAMSITE_MAP.get(same_site_raw, "Lax")

        cookies.append({
            "name":     c["name"],
            "value":    c["value"],
            "domain":   c.get("domain", ".x.com"),
            "path":     c.get("path", "/"),
            "expires":  c.get("expirationDate", -1),
            "httpOnly": c.get("httpOnly", False),
            "secure":   c.get("secure", True),
            "sameSite": same_site,
        })
    return {"cookies": cookies, "origins": []}


def _write_session_secure(session: dict) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(dir=SESSION_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(session, f, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, SESSION_FILE)
        SESSION_FILE.chmod(0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    if not COOKIES_FILE.exists():
        print(f"[✗] {COOKIES_FILE} not found.")
        print("    Export your x.com cookies with Cookie-Editor → Export as JSON,")
        print("    save the file as  cookies.json  in this folder, then re-run.")
        sys.exit(1)

    with open(COOKIES_FILE) as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        print("[✗] Expected a JSON array of cookies. Make sure you used 'Export as JSON'.")
        sys.exit(1)

    session = convert(raw)

    _write_session_secure(session)

    print(f"[✓] Wrote {len(session['cookies'])} cookies to {SESSION_FILE}")
    print("    The scraper will now use this session and skip login.")


if __name__ == "__main__":
    main()
