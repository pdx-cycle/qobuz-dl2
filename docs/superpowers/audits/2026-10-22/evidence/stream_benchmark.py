# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio
import logging
import pathlib
import statistics
import sys
import tempfile
import time

import httpx
from rich.progress import Progress

from qobuz_dl2.downloader import _stream_to_file

logging.disable(logging.CRITICAL)


class NullProgress:
    def update(self, *a, **k):
        pass


payload = b"x" * (64 * 1024 * 1024)


async def write_big(client, url, fname, progress, task_id):
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        progress.update(task_id, total=len(payload), completed=0)
        with open(fname, "wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=256 * 1024):
                size = f.write(chunk)
                progress.update(task_id, advance=size)


async def main():
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="qobuz-perf-") as tmp:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=payload))
        ) as c:
            for label, fn, p in [
                ("8KiB real Rich", _stream_to_file, Progress()),
                ("8KiB no progress", _stream_to_file, NullProgress()),
                ("256KiB real Rich", write_big, Progress()),
            ]:
                task = p.add_task("bench", total=0) if isinstance(p, Progress) else 1
                timings = []
                for i in range(5):
                    path = str(pathlib.Path(tmp) / "file")
                    start = time.perf_counter()
                    await fn(c, "https://example.test/track", path, p, task)
                    timings.append(time.perf_counter() - start)
                print(
                    label,
                    "median_seconds=",
                    round(statistics.median(timings), 5),
                    "MiB/s=",
                    round(64 / statistics.median(timings), 1),
                )


asyncio.run(main())
