import configparser

import pytest

from qobuz_dl2 import cli


def test_load_config_uses_download_limit_fallback(tmp_path, monkeypatch):
    config_file = tmp_path / "config.ini"
    config = configparser.ConfigParser()
    config["DEFAULT"] = {
        "email": "user@example.test",
        "password": "token",
        "default_folder": "Qobuz Downloads",
        "default_limit": "20",
        "default_quality": "6",
        "no_m3u": "false",
        "albums_only": "false",
        "no_fallback": "false",
        "og_cover": "false",
        "embed_art": "false",
        "no_cover": "false",
        "no_database": "false",
        "app_id": "123",
        "smart_discography": "false",
        "folder_format": "{artist} - {album}",
        "track_format": "{tracknumber}. {tracktitle}",
        "secrets": "abc",
    }
    with config_file.open("w") as file:
        config.write(file)

    monkeypatch.setattr(cli, "CONFIG_FILE", str(config_file))

    loaded = cli._load_config()

    assert loaded["defaults"]["download_limit"] == cli.DEFAULT_DOWNLOAD_LIMIT


def test_auth_failure_exit_is_nonzero(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from qobuz_dl2.exceptions import AuthenticationError

    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(cli, "_ensure_config_exists", lambda: None)
    monkeypatch.setattr(cli, "_load_config", lambda: {})

    async def fail(*a, **kw):
        raise AuthenticationError("synthetic rejection")

    monkeypatch.setattr(cli, "_build_qobuz", fail)
    result = CliRunner().invoke(cli.cli, ["dl", "https://open.qobuz.com/track/1"])
    assert result.exit_code == 1


def test_unrelated_tmp_survives_cleanup(tmp_path):
    f = tmp_path / ".another-app.tmp"
    f.write_text("preserve")
    cli._remove_leftovers(str(tmp_path))
    assert f.read_text() == "preserve"


def test_help_never_bootstraps_config(tmp_path, monkeypatch):
    from click.testing import CliRunner

    target = tmp_path / "missing"
    monkeypatch.setattr(cli, "CONFIG_PATH", str(target))
    monkeypatch.setattr(cli, "CONFIG_FILE", str(target / "config.ini"))
    for args in [[], ["--help"], ["dl", "--help"], ["login", "--help"], ["set-token", "--help"]]:
        result = CliRunner().invoke(cli.cli, args)
        assert result.exit_code in (0, 2)
        assert not target.exists()


def test_root_show_config_works_and_redacts_secrets(tmp_path, monkeypatch):
    from click.testing import CliRunner

    path = tmp_path / "config.ini"
    path.write_text("[DEFAULT]\npassword=SYNTHETIC_SECRET\nsecrets=SYNTHETIC_APP_SECRET\n")
    monkeypatch.setattr(cli, "CONFIG_FILE", str(path))
    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path))
    result = CliRunner().invoke(cli.cli, ["--show-config"])
    assert result.exit_code == 0
    assert "SYNTHETIC_SECRET" not in result.output
    assert "SYNTHETIC_APP_SECRET" not in result.output


def test_set_token_initializes_without_prompts(tmp_path, monkeypatch):
    from click.testing import CliRunner

    path = tmp_path / "missing" / "config.ini"
    monkeypatch.setattr(cli, "CONFIG_PATH", str(path.parent))
    monkeypatch.setattr(cli, "CONFIG_FILE", str(path))
    result = CliRunner().invoke(cli.cli, ["set-token", "synthetic-token"])
    assert result.exit_code == 0
    assert "synthetic-token" in path.read_text()
    assert "synthetic-token" not in result.output


def test_saved_boolean_defaults_remain_effective(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from qobuz_dl2.results import BatchResult

    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path))
    monkeypatch.setattr(cli, "_ensure_config_exists", lambda: None)
    cfg = {
        "password": "synthetic",
        "app_id": "123",
        "secrets": ["x"],
        "defaults": {
            "default_folder": str(tmp_path),
            "default_quality": 6,
            "download_limit": 2,
            "no_m3u": True,
            "albums_only": True,
            "no_fallback": True,
            "og_cover": True,
            "embed_art": True,
            "no_cover": True,
            "no_database": True,
            "folder_format": "{artist} - {album}",
            "track_format": "{tracknumber}. {tracktitle}",
            "smart_discography": True,
        },
    }
    monkeypatch.setattr(cli, "_load_config", lambda: cfg)
    observed = {}

    class Q:
        def __init__(self, **kw):
            observed.update(kw)
            self.last_result = BatchResult()

        async def initialize_client(self, *a, **kw):
            pass

        async def aclose(self):
            pass

        async def download_list_of_urls(self, urls):
            return BatchResult()

    monkeypatch.setattr(cli, "QobuzDL", Q)
    result = CliRunner().invoke(cli.cli, ["dl", "https://open.qobuz.com/album/abc"])
    assert result.exit_code == 0, result.output
    assert observed["embed_art"] and observed["no_cover"] and observed["ignore_singles_eps"]
    assert not observed["quality_fallback"] and observed["downloads_db"] is None


@pytest.mark.parametrize(
    "args",
    [
        ["dl", "-q", "99", "x"],
        ["fun", "--limit", "0"],
        ["login", "--timeout", "-1"],
        ["lucky", "--type", "invalid", "test"],
        ["lucky", "--number", "0", "test"],
    ],
)
def test_invalid_options_fail_before_side_effects(tmp_path, monkeypatch, args):
    from click.testing import CliRunner

    target = tmp_path / "missing"
    monkeypatch.setattr(cli, "CONFIG_PATH", str(target))
    result = CliRunner().invoke(cli.cli, args)
    assert result.exit_code == 2
    assert not target.exists()


def test_missing_config_show_has_actionable_diagnostic():
    from click.testing import CliRunner

    result = CliRunner().invoke(cli.cli, ["--show-config"])
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_login_save_error_has_actionable_diagnostic(monkeypatch):
    from click.testing import CliRunner

    from qobuz_dl2 import browser_login

    monkeypatch.setattr(browser_login, "fetch_token_via_browser", lambda **kw: "synthetic")

    def fail(token):
        raise cli.config_store.ConfigError("Cannot save settings")

    monkeypatch.setattr(cli, "_save_browser_token", fail)
    result = CliRunner().invoke(cli.cli, ["login"])
    assert result.exit_code == 1
    assert "Error: Cannot save settings" in result.output


def test_browser_login_preserves_saved_app_credentials(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from qobuz_dl2 import browser_login, config

    path = tmp_path / "config.ini"
    monkeypatch.setattr(cli, "CONFIG_FILE", str(path))
    config.update_config(str(path), {"password": "old-token", "app_id": "123456789", "secrets": "one,two"})
    monkeypatch.setattr(browser_login, "fetch_token_via_browser", lambda **kwargs: "new-token")

    result = CliRunner().invoke(cli.cli, ["login"])

    assert result.exit_code == 0, result.output
    saved = config.load_settings(str(path))
    assert saved["password"] == "new-token"
    assert saved["app_id"] == "123456789"
    assert saved["secrets"] == ["one", "two"]
