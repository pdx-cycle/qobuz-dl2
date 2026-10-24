import asyncio
import hashlib
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Optional, Tuple

import httpx
from pathvalidate import sanitize_filename, sanitize_filepath
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from tenacity import RetryError

import qobuz_dl2.metadata as metadata
from qobuz_dl2.color import OFF, RED, YELLOW
from qobuz_dl2.db import expected_artifact_digest, quality_satisfies
from qobuz_dl2.exceptions import NonStreamable, ResponseError
from qobuz_dl2.network import get_retry
from qobuz_dl2.results import Artifact, DownloadResult, DownloadStatus
from qobuz_dl2.scheduling import bounded_map, run_owned_thread
from qobuz_dl2.storage import (
    StagedArtifact,
    audio_quality,
    copy_cached_extra,
    file_digest,
    publish_extra,
    read_provenance,
    replace_known,
    requirements_key,
    safe_destination,
    stamp_provenance,
    validate_audio,
    validate_extra,
)

QL_DOWNGRADE = "FormatRestrictedByFormatAvailability"
# used in case of error
DEFAULT_FORMATS = {
    "MP3": [
        "{artist} - {album} ({year}) [MP3]",
        "{tracknumber}. {tracktitle}",
    ],
    "Unknown": [
        "{artist} - {album}",
        "{tracknumber}. {tracktitle}",
    ],
}

DEFAULT_FOLDER = "{artist} - {album} ({year}) [{bit_depth}B-{sampling_rate}kHz]"
DEFAULT_TRACK = "{tracknumber}. {tracktitle}"
DEFAULT_DOWNLOAD_LIMIT = 2
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    def __init__(self, message, artifacts=()):
        super().__init__(message)
        self.artifacts = tuple(artifacts)


