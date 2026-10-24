import asyncio
import sqlite3
from types import SimpleNamespace

from qobuz_dl2.core import QobuzDL
from qobuz_dl2.db import handle_download_id
from qobuz_dl2.results import Artifact, DownloadResult, DownloadStatus


def test_history_failure_preserves_completed_artifact_outcome(tmp_path, monkeypatch):
    from qobuz_dl2 import core

    artifact = Artifact(str(tmp_path / "one.mp3"), "track", "1", "1", "key", 5)

    async def complete(*args):
        return DownloadResult(DownloadStatus.COMPLETED, (artifact,))

    def fail(*args):
        raise sqlite3.OperationalError("synthetic database failure")

    monkeypatch.setattr(core.downloader.Download, "download_id_by_type", complete)
    monkeypatch.setattr(core, "record_artifacts", fail)

    async def run():
        q = QobuzDL(directory=str(tmp_path))
        q.client = SimpleNamespace()
        try:
            result = await q.download_from_id("1", False)
            assert result.status is DownloadStatus.FAILED
            assert result.artifacts == (artifact,)
        finally:
            await q.aclose()

    asyncio.run(run())


def test_malformed_url_does_not_abort_other_requested_urls(tmp_path):
    async def run():
        q = QobuzDL(directory=str(tmp_path))
        q.client = SimpleNamespace()
        result = await q.download_list_of_urls(["https://[invalid", "https://foreign.test/track/1"])
        assert len(result.results) == 2 and result.has_failures

    asyncio.run(run())


def test_lucky_short_query_is_failed_request(tmp_path):
    async def run():
        q = QobuzDL(directory=str(tmp_path))
        assert await q.lucky_mode("a") is None
        assert q.last_result.has_failures

    asyncio.run(run())


def test_album_skip_does_not_write_history(tmp_path):
    async def run():
        q = QobuzDL(directory=str(tmp_path / "out"), downloads_db=str(tmp_path / "db"), ignore_singles_eps=True)

        async def meta(_):
            return {"streamable": True, "release_type": "single", "artist": {"name": "Artist"}, "title": "Skipped"}

        q.client = SimpleNamespace(get_album_meta=meta)
        result = await q.download_from_id("skipped")
        assert handle_download_id(q.downloads_db, "skipped") is None
        assert result.status.value == "skipped"

    asyncio.run(run())


def test_unavailable_track_does_not_write_history(tmp_path):
    async def run():
        q = QobuzDL(directory=str(tmp_path / "out"), downloads_db=str(tmp_path / "db"))

        async def url(*a, **kw):
            return {"sample": True}

        q.client = SimpleNamespace(get_track_url=url)
        r = await q.download_from_id("demo", album=False)
        assert r.status.value == "skipped"
        assert handle_download_id(q.downloads_db, "demo") is None

    asyncio.run(run())


def test_collection_downloads_every_page(tmp_path):
    from qobuz_dl2.results import DownloadResult, DownloadStatus

    async def run():
        async def pages(_):
            for n in range(3):
                yield {
                    "name": "A",
                    "albums": {"items": [{"id": str(i)} for i in range(n * 500, min(n * 500 + 500, 1001))]},
                }

        q = QobuzDL(directory=str(tmp_path), no_m3u_for_playlists=True)
        q.client = SimpleNamespace(get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages)
        ids = []

        async def record(id, *args):
            ids.append(id)
            return DownloadResult(DownloadStatus.SKIPPED, reason="synthetic")

        q.download_from_id = record
        result = await q.handle_url("https://play.qobuz.com/artist/1")
        assert ids == [str(i) for i in range(1001)]
        assert not result.has_failures

    asyncio.run(run())


def test_recursive_txt_include_is_rejected(tmp_path):
    async def run():
        path = tmp_path / "urls.txt"
        path.write_text(str(path))
        q = QobuzDL(directory=str(tmp_path / "out"))
        result = await q.download_from_txt_file(str(path))
        assert result.has_failures

    asyncio.run(run())


def test_partial_album_retry_fetches_missing_members_only(tmp_path, audio_bytes, track_meta):
    import copy

    import httpx

    from qobuz_dl2.db import find_valid_artifacts

    async def run():
        tracks = []
        for n in [1, 2]:
            data = copy.deepcopy(track_meta)
            data.update(id=str(n), track_number=n, title=f"Track {n}")
            tracks.append(data)

        async def meta(_):
            return {
                "id": "album",
                "title": "Album",
                "streamable": True,
                "artist": {"name": "Artist"},
                "release_date_original": "2020-01-01",
                "genres_list": ["Test"],
                "tracks_count": 2,
                "tracks": {"items": tracks},
            }

        async def url(id, *a, **kw):
            return {"url": "https://example.test/" + str(id), "format_id": 5}

        q = QobuzDL(directory=str(tmp_path / "out"), quality=5, no_cover=True, downloads_db=str(tmp_path / "db"))
        q.client = SimpleNamespace(get_album_meta=meta, get_track_url=url)
        fail = True
        calls = []

        def response(req):
            calls.append(req.url.path)
            return httpx.Response(404) if fail and req.url.path == "/2" else httpx.Response(200, content=audio_bytes)

        q._download_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        try:
            first = await q.download_from_id("album")
            assert first.status.value == "failed"
            assert len(first.artifacts) == 1
            a = first.artifacts[0]
            assert find_valid_artifacts(q.downloads_db, "album", "album", q.directory, 5, a.requirements_key) == (a,)
            fail = False
            second = await q.download_from_id("album")
            assert second.status.value == "completed"
            assert len(second.artifacts) == 2
            assert calls.count("/1") == 1
            assert calls.count("/2") == 2
        finally:
            await q.aclose()

    asyncio.run(run())


def test_second_playlist_destination_materializes(tmp_path, audio_bytes, track_meta):
    from pathlib import Path

    import httpx

    async def run():
        async def meta(_):
            return track_meta

        async def url(*a, **kw):
            return {"url": "https://example.test/audio", "format_id": 5}

        q = QobuzDL(directory=str(tmp_path / "out"), quality=5, no_cover=True, downloads_db=str(tmp_path / "db"))
        q.client = SimpleNamespace(get_track_meta=meta, get_track_url=url)
        q._download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        )
        try:
            one = await q.download_from_id("1", False, str(tmp_path / "first"))
            two = await q.download_from_id("1", False, str(tmp_path / "second"))
            assert one.status.value == two.status.value == "completed"
            assert one.artifacts[0].path != two.artifacts[0].path
            assert Path(one.artifacts[0].path).is_file() and Path(two.artifacts[0].path).is_file()
        finally:
            await q.aclose()

    asyncio.run(run())
