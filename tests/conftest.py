import base64
from pathlib import Path

import pytest


@pytest.fixture
def audio_bytes():
    return (Path(__file__).parent / "fixtures" / "tone.mp3").read_bytes()


@pytest.fixture
def cover_bytes():
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )


@pytest.fixture
def track_meta():
    return {
        "id": "1",
        "title": "Tone",
        "track_number": 1,
        "media_number": 1,
        "duration": 5,
        "maximum_bit_depth": 16,
        "maximum_sampling_rate": 44.1,
        "performer": {"name": "Artist"},
        "album": {
            "id": "album",
            "artist": {"name": "Artist"},
            "title": "Album",
            "release_date_original": "2020-01-01",
            "genres_list": ["Test"],
            "tracks_count": 1,
            "image": {"large": "https://example.test/cover"},
        },
    }


@pytest.fixture(autouse=True)
def isolate_cli_paths(tmp_path, monkeypatch):
    from qobuz_dl2 import cli

    root = tmp_path / "isolated-config"
    monkeypatch.setattr(cli, "CONFIG_PATH", str(root))
    monkeypatch.setattr(cli, "CONFIG_FILE", str(root / "config.ini"))
    monkeypatch.setattr(cli, "QOBUZ_DB", str(root / "history.db"))


@pytest.fixture(autouse=True)
def forbid_live_http(monkeypatch):
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden in offline tests; use MockTransport")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden in offline tests; use MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)
