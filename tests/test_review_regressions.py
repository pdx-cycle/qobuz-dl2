import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3

from qobuz_dl2.core import QobuzDL
from qobuz_dl2.downloader import Download
from qobuz_dl2.qopy import Client
from qobuz_dl2.results import Artifact, DownloadResult, DownloadStatus


@pytest.mark.parametrize("manifest", [None, {"items": None}, {"items": []}, "short"])
def test_truncated_album_manifest_cannot_claim_completion(tmp_path, track_meta, audio_bytes, manifest):
    async def run():
        url_calls = 0
        data = copy.deepcopy(track_meta["album"])
        data.update(
            streamable=True,
            tracks_count=2,
            tracks={"items": [copy.deepcopy(track_meta)]} if manifest == "short" else manifest,
        )

        async def meta(_):
            return data

        async def url(*args, **kwargs):
            nonlocal url_calls
            url_calls += 1
            return {"url": "https://example.test/audio", "format_id": 5}

        q = QobuzDL(directory=str(tmp_path), quality=5, no_cover=True)
        q.client = SimpleNamespace(get_album_meta=meta, get_track_url=url)
        q._download_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=audio_bytes))
        )
        try:
            result = await q.download_from_id("album")
            assert result.status is DownloadStatus.FAILED and not result.artifacts
            assert url_calls == 0
        finally:
            await q.aclose()

    asyncio.run(run())


def test_true_zero_track_album_is_skipped(tmp_path, track_meta):
    async def run():
        data = copy.deepcopy(track_meta["album"])
        data.update(streamable=True, tracks_count=0, tracks={"items": []})

        async def meta(_):
            return data

        q = QobuzDL(directory=str(tmp_path), quality=5, no_cover=True)
        q.client = SimpleNamespace(get_album_meta=meta)
        try:
            result = await q.download_from_id("album")
            assert result.status is DownloadStatus.SKIPPED and not result.artifacts
        finally:
            await q.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["flac", "mp3"])
def test_artwork_refresh_replaces_existing_front_cover(tmp_path, cover_bytes, track_meta, kind):
    async def run():
        data = copy.deepcopy(track_meta)
        payload = (Path(__file__).parent / "fixtures" / f"tone.{kind}").read_bytes()
        current_cover = cover_bytes
        transfers = 0

        async def meta(_):
            return data

        async def url(*args, **kwargs):
            return {
                "url": "https://example.test/audio",
                "format_id": 5 if kind == "mp3" else 6,
                "bit_depth": 16,
                "sampling_rate": 44.1,
            }

        def response(request):
            nonlocal transfers
            if request.url.path == "/audio":
                transfers += 1
                return httpx.Response(200, content=payload)
            return httpx.Response(200, content=current_cover)

        api = SimpleNamespace(get_track_meta=meta, get_track_url=url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            first = await Download(api, "1", str(tmp_path), 5 if kind == "mp3" else 6, embed_art=True).download_track(
                client
            )
            path = first.artifacts[0].path
            if kind == "mp3":
                tags = ID3(path)
                tags.add(APIC(encoding=3, mime="image/png", type=3, desc="existing cover", data=cover_bytes))
                tags.add(APIC(encoding=3, mime="image/png", type=4, desc="back cover", data=cover_bytes))
                tags.save(path, v2_version=3)
            else:
                audio = FLAC(path)
                picture = Picture()
                picture.type, picture.mime, picture.data = 4, "image/png", cover_bytes
                audio.add_picture(picture)
                audio.save()
            data["album"]["image"]["large"] = "https://example.test/updated-cover"
            # A bounded valid-signature image fixture with distinguishable bytes.
            current_cover = cover_bytes[:16] + b"changed" + cover_bytes[16:]
            second = await Download(api, "1", str(tmp_path), 5 if kind == "mp3" else 6, embed_art=True).download_track(
                client
            )
            assert second.status is DownloadStatus.COMPLETED
            images = FLAC(path).pictures if kind == "flac" else ID3(path).getall("APIC")
            front = [picture for picture in images if picture.type == 3]
            assert len(front) == 1 and front[0].data == current_cover
            assert [picture.data for picture in images if picture.type == 4] == [cover_bytes]
            assert transfers == 1
            assert list(Path(path).parent.glob("*.previous-*"))

    asyncio.run(run())


@pytest.mark.parametrize("block", [None, {}, {"items": None}, {"items": []}])
def test_positive_collection_count_cannot_succeed_without_members(tmp_path, block):
    async def run():
        api = Client("synthetic", "123", [])

        async def response(*args, **kwargs):
            return {"name": "Incomplete", "tracks_count": 2, "tracks": block}

        api.api_call = response
        q = QobuzDL(directory=str(tmp_path), no_m3u_for_playlists=True)
        q.client = api
        try:
            result = await q.handle_url("https://open.qobuz.com/playlist/1")
            assert result.has_failures and not result.artifacts
            assert result.results[-1].errors == ("ResponseError",)
        finally:
            await q.aclose()

    asyncio.run(run())


def test_premature_empty_page_preserves_successful_siblings_and_fails(tmp_path):
    async def run():
        api = Client("synthetic", "123", [])

        async def response(*args, **kwargs):
            return {
                "name": "Incomplete",
                "tracks_count": 2,
                "tracks": {"items": [{"id": "1"}] if kwargs["offset"] == 0 else []},
            }

        api.api_call = response
        q = QobuzDL(directory=str(tmp_path), no_m3u_for_playlists=True)
        q.client = api
        artifact = Artifact(str(tmp_path / "one.mp3"), "track", "1", "1", "key", 5)

        async def complete(*args):
            return DownloadResult(DownloadStatus.COMPLETED, (artifact,))

        q.download_from_id = complete
        try:
            result = await q.handle_url("https://open.qobuz.com/playlist/1")
            assert result.has_failures
            assert result.artifacts == (artifact,)
        finally:
            await q.aclose()

    asyncio.run(run())


def test_true_empty_collection_is_explicit_skip(tmp_path):
    async def run():
        api = Client("synthetic", "123", [])

        async def response(*args, **kwargs):
            return {"name": "Empty", "tracks_count": 0, "tracks": {"items": []}}

        api.api_call = response
        q = QobuzDL(directory=str(tmp_path), no_m3u_for_playlists=True)
        q.client = api
        try:
            result = await q.handle_url("https://open.qobuz.com/playlist/1")
            assert len(result.results) == 1 and result.results[0].status is DownloadStatus.SKIPPED
        finally:
            await q.aclose()

    asyncio.run(run())
