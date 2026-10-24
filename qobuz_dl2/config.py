"""Validated configuration and atomic, private, serialized updates."""

import configparser
import ntpath
import os
import string
from pathlib import Path
from typing import Any, Dict

from qobuz_dl2.downloader import DEFAULT_DOWNLOAD_LIMIT, DEFAULT_FOLDER, DEFAULT_TRACK
from qobuz_dl2.storage import StagedArtifact, file_lock


class ConfigError(ValueError):
    pass


BOOLEAN_KEYS = (
    "no_m3u",
    "albums_only",
    "no_fallback",
    "og_cover",
    "embed_art",
    "no_cover",
    "no_database",
    "smart_discography",
)


def default_config():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg["DEFAULT"] = {
        "email": "",
        "password": "",
        "app_id": "",
        "secrets": "",
        "default_folder": "Qobuz Downloads",
        "default_quality": "6",
        "default_limit": "20",
        "download_limit": str(DEFAULT_DOWNLOAD_LIMIT),
        "folder_format": DEFAULT_FOLDER,
        "track_format": DEFAULT_TRACK,
        **{key: "false" for key in BOOLEAN_KEYS},
    }
    return cfg


def read_config(path: str):
    cfg = default_config()
    try:
        with open(path, encoding="utf-8") as source:
            cfg.read_file(source)
    except (OSError, UnicodeError, configparser.Error) as error:
        raise ConfigError(
            f"Cannot read configuration ({type(error).__name__}); check or reset the config file"
        ) from error
    return cfg


def write_config_atomic(path: str, cfg):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with StagedArtifact(path) as stage:
        with open(stage.path, "w", encoding="utf-8", newline="\n") as target:
            cfg.write(target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(stage.path, 0o600)
        os.replace(stage.path, path)


def update_config(path: str, changes):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(path + ".lock"):
        cfg = read_config(path) if os.path.exists(path) else default_config()
        for key, value in changes.items():
            cfg["DEFAULT"][key] = str(value)
        write_config_atomic(path, cfg)


def update_token(path: str, token: str):
    if not isinstance(token, str) or not token.strip():
        raise ConfigError("Token must be a nonempty string")
    update_config(path, {"password": token.strip()})


def validate_pattern(pattern: str, track: bool = False):
    fields = (
        {"artist", "albumartist", "bit_depth", "sampling_rate", "tracktitle", "version", "tracknumber"}
        if track
        else {"artist", "album", "year", "format", "bit_depth", "sampling_rate", "tracktitle"}
    )
    sample: Dict[str, Any] = {name: "Example" for name in fields}
    sample.update(bit_depth=24, sampling_rate=96, tracknumber="01")
    try:
        for _, field, spec, conversion in string.Formatter().parse(pattern):
            if field is not None and (field not in fields or "{" in (spec or "") or conversion not in (None, "s")):
                raise ValueError("Unsupported format field")
        rendered = pattern.format(**sample)
        if not rendered.strip() or ntpath.isabs(rendered) or os.path.isabs(rendered):
            raise ValueError("Expected a relative naming pattern")
        if ".." in rendered.replace("\\", "/").split("/") or (track and ("/" in rendered or "\\" in rendered)):
            raise ValueError("Naming patterns cannot escape their destination")
    except (KeyError, ValueError, TypeError, IndexError) as error:
        raise ConfigError("Invalid track naming pattern" if track else "Invalid folder naming pattern") from error


def load_settings(path: str):
    cfg = read_config(path)
    values = cfg["DEFAULT"]
    defaults = {}
    for key in BOOLEAN_KEYS:
        try:
            defaults[key] = cfg.getboolean("DEFAULT", key)
        except ValueError as error:
            raise ConfigError(f"Invalid boolean setting: {key}") from error
    for key in ("default_limit", "download_limit", "default_quality"):
        try:
            value = int(values[key])
            if value < 1 or (key == "default_quality" and value not in (5, 6, 7, 27)):
                raise ValueError()
        except (ValueError, KeyError) as error:
            raise ConfigError(f"Invalid numeric setting: {key}") from error
        defaults[key] = value
    defaults.update(
        default_folder=values["default_folder"],
        folder_format=values["folder_format"],
        track_format=values["track_format"],
    )
    if not defaults["default_folder"].strip():
        raise ConfigError("Download directory cannot be blank")
    validate_pattern(defaults["folder_format"])
    validate_pattern(defaults["track_format"], True)
    return {
        "email": values["email"],
        "password": values["password"],
        "app_id": values["app_id"],
        "secrets": [s.strip() for s in values["secrets"].split(",") if s.strip()],
        "defaults": defaults,
    }
