# ruff: noqa: E402
"""Synthetic offline audit probe. Observations describe defects, not passing regression tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

import asyncio
import gc
import os
import tempfile
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qobuz_dl2 import core, qopy, utils
from qobuz_dl2.core import QobuzDL
from qobuz_dl2.db import create_db, handle_download_id

_temp = tempfile.TemporaryDirectory(prefix="qobuz-core-audit-", dir="/private/tmp")
root = Path(_temp.name)
print("ARTIFACT_DIR", root)


def album(title, artist="A", depth=24, rate=96, version=None):
    return {
        "id": title + artist + str(depth),
        "title": title,
        "version": version,
        "artist": {"name": artist},
        "maximum_bit_depth": depth,
        "maximum_sampling_rate": rate,
    }


async def main():
    dl = QobuzDL(directory=str(root / "out"), no_m3u_for_playlists=True)
    dl.client = qopy.Client.__new__(qopy.Client)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        try:
            await dl.handle_url("https://play.qobuz.com/artist/1")
        except Exception as exc:
            print("COLLECTION", type(exc).__name__, str(exc))
        print("COLLECTION_WARNINGS", [str(x.message) for x in w])

    async def pages(_):
        for n in [1, 2]:
            yield {"name": "A", "albums": {"items": [{"id": str(n)}]}}

    dl.client = SimpleNamespace(get_plist_meta=pages, get_artist_meta=pages, get_label_meta=pages)
    downloaded = []

    async def record(item_id, *args):
        downloaded.append(item_id)

    dl.download_from_id = record
    await dl.handle_url("https://play.qobuz.com/artist/1")
    print("TWO_PAGE_IDS", downloaded)

    dl2 = QobuzDL(directory=str(root / "skipped"), downloads_db=str(root / "skip.db"), ignore_singles_eps=True)
    calls = []

    async def get_album_meta(item_id):
        calls.append(item_id)
        return {"streamable": True, "release_type": "single", "artist": {"name": "A"}, "title": "Single"}

    dl2.client = SimpleNamespace(get_album_meta=get_album_meta)
    await dl2.download_from_id("skipped-single")
    print(
        "SKIPPED_IN_DB",
        handle_download_id(dl2.downloads_db, "skipped-single"),
        "FILES",
        list((root / "skipped").iterdir()),
    )
    dl2.ignore_singles_eps = False
    await dl2.download_from_id("skipped-single")
    print("METADATA_CALLS_AFTER_RETRY", calls)

    class FakeDownload:
        def __init__(self, *args):
            calls.append((args[1], args[2], args[3]))

        async def download_id_by_type(self, *args):
            pass

    dl3 = QobuzDL(directory=str(root / "first"), downloads_db=str(root / "path.db"))
    dl3.client = object()
    calls = []
    with patch.object(core.downloader, "Download", FakeDownload):
        await dl3.download_from_id("same", alt_path=str(root / "playlist1"))
        dl3.quality = 27
        await dl3.download_from_id("same", alt_path=str(root / "playlist2"))
    print("DIFFERENT_TARGET_QUALITY_DOWNLOADS", calls)


asyncio.run(main())
for name, items, kwargs in [
    ("other_artist_higher_quality", [album("Same", depth=16, rate=44.1), album("Same", artist="B")], {}),
    ("lower_quality_remaster", [album("Same"), album("Same", depth=16, rate=44.1, version="Remastered")], {}),
    ("higher_quality_extras", [album("Same", depth=16, rate=44.1), album("Same (Deluxe)")], {"skip_extras": True}),
]:
    print("FILTER", name, utils.smart_discography_filter([{"name": "A", "albums": {"items": items}}], **kwargs))

pl = root / "playlist"
(pl / "Album" / "Disc 1").mkdir(parents=True)
(pl / "Root.flac").touch()
(pl / "Album" / "Disc 1" / "Nested.flac").touch()


class Tags(dict):
    info = SimpleNamespace(length=120)


def fake_tags(_):
    return Tags(TITLE=["Title"], ARTIST=["Artist"])


with patch.object(utils, "FLAC", fake_tags):
    utils.make_m3u(str(pl))
text = (pl / "playlist.m3u").read_text()
print("M3U", repr(text))
print(
    "M3U_TARGET_EXISTENCE",
    [(line, (pl / line).exists()) for line in text.splitlines() if line and not line.startswith("#")],
)
for url in [
    "https://example.com/album/123",
    "https://play.qobuz.com/album/abc/garbage",
    "https://play.qobuz.com/album/abc-123",
    "xxx /track/123",
]:
    try:
        print("URL", url, utils.get_url_info(url))
    except ValueError as exc:
        print("URL_REJECTED", str(exc))
print("DURATION_25H", utils.format_duration(25 * 3600))
gc.collect()
gc.disable()
db = str(root / "fds.db")
create_db(db)
before = len(os.listdir("/dev/fd"))
for n in range(100):
    handle_download_id(db, n)
after = len(os.listdir("/dev/fd"))
print("DB_FDS", before, after)
gc.enable()
gc.collect()
print("DB_FDS_AFTER_GC", len(os.listdir("/dev/fd")))

_temp.cleanup()
