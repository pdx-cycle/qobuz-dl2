"""Capture a Qobuz ``user_auth_token`` by driving a real browser.

Qobuz removed scriptable email/password login (OAuth + reCAPTCHA), so we open a
genuine browser window and let the user log in normally. We listen for the
``user/login`` network response in the background and extract the token from it,
which sidesteps the captcha entirely (the human solves it) without any manual
DevTools copy-paste.

Playwright is an optional dependency, installed via the ``browser`` extra
(from a checkout of this project):

    uv tool install --force ".[browser]"
    python -m playwright install chromium   # one-time browser download
"""

import logging
import time
from typing import Optional
from urllib.parse import urlsplit

LOGIN_URL = "https://play.qobuz.com/login"
_INSTALL_HINT = (
    "The browser login helper needs Playwright. From a checkout of this\n"
    "project, install the optional extra with:\n"
    '  uv tool install --force ".[browser]"\n'
    "then download the browser once with:\n"
    "  python -m playwright install chromium"
)

logger = logging.getLogger(__name__)


def fetch_token_via_browser(timeout: int = 300) -> Optional[str]:
    """Open a browser, wait for the user to log in, and return the captured
    ``user_auth_token`` (or ``None`` if the window is closed / times out).

    :param timeout: seconds to wait for a successful login before giving up.
    :raises RuntimeError: if Playwright (or its browser binary) is unavailable.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(_INSTALL_HINT) from e

    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("Timeout must be positive")
    captured: dict = {}

    def _on_response(response) -> None:
        try:
            url = urlsplit(response.url)
            if (
                url.scheme == "https"
                and url.hostname == "www.qobuz.com"
                and url.port in (None, 443)
                and not url.username
                and not url.password
                and url.path.rstrip("/") == "/api.json/0.2/user/login"
                and response.request.method == "POST"
                and 200 <= response.status < 300
            ):
                data = response.json()
                token = data.get("user_auth_token") if isinstance(data, dict) else None
                if isinstance(token, str) and token.strip():
                    captured["token"] = token.strip()
        except Exception:  # pragma: no cover - response bodies vary
            pass

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False)
            except PlaywrightError as e:
                raise RuntimeError(f"Could not launch a browser. {_INSTALL_HINT}\n\n({e})") from e
            try:
                context = browser.new_context()
                page = context.new_page()
                page.on("response", _on_response)
                deadline = time.monotonic() + timeout
                page.goto(LOGIN_URL, timeout=timeout * 1000)
                while "token" not in captured and time.monotonic() < deadline:
                    try:
                        page.wait_for_timeout(min(500, max(1, int((deadline - time.monotonic()) * 1000))))
                    except PlaywrightError:
                        break
            finally:
                try:
                    browser.close()
                except PlaywrightError:
                    pass
    except RuntimeError:
        raise
    except Exception as e:  # pragma: no cover - environment specific
        raise RuntimeError(f"Browser login failed: {e}") from e

    return captured.get("token")
