# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio
import gzip
import logging
import pathlib
import sys
import tempfile
from unittest.mock import patch

import httpx
from mutagen.mp3 import MP3
from tenacity import wait_none

import qobuz_dl2.downloader as dl
from qobuz_dl2 import metadata

logging.basicConfig(level=logging.CRITICAL)


class Progress:
    def add_task(self, *args, **kwargs):
        return 1

    def update(self, *args, **kwargs):
        pass


P = Progress()


def track():
    return dict(
        title="Track",
        track_number=1,
        media_number=1,
        maximum_bit_depth=16,
        maximum_sampling_rate=44.1,
        performer={"name": "Artist"},
        album={
            "artist": {"name": "Artist"},
            "genres_list": ["Rock"],
            "tracks_count": 1,
            "title": "Album",
            "release_date_original": "2020-01-01",
        },
    )


async def main():
    with tempfile.TemporaryDirectory(prefix="qobuz-probe-", dir="/private/tmp") as tmp:
        base = pathlib.Path(tmp)
        # Swallowed tagging failure.
        folder = base / "badflac"
        folder.mkdir()
        d = dl.Download(None, "x", str(folder), 27)
        d.progress = P
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"not FLAC"))
        ) as client:
            await d._run_download_tasks(
                [
                    d._download_and_tag(
                        client, str(folder), 0, {"url": "https://example.test/track"}, track(), track(), True, False
                    )
                ],
                "Album",
            )
        print(
            "tag failure outcome: returned success; finals=",
            list(folder.glob("*.flac")),
            "temporary=",
            [p.name for p in folder.iterdir()],
        )
        # Cancellation leaves partial final cover, next call skips it.
        folder = base / "cancel"
        folder.mkdir()
        started = asyncio.Event()
        requests = []

        class Paused(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 8192
                started.set()
                await asyncio.Event().wait()

        async def handler(req):
            requests.append(str(req.url))
            return httpx.Response(200, headers={"Content-Length": "16384"}, stream=Paused())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(d._get_extra(client, "https://example.test/cover", str(folder)))
            await started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await d._get_extra(client, "https://example.test/cover", str(folder))
        print(
            "cancel outcome: partial cover size=",
            (folder / "cover.jpg").stat().st_size,
            "requests including retry call=",
            len(requests),
        )
        # Cover write is deliberately gated until tagging starts, exposing race.
        folder = base / "cover-race"
        folder.mkdir()
        tag_done = asyncio.Event()
        errs = []
        original = metadata.tag_mp3

        def observed_tag(*args):
            try:
                return original(*args)
            except Exception as exc:
                errs.append(type(exc).__name__)
                raise
            finally:
                loop.call_soon_threadsafe(tag_done.set)

        async def handler(req):
            if req.url.path == "/cover":
                await tag_done.wait()
                return httpx.Response(200, content=b"fake jpeg")
            return httpx.Response(200, content=b"audio bytes")

        d = dl.Download(None, "x", str(folder), 5, embed_art=True)
        d.progress = P
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(metadata, "tag_mp3", observed_tag):
                await d._run_download_tasks(
                    [
                        d._get_extra(client, "https://example.test/cover", str(folder)),
                        d._download_and_tag(
                            client, str(folder), 0, {"url": "https://example.test/track"}, track(), track(), True, True
                        ),
                    ],
                    "Album",
                )
        print(
            "cover race outcome: tagging errors=",
            errs,
            "final MP3 count=",
            len(list(folder.glob("*.mp3"))),
            "cover exists=",
            (folder / "cover.jpg").exists(),
        )
        # MP3 tagger accepts HTML/empty body without validating any MPEG audio.
        folder = base / "badmp3"
        folder.mkdir()
        d = dl.Download(None, "x", str(folder), 5)
        d.progress = P
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"<html>not audio</html>"))
        ) as client:
            await d._download_and_tag(
                client, str(folder), 0, {"url": "https://example.test/track"}, track(), track(), True, True
            )
        final = next(folder.glob("*.mp3"))
        try:
            MP3(final)
            result = "valid"
        except Exception as exc:
            result = type(exc).__name__
        print("non-audio MP3 outcome: published=", final.name, "size=", final.stat().st_size, "parser result=", result)
        # Decoded bytes are compared against wire Content-Length.
        folder = base / "gzip"
        folder.mkdir()
        payload = gzip.compress(b"x" * 32768)
        count = 0

        async def compressed(req):
            nonlocal count
            count += 1
            return httpx.Response(
                200, content=payload, headers={"Content-Encoding": "gzip", "Content-Length": str(len(payload))}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(compressed)) as client:
            try:
                await dl._stream_to_file.retry_with(wait=wait_none())(
                    client, "https://example.test/track", str(folder / "file"), P, 1
                )
            except Exception as exc:
                print(
                    "gzip outcome:",
                    type(exc).__name__,
                    "requests=",
                    count,
                    "wire bytes=",
                    len(payload),
                    "decoded bytes=32768",
                    "file exists=",
                    (folder / "file").exists(),
                )


asyncio.run(main())
