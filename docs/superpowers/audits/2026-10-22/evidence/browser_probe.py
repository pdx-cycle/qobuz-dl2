# ruff: noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
"""Offline browser-login origin probe. No browser launches or network calls."""
import sys
import types


class FakePlaywrightError(Exception):
    pass


class FakePage:
    def on(self, event, handler):
        assert event == "response"
        self.handler = handler

    def goto(self, url):
        # Simulate a third-party POST whose query happens to contain user/login.
        response = types.SimpleNamespace(
            url="https://third-party.invalid/anything?next=user/login",
            status=500,
            request=types.SimpleNamespace(method="POST"),
            json=lambda: {"user_auth_token": "synthetic-untrusted-token"},
        )
        self.handler(response)

    def wait_for_timeout(self, timeout):
        raise AssertionError("Token should already be captured")


class FakeBrowser:
    def new_context(self):
        return types.SimpleNamespace(new_page=lambda: FakePage())

    def close(self):
        pass


class FakePlaywrightContext:
    def __enter__(self):
        return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **kwargs: FakeBrowser()))

    def __exit__(self, *args):
        pass


sync_api = types.ModuleType("playwright.sync_api")
sync_api.Error = FakePlaywrightError
sync_api.sync_playwright = FakePlaywrightContext
sys.modules["playwright"] = types.ModuleType("playwright")
sys.modules["playwright.sync_api"] = sync_api

from qobuz_dl2.browser_login import fetch_token_via_browser

result = fetch_token_via_browser(timeout=1)
print("Third-party HTTP500 response accepted:", result)
assert result == "synthetic-untrusted-token"
