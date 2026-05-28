import asyncio
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

load_dotenv()

SESSION_FILE = Path(__file__).parent / "session.json"

# Valid X username: 1–50 alphanumeric/underscore characters
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,50}$')


def _emit(progress, msg: dict) -> None:
    if progress:
        try:
            progress(msg)
        except Exception:
            pass


class InteractiveLoginRequired(Exception):
    """
    Raised when X demands interactive verification (unusual-activity check or
    2FA) but there is no TTY available to prompt the user.  The caller should
    surface this as a user-facing error and ask them to run the scraper once
    from a terminal so the session can be cached.
    """


def validate_username(username: str) -> str:
    """Strip @ and assert the username matches X's allowed character set."""
    username = username.strip().lstrip("@")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            f"Invalid username '{username}'. "
            "Only letters, digits and underscores are allowed (max 50 chars)."
        )
    return username


async def _login(page, username: str, password: str) -> None:
    print("[→] Logging in to X...")
    await page.goto("https://x.com/login", wait_until="networkidle")

    await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
    await page.fill('input[autocomplete="username"]', username)
    await asyncio.sleep(0.7)
    await page.keyboard.press("Enter")
    await asyncio.sleep(1.5)

    # Unusual-activity check — X asks for email/phone before password.
    # We cannot prompt interactively from an HTTP request handler, so we raise
    # a structured error.  The user must complete login once from a terminal.
    try:
        await page.wait_for_selector(
            'input[data-testid="ocfEnterTextTextInput"]', timeout=4000
        )
        raise InteractiveLoginRequired(
            "X is asking for an unusual-activity verification (email/phone). "
            "Run `python3 -c \"import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())\"` "
            "from your terminal to complete login, then retry."
        )
    except PWTimeout:
        pass  # no unusual-activity prompt — continue

    await page.wait_for_selector('input[name="password"]', timeout=10000)
    await page.fill('input[name="password"]', password)
    await asyncio.sleep(0.7)
    await page.keyboard.press("Enter")
    await asyncio.sleep(2.5)

    # 2FA — same problem: cannot block on input() in an HTTP handler.
    try:
        await page.wait_for_selector(
            'input[data-testid="LoginForm_2FA_Input"], input[name="text"]',
            timeout=5000,
        )
        raise InteractiveLoginRequired(
            "X is asking for a 2FA code. "
            "Run `python3 -c \"import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())\"` "
            "from your terminal to complete login and cache the session, then retry."
        )
    except PWTimeout:
        pass  # no 2FA prompt

    await page.wait_for_url("**/home", timeout=25000)
    print("[✓] Login successful")


async def _manual_login() -> None:
    """
    Interactive login helper — run this directly from a terminal when X
    demands verification that cannot be handled headlessly.
    Saves the session so subsequent scrapes run headlessly.
    """
    x_user = os.getenv("X_USERNAME", "").strip()
    x_pass = os.getenv("X_PASSWORD", "").strip()
    if not x_user or not x_pass:
        raise ValueError("Set X_USERNAME and X_PASSWORD in .env")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=80)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto("https://x.com/login", wait_until="networkidle")

        await page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        await page.fill('input[autocomplete="username"]', x_user)
        await asyncio.sleep(0.7)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)

        # Unusual-activity prompt — fill interactively
        try:
            unusual = await page.wait_for_selector(
                'input[data-testid="ocfEnterTextTextInput"]', timeout=4000
            )
            val = input("[X] Unusual activity — enter your email or phone: ").strip()
            await unusual.fill(val)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.5)
        except PWTimeout:
            pass

        await page.wait_for_selector('input[name="password"]', timeout=10000)
        await page.fill('input[name="password"]', x_pass)
        await asyncio.sleep(0.7)
        await page.keyboard.press("Enter")
        await asyncio.sleep(2.5)

        # 2FA prompt — fill interactively
        try:
            totp = await page.wait_for_selector(
                'input[data-testid="LoginForm_2FA_Input"], input[name="text"]',
                timeout=5000,
            )
            code = input("[X] Enter your 2FA code: ").strip()
            await totp.fill(code)
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)
        except PWTimeout:
            pass

        await page.wait_for_url("**/home", timeout=25000)
        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()
        print(f"[✓] Session saved to {SESSION_FILE}. You can now use the web app.")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


