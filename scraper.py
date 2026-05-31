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


class SessionExpired(Exception):
    """Raised when there is no valid cached session. Use /connect-x to log in."""


def session_status() -> dict:
    """Return whether a cached session file exists."""
    return {"connected": SESSION_FILE.exists()}


def validate_username(username: str) -> str:
    """Strip @ and assert the username matches X's allowed character set."""
    username = username.strip().lstrip("@")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            f"Invalid username '{username}'. "
            "Only letters, digits and underscores are allowed (max 50 chars)."
        )
    return username


async def _do_login(page, username: str, password: str, email: str = "", progress=None) -> None:
    """
    Shared login logic for both headless and visible-browser paths.
    Uses X's 2026 step-by-step flow: username → Next → password → Log in.
    Auto-fills all fields from the supplied credentials.
    """
    print("[→] Logging in to X...")
    # Use the direct login flow URL (avoids the onboarding modal redirect)
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Step 1: username / email field
    try:
        un_field = await page.wait_for_selector(
            'input[autocomplete="username"], input[name="text"]',
            state="visible", timeout=20000,
        )
        await un_field.fill(username)
        await asyncio.sleep(0.4)
    except PWTimeout:
        raise RuntimeError("X login form did not appear — X may be temporarily blocking logins.")

    # Click the "Next" button
    try:
        next_btn = await page.wait_for_selector(
            '[data-testid="LoginForm_Login_Button"], [role="button"]:has-text("Next")',
            state="visible", timeout=8000,
        )
        await next_btn.click()
    except PWTimeout:
        await page.keyboard.press("Enter")
    await asyncio.sleep(1.5)

    # Unusual-activity prompt (asks for email/phone to verify identity)
    try:
        unusual = await page.wait_for_selector(
            'input[data-testid="ocfEnterTextTextInput"]', state="visible", timeout=4000
        )
        val = email or username  # use X_EMAIL if set, else fall back to username
        await unusual.fill(val)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1.5)
    except PWTimeout:
        pass

    # Step 2: password field
    try:
        pw_field = await page.wait_for_selector(
            'input[name="password"], input[autocomplete="current-password"]',
            state="visible", timeout=15000,
        )
        await pw_field.fill(password)
        await asyncio.sleep(0.4)
    except PWTimeout:
        raise RuntimeError("Password field did not appear after entering username.")

    # Click "Log in"
    try:
        login_btn = await page.wait_for_selector(
            '[data-testid="LoginForm_Login_Button"], [role="button"]:has-text("Log in")',
            state="visible", timeout=8000,
        )
        await login_btn.click()
    except PWTimeout:
        await page.keyboard.press("Enter")
    await asyncio.sleep(2)

    # 2FA prompt — auto-fill not supported; user must complete in the browser.
    try:
        await page.wait_for_selector(
            'input[data-testid="LoginForm_2FA_Input"]', state="visible", timeout=5000
        )
        _emit(progress, {
            "type": "progress",
            "message": "⚠️ X is asking for a 2FA code — please enter it in the browser window (up to 2 min).",
        })
        print("[!] 2FA prompt — waiting up to 2 min for manual completion...")
    except PWTimeout:
        pass

    # Wait for successful redirect to home (up to 2 min covers manual 2FA)
    await page.wait_for_url("**/home", timeout=120_000)
    print("[✓] Login successful")


async def _login(page, username: str, password: str, progress=None) -> None:
    x_email = os.getenv("X_EMAIL", "").strip()
    await _do_login(page, username, password, email=x_email, progress=progress)


async def _manual_login() -> None:
    """
    Fully-automatic login using credentials from .env.
    Opens a real Chrome window (avoids automation detection).
    Saves session.json on success.
    """
    x_user = os.getenv("X_USERNAME", "").strip()
    x_pass = os.getenv("X_PASSWORD", "").strip()
    x_email = os.getenv("X_EMAIL", "").strip()
    if not x_user or not x_pass:
        raise ValueError("Set X_USERNAME and X_PASSWORD in .env")

    async with async_playwright() as pw:
        for channel in ("chrome", None):
            try:
                kwargs = {"headless": False, "slow_mo": 80}
                if channel:
                    kwargs["channel"] = channel
                browser = await pw.chromium.launch(**kwargs)
                break
            except Exception:
                if channel is None:
                    raise

        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await _do_login(page, x_user, x_pass, email=x_email)
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


_NUM_RE = re.compile(r'([\d,.]+)\s*([KMB]?)', re.IGNORECASE)
_MULT = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _to_int(token: str) -> Optional[int]:
    """Parse engagement strings like '1,234', '12.3K', '4M' into ints."""
    if not token:
        return None
    m = _NUM_RE.search(token.strip())
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")) * _MULT[m.group(2).upper()])
    except (ValueError, KeyError):
        return None


