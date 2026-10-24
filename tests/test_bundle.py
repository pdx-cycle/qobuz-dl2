"""Regression tests for the regexes used to scrape the Qobuz web bundle.

These guard against accidental edits to the patterns; they do not hit the
network. If Qobuz changes their bundle format these will keep passing but the
live scrape will fail loudly via the NotImplementedError guards in Bundle.
"""

from qobuz_dl2.bundle import _APP_ID_REGEX, _BUNDLE_URL_REGEX, _SEED_TIMEZONE_REGEX


def test_app_id_regex_matches():
    sample = 'production:{api:{appId:"123456789",appSecret:"' + "a" * 32 + '"'
    m = _APP_ID_REGEX.search(sample)
    assert m is not None
    assert m.group("app_id") == "123456789"


def test_bundle_url_regex_matches():
    sample = '<script src="/resources/1.2.3-a456/bundle.js"></script>'
    m = _BUNDLE_URL_REGEX.search(sample)
    assert m is not None
    assert m.group(1) == "/resources/1.2.3-a456/bundle.js"


def test_seed_timezone_regex_matches():
    sample = 'a.initialSeed("c2VlZHZhbHVl",window.utimezone.berlin)'
    m = _SEED_TIMEZONE_REGEX.search(sample)
    assert m is not None
    assert m.group("seed") == "c2VlZHZhbHVl"
    assert m.group("timezone") == "berlin"


def test_incomplete_secrets_are_rejected():
    import pytest

    from qobuz_dl2.bundle import Bundle

    b = Bundle.__new__(Bundle)
    b._bundle = (
        'a.initialSeed("c2VlZHZhbHVl",window.utimezone.berlin);b.initialSeed("c2VlZHZhbHVl",window.utimezone.paris)'
    )
    with pytest.raises(NotImplementedError):
        b.get_secrets()


def test_full_valid_bundle_extracts_secrets():
    import base64

    from qobuz_dl2.bundle import Bundle

    pieces = []
    for zone in ["berlin", "paris"]:
        encoded = base64.b64encode(("secret-" + zone).encode()).decode() + "Z" * 44
        pieces.append(
            f'a.initialSeed("{encoded[:8]}",window.utimezone.{zone});name:"X/{zone.capitalize()}",info:"{encoded[8:12]}",extras:"{encoded[12:]}"'
        )
    b = Bundle.__new__(Bundle)
    b._bundle = ";".join(pieces)
    assert dict(b.get_secrets()) == {"paris": "secret-paris", "berlin": "secret-berlin"}
