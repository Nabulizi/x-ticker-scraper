import asyncio
import base64
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from lxml import html as lxml_html
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

load_dotenv()

SESSION_FILE = Path(os.getenv("XTS_SESSION_FILE", Path(__file__).parent / "session.json")).expanduser()

# Valid X username: 1–50 alphanumeric/underscore characters
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,50}$')
_WHITESPACE_RE = re.compile(r"\s+")


def _secure_session_file() -> None:
    """Restrict session.json to owner-read/write only (chmod 600).
    Silently ignored on platforms that don't support chmod (e.g. Windows)."""
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


def _bootstrap_session_from_env() -> bool:
    """
    Write session.json from the XTS_SESSION_B64 env var when the file is absent.

    Usage on Render (or any headless server):
      1. Log in locally: python3 -c "import asyncio; from scraper import _manual_login; asyncio.run(_manual_login())"
      2. Encode: base64 -i session.json | tr -d '\\n'
      3. Paste the output as the XTS_SESSION_B64 environment variable on Render.
      4. Redeploy — the session file is written on first startup.

    Returns True if the session file was written, False otherwise.
    """
    raw = os.getenv("XTS_SESSION_B64", "").strip()
    if not raw or SESSION_FILE.exists():
        return False
    try:
        data = base64.b64decode(raw)
        json.loads(data)  # validate it is well-formed JSON before writing
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
        print(f"[✓] Session bootstrapped from XTS_SESSION_B64 → {SESSION_FILE}")
        return True
    except Exception as exc:
        print(f"[!] Failed to bootstrap session from XTS_SESSION_B64: {exc}")
        return False


# Bootstrap on module load so the session is ready before any request is handled.
_bootstrap_session_from_env()


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
            "Only letters, digits and underscores are allowed (1–50 chars)."
        )
    return username


# ── Stealth / anti-automation hardening ─────────────────────────────────────
# A headless Chromium broadcasts that it is automated: the UA says
# "HeadlessChrome", the AutomationControlled blink feature is on, the
# --enable-automation switch is set, and navigator.webdriver === true. Logged in
# to a real account, those are exactly the signals that get a scraping session
# rate-limited or the account flagged. We strip them here:
#   • a genuine-looking UA built from the REAL engine version (so the UA header
#     and the Sec-CH-UA client hints stay consistent — a mismatch is itself a
#     tell),
#   • automation launch flags removed,
#   • an init script that patches the usual JS fingerprint probes.
# (Inspired by Scrapling's StealthyFetcher — we keep our own Playwright auth /
# scroll logic and just borrow the hardening ideas.)

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    # Required for Chromium inside Docker/container environments (Render, etc.).
    # --no-sandbox: kernel namespacing is unavailable in most container runtimes.
    # --disable-dev-shm-usage: /dev/shm is only 64 MB by default in Docker;
    #   Chrome uses it for shared memory and crashes without this flag.
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

_STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(p);
}
try {
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';               // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
    return _getParam.call(this, p);
  };
} catch (e) {}
"""


def _stealth_user_agent(browser) -> str:
    """Genuine-looking desktop UA derived from the live engine version so the UA
    header and Sec-CH-UA client hints agree. Falls back to a recent version.
    Uses a Linux UA on Linux hosts (Render/Docker) so the OS in the UA string
    matches the actual platform — a Mac UA on a Linux host is a fingerprint tell."""
    version = ""
    try:
        version = (browser.version or "").strip()
    except Exception:
        pass
    if not version:
        version = "148.0.0.0"
    if sys.platform.startswith("linux"):
        platform_str = "X11; Linux x86_64"
    else:
        platform_str = "Macintosh; Intel Mac OS X 10_15_7"
    return (
        f"Mozilla/5.0 ({platform_str}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


async def _launch_stealth_browser(pw, headless: bool, slow_mo: int = 0):
    """Launch Chromium with the automation fingerprints stripped. Prefers the
    real installed Chrome channel (most genuine fingerprint) and falls back to
    Playwright's bundled Chromium."""
    last_err = None
    for channel in ("chrome", None):
        try:
            kwargs = {
                "headless": headless,
                "slow_mo": slow_mo,
                "args": _STEALTH_ARGS,
                "ignore_default_args": ["--enable-automation"],
            }
            if channel:
                kwargs["channel"] = channel
            return await pw.chromium.launch(**kwargs)
        except Exception as exc:  # noqa: BLE001 — try next channel
            last_err = exc
    raise last_err


