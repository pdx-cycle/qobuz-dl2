import asyncio
import copy
from types import SimpleNamespace

import httpx

from qobuz_dl2.core import QobuzDL
from qobuz_dl2.results import DownloadResult, DownloadStatus


def test_playlist_reaches_configured_global_concurrency(tmp_path):
    async def run():
        q = QobuzDL(directory=str(tmp_path), download_limit=2, no_m3u_for_playlists=True)

        async def pages(_):
            yield {"name": "Playlist", "tracks": {"items": [{"id": str(i)} for i in range(8)]}}

        q.client = SimpleNamespace(get_artist_meta=pages, get_label_meta=pages, get_plist_meta=pages)
        active = peak = 0

        async def delayed(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return DownloadResult(DownloadStatus.SKIPPED, reason="synthetic")

        q.download_from_id = delayed
        result = await q.handle_url("https://play.qobuz.com/playlist/1")
        assert not result.has_failures
        assert peak == 2

    asyncio.run(run())


def test_concurrent_repeat_transfers_once_and_preserves_entries(tmp_path, audio_bytes, track_meta):
    async def run():
        q = QobuzDL(directory=str(tmp_path), quality=5, no_cover=True, download_limit=2)

        async def pages(_):
            yield {"name": "Playlist", "tracks": {"items": [{"id": "1"}] * 4}}

        async def meta(_):
            return copy.deepcopy(track_meta)

        async def url(*a, **kw):
            return {"url": "https://example.test/audio", "format_id": 5, "sampling_rate": 44.1}

        q.client = SimpleNamespace(
            get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages, get_track_meta=meta, get_track_url=url
        )
        requests = []

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(0.01)
                yield audio_bytes

        def response(req):
            requests.append(req)
            return httpx.Response(200, stream=Stream())

        q._download_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            result = await q.handle_url("https://play.qobuz.com/playlist/1")
            assert not result.has_failures
            assert len(result.artifacts) == 4
            assert len(requests) == 1
            lines = next(tmp_path.rglob("*.m3u")).read_text().splitlines()
            assert len([s for s in lines if s and not s.startswith("#")]) == 4
        finally:
            await q.aclose()

    asyncio.run(run())


def test_pending_work_is_bounded():
    from importlib.util import find_spec

    assert find_spec("qobuz_dl2.scheduling") is not None, "Bounded work queue missing"
    from qobuz_dl2.scheduling import bounded_map

    async def run():
        consumed = 0
        gate = asyncio.Event()
        started = asyncio.Event()

        async def items():
            nonlocal consumed
            for i in range(1000):
                consumed += 1
                yield i

        async def work(item):
            started.set()
            await gate.wait()
            return item

        task = asyncio.create_task(bounded_map(work, items(), 2))
        await started.wait()
        await asyncio.sleep(0.01)
        assert consumed <= 7
        gate.set()
        assert await task == list(range(1000))

    asyncio.run(run())


def test_multiple_albums_do_not_multiply_transfer_budget(tmp_path, audio_bytes, track_meta):
    async def run():
        q = QobuzDL(directory=str(tmp_path), quality=5, no_cover=True, download_limit=2, no_m3u_for_playlists=True)

        async def pages(_):
            yield {"name": "Artist", "albums": {"items": [{"id": str(n)} for n in range(4)]}}

        async def album(id):
            tracks = []
            for n in range(3):
                data = copy.deepcopy(track_meta)
                data.update(id=f"{id}{n}", track_number=n + 1, title=f"Track {n}")
                tracks.append(data)
            return {
                "id": id,
                "streamable": True,
                "title": f"Album {id}",
                "artist": {"name": "Artist"},
                "release_date_original": "2020-01-01",
                "genres_list": ["Test"],
                "tracks_count": 3,
                "tracks": {"items": tracks},
            }

        async def url(id, *a, **kw):
            return {"url": "https://example.test/" + id, "format_id": 5, "sampling_rate": 44.1}

        q.client = SimpleNamespace(
            get_artist_meta=pages, get_label_meta=pages, get_plist_meta=pages, get_album_meta=album, get_track_url=url
        )
        active = peak = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.005)
                    yield audio_bytes
                finally:
                    active -= 1

        q._download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream()))
        )
        try:
            result = await q.handle_url("https://play.qobuz.com/artist/1")
            assert not result.has_failures
            assert len(result.artifacts) == 12
            assert peak == 2
        finally:
            await q.aclose()

    asyncio.run(run())


def test_cancel_stops_workers_and_closes_pool(tmp_path, audio_bytes, track_meta):
    import pytest

    async def run():
        q = QobuzDL(directory=str(tmp_path), quality=5, no_cover=True, download_limit=2, no_m3u_for_playlists=True)

        async def pages(_):
            yield {"name": "List", "tracks": {"items": [{"id": str(n)} for n in range(20)]}}

        async def meta(id):
            data = copy.deepcopy(track_meta)
            data.update(id=id, track_number=int(id) + 1)
            return data

        async def url(id, *a, **kw):
            return {"url": "https://example.test/" + id, "format_id": 5}

        q.client = SimpleNamespace(
            get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages, get_track_meta=meta, get_track_url=url
        )
        started = asyncio.Event()
        active = 0

        class Blocked(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active
                active += 1
                try:
                    yield audio_bytes[:8192]
                    started.set()
                    await asyncio.Event().wait()
                finally:
                    active -= 1

        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Blocked())))
        q._download_client = client
        task = asyncio.create_task(q.handle_url("https://play.qobuz.com/playlist/1"))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await q.aclose()
        assert active == 0 and client.is_closed and not q._inflight
        assert not list(tmp_path.rglob("*.tmp")) and not list(tmp_path.rglob("*.mp3"))

    asyncio.run(run())
