import asyncio

import pytest

from qobuz_dl2.downloader import DEFAULT_DOWNLOAD_LIMIT, Download, DownloadError


class DummyClient:
    pass


def test_download_limit_defaults_to_conservative_value(tmp_path):
    downloader = Download(DummyClient(), "album-id", str(tmp_path), 27)

    assert downloader.limit == DEFAULT_DOWNLOAD_LIMIT


def test_download_task_failure_is_aggregated(tmp_path):
    downloader = Download(DummyClient(), "album-id", str(tmp_path), 27)

    async def fail():
        raise ConnectionError("stream closed")

    with pytest.raises(DownloadError, match="1 download task"):
        asyncio.run(downloader._run_download_tasks([fail()], "Test Album"))


def test_tag_failure_propagates_failed_result(tmp_path):
    import httpx

    async def run():
        d = Download(DummyClient(), "track", str(tmp_path), 6)
        meta = {
            "id": "1",
            "title": "Broken",
            "track_number": 1,
            "maximum_bit_depth": 16,
            "maximum_sampling_rate": 44.1,
            "performer": {"name": "Artist"},
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"bad FLAC"))
        ) as c:
            with pytest.raises(DownloadError):
                await d._run_download_tasks(
                    [
                        d._download_and_tag(
                            c, str(tmp_path), 0, {"url": "https://example.test/audio"}, meta, meta, True, False
                        )
                    ],
                    "Broken",
                )

    asyncio.run(run())


def test_cancelled_extra_leaves_no_final(tmp_path):
    import httpx

    async def run():
        started = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 8192
                started.set()
                await asyncio.Event().wait()

        async def reply(req):
            return httpx.Response(200, headers={"Content-Length": "16384"}, stream=Stream())

        d = Download(DummyClient(), "x", str(tmp_path), 6)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as c:
            task = asyncio.create_task(d._get_extra(c, "https://example.test/cover", str(tmp_path)))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not (tmp_path / "cover.jpg").exists()
        assert not list(tmp_path.iterdir())

    asyncio.run(run())


def test_encoding_aware_transfer_integrity(tmp_path):
    import gzip

    import httpx
    from rich.progress import Progress

    from qobuz_dl2.downloader import _stream_to_file

    async def run():
        body = b"x" * 32768
        encoded = gzip.compress(body)

        class Encoded(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield encoded

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, stream=Encoded(), headers={"Content-Encoding": "gzip", "Content-Length": str(len(encoded))}
                )
            )
        ) as c:
            progress = Progress()
            task = progress.add_task("test")
            await _stream_to_file.retry_with(wait=lambda state: 0)(
                c, "https://example.test/file", str(tmp_path / "file"), progress, task
            )
        assert (tmp_path / "file").read_bytes() == body

    asyncio.run(run())


@pytest.mark.parametrize("partial", [False, True], ids=["creation", "flush"])
def test_embedding_waits_for_complete_cover(tmp_path, audio_bytes, cover_bytes, track_meta, partial):
    from types import SimpleNamespace

    import httpx
    from mutagen.id3 import ID3

    async def run():
        ready = asyncio.Event()
        release = asyncio.Event()

        class Cover(httpx.AsyncByteStream):
            async def __aiter__(self):
                if partial:
                    yield cover_bytes[:20]
                ready.set()
                await release.wait()
                yield cover_bytes[20:] if partial else cover_bytes

        async def meta(_):
            return track_meta

        async def url(*a, **k):
            return {"url": "https://example.test/audio", "sampling_rate": 44.1, "format_id": 5}

        async def reply(req):
            return (
                httpx.Response(200, stream=Cover())
                if req.url.path == "/cover"
                else httpx.Response(200, content=audio_bytes)
            )

        d = Download(SimpleNamespace(get_track_meta=meta, get_track_url=url), "1", str(tmp_path), 5, embed_art=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as c:
            task = asyncio.create_task(d.download_track(c))
            await asyncio.wait_for(ready.wait(), 2)
            await asyncio.sleep(0.03)
            assert not list(tmp_path.rglob("*.mp3"))
            release.set()
            result = await asyncio.wait_for(task, 2)
        assert result.status.value == "completed"
        tags = ID3(result.artifacts[0].path)
        assert tags.getall("APIC")[0].data == cover_bytes

    asyncio.run(run())


def test_second_track_downgrade_respects_no_fallback(tmp_path):
    from types import SimpleNamespace

    from qobuz_dl2.downloader import QL_DOWNGRADE

    async def run():
        async def url(*a, **kw):
            return {
                "url": "https://example.test/audio",
                "bit_depth": 16,
                "sampling_rate": 44.1,
                "restrictions": [{"code": QL_DOWNGRADE}],
            }

        d = Download(SimpleNamespace(get_track_url=url), "album", str(tmp_path), 27, downgrade_quality=False)
        result = await d._process_track(None, str(tmp_path), 0, {"id": "1", "media_number": 1}, {}, False)
        assert result.status.value == "skipped"
        assert not list(tmp_path.iterdir())

    asyncio.run(run())


def test_no_cover_with_embed_art_does_not_retain_cover(tmp_path, audio_bytes, cover_bytes, track_meta):
    from types import SimpleNamespace

    import httpx

    async def run():
        async def meta(_):
            return track_meta

        async def url(*a, **k):
            return {"url": "https://example.test/audio", "sampling_rate": 44.1}

        d = Download(
            SimpleNamespace(get_track_meta=meta, get_track_url=url),
            "1",
            str(tmp_path),
            5,
            embed_art=True,
            no_cover=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=cover_bytes if r.url.path == "/cover" else audio_bytes)
            )
        ) as c:
            result = await d.download_id_by_type() if False else await d.download_track(c)
        assert result.status.value == "completed"
        assert not list(tmp_path.rglob("cover.jpg"))
        assert not list(tmp_path.rglob("*.tmp"))

    asyncio.run(run())