def _stealth_context_kwargs(browser) -> dict:
    """new_context kwargs that make the session look like a normal desktop
    browser. Merge with storage_state at the call site."""
    return {
        "viewport": {"width": 1280, "height": 900},
        "user_agent": _stealth_user_agent(browser),
        "locale": "en-US",
        "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }


async def _apply_stealth(context) -> None:
    """Install the fingerprint-patching init script on every page in the context."""
    nav_platform = "Linux x86_64" if sys.platform.startswith("linux") else "MacIntel"
    platform_js = f'Object.defineProperty(navigator, "platform", {{get: () => "{nav_platform}"}});\n'
    await context.add_init_script(platform_js + _STEALTH_INIT_JS)


async def _wait_for_visible(page, selectors: list[str], timeout: int = 20000):
    last_exc = None
    for selector in selectors:
        try:
            return await page.wait_for_selector(selector, state="visible", timeout=timeout)
        except PWTimeout as exc:
            last_exc = exc
    raise last_exc or PWTimeout("No matching selector became visible")


async def _wait_for_first_locator(
    page,
    locator_builders: list[Callable],
    timeout: int = 20000,
):
    """
    Return the first locator that becomes visible within `timeout` ms.

    Polls all locators in a tight loop (every ~500 ms per candidate) until the
    global deadline, rather than splitting the budget equally.  This means any
    locator can match at any point during the full window — important when a
    page loads slowly (e.g. on a cold container).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000
    last_exc: Exception = PWTimeout("No matching locator became visible")
    while loop.time() < deadline:
        for build in locator_builders:
            try:
                locator = build(page).first
                await locator.wait_for(state="visible", timeout=500)
                return locator
            except PWTimeout:
                continue
        await asyncio.sleep(0.1)
    raise last_exc


async def _click_first_available(page, locator_builders: list[Callable], timeout: int = 8000) -> bool:
    per_try_timeout = max(800, timeout // max(len(locator_builders), 1))
    for build in locator_builders:
        try:
            locator = build(page).first
            await locator.wait_for(state="visible", timeout=per_try_timeout)
            await locator.click()
            return True
        except PWTimeout:
            continue
    return False


def _normalize_button_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip()).lower()


async def _click_button_by_text(page, names: list[str], timeout: int = 8000) -> bool:
    deadline = asyncio.get_running_loop().time() + (timeout / 1000)
    normalized_names = {_normalize_button_text(name) for name in names}
    while asyncio.get_running_loop().time() < deadline:
        buttons = await page.locator("button").all()
        for button in buttons:
            try:
                if not await button.is_visible():
                    continue
                text = _normalize_button_text(await button.inner_text())
                if text in normalized_names:
                    await button.click()
                    return True
            except Exception:
                continue
        await asyncio.sleep(0.2)
    return False


async def _wait_for_signed_in(page, timeout: int = 120_000) -> None:
    """
    Treat a signed-in shell as success even if X uses a different post-login URL.
    """
    selectors = [
        '[data-testid="SideNav_AccountSwitcher_Button"]',
        '[data-testid="AppTabBar_Home_Link"]',
        'a[aria-label="Home"]',
    ]
    last_exc = None
    per_try_timeout = max(1500, timeout // max(len(selectors), 1))
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, state="visible", timeout=per_try_timeout)
            return
        except PWTimeout as exc:
            last_exc = exc
    raise last_exc or PWTimeout("Signed-in X shell did not appear")


def _get_login_method() -> str:
    method = os.getenv("X_LOGIN_METHOD", "native").strip().lower()
    if method not in {"auto", "native", "google"}:
        return "native"
    return method


def _get_google_credentials(
    *,
    x_username: str = "",
    x_password: str = "",
    x_email: str = "",
) -> tuple[str, str]:
    google_email = os.getenv("GOOGLE_EMAIL", "").strip() or x_email
    google_password = os.getenv("GOOGLE_PASSWORD", "").strip() or x_password
    if not google_email or not google_password:
        raise ValueError(
            "Google sign-in requires GOOGLE_EMAIL and GOOGLE_PASSWORD "
            "(or X_EMAIL + X_PASSWORD) in .env."
        )
    return google_email, google_password


def _use_google_login() -> bool:
    return _get_login_method() == "google"


def _headless_only_mode() -> bool:
    flag = os.getenv("XTS_CONNECT_HEADLESS", "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    return not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


def _should_prefer_google_login(
    *,
    x_username: str = "",
    x_password: str = "",
    x_email: str = "",
) -> bool:
    method = _get_login_method()
    if method == "google":
        return True
    if method == "native":
        return False
    return _google_credentials_available(
        x_username=x_username,
        x_password=x_password,
        x_email=x_email,
    )


def _google_credentials_available(
    *,
    x_username: str = "",
    x_password: str = "",
    x_email: str = "",
) -> bool:
    try:
        _get_google_credentials(
            x_username=x_username,
            x_password=x_password,
            x_email=x_email,
        )
        return True
    except ValueError:
        return False


async def _complete_google_popup_login(
    page,
    *,
    google_email: str,
    google_password: str,
    progress=None,
) -> None:
    _emit(progress, {
        "type": "progress",
        "message": "X login page loaded. Opening Google sign-in flow...",
    })
    popup = None
    try:
        async with page.expect_popup(timeout=10000) as popup_info:
            clicked = await _click_first_available(
                page,
                [
                    lambda p: p.get_by_role("button", name="Continue with Google"),
                    lambda p: p.get_by_role("link", name="Continue with Google"),
                    lambda p: p.get_by_text("Continue with Google", exact=True),
                ],
                timeout=4000,
            )
            if not clicked:
                clicked = await _click_button_by_text(page, ["Continue with Google"], timeout=4000)
            if not clicked:
                raise RuntimeError("Continue with Google button did not appear.")
        popup = await popup_info.value
    except Exception:
        # Some headless environments do not materialize the popup. If the click
        # navigated the current page in place, keep going with that page.
        popup = page

    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        if popup is page:
            try:
                await page.wait_for_url("**accounts.google.com/**", timeout=5000)
            except PWTimeout:
                pass
        _emit(progress, {
            "type": "progress",
            "message": "Google sign-in ready. Entering Google email...",
        })
        email_field = await _wait_for_first_locator(
            popup,
            [
                lambda p: p.get_by_label("Email or phone"),
                lambda p: p.get_by_placeholder("Email or phone"),
                lambda p: p.locator('input[type="email"]'),
                lambda p: p.locator('input[name="identifier"]'),
            ],
            timeout=20000,
        )
        await email_field.fill(google_email)
        await asyncio.sleep(0.4)

        clicked = await _click_first_available(
            popup,
            [
                lambda p: p.get_by_role("button", name="Next"),
                lambda p: p.locator('button:has-text("Next")'),
            ],
            timeout=8000,
        )
        if not clicked:
            clicked = await _click_button_by_text(popup, ["Next"], timeout=4000)
        if not clicked:
            await popup.keyboard.press("Enter")

        _emit(progress, {
            "type": "progress",
            "message": "Google email submitted. Waiting for password field...",
        })
        pw_field = await _wait_for_first_locator(
            popup,
            [
                lambda p: p.get_by_label("Enter your password"),
                lambda p: p.get_by_label("Password"),
                lambda p: p.get_by_placeholder("Enter your password"),
                lambda p: p.locator('input[type="password"]'),
                lambda p: p.locator('input[name="Passwd"]'),
            ],
            timeout=20000,
        )
        await pw_field.fill(google_password)
        await asyncio.sleep(0.4)

        clicked = await _click_first_available(
            popup,
            [
                lambda p: p.get_by_role("button", name="Next"),
                lambda p: p.locator('button:has-text("Next")'),
            ],
            timeout=8000,
        )
        if not clicked:
            clicked = await _click_button_by_text(popup, ["Next"], timeout=4000)
        if not clicked:
            await popup.keyboard.press("Enter")

        _emit(progress, {
            "type": "progress",
            "message": "Google password submitted. Waiting for X to finish sign-in...",
        })
        try:
            await popup.wait_for_event("close", timeout=90000)
        except PWTimeout:
            await popup.wait_for_load_state("networkidle", timeout=15000)
            if not popup.is_closed():
                try:
                    await popup.close()
                except Exception:
                    pass
    finally:
        if popup is not page and popup and not popup.is_closed():
            try:
                await popup.close()
            except Exception:
                pass


async def _do_login(page, username: str, password: str, email: str = "", progress=None) -> None:
    """
    Shared login logic for both headless and visible-browser paths.
    Uses X's 2026 step-by-step flow: username → Next → password → Log in.
    Auto-fills all fields from the supplied credentials.
    """
    print("[→] Logging in to X...")
    # Use the direct login flow URL (avoids the onboarding modal redirect)
    _emit(progress, {
        "type": "progress",
        "message": "Opening the X login page...",
    })
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded")
    # Wait for React to finish mounting the login form. networkidle is ideal but
    # can hang on slow networks; fall back silently after 8 s.
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except PWTimeout:
        pass
    await asyncio.sleep(2)

    if _should_prefer_google_login(
        x_username=username,
        x_password=password,
        x_email=email,
    ):
        google_email, google_password = _get_google_credentials(
            x_username=username,
            x_password=password,
            x_email=email,
        )
        await _complete_google_popup_login(
            page,
            google_email=google_email,
            google_password=google_password,
            progress=progress,
        )
        await _wait_for_signed_in(page, timeout=120_000)
        print("[✓] Login successful")
        return

    # Step 1: username / email field
    try:
        _emit(progress, {
            "type": "progress",
            "message": "X login page loaded. Looking for the username field...",
        })
        un_field = await _wait_for_first_locator(
            page,
            [
                lambda p: p.get_by_label("Email or username", exact=True),
                lambda p: p.get_by_label("Phone, email, or username", exact=True),
                lambda p: p.get_by_placeholder("Email or username", exact=True),
                lambda p: p.get_by_placeholder("Phone, email, or username", exact=True),
                lambda p: p.locator('input[autocomplete="username"]'),
                lambda p: p.locator('input[name="text"]'),
                lambda p: p.locator('input[data-testid="ocfEnterTextTextInput"]'),
                lambda p: p.locator('input[type="text"]'),
            ],
            timeout=20000,
        )
        await un_field.fill(username)
        _emit(progress, {
            "type": "progress",
            "message": "Entered the X username/email. Continuing to password...",
        })
        await asyncio.sleep(0.4)
    except PWTimeout:
        raise RuntimeError("X login form did not appear — X may be temporarily blocking logins.")

    # Click the "Next" / "Continue" button
    clicked = await _click_first_available(
        page,
        [
            lambda p: p.get_by_role("button", name="Next"),
            lambda p: p.get_by_role("button", name="Continue"),
            lambda p: p.locator('[data-testid="LoginForm_Login_Button"]'),
            lambda p: p.locator('button:has-text("Next")'),
            lambda p: p.locator('button:has-text("Continue")'),
        ],
        timeout=8000,
    )
    if not clicked:
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
        _emit(progress, {
            "type": "progress",
            "message": "Waiting for the X password field...",
        })
        pw_field = await _wait_for_first_locator(
            page,
            [
                lambda p: p.get_by_label("Password", exact=True),
                lambda p: p.get_by_placeholder("Password", exact=True),
                lambda p: p.locator('input[name="password"]'),
                lambda p: p.locator('input[autocomplete="current-password"]'),
                lambda p: p.locator('input[type="password"]'),
            ],
            timeout=15000,
        )
        await pw_field.fill(password)
        _emit(progress, {
            "type": "progress",
            "message": "Entered the X password. Submitting login...",
        })
        await asyncio.sleep(0.4)
    except PWTimeout:
        raise RuntimeError("Password field did not appear after entering username.")

    # Click "Log in"
    clicked = await _click_first_available(
        page,
        [
            lambda p: p.get_by_role("button", name="Log in"),
            lambda p: p.get_by_role("button", name="Login"),
            lambda p: p.locator('[data-testid="LoginForm_Login_Button"]'),
            lambda p: p.locator('button:has-text("Log in")'),
            lambda p: p.locator('button:has-text("Login")'),
        ],
        timeout=8000,
    )
    if not clicked:
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

    # Wait for the signed-in shell, not just one exact URL shape.
    _emit(progress, {
        "type": "progress",
        "message": "Credentials submitted. Waiting for X to finish signing in...",
    })
    await _wait_for_signed_in(page, timeout=120_000)
    print("[✓] Login successful")


async def _login(page, username: str, password: str, progress=None) -> None:
    x_email = os.getenv("X_EMAIL", "").strip()
    await _do_login(page, username, password, email=x_email, progress=progress)


def _get_x_credentials() -> tuple[str, str, str]:
    x_user = os.getenv("X_USERNAME", "").strip()
    x_pass = os.getenv("X_PASSWORD", "").strip()
    x_email = os.getenv("X_EMAIL", "").strip()
    if not x_user or not x_pass:
        raise ValueError("Set X_USERNAME and X_PASSWORD in .env")
    return x_user, x_pass, x_email


async def _save_login_session(*, headless: bool, slow_mo: int = 0, progress=None) -> None:
    x_user, x_pass, x_email = _get_x_credentials()
    mode = "headless" if headless else "visible"
    _emit(progress, {
        "type": "progress",
        "message": f"Launching {mode} X login browser...",
    })

    async with async_playwright() as pw:
        _emit(progress, {
            "type": "progress",
            "message": f"{mode.capitalize()} Playwright runtime started. Launching Chrome...",
        })
        browser = await _launch_stealth_browser(pw, headless=headless, slow_mo=slow_mo)
        try:
            context = await browser.new_context(**_stealth_context_kwargs(browser))
            await _apply_stealth(context)
            page = await context.new_page()
            await _do_login(page, x_user, x_pass, email=x_email, progress=progress)

            try:
                await _wait_for_signed_in(page, timeout=10000)
            except PWTimeout as exc:
                raise SessionExpired("X login did not complete successfully.") from exc

            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(SESSION_FILE))
            _secure_session_file()
        finally:
            await browser.close()


async def _refresh_session(progress=None) -> None:
    """
    Attempt to restore session from XTS_SESSION_B64 env var.
    If unavailable, raise SessionExpired — the user must paste cookies or
    import a session.json via the web UI.
    """
    if _bootstrap_session_from_env():
        _emit(progress, {
            "type": "progress",
            "message": "Session restored from XTS_SESSION_B64 environment variable.",
        })
        return

    raise SessionExpired(
        "X session expired or missing. "
        "Paste fresh cookies via the 'Paste Cookies' button or import a session.json file."
    )


async def _manual_login() -> None:
    """
    Fully-automatic login using credentials from .env.
    Opens a real Chrome window (avoids automation detection).
    Saves session.json on success.
    """
    await _save_login_session(headless=False, slow_mo=80)
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
        val = int(float(m.group(1).replace(",", "")) * _MULT[m.group(2).upper()])
        # Cap at SQLite INTEGER max to prevent overflow on store
        return min(val, 9_223_372_036_854_775_807)
    except (ValueError, KeyError, OverflowError):
        return None


def _parse_engagement_label(label: Optional[str]) -> dict:
    """
    Parse an engagement aria-label like
    '12 replies, 34 reposts, 567 likes, 8901 views' into counts. Shared by the
    lxml fast path and the async fallback. Any drift yields None, never raises.
    """
    out = {"replies": None, "reposts": None, "likes": None, "views": None}
    if not label:
        return out
    for part in label.lower().split(","):
        part = part.strip()
        for key, kw in (("replies", "repl"), ("reposts", "repost"),
                        ("likes", "like"), ("views", "view")):
            if kw in part:
                out[key] = _to_int(part)
    return out


async def _extract_engagement(article) -> dict:
    """
    Best-effort engagement counts from the article's role="group" aria-label.
    All guarded — any DOM drift just yields None rather than raising.
    """
    try:
        group = await article.query_selector('[role="group"][aria-label]')
        if not group:
            return {"replies": None, "reposts": None, "likes": None, "views": None}
        return _parse_engagement_label(await group.get_attribute("aria-label"))
    except Exception:
        return {"replies": None, "reposts": None, "likes": None, "views": None}


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


async def _get_full_text(page2, url: str) -> Optional[str]:
    """
    Fetch the full text of a truncated post by navigating to its permalink.
    On the tweet detail page X always renders the complete text without "Show more".
    page2 is a secondary authenticated Playwright page — navigating it does not
    disrupt the main timeline scrape in progress on the primary page.
    """
    try:
        await page2.goto(url, wait_until="domcontentloaded")
        await page2.wait_for_selector(
            'article[data-testid="tweet"] [data-testid="tweetText"]', timeout=10000
        )
        # The first article on the detail page is always the main tweet
        articles = await page2.query_selector_all('article[data-testid="tweet"]')
        for art in articles:
            el = await art.query_selector('[data-testid="tweetText"]')
            if el:
                full = (await el.inner_text()).strip()
                if full:
                    return full
    except Exception:
        pass
    return None


def _normalize_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    return href if href.startswith("http") else f"https://x.com{href}"


def _render_tweet_text(node) -> str:
    """Reconstruct a tweetText node's visible text the way a browser would:
    emoji <img> become their alt char, <br> becomes a newline. Used only for
    dedup / empty checks — the accepted post's text is still read live via
    inner_text for byte-for-byte fidelity."""
    parts: list = []

    def walk(el):
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "img":
            parts.append(el.get("alt") or "")
        elif tag == "br":
            parts.append("\n")
        if el.text:
            parts.append(el.text)
        for child in el:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return "".join(parts).strip()


def _first(values: list):
    return values[0] if values else None


def _parse_article(article_html: str) -> dict:
    """Browser-free extraction of one timeline <article> via lxml — replaces a
    half-dozen async query_selector round-trips per article with one synchronous
    parse. Pure function → unit-testable against saved HTML fixtures.

    Returns a meta dict (has_text_node, text, url, posted_at, is_repost,
    has_show_more, engagement). On any parse failure returns {} so the caller
    falls back to the async path.
    """
    try:
        root = lxml_html.fromstring(article_html)
    except Exception:
        return {}

    text_nodes = root.xpath('.//*[@data-testid="tweetText"]')
    has_text_node = bool(text_nodes)
    text = _render_tweet_text(text_nodes[0]) if has_text_node else ""

    # Timestamp anchor first (the one wrapping <time>), else any /status/ link.
    href = _first(root.xpath('.//a[.//time]/@href')) or \
        _first(root.xpath('.//a[contains(@href, "/status/")]/@href'))
    url = _normalize_url(href)

    posted_at = _first(root.xpath('.//time/@datetime'))

    is_repost = False
    ctx_nodes = root.xpath('.//*[@data-testid="socialContext"]')
    if ctx_nodes:
        ctx_txt = (ctx_nodes[0].text_content() or "").lower()
        is_repost = "repost" in ctx_txt or "retweet" in ctx_txt

    has_show_more = bool(
        root.xpath('.//*[@data-testid="tweet-text-show-more-link"]'))

    label = _first(root.xpath('.//*[@role="group" and @aria-label]/@aria-label'))

    return {
        "has_text_node": has_text_node,
        "text": text,
        "url": url,
        "posted_at": posted_at,
        "is_repost": is_repost,
        "has_show_more": has_show_more,
        "engagement": _parse_engagement_label(label),
    }


async def _extract_meta_async(article, username: str) -> dict:
    """Async fallback mirroring _parse_article, used when the batched-HTML fast
    path is unavailable (parse failure or a DOM race). Same dict shape; behaves
    exactly like the pre-refactor inline logic."""
    el = await article.query_selector('[data-testid="tweetText"]')
    has_text_node = el is not None
    text = (await el.inner_text()).strip() if el else ""

    url = await _extract_permalink(article, username)

    is_repost = False
    social_ctx = await article.query_selector('[data-testid="socialContext"]')
    if social_ctx:
        try:
            ctx_txt = (await social_ctx.inner_text()).lower()
            is_repost = "repost" in ctx_txt or "retweet" in ctx_txt
        except Exception:
            pass

    posted_at = None
    time_el = await article.query_selector('time')
    if time_el:
        posted_at = await time_el.get_attribute('datetime')

    show_more = await article.query_selector('[data-testid="tweet-text-show-more-link"]')

    return {
        "has_text_node": has_text_node,
        "text": text,
        "url": url,
        "posted_at": posted_at,
        "is_repost": is_repost,
        "has_show_more": show_more is not None,
        "engagement": await _extract_engagement(article),
    }


async def _read_text(article) -> str:
    """Read the exact rendered tweet text from the live element (emoji, line
    breaks and link text identical to before the refactor)."""
    try:
        el = await article.query_selector('[data-testid="tweetText"]')
        if el:
            return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


def _timeline_load_error(username: str, body: str) -> ValueError:
    text = (body or "").lower()
    if "doesn't exist" in text or "account suspended" in text:
        return ValueError(f"Account @{username} not found or suspended")
    if "protected" in text:
        return ValueError(f"Account @{username} has protected tweets")
    if "rate limit" in text or "try again later" in text:
        return ValueError(f"X temporarily rate-limited @{username}'s timeline")
    if "something went wrong" in text or "retry" in text or "reload" in text:
        return ValueError(f"X temporarily failed to render @{username}'s timeline")
    if "log in" in text or "sign in" in text:
        return ValueError(f"X stopped showing @{username}'s timeline because the session is no longer fully authenticated")
    return ValueError(f"Could not load timeline for @{username}")


async def _fetch_posts(
    page,
    username: str,
    count: int = 10,
    since_date: Optional[datetime] = None,
    page2=None,
) -> dict:
    """
    Collect up to `count` posts, stopping early if a post is older than `since_date`.

    page2 is an optional secondary authenticated page used to expand truncated posts
    (those with a "Show more" link on the timeline). Without it, truncated text is
    still captured — just cut off at the point X hides.

    Returns {"posts": [...], "stopped_by": "count"|"date"|"end_of_timeline"}.
    """
    await page.goto(f"https://x.com/{username}", wait_until="domcontentloaded")

    for attempt in range(2):
        try:
            await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
            break
        except PWTimeout:
            body = await page.inner_text("body")
            if attempt == 0 and not any(
                token in (body or "").lower()
                for token in ("doesn't exist", "account suspended", "protected")
            ):
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
                continue
            raise _timeline_load_error(username, body)

    # Let the full initial viewport render — wait_for_selector fires on the FIRST
    # article, but React may still be painting the rest of the visible posts.
    await asyncio.sleep(1.5)

    follower_count = await _fetch_follower_count(page)

    posts = []
    seen_urls: set = set()   # URL-based dedup (more reliable than text)
    seen_texts: set = set()  # fallback dedup for posts without a URL
    scrolls = 0
    stopped_by = "end_of_timeline"
    last_old_date: Optional[str] = None
    MAX_SCROLLS = max(30, count // 3)
    # Stop after this many consecutive old *original* posts with no newer content.
    MAX_OLD_SKIPS = 5
    consecutive_old = 0
    # Heavy-curator accounts (mostly reposts) can have a long run of old reposts at
    # the top of the timeline. We drop those silently, but bound the scroll so we
    # don't crawl the whole profile looking for recent content that isn't there.
    MAX_OLD_REPOST_SKIPS = 20
    consecutive_old_reposts = 0

    while len(posts) < count and scrolls < MAX_SCROLLS:
        # ── Fast path ──────────────────────────────────────────────────────────
        # Read every visible article's HTML in ONE round-trip, then parse it
        # synchronously with lxml — instead of ~8 async query_selector calls per
        # article. `articles` (live handles) stays aligned with `parsed` (meta)
        # by document order; if the two reads disagree on count (a virtualization
        # mutation raced between them) we drop to the async path for safety.
        articles = await page.query_selector_all('article[data-testid="tweet"]')
        try:
            htmls = await page.eval_on_selector_all(
                'article[data-testid="tweet"]', '(els) => els.map((e) => e.outerHTML)')
        except Exception:
            htmls = []
        use_fast = len(htmls) == len(articles) and len(htmls) > 0
        parsed = [_parse_article(h) for h in htmls] if use_fast else None
        cutoff_hit = False

        for idx, article in enumerate(articles):
            # lxml meta if the fast path is usable for this article, else async.
            meta = parsed[idx] if (parsed and parsed[idx]) else \
                await _extract_meta_async(article, username)

            if not meta.get("has_text_node"):
                continue

            url = meta["url"]

            # URL-based dedup: skip articles we've already processed.
            if url:
                if url in seen_urls:
                    continue
            else:
                # No URL (rare) — fall back to text dedup.
                if not meta["text"] or meta["text"] in seen_texts:
                    continue

            # socialContext distinguishes reposts from pinned posts. A repost's
            # displayed timestamp is the *original* author's date — NOT when this
            # account reposted it — so its date is unreliable. Pinned posts carry
            # their own real timestamp and are date-filtered like any normal post.
            is_repost = meta["is_repost"]
            posted_at = meta["posted_at"]

            # ── Date cutoff ────────────────────────────────────────────────────
            # Drop anything older than since_date, INCLUDING reposts — this is what
            # stops months-old reposted content from flooding a "Today" scan and
            # eating the post budget before we reach genuinely recent posts.
            # A dropped repost does NOT count toward the consecutive-old early-stop,
            # because its (unreliable) original-author date shouldn't halt the scroll
            # before we reach recent original posts further down the timeline.
            if since_date and posted_at:
                post_dt = _parse_iso(posted_at)
                if post_dt and post_dt < since_date:
                    if url:
                        seen_urls.add(url)
                    if is_repost:
                        # Skip old reposts, keep scrolling — but bound the run so a
                        # pure-curator timeline doesn't get crawled end to end.
                        consecutive_old_reposts += 1
                        if consecutive_old_reposts >= MAX_OLD_REPOST_SKIPS:
                            cutoff_hit = True
                            stopped_by = "date"
                            break
                        continue
                    consecutive_old += 1
                    if consecutive_old == 1:
                        last_old_date = posted_at
                    if consecutive_old >= MAX_OLD_SKIPS:
                        cutoff_hit = True
                        stopped_by = "date"
                        break
                    continue

            # ── Get full post text ────────────────────────────────────────────
            # Read the exact rendered text from the live element so emoji, line
            # breaks and link text are byte-for-byte identical to before. X
            # truncates long posts with a "Show more" link; when present, fetch
            # the complete text from the permalink via the secondary page.
            text = await _read_text(article)
            if meta["has_show_more"] and url and page2:
                full_text = await _get_full_text(page2, url)
                if full_text:
                    text = full_text

            if not text:
                continue

            # Register as seen before any further checks so we never double-count.
            if url:
                seen_urls.add(url)
            else:
                seen_texts.add(text)

            consecutive_old = 0          # reset whenever we accept a post
            consecutive_old_reposts = 0
            posts.append({
                "text": text,
                "posted_at": posted_at,
                "url": url,
                "is_repost": is_repost,
                **meta["engagement"],
            })

            if len(posts) >= count:
                stopped_by = "count"
                break

        if cutoff_hit or stopped_by == "count":
            break

        await page.evaluate("window.scrollBy(0, 900)")
        await asyncio.sleep(1.3)
        scrolls += 1

    # Sort newest-first. X's timeline is not strictly chronological (pinned posts,
    # reposts, and algorithmic mixing can interleave old and new content).
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
            await _refresh_session(progress=progress)

        browser = await _launch_stealth_browser(pw, headless=True, slow_mo=60)
        ctx_kwargs = _stealth_context_kwargs(browser)
        ctx_kwargs["storage_state"] = str(SESSION_FILE)
        context = await browser.new_context(**ctx_kwargs)
        await _apply_stealth(context)
        page = await context.new_page()
        # Secondary page used exclusively to fetch full text of truncated posts.
        # Kept open for the whole session so we avoid repeated browser-context overhead.
        page2 = await context.new_page()

        await page.goto("https://x.com/home", wait_until="domcontentloaded")

        # Wait up to 10 s for the account-switcher button — gives the page time
        # to fully hydrate before we conclude the session is expired.
        logged_in = None
        try:
            await _wait_for_signed_in(page, timeout=10000)
            logged_in = True
        except PWTimeout:
            logged_in = False

        if not logged_in:
            await browser.close()
            SESSION_FILE.unlink(missing_ok=True)
            raise SessionExpired(
                "X session expired. Paste fresh cookies or import a new session.json to continue scanning."
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
                result = await _fetch_posts(page, username, count=count, since_date=since_date, page2=page2)
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
        _secure_session_file()
        await browser.close()

    return results