class Download:
    def __init__(
        self,
        client,
        item_id: str,
        path: str,
        quality: int,
        embed_art: bool = False,
        albums_only: bool = False,
        downgrade_quality: bool = False,
        cover_og_quality: bool = False,
        no_cover: bool = False,
        folder_format=None,
        track_format=None,
        limit: int = DEFAULT_DOWNLOAD_LIMIT,
        http_client=None,
        transfer_budget=None,
        history_path=None,
    ):
        self.client = client
        self.item_id = item_id
        self.path = path
        self.quality = quality
        self.albums_only = albums_only
        self.embed_art = embed_art
        self.downgrade_quality = downgrade_quality
        self.cover_og_quality = cover_og_quality
        self.no_cover = no_cover
        self.folder_format = folder_format or DEFAULT_FOLDER
        self.track_format = track_format or DEFAULT_TRACK
        self.cover_path = None
        self.requirements_key = requirements_key(self.folder_format, self.track_format, self.embed_art)
        self.limit = max(1, int(limit or DEFAULT_DOWNLOAD_LIMIT))
        self.semaphore = None
        self.history_path = history_path
        self.http_client = http_client
        self.transfer_budget = transfer_budget
        self.progress = Progress(
            TextColumn("[bold blue]{task.description}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•",
            DownloadColumn(),
            "•",
            TransferSpeedColumn(),
            "•",
            TimeRemainingColumn(),
        )

    def _ensure_budgets(self):
        # Python 3.9 primitives bind to their creation loop. Create them only
        # inside the operation, so library objects can be constructed beforehand.
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.limit)
        if self.transfer_budget is None:
            self.transfer_budget = asyncio.Semaphore(self.limit)

    async def download_id_by_type(self, track=True):
        self._ensure_budgets()
        with self.progress:
            if self.http_client is not None:
                return await (
                    self.download_track(self.http_client) if track else self.download_release(self.http_client)
                )
            connection_limit = max(self.limit + 2, 4)
            limits = httpx.Limits(max_connections=connection_limit, max_keepalive_connections=connection_limit)
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, limits=limits) as client:
                return await (self.download_track(client) if track else self.download_release(client))

    async def download_release(self, client: httpx.AsyncClient):
        self._ensure_budgets()
        meta = await self.client.get_album_meta(self.item_id)
        if "id" in meta and str(meta["id"]) != str(self.item_id):
            raise ResponseError("Album metadata identity mismatch")

        if not meta.get("streamable"):
            raise NonStreamable("This release is not streamable")

        if self.albums_only and (
            meta.get("release_type") != "album" or _safe_get(meta, "artist", "name") == "Various Artists"
        ):
            logger.info(f"{OFF}Ignoring Single/EP/VA: {meta.get('title', 'n/a')}")
            return DownloadResult(DownloadStatus.SKIPPED, reason="Excluded release type")

        block = meta.get("tracks")
        if not isinstance(block, dict) or not isinstance(block.get("items"), list):
            raise ResponseError("Missing or invalid album track manifest")
        tracks = block["items"]
        counts = [
            container[field] for container, field in ((meta, "tracks_count"), (block, "total")) if field in container
        ]
        if any(isinstance(count, bool) or not isinstance(count, int) or count != len(tracks) for count in counts):
            raise ResponseError("Album track manifest does not match its advertised count")
        if not tracks:
            return DownloadResult(DownloadStatus.SKIPPED, reason="Empty release")
        album_title = _get_title(meta)
        format_info = await self._get_format(meta)
        file_format, quality_met, bit_depth, sampling_rate = format_info

        logger.info(f"\n{YELLOW}Downloading: {album_title}\nQuality: {file_format} ({bit_depth}/{sampling_rate})\n")
        album_attr = self._get_album_attr(meta, album_title, file_format, bit_depth, sampling_rate)
        folder_format, _ = _clean_format_str(self.folder_format, self.track_format, file_format)
        sanitized_title = sanitize_filepath(folder_format.format(**album_attr))
        dirn = safe_destination(self.path, sanitized_title)
        os.makedirs(dirn, exist_ok=True)

        async with self._cover(client, meta, dirn) as cover_path:
            self.cover_path = cover_path
            tasks = []
            if "goodies" in meta:
                try:
                    tasks.append((client, meta["goodies"][0]["url"], dirn, "booklet.pdf"))
                except:  # noqa
                    pass

            media_numbers = [track["media_number"] for track in meta["tracks"]["items"]]
            is_multiple = len(set(media_numbers)) > 1

            async def process(job):
                count, track = job
                return await self._process_track(client, dirn, count, track, meta, is_multiple)

            outcomes = await bounded_map(process, enumerate(meta["tracks"]["items"]), self.limit)
            if tasks:
                outcomes.extend(await bounded_map(lambda args: self._optional_extra(*args), tasks, self.limit))
            return self._collect_download_results(outcomes, album_title)

    async def _process_track(
        self,
        client: httpx.AsyncClient,
        dirn: str,
        count: int,
        track_meta: dict,
        album_meta: dict,
        is_multiple: bool,
    ):
        self._ensure_budgets()
        assert self.semaphore is not None
        async with self.semaphore:
            parse = await self.client.get_track_url(track_meta["id"], fmt_id=self.quality)
            if "track_id" in parse and str(parse["track_id"]) != str(track_meta["id"]):
                raise ResponseError("Track URL identity mismatch")
            if not parse.get("sample") and parse.get("url"):
                _, quality_met, _, _ = await self._get_format(track_meta, True, parse)
                if not self.downgrade_quality and not quality_met:
                    return DownloadResult(DownloadStatus.SKIPPED, reason="Quality policy")
                is_mp3 = int(self.quality) == 5
                return await self._download_and_tag(
                    client,
                    dirn,
                    count,
                    parse,
                    track_meta,
                    album_meta,
                    False,
                    is_mp3,
                    track_meta["media_number"] if is_multiple else None,
                )
            else:
                return DownloadResult(DownloadStatus.SKIPPED, reason="Demo track")

    async def download_track(self, client: httpx.AsyncClient):
        self._ensure_budgets()
        parse = await self.client.get_track_url(self.item_id, self.quality)
        if "track_id" in parse and str(parse["track_id"]) != str(self.item_id):
            raise ResponseError("Track URL identity mismatch")

        if not parse.get("sample") and parse.get("url"):
            meta = await self.client.get_track_meta(self.item_id)
            if str(meta.get("id", "")) != str(self.item_id):
                raise ResponseError("Track metadata identity mismatch")
            track_title = _get_title(meta)
            artist = _safe_get(meta, "performer", "name")
            logger.info(f"\n{YELLOW}Downloading: {artist} - {track_title}")
            format_info = await self._get_format(meta, is_track_id=True, track_url_dict=parse)
            file_format, quality_met, bit_depth, sampling_rate = format_info

            if not self.downgrade_quality and not quality_met:
                logger.info(f"{OFF}Skipping {track_title} as it doesn't meet quality requirement")
                return DownloadResult(DownloadStatus.SKIPPED, reason="Quality policy")

            track_attr = self._get_track_attr(meta, track_title, bit_depth, sampling_rate)
            track_attr["format"] = file_format
            folder_format, _ = _clean_format_str(self.folder_format, self.track_format, file_format)
            sanitized_title = sanitize_filepath(folder_format.format(**track_attr))
            dirn = safe_destination(self.path, sanitized_title)
            os.makedirs(dirn, exist_ok=True)

            async with self._cover(client, meta.get("album") or {}, dirn) as cover_path:
                self.cover_path = cover_path
                tasks = []
                is_mp3 = int(self.quality) == 5
                tasks.append(self._download_and_tag(client, dirn, 1, parse, meta, meta, True, is_mp3, None))
                return await self._run_download_tasks(tasks, track_title)
        return DownloadResult(DownloadStatus.SKIPPED, reason="Demo track")

    async def _run_download_tasks(self, tasks, item_title: str):
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return self._collect_download_results(results, item_title)

    def _collect_download_results(self, results, item_title):
        failures = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                failures.append(result)
            elif isinstance(result, DownloadResult) and result.status is DownloadStatus.FAILED:
                failures.append(DownloadError(result.reason or "Child download failed"))

        if failures:
            for failure in failures:
                logger.error(f"{RED}Download failed: {_describe_exception(failure)}")
            raise DownloadError(
                f"{len(failures)} download task(s) failed for {item_title}",
                artifacts=tuple(a for r in results if isinstance(r, DownloadResult) for a in r.artifacts),
            )

        artifacts = tuple(a for r in results if isinstance(r, DownloadResult) for a in r.artifacts)
        if artifacts:
            skipped = sum(isinstance(r, DownloadResult) and r.status is DownloadStatus.SKIPPED for r in results)
            logger.info("Completed eligible tracks: %s (%s policy skips)", item_title, skipped)
            return DownloadResult(DownloadStatus.COMPLETED, artifacts, reason=f"{skipped} policy skips")
        return DownloadResult(DownloadStatus.SKIPPED, reason="No eligible tracks")

    async def _download_and_tag(
        self,
        client: httpx.AsyncClient,
        root_dir: str,
        tmp_count: int,
        track_url_dict: dict,
        track_metadata: dict,
        album_or_track_metadata: dict,
        is_track: bool,
        is_mp3: bool,
        multiple: Optional[int] = None,
    ):
        extension = ".mp3" if is_mp3 else ".flac"
        url = track_url_dict.get("url")
        if not url:
            return DownloadResult(DownloadStatus.SKIPPED, reason="Track unavailable")

        if multiple:
            root_dir = os.path.join(root_dir, f"Disc {multiple}")
            os.makedirs(root_dir, exist_ok=True)

        track_title = track_metadata.get("title", "Untitled")
        artist = _safe_get(track_metadata, "performer", "name", default="Unknown Artist")
        attrs = self._get_filename_attr(artist, track_metadata, track_title)
        _, track_pattern = _clean_format_str(self.folder_format, self.track_format, "MP3" if is_mp3 else "FLAC")
        formatted = sanitize_filename(track_pattern.format(**attrs))
        # POSIX filename limits are byte limits; keep the extension within 255 bytes.
        stem = formatted.encode("utf-8")[:250].decode("utf-8", errors="ignore") or "Untitled"
        final_file = safe_destination(root_dir, stem + extension)
        source_id = str(track_metadata.get("id", self.item_id))
        release = (track_metadata.get("album") or {}) if is_track else album_or_track_metadata
        track_fields = {
            k: track_metadata.get(k)
            for k in ("title", "version", "work", "track_number", "media_number", "performer", "composer", "copyright")
        }
        album_fields = {
            k: release.get(k)
            for k in ("title", "artist", "release_date_original", "genres_list", "tracks_count", "copyright", "label")
        }
        cover_hash = (
            await run_owned_thread(file_digest, self.cover_path) if self.embed_art and self.cover_path else None
        )
        key = hashlib.sha256(
            json.dumps([self.requirements_key, track_fields, album_fields, cover_hash], sort_keys=True).encode()
        ).hexdigest()
        artifact = Artifact(
            final_file,
            "track" if is_track else "album",
            str(self.item_id),
            source_id,
            key,
            int(track_url_dict.get("format_id") or self.quality),
            track_url_dict.get("bit_depth"),
            track_url_dict.get("sampling_rate"),
        )
        duration = track_metadata.get("duration")
        duration = float(duration) if isinstance(duration, (int, float)) and duration > 0 else None
        existing_digest = None
        copy_existing = False
        effective_quality = int(self.quality)
        if self.downgrade_quality and not is_mp3:
            advertised_depth = track_url_dict.get("bit_depth")
            advertised_rate = track_url_dict.get("sampling_rate")
            if isinstance(advertised_depth, (int, float)) and isinstance(advertised_rate, (int, float)):
                effective_quality = 6 if advertised_depth <= 16 else 7 if advertised_rate <= 96 else 27
        if os.path.exists(final_file):

            def inspect_existing():
                identity, old_key = read_provenance(final_file, is_mp3)
                if identity != source_id:
                    raise DownloadError("Destination collision: existing audio has different or unknown provenance")
                digest = file_digest(final_file)
                known = expected_artifact_digest(self.history_path, final_file, source_id, old_key)
                try:
                    validate_audio(final_file, is_mp3, duration)
                    fmt, depth, rate = audio_quality(final_file, is_mp3)
                    usable = quality_satisfies(fmt, depth, rate, effective_quality) and (
                        known is None or known == digest
                    )
                except ValueError:
                    fmt, depth, rate, usable = int(self.quality), None, None, False
                return old_key, digest, usable, fmt, depth, rate

            old_key, existing_digest, copy_existing, fmt, depth, rate = await run_owned_thread(inspect_existing)
            if copy_existing and old_key == key:
                return DownloadResult(
                    DownloadStatus.COMPLETED, (replace(artifact, format_id=fmt, bit_depth=depth, sampling_rate=rate),)
                )

        with StagedArtifact(final_file) as staged:
            if copy_existing:
                await run_owned_thread(shutil.copyfile, final_file, staged.path)
            else:
                await self._transfer(client, url, staged.path, track_title)

            def finalize():
                validate_audio(staged.path, is_mp3, duration)
                fmt, depth, rate = audio_quality(staged.path, is_mp3)
                if not self.downgrade_quality and not quality_satisfies(fmt, depth, rate, int(self.quality)):
                    raise DownloadError("Delivered audio does not meet the requested quality")
                tag_function = metadata.tag_mp3 if is_mp3 else metadata.tag_flac
                tag_function(
                    staged.path,
                    root_dir,
                    final_file,
                    track_metadata,
                    album_or_track_metadata,
                    is_track,
                    self.embed_art,
                    self.cover_path,
                )
                stamp_provenance(staged.path, is_mp3, source_id, key)
                validate_audio(staged.path, is_mp3, duration)
                return fmt, depth, rate

            fmt, depth, rate = await run_owned_thread(finalize)
            artifact = replace(artifact, format_id=fmt, bit_depth=depth, sampling_rate=rate)
            # Commit starts only after the cancellable tagging/validation phase.
            # If cancellation arrives during atomic commit, settle that commit.
            if existing_digest:
                await run_owned_thread(replace_known, staged, existing_digest)
            else:
                await run_owned_thread(staged.publish)
        return DownloadResult(DownloadStatus.COMPLETED, (artifact,))

    @staticmethod
    def _get_filename_attr(artist, track_metadata, track_title):
        return {
            "artist": artist,
            "albumartist": _safe_get(track_metadata, "album", "artist", "name", default=artist),
            "bit_depth": track_metadata.get("maximum_bit_depth"),
            "sampling_rate": track_metadata.get("maximum_sampling_rate"),
            "tracktitle": track_title,
            "version": track_metadata.get("version"),
            "tracknumber": f"{track_metadata['track_number']:02}",
        }

    @staticmethod
    def _get_track_attr(meta, track_title, bit_depth, sampling_rate):
        return {
            "album": sanitize_filename(meta["album"]["title"]),
            "artist": sanitize_filename(meta["album"]["artist"]["name"]),
            "tracktitle": track_title,
            "year": meta["album"]["release_date_original"].split("-")[0],
            "bit_depth": bit_depth,
            "sampling_rate": sampling_rate,
        }

    @staticmethod
    def _get_album_attr(meta, album_title, file_format, bit_depth, sampling_rate):
        return {
            "artist": sanitize_filename(meta["artist"]["name"]),
            "album": sanitize_filename(album_title),
            "tracktitle": album_title,
            "year": meta["release_date_original"].split("-")[0],
            "format": file_format,
            "bit_depth": bit_depth,
            "sampling_rate": sampling_rate,
        }

    async def _get_format(
        self, item_dict, is_track_id=False, track_url_dict=None
    ) -> Tuple[str, bool, Optional[int], Optional[int]]:
        quality_met = True
        if int(self.quality) == 5:
            return "MP3", quality_met, None, None

        track_dict = item_dict
        if not is_track_id:
            track_dict = item_dict["tracks"]["items"][0]

        try:
            new_track_dict = (
                await self.client.get_track_url(track_dict["id"], fmt_id=self.quality)
                if not track_url_dict
                else track_url_dict
            )
            restrictions = new_track_dict.get("restrictions")
            if isinstance(restrictions, list) and any(
                isinstance(r, dict) and r.get("code") == QL_DOWNGRADE for r in restrictions
            ):
                quality_met = False

            depth = new_track_dict.get("bit_depth")
            rate = new_track_dict.get("sampling_rate")
            if not isinstance(depth, (int, float)) or not isinstance(rate, (int, float)):
                quality_met = False
            elif self.quality == 7 and depth < 24 or self.quality == 27 and (depth < 24 or rate <= 96):
                quality_met = False
            return (
                "FLAC",
                quality_met,
                new_track_dict.get("bit_depth"),
                new_track_dict.get("sampling_rate"),
            )
        except (KeyError, httpx.HTTPError):
            return "Unknown", quality_met, None, None

    async def _transfer(self, client, url, path, description):
        self._ensure_budgets()
        async with self.transfer_budget:
            await rich_download(client, url, path, self.progress, description)

    @asynccontextmanager
    async def _cover(self, client, meta, directory):
        staged = None
        cover = None
        try:
            if self.embed_art or not self.no_cover:
                url = _safe_get(meta, "image", "large")
                if not url:
                    raise DownloadError("Cover URL unavailable")
                if self.cover_og_quality:
                    url = url.replace("_600.", "_org.")
                if self.embed_art:
                    target = safe_destination(directory, "cover.jpg")
                    staged = StagedArtifact(target)
                    cached = await run_owned_thread(copy_cached_extra, target, url, staged.path, "cover.jpg")
                    if not cached:
                        await self._transfer(client, url, staged.path, "cover")
                        await run_owned_thread(validate_extra, staged.path, "cover.jpg")
                        if not self.no_cover:
                            await run_owned_thread(publish_extra, staged.path, target, url)
                    cover = staged.path
                else:
                    cover = await self._get_extra(client, url, directory)
        except Exception as error:
            if staged:
                staged.cleanup()
            if self.embed_art:
                raise DownloadError("Requested cover embedding unavailable") from error
            logger.warning("Optional cover unavailable (%s)", type(error).__name__)
        except BaseException:
            if staged:
                staged.cleanup()
            raise
        try:
            yield cover
        finally:
            if staged:
                staged.cleanup()

    async def _optional_extra(self, *args):
        try:
            return await self._get_extra(*args)
        except Exception as error:
            logger.warning("Optional booklet unavailable (%s)", type(error).__name__)
            return None

    async def _get_extra(
        self,
        client: httpx.AsyncClient,
        item: str,
        dirn: str,
        extra: str = "cover.jpg",
        og_quality: bool = False,
    ):
        extra_file = safe_destination(dirn, extra)
        url = item.replace("_600.", "_org.") if og_quality else item
        with StagedArtifact(extra_file) as stage:
            cached = await run_owned_thread(copy_cached_extra, extra_file, url, stage.path, extra)
            if not cached:
                await self._transfer(client, url, stage.path, extra)
                await run_owned_thread(validate_extra, stage.path, extra)
                await run_owned_thread(publish_extra, stage.path, extra_file, url)
        return extra_file