async def _fetch_posts(
    page,
    username: str,
    count: int = 10,
    since_date: Optional[datetime] = None,
) -> dict:
    """
    Collect up to `count` posts, stopping early if a post is older than `since_date`.
    Returns {"posts": [...], "stopped_by": "count"|"date"|"end_of_timeline"}.
    """
    await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")

    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
    except PWTimeout:
        body = await page.inner_text("body")
        if "doesn't exist" in body or "account suspended" in body.lower():
            raise ValueError(f"Account @{username} not found or suspended")
        if "protected" in body.lower():
            raise ValueError(f"Account @{username} has protected tweets")
        raise ValueError(f"Could not load timeline for @{username}")

    posts = []
    seen: set = set()
    scrolls = 0
    stopped_by = "end_of_timeline"
    MAX_SCROLLS = max(30, count // 3)

    while len(posts) < count and scrolls < MAX_SCROLLS:
        articles = await page.query_selector_all('article[data-testid="tweet"]')
        cutoff_hit = False

        for article in articles:
            el = await article.query_selector('[data-testid="tweetText"]')
            if not el:
                continue
            text = (await el.inner_text()).strip()
            if not text or text in seen:
                continue

            # socialContext appears on pinned posts and retweets.
            # Both bypass the date cutoff: pinned posts are intentionally promoted
            # by the account owner regardless of age, and retweets carry the
            # *original* post's datetime — filtering on that would stop the scrape
            # before collecting genuinely recent content.
            skip_date_cutoff = False
            social_ctx = await article.query_selector('[data-testid="socialContext"]')
            if social_ctx:
                skip_date_cutoff = True

            time_el = await article.query_selector('time')
            posted_at = None
            if time_el:
                posted_at = await time_el.get_attribute('datetime')

            # Date cutoff — only applied to original posts, not retweets
            if since_date and posted_at and not skip_date_cutoff:
                post_dt = _parse_iso(posted_at)
                if post_dt and post_dt < since_date:
                    cutoff_hit = True
                    stopped_by = "date"
                    break

            seen.add(text)
            posts.append({"text": text, "posted_at": posted_at})

            if len(posts) >= count:
                stopped_by = "count"
                break

        if cutoff_hit or stopped_by == "count":
            break

        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.3)
        scrolls += 1

    return {"posts": posts, "stopped_by": stopped_by}


async def scrape_accounts(
    usernames: list,
    count: int = 10,
    since_date: Optional[datetime] = None,
    progress=None,
) -> dict:
    """
    Scrape posts from multiple X accounts.
    - usernames: already-validated list (no @ prefix, alphanumeric/underscore only)
    - count: max posts per account (1–200)
    - since_date: stop collecting posts older than this UTC datetime
    - progress: optional callable(dict) for real-time progress events

    NOTE: This function uses asyncio.run() from the Flask route, which works
    with Flask's default synchronous WSGI server.  Do not upgrade to async Flask
    or an ASGI server without refactoring this to an async view.

    Returns { username: { posts, stopped_by, error } }
    """
    x_user = os.getenv("X_USERNAME", "").strip()
    x_pass = os.getenv("X_PASSWORD", "").strip()

    if not x_user or not x_pass:
        raise ValueError("Set X_USERNAME and X_PASSWORD in your .env file")

    count = max(1, min(count, 200))
    results: dict = {}

    _emit(progress, {"type": "start", "message": f"Starting scan for {len(usernames)} account(s)..."})

    async with async_playwright() as pw:
        headless = SESSION_FILE.exists()
        browser = await pw.chromium.launch(headless=headless, slow_mo=60)

        ctx_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if SESSION_FILE.exists():
            ctx_kwargs["storage_state"] = str(SESSION_FILE)

        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded")
        await asyncio.sleep(2)

        if "login" in page.url or "/i/flow/" in page.url:
            if SESSION_FILE.exists():
                SESSION_FILE.unlink()
                await browser.close()
                browser = await pw.chromium.launch(headless=False, slow_mo=60)
                context = await browser.new_context(viewport={"width": 1280, "height": 900})
                page = await context.new_page()

            _emit(progress, {"type": "progress", "message": "Logging in to X..."})
            await _login(page, x_user, x_pass)
            await context.storage_state(path=str(SESSION_FILE))
        else:
            print("[✓] Using cached session")
            _emit(progress, {"type": "progress", "message": "Using cached X session"})

        for username in usernames:
            depth_msg = f"up to {count} posts"
            if since_date:
                depth_msg += f" since {since_date.strftime('%Y-%m-%d')}"
            print(f"[→] @{username} — fetching {depth_msg}...")
            _emit(progress, {"type": "account_start", "username": username,
                             "message": f"Scanning @{username} ({depth_msg})..."})
            try:
                result = await _fetch_posts(page, username, count=count, since_date=since_date)
                n = len(result["posts"])
                reason = result["stopped_by"]
                print(f"[✓] @{username}: {n} posts (stopped: {reason})")
                _emit(progress, {
                    "type": "account_done", "username": username,
                    "posts": n, "stopped_by": reason,
                    "message": f"@{username}: {n} posts ({reason.replace('_', ' ')})",
                })
                results[username] = {
                    "posts": result["posts"],
                    "stopped_by": reason,
                    "error": None,
                }
            except (ValueError, InteractiveLoginRequired) as exc:
                results[username] = {"posts": [], "stopped_by": None, "error": str(exc)}
                _emit(progress, {"type": "account_error", "username": username, "message": str(exc)})
                print(f"[✗] {exc}")
            except Exception as exc:
                results[username] = {"posts": [], "stopped_by": None, "error": f"Unexpected error: {exc}"}
                _emit(progress, {"type": "account_error", "username": username,
                                 "message": f"@{username}: Unexpected error: {exc}"})
                print(f"[✗] @{username}: {exc}")

            await asyncio.sleep(1.5)

        await browser.close()

    return results
