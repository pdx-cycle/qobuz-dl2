# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

"""Offline audit observations; all config and file operations use TemporaryDirectory."""
import configparser
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from qobuz_dl2 import cli
from qobuz_dl2.exceptions import AuthenticationError


def config_at(folder):
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = dict(
        email="synthetic@example.test",
        password="SYNTHETIC_TOKEN",
        default_folder=str(folder / "music"),
        default_limit="20",
        default_quality="6",
        no_m3u="true",
        albums_only="true",
        no_fallback="true",
        og_cover="true",
        embed_art="true",
        no_cover="true",
        no_database="true",
        app_id="123",
        smart_discography="true",
        folder_format="{artist} - {album}",
        track_format="{tracknumber}. {tracktitle}",
        secrets="SYNTHETIC_SECRET",
    )
    path = folder / "config.ini"
    with path.open("w") as out:
        cfg.write(out)
    return path


with tempfile.TemporaryDirectory(prefix="qobuz-cli-audit-") as tmp:
    folder = Path(tmp)
    path = config_at(folder)
    with patch.multiple(cli, CONFIG_PATH=str(folder), CONFIG_FILE=str(path), QOBUZ_DB=str(folder / "db")):

        class FakeQobuz:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.directory = kwargs["directory"]
                print(
                    "effective bools",
                    {k: v for k, v in kwargs.items() if isinstance(v, bool)},
                    "db",
                    kwargs["downloads_db"],
                )

            async def initialize_client(self, *args, **kwargs):
                pass

            async def aclose(self):
                pass

            async def download_list_of_urls(self, urls):
                pass

        with patch.object(cli, "QobuzDL", FakeQobuz):
            result = CliRunner().invoke(cli.cli, ["dl", "https://open.qobuz.com/album/synthetic"])
            print("saved_true_defaults", result.output.strip(), "exit", result.exit_code)

        async def auth_fail(*args, **kwargs):
            raise AuthenticationError("synthetic authentication rejection")

        with patch.object(cli, "_build_qobuz", auth_fail):
            result = CliRunner().invoke(cli.cli, ["dl", "https://open.qobuz.com/album/synthetic"])
            print("auth_failure_exit", result.exit_code)
        path.write_text("[DEFAULT]\nno_cover=maybe\n")
        # Preserve complete config so invalid bool is reached.
        config_at(folder)
        content = path.read_text().replace("no_cover = true", "no_cover = maybe")
        path.write_text(content)
        result = CliRunner().invoke(cli.cli, ["dl", "synthetic"])
        print("invalid_boolean", type(result.exception).__name__, str(result.exception))
        path.write_text("not an INI file\n")
        result = CliRunner().invoke(cli.cli, ["dl", "synthetic"])
        print("malformed_ini", type(result.exception).__name__)
        config_at(folder)

        def bad_write(*args, **kwargs):
            raise OSError("synthetic disk error")

        try:
            with patch.object(configparser.ConfigParser, "write", bad_write):
                cli._save_token("SYNTHETIC_NEW_TOKEN")
        except OSError:
            pass
        print("config_bytes_after_failed_save", path.stat().st_size)
        config_at(folder)
        with patch.object(cli, "_ensure_config_exists") as ensure:
            result = CliRunner().invoke(cli.cli, ["dl", "--help"])
            print("subcommand_help_calls_bootstrap", ensure.call_count, "exit", result.exit_code)
        # Plain filesystem permission observation, never real config.
        path.unlink()
        old = os.umask(0o022)
        try:
            cli._save_token("SYNTHETIC_TOKEN")
        finally:
            os.umask(old)
        print("new_config_mode", oct(path.stat().st_mode & 0o777))
        # Test show-config with a subcommand: root currently requires one.
        result = CliRunner().invoke(cli.cli, ["--show-config", "dl", "synthetic"])
        print("show_config_exposes_synthetic_token", "SYNTHETIC_TOKEN" in result.output)
        result = CliRunner().invoke(cli.cli, ["--show-config"])
        print("root_show_config_without_subcommand", result.exit_code, result.output.strip().splitlines()[-1])
        owned = folder / "music"
        owned.mkdir()
        unrelated = owned / ".another-app.tmp"
        unrelated.write_text("preserve")
        cli._remove_leftovers(str(owned))
        print("unrelated_hidden_tmp_survives", unrelated.exists())