@get_retry
async def _stream_to_file(client: httpx.AsyncClient, url: str, fname: str, progress: Progress, task_id) -> None:
    download_size = 0
    try:
        async with client.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            # Reset in case this is a retry of a partially-completed attempt.
            progress.update(task_id, total=total, completed=0)
            with open(fname, "wb") as file:
                async for data in r.aiter_bytes(chunk_size=8192):
                    size = file.write(data)
                    progress.update(task_id, advance=size)
                    download_size += size

        measured = (
            r.num_bytes_downloaded if r.headers.get("content-encoding", "identity") != "identity" else download_size
        )
        if total != 0 and total != measured:
            raise ConnectionError(f"File download was interrupted for {fname}")
    except BaseException:
        if os.path.exists(fname):
            try:
                os.remove(fname)
            except OSError:
                pass
        raise


async def rich_download(
    client: httpx.AsyncClient,
    url: str,
    fname: str,
    progress: Progress,
    desc: Optional[str] = None,
):
    """Asynchronously download a file with a progress bar, retrying on
    transient network errors."""
    task_id = progress.add_task(f"[cyan]Downloading[/] [bold magenta]{desc or fname}[/]", total=0)
    try:
        await _stream_to_file(client, url, fname, progress, task_id)
    finally:
        progress.update(task_id, visible=False)