async def _extract_engagement(article) -> dict:
    """
    Best-effort engagement counts from the article's role="group" aria-label,
    e.g. '12 replies, 34 reposts, 567 likes, 8901 views'. All guarded — any
    DOM drift just yields None rather than raising.
    """
    out = {"replies": None, "reposts": None, "likes": None, "views": None}
    try:
        group = await article.query_selector('[role="group"][aria-label]')
        if not group:
            return out
        label = (await group.get_attribute("aria-label") or "").lower()
        for part in label.split(","):
            part = part.strip()
            for key, kw in (("replies", "repl"), ("reposts", "repost"),
                            ("likes", "like"), ("views", "view")):
                if kw in part:
                    out[key] = _to_int(part)
    except Exception:
        pass
    return out


async def _extract_permalink(article, username: str) -> Optional[str]:
    """Status URL from the timestamp link (a[href*='/status/'])."""
    try:
        a = await article.query_selector('a[href*="/status/"]')
        if a:
            href = await a.get_attribute("href")
            if href:
                return href if href.startswith("http") else f"https://x.com{href}"
    except Exception:
        pass
    return None


async def _fetch_follower_count(page) -> Optional[int]:
    """Best-effort follower count from the profile header."""
    try:
        a = await page.query_selector('a[href$="/verified_followers"], a[href$="/followers"]')
        if a:
            txt = (await a.inner_text()).strip()
            return _to_int(txt.split()[0]) if txt else None
    except Exception:
        pass
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

    follower_count = await _fetch_follower_count(page)

    posts = []
    seen: set = set()
    scrolls = 0
    stopped_by = "end_of_timeline"
    last_old_date: Optional[str] = None  # most recent original post that was date-filtered
    MAX_SCROLLS = max(30, count // 3)
    # Stop after this many consecutive old original posts without finding new content.
    # Stopping on the very first old post is too aggressive — recent content can be
    # interleaved further down the timeline.
    MAX_OLD_SKIPS = 5
    consecutive_old = 0

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
            is_repost = False
            social_ctx = await article.query_selector('[data-testid="socialContext"]')
            if social_ctx:
                skip_date_cutoff = True
                try:
                    ctx_txt = (await social_ctx.inner_text()).lower()
                    is_repost = "repost" in ctx_txt or "retweet" in ctx_txt
                except Exception:
                    is_repost = False

            time_el = await article.query_selector('time')
            posted_at = None
            if time_el:
                posted_at = await time_el.get_attribute('datetime')

            # Date cutoff — only applied to original posts, not retweets/pinned.
            # Skip old posts and keep going; only stop after MAX_OLD_SKIPS consecutive
            # old original posts with no newer content found in between.
            if since_date and posted_at and not skip_date_cutoff:
                post_dt = _parse_iso(posted_at)
                if post_dt and post_dt < since_date:
                    seen.add(text)  # mark seen so we skip it on future scrolls too
                    consecutive_old += 1
                    if consecutive_old == 1:
                        last_old_date = posted_at  # track most recent skipped date
                    if consecutive_old >= MAX_OLD_SKIPS:
                        cutoff_hit = True
                        stopped_by = "date"
                        break
                    continue

            consecutive_old = 0  # reset whenever we accept a post
            seen.add(text)
            engagement = await _extract_engagement(article)
            url = await _extract_permalink(article, username)
            posts.append({
                "text": text,
                "posted_at": posted_at,
                "url": url,
                "is_repost": is_repost,
                **engagement,
            })

            if len(posts) >= count:
                stopped_by = "count"
                break

        if cutoff_hit or stopped_by == "count":
            break

        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.3)
        scrolls += 1

    # Sort newest-first so the most recently posted content is always at the top.
    # X's timeline order is not strictly chronological (pinned posts, retweets,
    # and algorithmic mixing can interleave old and new content).
    posts.sort(key=lambda p: p.get("posted_at") or "", reverse=True)

    return {"posts": posts, "stopped_by": stopped_by,
            "last_post_date": last_old_date, "follower_count": follower_count}


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
    count = max(1, min(count, 200))
    results: dict = {}

    _emit(progress, {"type": "start", "message": f"Starting scan for {len(usernames)} account(s)..."})

    async with async_playwright() as pw:
        if not SESSION_FILE.exists():
            raise SessionExpired(
                "No X session found. Click 'Connect X Account' to log in first."
            )

        browser = await pw.chromium.launch(headless=True, slow_mo=60)
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "storage_state": str(SESSION_FILE),
        }
        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded")

        # Wait up to 10 s for the account-switcher button — gives the page time
        # to fully hydrate before we conclude the session is expired.
        logged_in = None
        try:
            await page.wait_for_selector(
                '[data-testid="SideNav_AccountSwitcher_Button"]', timeout=10000
            )
            logged_in = True
        except PWTimeout:
            logged_in = False

        if not logged_in:
            await browser.close()
            SESSION_FILE.unlink(missing_ok=True)
            raise SessionExpired(
                "X session has expired. Click 'Connect X Account' to log in again."
            )

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
                    "last_post_date": result.get("last_post_date"),
                    "follower_count": result.get("follower_count"),
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

        # Persist any refreshed cookies so the session stays alive between runs
        await context.storage_state(path=str(SESSION_FILE))
        await browser.close()

    return results
