import configparser
import importlib.util

import pytest


def config_module():
    assert importlib.util.find_spec("qobuz_dl2.config") is not None, "Atomic config storage missing"
    from qobuz_dl2 import config

    return config


def test_failed_config_write_preserves_original(tmp_path, monkeypatch):
    c = config_module()
    path = tmp_path / "config.ini"
    path.write_text("original")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg["DEFAULT"]["password"] = "synthetic%token"

    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cfg, "write", fail)
    with pytest.raises(OSError):
        c.write_config_atomic(str(path), cfg)
    assert path.read_text() == "original"
    assert not list(tmp_path.glob("*.tmp"))


def test_config_is_private_and_token_is_literal(tmp_path):
    c = config_module()
    path = tmp_path / "config.ini"
    c.update_token(str(path), "synthetic%token")
    assert c.read_config(str(path))["DEFAULT"]["password"] == "synthetic%token"
    if __import__("os").name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_boolean_is_a_configuration_error(tmp_path):
    c = config_module()
    path = tmp_path / "config.ini"
    cfg = c.default_config()
    cfg["DEFAULT"]["no_cover"] = "maybe"
    c.write_config_atomic(str(path), cfg)
    with pytest.raises(c.ConfigError):
        c.load_settings(str(path))


def test_concurrent_updates_preserve_independent_settings(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    c = config_module()
    path = str(tmp_path / "config.ini")
    c.update_token(path, "synthetic")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: c.update_config(path, {f"field_{n}": str(n)}), range(16)))
    cfg = c.read_config(path)
    assert cfg["DEFAULT"]["password"] == "synthetic"
    assert all(cfg["DEFAULT"][f"field_{n}"] == str(n) for n in range(16))