def _describe_exception(error: BaseException) -> str:
    if isinstance(error, RetryError):
        try:
            cause = error.last_attempt.exception()
        except Exception:
            cause = None
        if cause is not None:
            error = cause
    if isinstance(error, httpx.HTTPError):
        code = f" HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else ""
        return error.__class__.__name__ + code
    return f"{error.__class__.__name__}: {error}"


def _get_title(item_dict):
    album_title = item_dict["title"]
    version = item_dict.get("version")
    if version:
        album_title = f"{album_title} ({version})" if version.lower() not in album_title.lower() else album_title
    return album_title


def _clean_format_str(folder: str, track: str, file_format: str) -> Tuple[str, str]:
    """Cleans up the format strings, avoids errors
    with MP3 files.
    """
    cleaned: list[str] = []
    for i, fs in enumerate((folder, track)):
        if fs.endswith(".mp3"):
            fs = fs[:-4]
        elif fs.endswith(".flac"):
            fs = fs[:-5]
        fs = fs.strip()

        # default to pre-chosen string if format is invalid
        if file_format in ("MP3", "Unknown") and ("bit_depth" in fs or "sampling_rate" in fs):
            default = DEFAULT_FORMATS[file_format][i]
            logger.debug("Using %s naming pattern for %s", default, file_format)
            fs = default
        cleaned.append(fs)

    # Return a fixed-length tuple to satisfy typing (Tuple[str, str])
    return cleaned[0], cleaned[1]


def _safe_get(d: dict, *keys, default=None):
    """Safely traverse nested dicts using keys, returning default if any level is missing."""
    curr = d
    for i, key in enumerate(keys):
        if not isinstance(curr, dict):
            return default
        val = curr.get(key, default)
        if val is default:
            return default
        curr = val
    return curr
