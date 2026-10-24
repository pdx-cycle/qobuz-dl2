import pytest

from qobuz_dl2.utils import (
    PartialFormatter,
    format_duration,
    get_url_info,
    smart_discography_filter,
)


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://www.qobuz.com/us-en/album/some-name/abc123",
            ("album", "abc123"),
        ),
        ("https://open.qobuz.com/track/55", ("track", "55")),
        ("https://play.qobuz.com/playlist/999", ("playlist", "999")),
        ("/us-en/artist/-/777", ("artist", "777")),
        ("https://www.qobuz.com/us-en/label/foo/42", ("label", "42")),
    ],
)
def test_get_url_info_valid(url, expected):
    assert get_url_info(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/not/qobuz",
        "garbage",
        "https://www.qobuz.com/us-en/podcast/foo/1",
    ],
)
def test_get_url_info_invalid_raises(url):
    with pytest.raises(ValueError):
        get_url_info(url)


def test_format_duration():
    assert format_duration(0) == "00:00:00"
    assert format_duration(61) == "00:01:01"
    assert format_duration(3661) == "01:01:01"


def test_partial_formatter_missing_key():
    fmt = PartialFormatter()
    # missing key -> "n/a", present key -> value
    assert fmt.format("{artist} - {title}", title="Song") == "n/a - Song"


def test_partial_formatter_present():
    fmt = PartialFormatter()
    assert fmt.format("{a}/{b}", a="x", b="y") == "x/y"


def _album(title, artist, bit_depth=24, sampling_rate=96, version=None):
    return {
        "title": title,
        "version": version,
        "artist": {"name": artist},
        "maximum_bit_depth": bit_depth,
        "maximum_sampling_rate": sampling_rate,
        "id": title,
    }


def test_smart_discography_filter_drops_other_artists_and_dupes():
    contents = [
        {
            "name": "Real Artist",
            "albums": {
                "items": [
                    _album("Greatest Hits", "Real Artist", 24, 96),
                    # duplicate (lower quality) of the same album -> dropped
                    _album("Greatest Hits", "Real Artist", 16, 44),
                    # different artist feature -> dropped
                    _album("Compilation", "Other Artist"),
                ]
            },
        }
    ]
    result = smart_discography_filter(contents)
    titles = [a["title"] for a in result]
    assert titles == ["Greatest Hits"]
    assert result[0]["maximum_bit_depth"] == 24


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/album/123",
        "xxx /track/123",
        "https://play.qobuz.com/album/abc-123",
        "https://open.qobuz.com/album/abc/garbage",
        "https://play.qobuz.com.evil.test/track/1",
    ],
)
def test_url_rejects_foreign_or_partial_paths(url):
    with pytest.raises(ValueError):
        get_url_info(url)


def test_duration_over_24_hours_does_not_wrap():
    assert format_duration(90061) == "25:01:01"


@pytest.mark.parametrize(
    "items,kwargs",
    [
        ([_album("Same", "A", 16, 44), _album("Same", "B")], {}),
        ([_album("Same", "A"), _album("Same", "A", 16, 44, version="Remastered")], {}),
        ([_album("Same", "A", 16, 44), _album("Same (Deluxe)", "A")], {"skip_extras": True}),
    ],
)
def test_smart_filter_ranks_eligible_candidates_only(items, kwargs):
    result = smart_discography_filter([{"name": "A", "albums": {"items": items}}], **kwargs)
    assert len(result) == 1
    assert result[0]["artist"]["name"] == "A"


def test_m3u_root_album_disc_paths_resolve_and_repeat(tmp_path, audio_bytes, track_meta):
    from types import SimpleNamespace

    from qobuz_dl2.metadata import tag_mp3
    from qobuz_dl2.utils import make_m3u

    paths = [tmp_path / "Root.mp3", tmp_path / "Album" / "Disc 1" / "Nested.mp3"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes)
        tag_mp3(str(path), str(path.parent), str(path), track_meta, track_meta)
    manifest = make_m3u(
        str(tmp_path),
        [SimpleNamespace(path=str(paths[1])), SimpleNamespace(path=str(paths[0])), SimpleNamespace(path=str(paths[1]))],
    )
    entries = [s for s in __import__("pathlib").Path(manifest).read_text().splitlines() if s and not s.startswith("#")]
    assert entries == ["Album/Disc 1/Nested.mp3", "Root.mp3", "Album/Disc 1/Nested.mp3"]
    assert all((tmp_path / p).is_file() for p in entries)