def test_same_quality_embedding_upgrade_is_not_skipped(tmp_path, audio_bytes, cover_bytes, track_meta):
    from types import SimpleNamespace

    import httpx
    from mutagen.id3 import ID3

    async def run():
        async def meta(_):
            return track_meta

        async def url(*a, **k):
            return {"url": "https://example.test/audio", "sampling_rate": 44.1, "format_id": 5}

        api = SimpleNamespace(get_track_meta=meta, get_track_url=url)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=cover_bytes if r.url.path == "/cover" else audio_bytes)
            )
        ) as c:
            first = await Download(api, "1", str(tmp_path), 5).download_track(c)
            second = await Download(api, "1", str(tmp_path), 5, embed_art=True).download_track(c)
        assert second.status.value == "completed"
        assert second.artifacts[0].path == first.artifacts[0].path
        assert ID3(second.artifacts[0].path).getall("APIC")[0].data == cover_bytes

    asyncio.run(run())


def test_corrupt_known_audio_is_recovered_without_destroying_original(tmp_path, audio_bytes, track_meta):
    from types import SimpleNamespace

    import httpx
    from mutagen.id3 import ID3

    async def run():
        async def meta(_):
            return track_meta

        async def url(*a, **k):
            return {"url": "https://example.test/audio", "format_id": 5, "sampling_rate": 44.1}

        api = SimpleNamespace(get_track_meta=meta, get_track_url=url)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        ) as c:
            first = await Download(api, "1", str(tmp_path), 5, no_cover=True).download_track(c)
            from pathlib import Path

            path = Path(first.artifacts[0].path)
            damaged = path.read_bytes()[: len(audio_bytes) // 2]
            path.write_bytes(damaged)
            second = await Download(api, "1", str(tmp_path), 5, no_cover=True).download_track(c)
        assert second.status.value == "completed"
        assert ID3(second.artifacts[0].path).get("TXXX:QOBUZ_TRACK_ID").text == ["1"]
        assert any(p.read_bytes() == damaged for p in path.parent.glob("*.previous-*"))

    asyncio.run(run())


def test_nullable_optional_metadata_does_not_lose_audio(tmp_path, audio_bytes, track_meta):
    import copy
    from types import SimpleNamespace

    import httpx

    async def run():
        data = copy.deepcopy(track_meta)
        data["performer"] = None
        data["composer"] = None
        data["album"]["label"] = None
        data["album"]["genres_list"] = None

        async def meta(_):
            return data

        async def url(*a, **kw):
            return {"url": "https://example.test/audio", "sampling_rate": 44.1}

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        ) as c:
            result = await Download(
                SimpleNamespace(get_track_meta=meta, get_track_url=url), "1", str(tmp_path), 5, no_cover=True
            ).download_track(c)
        assert result.status.value == "completed"

    asyncio.run(run())


