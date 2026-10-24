# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio
import logging
import pathlib
import sys
import tempfile
from unittest.mock import patch

import httpx
import mutagen.id3

import qobuz_dl2.metadata as metadata
from qobuz_dl2.downloader import Download

logging.disable(logging.CRITICAL)


class P:
    def add_task(self, *a, **k):
        return 1

    def update(self, *a, **k):
        pass


async def main():
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="qobuz-cover-") as tmp:
        d = Download(None, "x", tmp, 5, embed_art=True)
        d.progress = P()
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        started = asyncio.Event()
        original = metadata.tag_mp3
        m = dict(
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

        class Cover(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 8192
                started.set()
                await done.wait()
                yield b"b" * 8192

        async def handler(req):
            if req.url.path == "/cover":
                return httpx.Response(200, headers={"Content-Length": "16384"}, stream=Cover())
            await started.wait()
            return httpx.Response(200, content=b"audio bytes")

        def observed(*a):
            try:
                return original(*a)
            finally:
                loop.call_soon_threadsafe(done.set)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with patch.object(metadata, "tag_mp3", observed):
                await d._run_download_tasks(
                    [
                        d._get_extra(c, "https://example.test/cover", tmp),
                        d._download_and_tag(c, tmp, 0, {"url": "https://example.test/track"}, m, m, True, True),
                    ],
                    "Album",
                )
        tags = mutagen.id3.ID3(next(pathlib.Path(tmp).glob("*.mp3")))
        print(
            "full cover bytes=",
            (pathlib.Path(tmp) / "cover.jpg").stat().st_size,
            "embedded bytes=",
            len(tags.getall("APIC")[0].data),
        )


asyncio.run(main())
