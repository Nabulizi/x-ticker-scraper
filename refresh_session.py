#!/usr/bin/env python3
"""
refresh_session.py — One command to refresh the X session on Render.

Usage:
    python3 refresh_session.py

What it does:
    1. Opens a real Chrome window on your Mac so you can log in to X normally.
    2. Saves session.json locally.
    3. Automatically uploads it to your running Render app via /import-session.

Required env vars (in .env or your shell):
    RENDER_APP_URL  — e.g. https://x-ticker-scraper.onrender.com
    APP_PASSWORD    — the APP_PASSWORD you set in Render
    APP_USERNAME    — optional, defaults to "admin"
"""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

RENDER_APP_URL = os.getenv("RENDER_APP_URL", "").rstrip("/")
APP_USERNAME = os.getenv("APP_USERNAME", "admin").strip() or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()


def _check_config() -> None:
    missing = []
    if not RENDER_APP_URL:
        missing.append("RENDER_APP_URL (e.g. https://x-ticker-scraper.onrender.com)")
    if not APP_PASSWORD:
        missing.append("APP_PASSWORD")
    if missing:
        print("ERROR: missing required env vars:")
        for m in missing:
            print(f"  {m}")
        print("\nAdd them to your .env file and re-run.")
        sys.exit(1)


async def _login_locally() -> Path:
    from scraper import SESSION_FILE, _manual_login

    print("Opening Chrome for X login...")
    print("Log in normally — the window will close automatically when done.\n")
    await _manual_login()
    print(f"\n✓ Session saved to {SESSION_FILE}")
    return SESSION_FILE


def _upload_session(session_file: Path) -> None:
    try:
        import requests
    except ImportError:
        print("ERROR: 'requests' not installed. Run: pip install requests")
        sys.exit(1)

    url = f"{RENDER_APP_URL}/import-session"
    print(f"\nUploading session to {url} ...")

    with open(session_file, "rb") as f:
        resp = requests.post(
            url,
            files={"session": ("session.json", f, "application/json")},
            auth=(APP_USERNAME, APP_PASSWORD),
            timeout=30,
        )

    try:
        data = resp.json()
    except Exception:
        print(f"ERROR: unexpected response ({resp.status_code}): {resp.text[:200]}")
        sys.exit(1)

    if resp.ok and data.get("ok"):
        print(f"✓ {data['message']}")
        print("\nDone! Your Render app is now connected to X.")
    else:
        msg = data.get("message") or resp.text[:200]
        print(f"ERROR: upload failed — {msg}")
        sys.exit(1)


async def main() -> None:
    _check_config()
    session_file = await _login_locally()
    _upload_session(session_file)


if __name__ == "__main__":
    asyncio.run(main())
