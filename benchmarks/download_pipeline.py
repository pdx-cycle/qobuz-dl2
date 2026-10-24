"""Offline synthetic real download/tag/publication pipeline; no account or CDN use.

Run: uv run python benchmarks/download_pipeline.py --runs 5 --output results.json
"""

import argparse
import asyncio
import io
import json
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from mutagen.id3 import ID3

from qobuz_dl2 import downloader
from qobuz_dl2.core import QobuzDL
from qobuz_dl2.scheduling import bounded_map


async def trial(payload, limit, count, slow=False, mixed=False):
    requests = wire = active = peak = pools = 0
    ticks = []
    stop = asyncio.Event()

    async def heartbeat():
        previous = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            ticks.append(now - previous)
            previous = now

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal active, peak, wire
            active += 1
            peak = max(peak, active)
            try:
                chunk = max(8192, len(payload) // 4)
                for offset in range(0, len(payload), chunk):
                    await asyncio.sleep(0.002)
                    data = payload[offset : offset + chunk]
                    wire += len(data)
                    yield data
            finally:
                active -= 1

    def response(request):
        nonlocal requests
        requests += 1
        if mixed and int(request.url.path[1:]) % 7 == 0:
            return httpx.Response(404)
        return httpx.Response(200, stream=Stream())

    async def pages(_):
        for offset in range(0, count, 500):
            yield {
                "name": "Synthetic",
                "tracks": {"items": [{"id": str(n)} for n in range(offset, min(count, offset + 500))]},
            }

    async def meta(identity):
        return {
            "id": identity,
            "title": f"Tone {identity}",
            "track_number": int(identity) + 1,
            "media_number": 1,
            "duration": 5,
            "performer": {"name": "Artist"},
            "album": {
                "id": "album",
                "title": "Album",
                "artist": {"name": "Artist"},
                "release_date_original": "2020-01-01",
                "genres_list": ["Test"],
                "tracks_count": count,
            },
        }

    async def url(identity, *args, **kwargs):
        return {"url": "https://example.test/" + str(identity), "format_id": 5}

    original_client, original_progress, original_tag = (
        httpx.AsyncClient,
        downloader.Progress,
        downloader.metadata.tag_mp3,
    )

    def client(*args, **kwargs):
        nonlocal pools
        pools += 1
        kwargs["transport"] = httpx.MockTransport(response)
        return original_client(*args, **kwargs)

    def tag(*args, **kwargs):
        if slow:
            time.sleep(0.005)
        return original_tag(*args, **kwargs)

    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="qobuz-bench-") as temporary:
        q = QobuzDL(directory=temporary, quality=5, download_limit=limit, no_cover=True)
        q.client = SimpleNamespace(
            get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages, get_track_meta=meta, get_track_url=url
        )
        beat = asyncio.create_task(heartbeat())
        start = time.perf_counter()
        try:
            with (
                patch.object(httpx, "AsyncClient", client),
                patch.object(
                    downloader, "Progress", lambda *args, **kwargs: original_progress(*args, **kwargs, disable=True)
                ),
                patch.object(downloader.metadata, "tag_mp3", tag),
            ):
                result = await q.handle_url("https://open.qobuz.com/playlist/1")
            elapsed = time.perf_counter() - start
            assert len(result.results) == count
            assert result.has_failures == mixed
            assert peak <= limit and pools == 1 and requests == count
            assert len(result.artifacts) == count - (len(range(0, count, 7)) if mixed else 0)
            assert not list(Path(temporary).rglob("*.tmp"))
        finally:
            await q.aclose()
            stop.set()
            await beat
    _, memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "seconds": elapsed,
        "requests": requests,
        "bytes": wire,
        "peak_streams": peak,
        "pools": pools,
        "peak_traced_bytes": memory,
        "max_heartbeat_gap_ms": max(ticks, default=0) * 1000,
    }


async def cancellation_probe():
    started = asyncio.Event()
    active = 0

    async def blocked(item):
        nonlocal active
        active += 1
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            active -= 1

    task = asyncio.create_task(bounded_map(blocked, range(1501), 2))
    await started.wait()
    start = time.perf_counter()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert active == 0
    return (time.perf_counter() - start) * 1000


async def run(args):
    audio = (Path(__file__).resolve().parents[1] / "tests/fixtures/tone.mp3").read_bytes()
    large = io.BytesIO(audio)
    ID3(large).save(large, padding=lambda info: 512 * 1024)
    cases = {}
    for name, payload, slow, mixed in [
        ("small", audio, False, False),
        ("large", large.getvalue(), False, False),
        ("slow_tag", audio, True, False),
        ("mixed_404", audio, False, True),
    ]:
        for limit in (1, 2, 4):
            samples = [await trial(payload, limit, args.tracks, slow, mixed) for _ in range(args.runs)]
            cases[f"{name}_limit_{limit}"] = {
                "median": {key: statistics.median(row[key] for row in samples) for key in samples[0]},
                "samples": samples,
            }
    if args.large_collection:
        cases["collection_1501_limit_4"] = await trial(audio, 4, 1501)
    return {
        "method": "MockTransport; generated sine; real Mutagen/disk publication; cold temp destination per run",
        "runs": args.runs,
        "tracks": args.tracks,
        "cases": cases,
        "scheduler_cancellation_ms": await cancellation_probe(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--tracks", type=int, default=12)
    parser.add_argument("--large-collection", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.tracks < 1:
        parser.error("runs and tracks must be positive")
    args.output.write_text(json.dumps(asyncio.run(run(args)), indent=2) + "\n", encoding="utf-8")