def test_quality_upgrade_does_not_reuse_lower_quality_same_path(tmp_path, track_meta):
    import copy
    from pathlib import Path
    from types import SimpleNamespace

    import httpx

    async def run():
        data = copy.deepcopy(track_meta)

        async def meta(_):
            return data

        async def url(id, fmt_id):
            return {
                "url": f"https://example.test/{fmt_id}",
                "bit_depth": 24 if fmt_id == 27 else 16,
                "sampling_rate": 192 if fmt_id == 27 else 44.1,
            }

        def response(req):
            fixture = "tone-hires.flac" if req.url.path == "/27" else "tone.flac"
            return httpx.Response(200, content=(Path(__file__).parent / "fixtures" / fixture).read_bytes())

        api = SimpleNamespace(get_track_meta=meta, get_track_url=url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            first = await Download(
                api, "1", str(tmp_path), 6, no_cover=True, folder_format="{artist} - {album}"
            ).download_track(c)
            second = await Download(
                api, "1", str(tmp_path), 27, no_cover=True, folder_format="{artist} - {album}"
            ).download_track(c)
        assert second.artifacts[0].path == first.artifacts[0].path
        assert second.artifacts[0].format_id == 27
        assert second.artifacts[0].bit_depth == 24

    asyncio.run(run())


def test_request_error_description_does_not_disclose_signed_url():
    import httpx

    from qobuz_dl2.downloader import _describe_exception

    request = httpx.Request("GET", "https://example.test/audio?token=SYNTHETIC_SECRET")
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError(
        "sensitive https://example.test?token=SYNTHETIC_SECRET", request=request, response=response
    )
    assert "SYNTHETIC_SECRET" not in _describe_exception(error)


def test_album_folder_cannot_escape_selected_root(tmp_path):
    from types import SimpleNamespace

    async def run():
        async def meta(_):
            return {
                "streamable": True,
                "title": "Album",
                "artist": {"name": "Artist"},
                "release_date_original": "2020-01-01",
                "tracks": {"items": [{"id": "1", "media_number": 1}]},
            }

        d = Download(
            SimpleNamespace(get_album_meta=meta),
            "x",
            str(tmp_path / "root"),
            5,
            folder_format="../outside",
            no_cover=True,
        )
        with pytest.raises((ValueError, DownloadError)):
            await d.download_release(None)
        assert not (tmp_path / "outside").exists()

    asyncio.run(run())


def test_embedding_uses_current_cover_source_not_unverified_old_file(tmp_path, audio_bytes, cover_bytes, track_meta):
    import copy
    import struct
    import zlib
    from types import SimpleNamespace

    import httpx
    from mutagen.id3 import ID3

    async def run():
        payload = b"Comment\x00new cover"
        chunk = b"tEXt" + payload
        newer = (
            cover_bytes[:-12]
            + struct.pack(">I", len(payload))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk))
            + cover_bytes[-12:]
        )
        data = copy.deepcopy(track_meta)

        async def meta(_):
            return data

        async def url(*a, **k):
            return {"url": "https://example.test/audio", "sampling_rate": 44.1}

        api = SimpleNamespace(get_track_meta=meta, get_track_url=url)

        async def response(req):
            return httpx.Response(
                200,
                content=audio_bytes
                if req.url.path == "/audio"
                else newer
                if req.url.path == "/cover-v2"
                else cover_bytes,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            await Download(api, "1", str(tmp_path), 5).download_track(c)
            data["album"]["image"]["large"] = "https://example.test/cover-v2"
            result = await Download(api, "1", str(tmp_path), 5, embed_art=True).download_track(c)
        assert ID3(result.artifacts[0].path).getall("APIC")[0].data == newer

    asyncio.run(run())


def test_cancel_during_tagging_cannot_publish_after_return(tmp_path, audio_bytes, track_meta, monkeypatch):
    import threading
    from types import SimpleNamespace

    import httpx

    from qobuz_dl2 import metadata

    original = metadata.tag_mp3

    async def run():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def delayed(*args):
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
            return original(*args)

        monkeypatch.setattr(metadata, "tag_mp3", delayed)

        async def meta(_):
            return track_meta

        async def url(*a, **kw):
            return {"url": "https://example.test/audio", "format_id": 5}

        d = Download(SimpleNamespace(get_track_meta=meta, get_track_url=url), "1", str(tmp_path), 5, no_cover=True)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        ) as c:
            task = asyncio.create_task(d.download_track(c))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            assert not list(tmp_path.rglob("*.mp3"))
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not list(tmp_path.rglob("*.mp3"))
        assert not list(tmp_path.rglob("*.tmp"))

    asyncio.run(run())


def test_metadata_identity_mismatch_does_not_publish(tmp_path, audio_bytes, track_meta):
    import copy
    from types import SimpleNamespace

    import httpx

    async def run():
        data = copy.deepcopy(track_meta)
        data["id"] = "2"

        async def meta(_):
            return data

        async def url(*a, **kw):
            return {"url": "https://example.test/audio", "format_id": 5}

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        ) as c:
            with pytest.raises((ValueError, DownloadError)):
                await Download(
                    SimpleNamespace(get_track_meta=meta, get_track_url=url), "1", str(tmp_path), 5, no_cover=True
                ).download_track(c)
        assert not list(tmp_path.rglob("*.mp3"))

    asyncio.run(run())


def test_truncated_unencoded_transfer_retries_and_cleans(tmp_path):
    import httpx
    from rich.progress import Progress
    from tenacity import wait_none

    from qobuz_dl2.downloader import _stream_to_file

    async def run():
        calls = []

        class Short(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 10

        def response(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Length": "20"}, stream=Short())

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as c:
            p = Progress()
            t = p.add_task("test")
            with pytest.raises(ConnectionError):
                await _stream_to_file.retry_with(wait=wait_none())(
                    c, "https://example.test/audio", str(tmp_path / "partial"), p, t
                )
        assert len(calls) == 3
        assert not (tmp_path / "partial").exists()

    asyncio.run(run())


def test_failed_outcome_cannot_be_aggregated_as_complete(tmp_path):
    from qobuz_dl2.results import DownloadResult, DownloadStatus

    async def run():
        async def failed():
            return DownloadResult(DownloadStatus.FAILED, errors=("failure",))

        d = Download(None, "x", str(tmp_path), 6)
        with pytest.raises(DownloadError):
            await d._run_download_tasks([failed()], "Item")

    asyncio.run(run())
