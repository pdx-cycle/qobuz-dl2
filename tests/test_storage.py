import importlib.util

import pytest


def storage():
    assert importlib.util.find_spec("qobuz_dl2.storage") is not None, "Owned staging missing"
    from qobuz_dl2 import storage

    return storage


def test_two_operations_have_distinct_staging_paths(tmp_path):
    cls = storage().StagedArtifact
    with cls(str(tmp_path / "song.mp3")) as first, cls(str(tmp_path / "song.mp3")) as second:
        assert first.path != second.path
        assert not (tmp_path / "song.mp3").exists()
    assert not list(tmp_path.iterdir())


def test_disk_full_preserves_existing_final(tmp_path):
    s = storage()
    target = tmp_path / "song.mp3"
    target.write_bytes(b"original")
    with s.StagedArtifact(str(target)) as stage:
        from pathlib import Path

        Path(stage.path).write_bytes(b"new")
        with pytest.raises(FileExistsError):
            stage.publish()
    assert target.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["song.mp3"]


@pytest.mark.parametrize("payload", [b"", b"<html>not audio</html>"])
def test_html_or_empty_mp3_is_never_published(tmp_path, payload):
    s = storage()
    f = tmp_path / "bad"
    f.write_bytes(payload)
    with pytest.raises(s.MediaValidationError):
        s.validate_audio(str(f), True)


def test_valid_audio_is_published_after_tagging(tmp_path):
    s = storage()
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "tone.mp3"
    with s.StagedArtifact(str(tmp_path / "song.mp3")) as stage:
        Path(stage.path).write_bytes(fixture.read_bytes())
        s.validate_audio(stage.path, True)
        stage.publish()
    assert (tmp_path / "song.mp3").is_file()


def test_truncated_audio_is_rejected(tmp_path):
    s = storage()
    from pathlib import Path

    body = (Path(__file__).parent / "fixtures" / "tone.mp3").read_bytes()
    target = tmp_path / "truncated.mp3"
    target.write_bytes(body[: len(body) // 3])
    with pytest.raises(s.MediaValidationError):
        s.validate_audio(str(target), True, expected_duration=5)


def test_distinct_tracks_same_custom_path_report_collision(tmp_path, audio_bytes, track_meta):
    import asyncio
    import copy

    import httpx

    from qobuz_dl2.downloader import Download, DownloadError

    async def run():
        one = tmp_path / "out"
        one.mkdir()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=audio_bytes))
        ) as client:
            first = Download(None, "1", str(one), 5, track_format="same")
            await first._download_and_tag(
                client, str(one), 0, {"url": "https://example.test/audio"}, track_meta, track_meta, True, True
            )
            original = (one / "same.mp3").read_bytes()
            second_meta = copy.deepcopy(track_meta)
            second_meta["id"] = "2"
            second = Download(None, "2", str(one), 5, track_format="same")
            from pytest import raises

            with raises(DownloadError):
                await second._download_and_tag(
                    client, str(one), 0, {"url": "https://example.test/audio"}, second_meta, second_meta, True, True
                )
            assert (one / "same.mp3").read_bytes() == original

    asyncio.run(run())
