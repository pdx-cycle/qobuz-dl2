import logging
import os
import re
import string
from urllib.parse import unquote, urlsplit

from mutagen.flac import FLAC
from mutagen.mp3 import EasyMP3

from qobuz_dl2.storage import StagedArtifact

logger = logging.getLogger(__name__)

EXTENSIONS = (".mp3", ".flac")


class PartialFormatter(string.Formatter):
    def __init__(self, missing="n/a", bad_fmt="n/a"):
        self.missing, self.bad_fmt = missing, bad_fmt

    def get_field(self, field_name, args, kwargs):
        try:
            val = super(PartialFormatter, self).get_field(field_name, args, kwargs)
        except (KeyError, AttributeError):
            val = None, field_name
        return val

    def format_field(self, value, format_spec):
        if not value:
            return self.missing
        try:
            return super(PartialFormatter, self).format_field(value, format_spec)
        except ValueError:
            if self.bad_fmt:
                return self.bad_fmt
            raise


def make_m3u(pl_directory, artifacts=None):
    """Write successful artifact paths in source order, retaining repeated entries."""
    directory = os.path.abspath(pl_directory)
    if artifacts is None:
        paths = [
            os.path.join(local, name)
            for local, dirs, files in os.walk(directory)
            for name in sorted(files)
            if os.path.splitext(name)[1].lower() in EXTENSIONS
        ]
    else:
        paths = [a.path for a in artifacts]
    lines = ["#EXTM3U"]
    for path in paths:
        suffix = os.path.splitext(path)[1].lower()
        if suffix not in EXTENSIONS:
            raise ValueError("Playlist artifact must be audio")
        audio = EasyMP3(path) if suffix == ".mp3" else FLAC(path)
        title = (audio.get("title") or [os.path.basename(path)])[0]
        artist = (audio.get("artist") or ["Unknown Artist"])[0]
        # Untrusted metadata must not inject additional M3U records.
        label = f"{artist} - {title}".replace("\r", " ").replace("\n", " ")
        relative = os.path.relpath(path, directory).replace(os.sep, "/")
        if "\n" in relative or "\r" in relative:
            raise ValueError("Playlist path contains a line separator")
        lines.extend([f"#EXTINF:{int(audio.info.length)}, {label}", relative])
    target = os.path.join(directory, os.path.basename(directory) + ".m3u")
    with StagedArtifact(target) as stage:
        with open(stage.path, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(stage.path, target)
    return target


def smart_discography_filter(contents: list, save_space: bool = False, skip_extras: bool = False) -> list:
    if not contents:
        return []
    artist = contents[0].get("name")
    groups: dict[str, list[dict]] = {}
    extras = re.compile(r"anniversary|deluxe|live|collector|demo|expanded", re.I)
    remaster = re.compile(r"(re)?master(ed)?", re.I)
    for page in contents:
        for album in (page.get("albums") or {}).get("items", []):
            text = f"{album.get('title', '')} {album.get('version') or ''}"
            if (album.get("artist") or {}).get("name") != artist or (skip_extras and extras.search(text)):
                continue
            key = re.split(r"[\(\[]", str(album.get("title", "")), maxsplit=1)[0].strip().casefold()
            groups.setdefault(key, []).append(album)
    result = []

    def quality(value):
        return value if isinstance(value, (int, float)) else 0

    for albums in groups.values():
        remasters = [a for a in albums if remaster.search(f"{a.get('title', '')} {a.get('version') or ''}")]
        candidates = remasters or albums
        result.append(
            max(
                candidates,
                key=lambda a: (
                    quality(a.get("maximum_bit_depth")),
                    (-1 if save_space else 1) * quality(a.get("maximum_sampling_rate")),
                ),
            )
        )
    return result


def format_duration(duration):
    seconds = int(duration)
    if seconds < 0:
        raise ValueError("Duration must be nonnegative")
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def create_and_return_dir(directory):
    fix = os.path.normpath(directory)
    os.makedirs(fix, exist_ok=True)
    return fix


def get_url_info(url: str) -> tuple[str, str]:
    """Parse a complete supported Qobuz URL or explicitly supported relative path."""
    value = urlsplit(url)
    allowed = {"www.qobuz.com", "qobuz.com", "open.qobuz.com", "play.qobuz.com"}
    if value.scheme or value.netloc:
        if value.scheme != "https" or value.hostname not in allowed or value.username or value.password:
            raise ValueError("Invalid Qobuz origin")
        if value.port not in (None, 443):
            raise ValueError("Invalid Qobuz port")
    elif not url.startswith("/") or url.startswith("//"):
        raise ValueError("Expected a complete Qobuz URL")
    parts = [unquote(part) for part in value.path.strip("/").split("/")]
    locale = bool(parts and re.fullmatch(r"[a-z]{2}-[a-z]{2}", parts[0]))
    if locale:
        parts.pop(0)
    if not parts or parts[0] not in {"album", "track", "artist", "label", "playlist"}:
        raise ValueError("Unsupported Qobuz resource")
    if value.hostname in {"open.qobuz.com", "play.qobuz.com"} and (locale or len(parts) != 2):
        raise ValueError("Invalid Qobuz player path")
    if len(parts) not in (2, 3) or not re.fullmatch(r"[A-Za-z0-9]+", parts[-1]):
        raise ValueError("Invalid Qobuz resource ID")
    if len(parts) == 3 and not re.fullmatch(r"[-\w]+", parts[1]):
        raise ValueError("Invalid Qobuz resource slug")
    return parts[0], parts[-1]
