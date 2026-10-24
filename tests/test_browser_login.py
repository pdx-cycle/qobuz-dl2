import sys
from types import ModuleType, SimpleNamespace

import pytest

from qobuz_dl2.browser_login import fetch_token_via_browser


@pytest.mark.parametrize(
    "url,status,token,want",
    [
        ("https://third-party.invalid/?next=user/login", 200, "synthetic", None),
        ("https://www.qobuz.com/api.json/0.2/user/login", 500, "synthetic", None),
        ("https://www.qobuz.com/api.json/0.2/user/login", 200, 123, None),
        ("https://www.qobuz.com/api.json/0.2/user/login", 200, " synthetic ", "synthetic"),
    ],
)
def test_browser_accepts_only_successful_intended_response(monkeypatch, url, status, token, want):
    class Error(Exception):
        pass

    class Page:
        def on(self, event, callback):
            self.callback = callback

        def goto(self, *args, **kwargs):
            self.callback(
                SimpleNamespace(
                    url=url,
                    status=status,
                    request=SimpleNamespace(method="POST"),
                    json=lambda: {"user_auth_token": token},
                )
            )

        def wait_for_timeout(self, *args):
            raise Error("closed")

    class Browser:
        def new_context(self):
            return SimpleNamespace(new_page=Page)

        def close(self):
            pass

    class Context:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: Browser()))

        def __exit__(self, *args):
            pass

    api = ModuleType("playwright.sync_api")
    api.Error = Error
    api.sync_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", api)
    assert fetch_token_via_browser(timeout=1) == want
