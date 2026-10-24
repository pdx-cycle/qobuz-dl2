"""Owned staging files, conservative media checks, and durable provenance."""

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX
from mutagen.mp3 import MP3


class MediaValidationError(ValueError):
    pass


class StagedArtifact:
    def __init__(self, final_path: str):
        self.final_path = os.path.abspath(final_path)
        os.makedirs(os.path.dirname(self.final_path), exist_ok=True)
        fd, self.path = tempfile.mkstemp(prefix=".qobuz-", suffix=".tmp", dir=os.path.dirname(self.final_path))
        os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

    def cleanup(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def publish(self) -> str:
        # A hard link is an atomic, no-overwrite publication on the same filesystem.
        # Never replace unknown user media or another operation's completed file.
        with open(self.path, "rb") as stream:
            os.fsync(stream.fileno())
        os.link(self.path, self.final_path)
        self.cleanup()
        return self.final_path


def requirements_key(folder_format: str, track_format: str, embed_art: bool) -> str:
    value = json.dumps([1, folder_format, track_format, bool(embed_art)], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def validate_audio(path: str, is_mp3: bool, expected_duration: Optional[float] = None) -> None:
    try:
        audio = MP3(path) if is_mp3 else FLAC(path)
        if audio.info is None or audio.info.length <= 0 or os.path.getsize(path) < 32:
            raise MediaValidationError("Empty or invalid audio")
        duration = _mp3_frame_duration(path, audio.info.frame_offset) if is_mp3 else audio.info.length
        if is_mp3 and expected_duration and abs(duration - expected_duration) > max(1.0, expected_duration * 0.01):
            raise MediaValidationError("Audio duration does not match expected track duration")
        if not is_mp3:
            # Mutagen validates metadata, not FLAC frame bodies. Reject metadata-only files.
            with open(path, "rb") as f:
                if f.read(4) != b"fLaC":
                    raise MediaValidationError("Invalid FLAC stream")
                while True:
                    header = f.read(4)
                    if len(header) != 4:
                        raise MediaValidationError("Incomplete FLAC metadata")
                    f.seek(int.from_bytes(header[1:], "big"), os.SEEK_CUR)
                    if header[0] & 128:
                        break
                if len(f.read(16)) < 16:
                    raise MediaValidationError("FLAC audio frames missing")
    except Exception as error:
        if isinstance(error, MediaValidationError):
            raise
        raise MediaValidationError("Invalid or unreadable audio") from error


def _mp3_frame_duration(path: str, offset: int) -> float:
    """Validate frame boundaries, including the last frame (Xing duration can lie)."""
    seconds = 0.0
    frames = 0
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(offset)
        while f.tell() < size:
            position = f.tell()
            data = f.read(4)
            if size - position == 128 and data[:3] == b"TAG":
                break  # optional ID3v1 trailer
            if data == b"APET":
                trailer = data + f.read(size - f.tell())
                if b"APETAGEX" in trailer[-32:]:
                    break
            if len(data) != 4:
                raise MediaValidationError("Incomplete MPEG frame")
            header = int.from_bytes(data, "big")
            version = (header >> 19) & 3
            layer = (header >> 17) & 3
            index = (header >> 12) & 15
            sampling = (header >> 10) & 3
            if header >> 21 != 2047 or version == 1 or layer != 1 or index in (0, 15) or sampling == 3:
                raise MediaValidationError("Invalid MPEG frame header")
            rates = (
                [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
                if version == 3
                else [8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
            )
            rate = [44100, 48000, 32000][sampling] // (1 if version == 3 else 2 if version == 2 else 4)
            length = (144000 if version == 3 else 72000) * rates[index - 1] // rate + ((header >> 9) & 1)
            if position + length > size:
                raise MediaValidationError("Truncated MPEG frame")
            f.seek(position + length)
            seconds += (1152 if version == 3 else 576) / rate
            frames += 1
    if frames < 2:
        raise MediaValidationError("Insufficient MPEG audio frames")
    return seconds


def read_provenance(path: str, is_mp3: bool):
    if is_mp3:
        tags = ID3(path)
        identity = tags.get("TXXX:QOBUZ_TRACK_ID")
        requirements = tags.get("TXXX:QOBUZ_REQUIREMENTS")
        return (str(identity.text[0]) if identity else "", str(requirements.text[0]) if requirements else "")
    flac_tags = FLAC(path)
    return (flac_tags.get("QOBUZ_TRACK_ID", [""])[0], flac_tags.get("QOBUZ_REQUIREMENTS", [""])[0])


def stamp_provenance(path: str, is_mp3: bool, track_id: str, key: str) -> None:
    if is_mp3:
        tags = ID3(path)
        tags.add(TXXX(encoding=3, desc="QOBUZ_TRACK_ID", text=track_id))
        tags.add(TXXX(encoding=3, desc="QOBUZ_REQUIREMENTS", text=key))
        tags.save(path, v2_version=3)
    else:
        flac_tags = FLAC(path)
        flac_tags["QOBUZ_TRACK_ID"] = track_id
        flac_tags["QOBUZ_REQUIREMENTS"] = key
        flac_tags.save()


def validate_extra(path: str, name: str) -> None:
    with open(path, "rb") as stream:
        head = stream.read(16)
        stream.seek(max(0, os.path.getsize(path) - 1024))
        tail = stream.read(1024)
    if name == "cover.jpg":
        jpeg = head.startswith(b"\xff\xd8") and tail.endswith(b"\xff\xd9")
        png = head.startswith(b"\x89PNG\r\n\x1a\n") and tail.endswith(b"IEND\xaeB`\x82")
        if not (jpeg or png):
            raise MediaValidationError("Invalid or incomplete cover image")
    elif not head.startswith(b"%PDF-") or b"%%EOF" not in tail:
        raise MediaValidationError("Invalid or incomplete PDF")


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audio_quality(path: str, is_mp3: bool):
    if is_mp3:
        mp3 = MP3(path)
        if mp3.info is None:
            raise MediaValidationError("MP3 audio information missing")
        return 5, None, mp3.info.sample_rate / 1000
    flac = FLAC(path)
    if flac.info is None:
        raise MediaValidationError("FLAC audio information missing")
    depth = flac.info.bits_per_sample
    rate = flac.info.sample_rate / 1000
    return (6 if depth <= 16 else 7 if rate <= 96 else 27), depth, rate


@contextmanager
def file_lock(path: str, timeout: float = 5.0):
    """Stable advisory lock file; never unlink an inode another waiter may hold."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")

            def acquire():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def release():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(fd, fcntl.LOCK_UN)

        while True:
            try:
                acquire()
                locked = True
                break
            except (BlockingIOError, PermissionError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for file ownership")
                time.sleep(0.01)
        yield
    finally:
        if locked:
            release()
        os.close(fd)


def replace_known(stage: StagedArtifact, expected_digest: str) -> str:
    lock = os.path.join(
        os.path.dirname(stage.final_path), ".qobuz-lock-" + hashlib.sha256(stage.final_path.encode()).hexdigest()[:16]
    )
    with file_lock(lock):
        if file_digest(stage.final_path) != expected_digest:
            raise FileExistsError("Destination changed during update")
        backup = stage.final_path + ".previous-" + uuid.uuid4().hex
        os.link(stage.final_path, backup)
        with open(stage.path, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(stage.path, stage.final_path)
    return stage.final_path


def safe_destination(root: str, relative: str) -> str:
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, relative))
    if os.path.commonpath([base, target]) != base:
        raise ValueError("Naming pattern escapes the selected directory")
    return target


def _extra_record(path):
    return os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".qobuz.json")


def copy_cached_extra(path, url, staged_path, name):
    record_path = _extra_record(path)
    if not os.path.isfile(record_path):
        return False
    lock = os.path.join(
        os.path.dirname(path), ".qobuz-lock-" + hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16]
    )
    try:
        with file_lock(lock):
            with open(record_path, encoding="utf-8") as source:
                record = json.load(source)
            if record.get("source") != hashlib.sha256(url.encode()).hexdigest() or record.get("sha256") != file_digest(
                path
            ):
                return False
            validate_extra(path, name)
            import shutil

            shutil.copyfile(path, staged_path)
            return True
    except (OSError, ValueError, TimeoutError):
        return False


def publish_extra(source_path, path, url):
    import shutil

    digest = file_digest(source_path)
    if not os.path.exists(path) or file_digest(path) != digest:
        previous = file_digest(path) if os.path.exists(path) else None
        with StagedArtifact(path) as stage:
            shutil.copyfile(source_path, stage.path)
            if previous:
                replace_known(stage, previous)
            else:
                try:
                    stage.publish()
                except FileExistsError:
                    if file_digest(path) != digest:
                        raise
    record_path = _extra_record(path)
    with StagedArtifact(record_path) as record:
        with open(record.path, "w", encoding="utf-8") as target:
            json.dump({"source": hashlib.sha256(url.encode()).hexdigest(), "sha256": digest}, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(record.path, record_path)
