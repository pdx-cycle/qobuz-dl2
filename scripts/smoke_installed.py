"""Offline installed-wheel smoke; intentionally independent of pytest/source imports."""

import json
import sys
import tempfile
from importlib.metadata import distribution
from pathlib import Path
from unittest.mock import patch

import httpx
from click.testing import CliRunner

import qobuz_dl2
from qobuz_dl2 import cli, config
from qobuz_dl2.storage import read_provenance, validate_audio


def main():
    package = Path(qobuz_dl2.__file__).resolve()
    installed = distribution("qobuz-dl2")
    distribution_root = Path(installed.locate_file("")).resolve()
    direct_url = json.loads(installed.read_text("direct_url.json") or "{}")
    assert distribution_root in package.parents, f"Package is outside its installed distribution: {package}"
    assert direct_url.get("url", "").endswith(".whl"), f"Not installed from a wheel: {direct_url}"
    audio = Path(sys.argv[1]).read_bytes()
    with tempfile.TemporaryDirectory(prefix="qobuz-wheel-") as temporary:
        root = Path(temporary)
        cli.CONFIG_PATH = str(root / "settings")
        cli.CONFIG_FILE = str(root / "settings" / "config.ini")
        cli.QOBUZ_DB = str(root / "settings" / "history.db")
        runner = CliRunner()
        for args in (["--help"], ["dl", "--help"], ["login", "--help"]):
            result = runner.invoke(cli.cli, args)
            assert result.exit_code == 0, result.output
        assert not Path(cli.CONFIG_PATH).exists()
        assert runner.invoke(cli.cli, ["dl", "-q", "99", "invalid"]).exit_code == 2
        assert runner.invoke(cli.cli, ["set-token"], input="synthetic-token\n").exit_code == 0
        config.update_config(
            cli.CONFIG_FILE,
            {
                "app_id": "123",
                "secrets": "synthetic",
                "default_quality": 5,
                "default_folder": str(root / "audio"),
                "no_cover": True,
            },
        )
        transfers = 0

        def response(request):
            nonlocal transfers
            path = request.url.path
            if path.endswith("user/login"):
                return httpx.Response(200, json={"user": {"id": 1}})
            if path.endswith("track/getFileUrl"):
                return httpx.Response(200, json={"url": "https://example.test/audio", "format_id": 5})
            if path.endswith("track/get"):
                return httpx.Response(
                    200,
                    json={
                        "id": "1",
                        "title": "Tone",
                        "track_number": 1,
                        "media_number": 1,
                        "duration": 5,
                        "performer": {"name": "Artist"},
                        "album": {
                            "id": "album",
                            "title": "Album",
                            "artist": {"name": "Artist"},
                            "release_date_original": "2020-01-01",
                            "genres_list": ["Test"],
                            "tracks_count": 1,
                        },
                    },
                )
            if request.url.host == "example.test" and path == "/audio":
                transfers += 1
                return httpx.Response(200, content=audio)
            raise AssertionError(f"Unexpected mock route: {request.method} {path}")

        original = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(response)
            return original(*args, **kwargs)

        with patch.object(httpx, "AsyncClient", client):
            for _ in range(2):
                result = runner.invoke(cli.cli, ["dl", "https://open.qobuz.com/track/1"])
                assert result.exit_code == 0, (result.output, result.exception)
            result = runner.invoke(cli.cli, ["dl", "https://foreign.test/track/1"])
            assert result.exit_code == 1, result.output
        files = list((root / "audio").rglob("*.mp3"))
        assert len(files) == transfers == 1
        validate_audio(str(files[0]), True, 5)
        assert read_provenance(str(files[0]), True)[0] == "1"
        assert not list(root.rglob("*.tmp"))
    print(json.dumps({"wheel_import": str(package), "python": sys.version.split()[0], "offline_smoke": "passed"}))


if __name__ == "__main__":
    main()
